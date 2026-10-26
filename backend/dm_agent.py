"""The decision makers: OpenRouterDM (Claude Haiku 5.5 via OpenRouter) and MockDM (offline stand-in)."""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any

from catalog import CARDS, ENEMIES, LAYOUT_PICKUP_MODES, PILLS, REWARDS, TREASURE_DISTANCE
from prompts import system_prompt
from schemas import TOOLS

def build_user_message(kind: str, state: dict, ctx: dict, memory: dict) -> str:
    return json.dumps({
        "decision": kind,
        "context": ctx,          # what is being decided (room type, candidates, ...)
        "state": state,          # freshest game snapshot
        "memory": memory,        # what you've done / are plotting this run
    }, separators=(",", ":"))


from google_ai import GOOGLE_URL, tidy, strip_cache_control
from http_retry import Cooldown, post_retrying, raise_for_status

DM_RETRIES = 1                  # a 5xx is sent again once: a decision has seconds, not minutes

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-haiku-5.5"      # OpenRouter slugs use dots

# Every call sends all four tools in a fixed order and picks one with tool_choice. Tools sit
# in front of the system prompt in the cache prefix, so one shared tool list means one cache
# entry for every decision kind instead of four (tool_choice changes don't break it).
_OPENAI_TOOLS = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS.values()]


def api_headers(api_key: str | None, google: bool = False) -> dict:
    if google:
        return {"Authorization": f"Bearer {api_key}"}
    return {"Authorization": f"Bearer {api_key or os.environ['OPENROUTER_API_KEY']}", "X-Title": "LLM Dungeon Master"}


def completion_json(resp, model: str, google: bool = False) -> dict:
    """The response body; Google's gets its cost worked out from the price list."""
    data = resp.json()
    return tidy(data, model) if google and isinstance(data, dict) else data


def _usage(data: dict) -> dict:
    u = data.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    return {"in": int(u.get("prompt_tokens") or 0), "cached": int(details.get("cached_tokens") or 0),
            "out": int(u.get("completion_tokens") or 0), "cost": float(u.get("cost") or 0.0)}


def call_record(model: str, system: list[str], data: dict) -> dict:
    """What the receipt keeps of one completion: who answered, the raw message, finish reason, usage."""
    choice = (data.get("choices") or [{}])[0]
    return {"model": data.get("model") or model, "provider": data.get("provider"), "id": data.get("id"),
            "system": system, "finish": choice.get("finish_reason"), "response": choice.get("message"),
            "usage": _usage(data), "error": data.get("error")}


class LLMCallError(RuntimeError):
    """A completion came back unusable. `call` keeps what it did return, for the receipt."""

    def __init__(self, message: str, call: dict | None = None) -> None:
        super().__init__(message)
        self.call = call


