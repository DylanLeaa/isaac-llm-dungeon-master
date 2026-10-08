"""The planners above the DM: a run narrative (Claude Opus 5.5, once per run) and a floor brief
(Claude Sonnet 5.5, once per floor). Both are written in the background and handed to the DM as
the CAMPAIGN block of its system prompt; a decision never waits for them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import Counter
from typing import Callable

from dm_agent import OPENROUTER_URL, LLMCallError, call_record
from prompts import floor_prompt, narrative_prompt
from receipt import plan_excerpt

log = logging.getLogger("llmdm")

NARRATIVE_DEFAULT_MODEL = "anthropic/claude-opus-5.5"
FLOOR_DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"

NARRATIVE_WAIT_S = 25.0     # a floor brief waits this long for the run narrative, then goes without


def _compact_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def floor_signature(state: dict) -> tuple | None:
    """Which floor a snapshot is on. A layout reseed keeps stage and type, so it isn't a new floor."""
    level = state.get("level") or {}
    if level.get("stage") is None:
        return None
    return (level.get("stage"), level.get("stage_type"))


def campaign_text(narrative: str | None, brief: str | None, floor_name: str | None) -> str:
    """The CAMPAIGN block the DM sees after its system prompt."""
    where = floor_name or "this floor"
    parts = ["CAMPAIGN"]
    parts.append("RUN NARRATIVE (from the showrunner)\n" + narrative if narrative
                 else "RUN NARRATIVE: still being written. Troll in your usual style for now.")
    parts.append(f"FLOOR BRIEF for {where} (from the floor strategist)\n" + brief if brief
                 else f"FLOOR BRIEF for {where}: still being prepared. Improvise in the spirit of the narrative.")
    return "\n\n".join(parts)


class OpenRouterPlanner:
    """Plain-text completions (no tools: Opus/Sonnet 5.5 reject forced tool choice)."""

    # Thinking can't be turned off on these models and counts toward max_tokens: leave room.
    def __init__(self, narrative_model: str = NARRATIVE_DEFAULT_MODEL, floor_model: str = FLOOR_DEFAULT_MODEL,
                 max_tokens: int = 6000, api_key: str | None = None) -> None:
        import httpx
        self.client = httpx.AsyncClient(timeout=120.0, headers={
            "Authorization": f"Bearer {api_key or os.environ['OPENROUTER_API_KEY']}",
            "X-Title": "LLM Dungeon Master",
        })
        self.narrative_model = narrative_model
        self.floor_model = floor_model
        self.max_tokens = max_tokens

    async def _complete(self, model: str, system: str, user: str) -> tuple[str, dict]:
        """The plan text, and usage with the receipt's call record under "_call"."""
        resp = await self.client.post(OPENROUTER_URL, json={
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "usage": {"include": True},
        })
        resp.raise_for_status()
        data = resp.json()
        record = {**call_record(model, [system], data), "user": user}
        if "error" in data:
            raise LLMCallError(f"openrouter: {data['error']}", record)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMCallError(f"hit max_tokens={self.max_tokens} before finishing the plan", record)
        text = (choice["message"].get("content") or "").strip()
        if not text:
            raise LLMCallError(f"empty plan (finish_reason={choice.get('finish_reason')})", record)
        return text, {**record["usage"], "_call": record}

    async def narrative(self, state: dict, cruelty: int) -> tuple[str, dict]:
        user = "Opening snapshot of the run:\n" + _compact_json(state)
        return await self._complete(self.narrative_model, narrative_prompt(cruelty), user)

    async def floor_brief(self, state: dict, narrative: str | None, digest: dict, prev_brief: str | None,
                          cruelty: int) -> tuple[str, dict]:
        user = "\n\n".join([
            "RUN NARRATIVE:\n" + (narrative or "(not written yet - set the tone yourself)"),
            "LAST FLOOR'S BRIEF:\n" + (prev_brief or "(none - this is the first floor)"),
            "RECENT EVENTS AND THE DM'S NOTES:\n" + _compact_json(digest),
            "SNAPSHOT AT FLOOR START:\n" + _compact_json(state),
        ])
        return await self._complete(self.floor_model, floor_prompt(cruelty), user)


class MockPlanner:
    """Canned plans with fake latency. For testing the pipeline offline."""

    def __init__(self, latency: tuple[float, float] = (0.05, 0.2), seed: int | None = None) -> None:
        self.latency = latency
        self.rng = random.Random(seed)
        self.narrative_model = self.floor_model = "mock"

    async def narrative(self, state: dict, cruelty: int) -> tuple[str, dict]:
        await asyncio.sleep(self.rng.uniform(*self.latency))
        who = (state.get("player") or {}).get("character", "the player")
        return (f"TITLE: Mock Run\nPREMISE: Make {who} trust the pedestals, then betray them.\n"
                "CHARACTER ANGLE: mock\nARC: Basement - generous; Caves - the turn\nRUNNING GAGS: pennies"), \
            {"_call": {"model": "mock"}}

    async def floor_brief(self, state, narrative, digest, prev_brief, cruelty) -> tuple[str, dict]:
        await asyncio.sleep(self.rng.uniform(*self.latency))
        name = (state.get("level") or {}).get("name", "this floor")
        return (f"ARC BEAT: {name} lulls them.\nRESOURCES: starve keys\nROOMS: light\n"
                "ITEMS & CONSUMABLES: one good item\nSET-PIECE: key drought, then key by a troll bomb\n"
                "VOICE: smug"), {"_call": {"model": "mock"}}


