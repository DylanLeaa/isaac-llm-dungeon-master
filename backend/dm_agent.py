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


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-haiku-5.5"      # OpenRouter slugs use dots

# Every call sends all four tools in a fixed order and picks one with tool_choice. Tools sit
# in front of the system prompt in the cache prefix, so one shared tool list means one cache
# entry for every decision kind instead of four (tool_choice changes don't break it).
_OPENAI_TOOLS = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS.values()]


def _usage(data: dict) -> dict:
    u = data.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    return {"in": int(u.get("prompt_tokens") or 0), "cached": int(details.get("cached_tokens") or 0),
            "out": int(u.get("completion_tokens") or 0), "cost": float(u.get("cost") or 0.0)}


class OpenRouterDM:
    """Same Haiku DM, called through OpenRouter's OpenAI-compatible chat endpoint."""

    # Output includes Haiku's reasoning, not just the tool JSON: real calls run ~140-390 tokens,
    # so leave plenty of headroom. A cut-off answer is a lost decision.
    def __init__(self, cruelty: int, model: str = OPENROUTER_DEFAULT_MODEL, max_tokens: int = 1024,
                 api_key: str | None = None) -> None:
        import httpx                                    # imported lazily so --mock needs no HTTP client
        self.client = httpx.AsyncClient(timeout=30.0, headers={
            "Authorization": f"Bearer {api_key or os.environ['OPENROUTER_API_KEY']}",
            "X-Title": "LLM Dungeon Master",
        })
        self.model = model
        self.max_tokens = max_tokens
        self.set_cruelty(cruelty)

    def set_cruelty(self, cruelty: int) -> None:
        self.cruelty = cruelty
        # Persona + catalogs are identical across calls -> cache them (OpenRouter passes cache_control on).
        self.system = {"role": "system", "content": [
            {"type": "text", "text": system_prompt(cruelty), "cache_control": {"type": "ephemeral"}}]}

    async def decide(self, kind: str, state: dict, ctx: dict, memory: dict) -> dict[str, Any]:
        tool = TOOLS[kind]
        resp = await self.client.post(OPENROUTER_URL, json={
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [self.system,
                         {"role": "user", "content": build_user_message(kind, state, ctx, memory)}],
            "tools": _OPENAI_TOOLS,
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
            "usage": {"include": True},                 # token + cost accounting in the response
        })
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"openrouter: {data['error']}")
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError(f"hit max_tokens={self.max_tokens} before finishing the tool call")
        msg = choice["message"]
        for call in msg.get("tool_calls") or []:
            if call["function"]["name"] == tool["name"]:
                return {**json.loads(call["function"]["arguments"]), "_usage": _usage(data)}
        raise RuntimeError(f"no tool call in response (finish_reason={choice.get('finish_reason')})")


class MockDM:
    """Rule-of-thumb troll with fake latency. For testing the pipeline offline."""

    def __init__(self, cruelty: int, latency: tuple[float, float] = (0.25, 1.2), seed: int | None = None):
        self.cruelty = cruelty
        self.latency = latency
        self.rng = random.Random(seed)

    def set_cruelty(self, cruelty: int) -> None:
        self.cruelty = cruelty

    async def decide(self, kind: str, state: dict, ctx: dict, memory: dict) -> dict[str, Any]:
        await asyncio.sleep(self.rng.uniform(*self.latency))
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
        if kind == "item_choice":
            cands = sorted(ctx.get("candidates", []), key=lambda c: c.get("quality", 2))
            return {"intent": "mock: lowest quality first", "order": [c["id"] for c in cands],
                    "taunt": "Ooh, shiny."}
        if kind == "floor_plan":
            return {"intent": "mock: dim the lights", "add_curses": ["darkness"],
                    "strip_vanilla_curses": False, "treasure_distance": r.choice(TREASURE_DISTANCE),
                    "taunt": "Watch your step."}
        if kind == "consumables":
            return {"intent": "mock: tower first, then whatever",
                    "cards": ["tower", "rune_hagalaz", *r.sample(list(CARDS), 2)],
                    "trinkets": [c["id"] for c in ctx.get("trinket_candidates", [])][:2],
                    "pills": [{"color": c["color"], "effect": r.choice(list(PILLS))}
                              for c in ctx.get("pill_colors_to_assign", [])],
                    "taunt": "Pick a card. Any card."}
        raise ValueError(kind)