class OpenRouterDM:
    """Same Haiku DM, called through OpenRouter's OpenAI-compatible chat endpoint."""

    # Deck answers ran up to ~1900 tokens in a real run: leave headroom. A cut-off answer is a lost decision.
    def __init__(self, cruelty: int, model: str = OPENROUTER_DEFAULT_MODEL, max_tokens: int = 2048,
                 api_key: str | None = None, google: bool = False) -> None:
        import httpx                                    # imported lazily so --mock needs no HTTP client
        self.google = google                            # Google AI Studio's endpoint instead of OpenRouter's
        self.client = httpx.AsyncClient(timeout=30.0, headers=api_headers(api_key, google))
        self.model = model
        self.effort: str | None = "none"               # Google's thinking switch: raised if the model insists, dropped if it has none
        self.max_tokens = max_tokens
        self.reasoning: dict = {"enabled": False}       # no thinking tokens: they eat max_tokens and add latency
        self.cooldown = Cooldown(model)                 # this model's rate-limit window (429s)
        self.campaign = ""
        self.set_cruelty(cruelty)

    def set_cruelty(self, cruelty: int) -> None:
        self.cruelty = cruelty
        self._build_system()

    def set_campaign(self, text: str) -> None:
        """The planners' narrative + floor brief, shown after the persona."""
        self.campaign = text
        self._build_system()

    def _build_system(self) -> None:
        # Persona + catalogs are identical across calls -> cache them (OpenRouter passes cache_control on).
        # The campaign changes about once a floor, so it gets its own breakpoint after them.
        content = [{"type": "text", "text": system_prompt(self.cruelty), "cache_control": {"type": "ephemeral"}}]
        if self.campaign:
            content.append({"type": "text", "text": self.campaign, "cache_control": {"type": "ephemeral"}})
        self.system = {"role": "system", "content": content}

    async def _post(self, body: dict):
        """POST with thinking off. A model that can't run without it (400 "Reasoning is mandatory")
        is retried, and from then on called, at low effort. A 5xx is sent again after a short backoff;
        a 429 starts this model's cooldown, and nothing is sent until it is over (CoolingDown)."""
        self.cooldown.check()
        resp = await self._post_once(body)
        if resp.status_code == 429:
            self.cooldown.hit(resp)
        elif resp.status_code < 400:
            self.cooldown.ok()
        return resp

    async def _post_once(self, body: dict):
        if self.google:
            body = {**{k: v for k, v in body.items() if k != "usage"}, "messages": strip_cache_control(body["messages"])}
            resp = await self._post_google(body)
            if resp.status_code == 400 and self.effort == "none":    # e.g. Gemini Pro can't run without thinking
                self.effort = "low"
                resp = await self._post_google(body)
            if resp.status_code == 400 and "thinking level is not supported" in resp.text.lower() and self.effort:
                self.effort = None                                   # e.g. Gemma: no thinking setting at all
                resp = await self._post_google(body)
            return resp
        resp = await self._post_openrouter(body)
        if resp.status_code == 400 and "reasoning is mandatory" in resp.text.lower() and "effort" not in self.reasoning:
            self.reasoning = {"effort": "low"}
            resp = await self._post_openrouter(body)
        return resp

    async def _post_google(self, body: dict):
        return await post_retrying(lambda: self.client.post(
            GOOGLE_URL, json={**body, **({"reasoning_effort": self.effort} if self.effort else {})}),
            what=self.model, retries=DM_RETRIES, base=0.5)

    async def _post_openrouter(self, body: dict):
        return await post_retrying(lambda: self.client.post(OPENROUTER_URL, json={**body, "reasoning": self.reasoning}),
                                   what=self.model, retries=DM_RETRIES, base=0.5)

    async def decide(self, kind: str, state: dict, ctx: dict, memory: dict) -> dict[str, Any]:
        tool = TOOLS[kind]
        resp = await self._post({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [self.system,
                         {"role": "user", "content": build_user_message(kind, state, ctx, memory)}],
            "tools": _OPENAI_TOOLS,
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
            "usage": {"include": True},                 # token + cost accounting in the response
        })
        raise_for_status(resp)
        data = completion_json(resp, self.model, self.google)
        record = call_record(self.model, [b["text"] for b in self.system["content"]], data)
        if "error" in data:
            raise LLMCallError(f"{'google' if self.google else 'openrouter'}: {data['error']}", record)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMCallError(f"hit max_tokens={self.max_tokens} before finishing the tool call", record)
        msg = choice["message"]
        for call in msg.get("tool_calls") or []:
            if call["function"]["name"] == tool["name"]:
                try:
                    args = json.loads(call["function"]["arguments"])
                except json.JSONDecodeError as e:
                    raise LLMCallError(f"tool arguments are not JSON ({e})", record) from e
                return {**args, "_usage": record["usage"], "_call": record}
        raise LLMCallError(f"no tool call in response (finish_reason={choice.get('finish_reason')})", record)

    async def epitaph(self, prompt: str) -> tuple[str, dict]:
        """Free text in the DM's voice (the after-death screen). Same tools and system prompt as a
        decision, with tool_choice none, so the cached persona and campaign still hit."""
        resp = await self._post({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [self.system, {"role": "user", "content": prompt}],
            "tools": _OPENAI_TOOLS,
            "tool_choice": "none",
            "usage": {"include": True},
        })
        raise_for_status(resp)
        data = completion_json(resp, self.model, self.google)
        record = call_record(self.model, [b["text"] for b in self.system["content"]], data)
        if "error" in data:
            raise LLMCallError(f"{'google' if self.google else 'openrouter'}: {data['error']}", record)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMCallError(f"hit max_tokens={self.max_tokens} before finishing the epitaph", record)
        text = (choice["message"].get("content") or "").strip()
        if not text:
            raise LLMCallError("empty epitaph", record)
        return text, record