class Campaign:
    """Starts the planners at the right moments and pushes their text to the DM."""

    def __init__(self, planner, push: Callable[[str], None], stats: dict[str, Counter], usage: Counter,
                 record: Callable[..., None] = lambda what, status, **fields: None) -> None:
        self.planner, self.push, self.stats, self.usage = planner, push, stats, usage
        self.record = record                          # receipt.add-like: (what, status, **fields)
        self.run_token = 0
        self.tasks: set[asyncio.Task] = set()
        self._clear()

    def _clear(self) -> None:
        self.narrative: str | None = None
        self.brief: str | None = None
        self.floor_sig: tuple | None = None
        self.floor_name: str | None = None
        self.narrative_task: asyncio.Task | None = None

    def reset(self) -> None:
        """New run: forget the plans and drop anything still being written for the old one."""
        self.run_token += 1
        for t in self.tasks:
            t.cancel()
        self.tasks.clear()
        self._clear()
        self._push()

    def _push(self) -> None:
        self.push(campaign_text(self.narrative, self.brief, self.floor_name) if self.planner else "")

    def observe(self, state: dict, digest: dict, cruelty: int) -> None:
        """Called for every decision request; starts a planner when a run or floor begins."""
        if not self.planner or not state:
            return
        if self.narrative_task is None:
            self.narrative_task = self._spawn(self._write_narrative(state, cruelty, self.run_token))
        sig = floor_signature(state)
        if sig is not None and sig != self.floor_sig:
            self.floor_sig = sig
            self.floor_name = (state.get("level") or {}).get("name") or f"stage {sig[0]}"
            prev, self.brief = self.brief, None      # never follow last floor's brief on this one
            self._push()
            self._spawn(self._write_brief(state, digest, prev, cruelty, self.run_token, sig))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _account(self, kind: str, usage: dict) -> None:
        self.stats[kind]["ok"] += 1
        if usage:
            self.usage.update({**usage, "calls": 1})

    def _failed(self, kind: str, e: Exception, t0: float, **fields) -> None:
        self.stats[kind]["error"] += 1
        self.record(kind, "error", ms=_ms(t0), error=f"{type(e).__name__}: {e}",
                    llm=getattr(e, "call", None), **fields)

    async def _write_narrative(self, state: dict, cruelty: int, token: int) -> None:
        self.stats["narrative"]["req"] += 1
        t0 = time.perf_counter()
        try:
            text, usage = await self.planner.narrative(state, cruelty)
        except asyncio.CancelledError:
            raise
        except Exception as e:                        # noqa: BLE001 - a planner must never break the DM
            self._failed("narrative", e, t0)
            log.warning("narrative   failed (%s: %s) -> the DM runs without one", type(e).__name__, e)
            return
        if token != self.run_token:
            return
        call = usage.pop("_call", None)
        self.record("narrative", "written", ms=_ms(t0), llm=call, result=plan_excerpt(text), text=text)
        self._account("narrative", usage)
        self.narrative = text
        log.info("narrative   [%s]\n%s", self.planner.narrative_model, _indent(text))
        self._push()

    async def _write_brief(self, state: dict, digest: dict, prev: str | None, cruelty: int,
                           token: int, sig: tuple) -> None:
        self.stats["floor_brief"]["req"] += 1
        if self.narrative is None and self.narrative_task and not self.narrative_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.narrative_task), NARRATIVE_WAIT_S)
            except asyncio.TimeoutError:
                log.info("floor_brief narrative not ready after %.0fs -> planning without it", NARRATIVE_WAIT_S)
            except asyncio.CancelledError:
                if token != self.run_token:
                    raise
        t0 = time.perf_counter()
        floor = f"{self.floor_name} ({sig[0]}.{sig[1]})"
        try:
            text, usage = await self.planner.floor_brief(state, self.narrative, digest, prev, cruelty)
        except asyncio.CancelledError:
            raise
        except Exception as e:                        # noqa: BLE001
            self._failed("floor_brief", e, t0, key=floor)
            log.warning("floor_brief failed (%s: %s) -> the DM improvises this floor", type(e).__name__, e)
            return
        if token != self.run_token:
            return                                    # the run moved on meanwhile
        call = usage.pop("_call", None)
        if sig != self.floor_sig:                     # paid for, but the floor moved on meanwhile
            self.record("floor_brief", "discarded", key=floor, ms=_ms(t0), llm=call, result=plan_excerpt(text),
                        text=text)
            return
        self.record("floor_brief", "written", key=floor, ms=_ms(t0), llm=call, result=plan_excerpt(text), text=text)
        self._account("floor_brief", usage)
        self.brief = text
        log.info("floor_brief %s [%s]\n%s", self.floor_name, self.planner.floor_model, _indent(text))
        self._push()


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())
