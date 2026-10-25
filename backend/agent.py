"""Agent mode's decision makers: one call per trigger, answered with any number of deck tool calls.

The Coordinator builds the turn (pending requests, open decks, state, economy, memory) and applies the
calls that come back; these only make the call. OpenRouterAgent (OpenRouter or Google AI Studio) and
MockAgent here, ClaudeCodeAgent in claude_code.py.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_tools import OPENAI_AGENT_TOOLS, SHUFFLE, STACK
from dm_agent import LLMCallError, MockDM, OpenRouterDM, call_record, completion_json
from http_retry import raise_for_status
from memory import DECK_FIELD
from prompts import system_prompt

# A turn can write several decks (a floor start: item rankings, cards, trinkets, pills), up to ~1900 tokens each,
# and medium reasoning comes out of the same allowance.
AGENT_MAX_TOKENS = 52000
# One turn runs every deck at once and has to check the open ones against the state (as CLAUDE_CODE_AGENT_EFFORT).
AGENT_REASONING_EFFORT = "medium"


def build_turn_message(turn: dict) -> str:
    return json.dumps(turn, separators=(",", ":"))


def tool_calls(msg: dict) -> tuple[list[dict], list[str]]:
    """([{tool, args}], why each unreadable call was dropped) from an OpenAI-style message."""
    calls, dropped = [], []
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as e:
            dropped.append(f"{fn.get('name')}: arguments are not JSON ({e})")
            continue
        if not isinstance(args, dict):
            dropped.append(f"{fn.get('name')}: arguments are not an object")
            continue
        calls.append({"tool": str(fn.get("name")), "args": args})
    return calls, dropped


class OpenRouterAgent(OpenRouterDM):
    """The DM in agent mode, through OpenRouter's (or Google AI Studio's) OpenAI-compatible endpoint. Same
    client, retries, cooldown and fallbacks as OpenRouterDM; its own system prompt and tools, and it thinks
    (AGENT_REASONING_EFFORT) where the single decisions don't."""

    def __init__(self, cruelty: int, max_tokens: int = AGENT_MAX_TOKENS,
                 effort: str = AGENT_REASONING_EFFORT, **kw) -> None:
        self.tool_choice: str = "required"        # dropped to "auto" if the provider won't take it
        super().__init__(cruelty, max_tokens=max_tokens, **kw)
        self.reasoning = {"effort": effort}       # OpenRouter; a model with no reasoning ignores it
        self.effort = effort                      # Google; dropped if the model has no thinking setting

    def _build_system(self) -> None:
        content = [{"type": "text", "text": system_prompt(self.cruelty, agent=True),
                    "cache_control": {"type": "ephemeral"}}]
        if self.campaign:
            content.append({"type": "text", "text": self.campaign, "cache_control": {"type": "ephemeral"}})
        self.system = {"role": "system", "content": content}

    def _body(self, message: str) -> dict:
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "messages": [self.system, {"role": "user", "content": message}],
                "tools": OPENAI_AGENT_TOOLS, "tool_choice": self.tool_choice,
                "usage": {"include": True}}
        if self.tool_choice == "required":
            body["parallel_tool_calls"] = True
        return body

    async def act(self, message: str) -> tuple[list[dict], dict]:
        """(the turn's tool calls in order, the call record)."""
        resp = await self._post(self._body(message))
        if resp.status_code == 400 and self.tool_choice == "required":
            self.tool_choice = "auto"             # a provider without forced/parallel tool calls
            resp = await self._post(self._body(message))
        raise_for_status(resp)
        data = completion_json(resp, self.model, self.google)
        record = call_record(self.model, [b["text"] for b in self.system["content"]], data)
        if "error" in data:
            raise LLMCallError(f"{'google' if self.google else 'openrouter'}: {data['error']}", record)
        choice = data["choices"][0]
        calls, dropped = tool_calls(choice.get("message") or {})
        if dropped:
            record["dropped"] = dropped
        if not calls and choice.get("finish_reason") == "length":
            raise LLMCallError(f"hit max_tokens={self.max_tokens} before finishing a tool call", record)
        return calls, record


class MockAgent:
    """Offline agent: stacks every pending deck the way MockDM would, and now and then shuffles an open one."""

    def __init__(self, cruelty: int, latency: tuple[float, float] = (0.25, 1.2), seed: int | None = None) -> None:
        self.dm = MockDM(cruelty, latency=latency, seed=seed)
        self.rng = self.dm.rng
        self.latency = latency
        self.campaign = ""

    def set_cruelty(self, cruelty: int) -> None:
        self.dm.set_cruelty(cruelty)

    def set_campaign(self, text: str) -> None:
        self.campaign = text

    async def act(self, message: str) -> tuple[list[dict], dict]:
        await asyncio.sleep(self.rng.uniform(*self.latency))
        turn = json.loads(message)
        state = turn.get("state") or {}
        calls: list[dict[str, Any]] = [
            {"tool": STACK + p["kind"], "args": {"key": p["key"], **self.dm._answer(p["kind"], state, p.get("context") or {})}}
            for p in turn.get("pending") or []]
        decks = [d for d in turn.get("open_decks") or [] if DECK_FIELD.get(d.get("kind")) and len(d.get("left") or []) > 1]
        if decks and (self.rng.random() < 0.5 or any(d.get("restack") for d in decks)):
            d = next((d for d in decks if d.get("restack")), None) or self.rng.choice(decks)
            calls.append({"tool": SHUFFLE, "args": {"key": d["key"], "order": list(range(len(d["left"])))[::-1]}})
        return calls, {"model": "mock", "finish": "tool_calls"}


__all__ = ["AGENT_MAX_TOKENS", "AGENT_REASONING_EFFORT", "MockAgent", "OpenRouterAgent", "build_turn_message", "tool_calls"]