class MockDM:
    """Rule-of-thumb troll with fake latency. For testing the pipeline offline."""

    def __init__(self, cruelty: int, latency: tuple[float, float] = (0.25, 1.2), seed: int | None = None):
        self.cruelty = cruelty
        self.latency = latency
        self.rng = random.Random(seed)
        self.campaign = ""

    def set_cruelty(self, cruelty: int) -> None:
        self.cruelty = cruelty

    def set_campaign(self, text: str) -> None:
        self.campaign = text

    async def decide(self, kind: str, state: dict, ctx: dict, memory: dict) -> dict[str, Any]:
        await asyncio.sleep(self.rng.uniform(*self.latency))
        return {**self._answer(kind, state, ctx), "_call": {"model": "mock", "finish": "tool_calls"}}

    async def epitaph(self, prompt: str) -> tuple[str, dict]:
        await asyncio.sleep(self.rng.uniform(*self.latency))
        return "Here lies a hero who trusted the dice. The dice were mine.", {"model": "mock", "finish": "stop"}

    def _answer(self, kind: str, state: dict, ctx: dict) -> dict[str, Any]:
        r = self.rng
        res = (state.get("player") or {}).get("resources") or {}
        if kind == "room_layout":
            return {"intent": "mock: sprinkle chaos",
                    "champion_chance": r.uniform(0, 0.4),
                    "champion_color": r.choice(["random", "red", "dark_red", "tiny"]),
                    "extra_enemies": [{"enemy": r.choice(list(ENEMIES)), "count": r.randint(1, 2)}],
                    "tinted_rocks": r.randint(0, 1) if res.get("bombs", 0) == 0 else 0,
                    "hide_tinted_rocks": res.get("bombs", 0) > 3,
                    "layout_pickups": r.choice(list(LAYOUT_PICKUP_MODES)),
                    "bonus_pickups": [r.choice(list(REWARDS))] if r.random() < 0.3 else [],
                    "taunt": r.choice(["", "Mind the furniture.", "I redecorated."]),
                    "scheme": "mock scheme"}
        if kind == "room_clear":
            need_key = res.get("keys", 0) == 0
            pick = "penny" if need_key else r.choice(["key", "bomb", "half_heart", "troll_bomb", "nickel"])
            return {"intent": "mock: give what they don't need",
                    "rewards": [] if r.random() < 0.3 else [{"reward": pick, "count": 1}],
                    "taunt": r.choice(["", "A penny for your thoughts.", "Earned it. Barely."])}
        if kind == "payout_plan":
            chest = ctx.get("source_class") == "chest"
            inside = ["item", "half_heart"] if ctx.get("has_item_pool") else ["half_heart", "bomb"]
            return {"intent": "mock: a machine with moods",
                    "schedule": [{"reward": n, "count": 1} for n in
                                 (inside if chest else ["key", "nothing", "penny"])],
                    "taunt": "", "scheme": "mock scheme"}
        if kind == "item_choice":
            cands = sorted(ctx.get("candidates", []), key=lambda c: c.get("quality", 2))
            return {"intent": "mock: lowest quality first", "order": [c["id"] for c in cands],
                    "taunt": "Ooh, shiny."}
        if kind == "floor_plan":
            return {"intent": "mock: dim the lights", "add_curses": ["darkness"],
                    "strip_vanilla_curses": False, "treasure_distance": r.choice(TREASURE_DISTANCE),
                    "taunt": "Watch your step."}
        if kind == "cards":
            return {"intent": "mock: tower first, then whatever",
                    "cards": ["tower", "rune_hagalaz", *r.sample(list(CARDS), 2)],
                    "taunt": "Pick a card. Any card."}
        if kind == "trinkets":
            return {"intent": "mock: the first candidates", "taunt": "Shiny.",
                    "trinkets": [c["id"] for c in ctx.get("trinket_candidates", [])][:max(2, len(ctx.get("owed") or []))]}
        if kind == "pills":
            return {"intent": "mock: whatever", "taunt": "Swallow it.",
                    "pills": [{"color": c["color"], "effect": r.choice(list(PILLS))}
                              for c in ctx.get("pill_colors_to_assign", [])]}
        if kind == "drops":
            n = max(1, int(ctx.get("wanted") or 3))
            outcomes = [{"reward": "nothing"} for _ in range(n)]
            outcomes[min(1, n - 1)] = {"reward": r.choice(["penny", "half_heart", "key", "bomb"]), "count": 1}
            return {"intent": "mock: one kill pays", "outcomes": outcomes, "taunt": ""}
        if kind == "rocks":
            n = max(1, int(ctx.get("wanted") or 3))
            return {"intent": "mock: a tease, then a gift", "taunt": "",
                    "outcomes": [{"reward": r.choice(["nothing", "penny", "key", "soul_heart"]), "count": 1}
                                 for _ in range(n)]}
        if kind == "shop":
            n = max(1, int(ctx.get("wanted") or 4))
            return {"intent": "mock: everything but what they need", "taunt": "Browse all you like.",
                    "goods": [r.choice(["bomb", "red_heart", "battery", "pill"]) for _ in range(n)]}
        raise ValueError(kind)
