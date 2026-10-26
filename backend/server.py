"""LLM Dungeon Master backend.

Talks to the Isaac mod through files the game already writes, so the mod runs in Isaac's normal
sandbox: the mod's messages come in through the game's log.txt, ours go out through the mod's save
file (see bridge.py). There's nothing to set up in the game: this finds the game's files.

Run:  python server.py                 # Haiku 5.5 DM + Opus narrative + Sonnet floor briefs
      python server.py --no-planners  # Haiku alone
      python server.py --mock          # no API key needed
      python server.py --backend claude-code   # same DM through your local Claude Code (claude -p)
      python server.py --agent         # agent mode: one call per trigger stacks every deck it needs to
      python server.py --isaac-dir "D:/SteamLibrary/steamapps/common/The Binding of Isaac Rebirth"
"""
from __future__ import annotations

import argparse
import asyncio
import heapq
import itertools
import logging
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import appdata
import bridge
import epilogue
from claude_code import (CLAUDE_CODE_CONCURRENCY, CLAUDE_CODE_DM_MODEL, CLAUDE_CODE_LATE_GRACE_S,
                         CLAUDE_CODE_PLANNER_MODEL, ClaudeCodeAgent, ClaudeCodeDM, ClaudeCodePlanner)
from google_ai import GOOGLE_DEFAULT_MODEL
from http_retry import CoolingDown, error_fields, error_line
from item_effects import with_effects
from agent import MockAgent, OpenRouterAgent, build_turn_message
from agent_tools import KEEP, SHUFFLE, shuffle_order, stack_kind
from dm_agent import OPENROUTER_DEFAULT_MODEL, MockDM, OpenRouterDM
from economy import floor_sig
from gate import GATE_DEFAULT_MODEL, GATE_THRESHOLD, MockGate, OpenRouterGate, diff, new_events
from memory import DECK_FIELD, RunMemory, kind_of
from planners import (FLOOR_DEFAULT_MODEL, NARRATIVE_DEFAULT_MODEL, NARRATIVE_WAIT_S, Campaign, MockPlanner, OpenRouterPlanner,
                      floor_signature, parse_economy)
from receipt import Receipt
from schemas import KINDS, MAX_DROPS, clamp, kill_drop_cap, kill_drops_left, max_curses, mercy_active

log = logging.getLogger("llmdm")
Send = Callable[[dict], Awaitable[None]]

MAX_DEADLINE_S = 12.0      # never let a single decision hang longer than this (plus any late grace)
MIN_DEADLINE_S = 0.4
EPITAPH_TIMEOUT_S = 20.0   # the stats screen shows without it; it only fills in the last line

# Who gets the next free DM slot. A floor's start sends a burst of item rankings and card/trinket/pill
# decks; the room the player is about to walk into (or is about to clear) must not queue behind it, and a
# deck the game is frozen on (ctx.owed) goes first of all.
URGENT, NEXT_DOOR, BACKGROUND = 0, 1, 2

# Agent mode. A request waits to share a turn with the ones after it, longer the less it is needed: a deck the
# game is frozen on, or a room clear, doesn't wait; the room next door waits a moment; a floor start's burst (every
# room, pool and deck on the floor) a second, so it lands in a few turns. Decks gone stale with no request behind
# them wait too. A request for a deck already written goes to the re-ask gate first and only waits if it says so.
AGENT_BATCH_S = {URGENT: 0.0, NEXT_DOOR: 0.25, BACKGROUND: 1.0}
AGENT_FLAG_WAIT_S = 1.0
AGENT_MAX_KEYS = 8         # requests a turn answers; more start another turn
AGENT_DROPS_DEPTH = MAX_DROPS   # a turn's drops deck: enough for several rooms, so the game rarely asks
AGENT_ROCKS_DEPTH = 6           # a turn's tinted rock deck: the floor's next rocks
# Open decks a turn may restack unasked. Not pills (a colour may have been swallowed since it was written) nor
# payouts (a machine's schedule is a story: it is only ever extended, when the game asks).
AGENT_RESTACK_KINDS = ("room_layout", "room_clear", "item_choice", "floor_plan", "cards", "trinkets", "drops",
                       "rocks")
# Re-asks of a deck the agent already wrote that never changed what the game did (run E989E1E1: 0 of 79): the
# player nearing its room, a state-change refresh, the boss's curse re-check. The deck answers them as it stands,
# with no gate and no turn; a deck that has really gone stale is flagged (_flag) and still goes to the agent.
AGENT_REUSE_REASONS = ("eta under lead time", "player entered an adjacent room", "player is next to this room",
                       "boss defeated")
AGENT_REUSE_PREFIXES = ("refresh:",)


def _priority(kind: str, ctx: dict) -> int:
    if ctx.get("owed"):
        return URGENT
    if ctx.get("reason") == "floor start":        # asked ahead (agent mode asks every room's layout and clear too)
        return BACKGROUND
    if kind == "room_clear":
        return URGENT
    if kind in ("room_layout", "payout_plan", "drops", "rocks", "item_choice"):
        return NEXT_DOOR
    return BACKGROUND


def _deck_short(kind: str, prev: dict, ctx: dict) -> bool:
    """A deck whose unused tail is shorter than the game asked for: the gate can't keep it (the game would
    be handed an empty deck back, and every roll after it left to vanilla)."""
    field = DECK_FIELD.get(kind)
    if not field:
        return False
    try:
        wanted = max(1, int(ctx.get("wanted") or 1))
    except (TypeError, ValueError):
        wanted = 1
    return len(prev.get(field) or []) < wanted


def _routine_reask(ctx: dict) -> bool:
    """A re-ask the agent's deck answers as it stands (AGENT_REUSE_REASONS)."""
    reason = str(ctx.get("reason") or "")
    return not ctx.get("owed") and (reason in AGENT_REUSE_REASONS or reason.startswith(AGENT_REUSE_PREFIXES))


def _spent_drops(ctx: dict) -> dict:
    """A drops deck once the floor's kill drops are spent: nothing for every kill the game asked for."""
    try:
        wanted = max(1, int(ctx.get("wanted") or 1))
    except (TypeError, ValueError):
        wanted = 1
    return {"intent": "This floor's kill drops are spent.", "taunt": "",
            "outcomes": [{"reward": "nothing", "count": 1} for _ in range(wanted)]}


class PrioritySemaphore:
    """A semaphore whose waiters get a free slot lowest priority first, then first come first served.
    `async with sem.slot(priority):`, or plain `async with sem:` for BACKGROUND work."""

    def __init__(self, value: int) -> None:
        self._value = value
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._order = itertools.count()

    def slot(self, priority: int = BACKGROUND) -> "_Slot":
        return _Slot(self, priority)

    async def __aenter__(self) -> None:
        await self.acquire(BACKGROUND)

    async def __aexit__(self, *exc) -> None:
        self.release()

    async def acquire(self, priority: int) -> None:
        if self._value > 0 and not self._waiters:
            self._value -= 1
            return
        entry = (priority, next(self._order), asyncio.get_running_loop().create_future())
        heapq.heappush(self._waiters, entry)
        try:
            await entry[2]
        except asyncio.CancelledError:
            if entry[2].done() and not entry[2].cancelled():
                self.release()                    # handed a slot just as we were cancelled: pass it on
            else:
                self._waiters.remove(entry)
                heapq.heapify(self._waiters)
            raise

    def release(self) -> None:
        while self._waiters:
            fut = heapq.heappop(self._waiters)[2]
            if not fut.done():
                fut.set_result(None)
                return
        self._value += 1


class _Slot:
    def __init__(self, sem: PrioritySemaphore, priority: int) -> None:
        self.sem, self.priority = sem, priority

    async def __aenter__(self) -> None:
        await self.sem.acquire(self.priority)

    async def __aexit__(self, *exc) -> None:
        self.sem.release()


class Coordinator:
    """Routes requests to the DM, enforces latest-wins per key, applies guardrails."""

    def __init__(self, dm, cruelty: int, planner=None, max_concurrency: int = 6,
                 receipt: Receipt | None = None, epitaph: bool = True, gate=None, late_grace_s: float = 0.0,
                 agent=None) -> None:
        self.dm = dm
        # Agent mode: one call per trigger stacks, shuffles or keeps any number of decks (None: one call per deck).
        self.agent = agent
        self.agent_queue: dict[str, dict] = {}    # key -> the newest request waiting for an agent turn
        self.agent_flags: dict[str, str] = {}     # key -> why an open deck needs restacking (no request behind it)
        self.agent_flagged: dict[str, float] = {} # key -> the answer (its issued time) already flagged once
        self.agent_inflight: dict[str, tuple[int, asyncio.Task]] = {}   # key -> (rid, turn) deciding it now
        self.agent_gating: dict[str, tuple[int, asyncio.Task]] = {}     # key -> (rid, task) the gate is checking
        self.agent_turns: set[asyncio.Task] = set()
        self.agent_timer: asyncio.Task | None = None
        self.agent_due = 0.0                      # monotonic time agent_timer starts the next turn
        self.push_id = 0                          # pushes count down from -1: never one of the game's request ids
        self.agent_send: Send | None = None       # how the game was last reached (a switch of mode tells it)
        self.last_state: dict = {}                # the freshest snapshot the game sent (a turn with no request uses it)
        self.late_grace_s = late_grace_s          # how long past its deadline a call may still be delivered
        self.gate = gate                          # fast model that vetoes re-asks the DM would answer the same
        self.last_req: dict[str, dict] = {}       # key -> what the issued answer was written against
        self.epitaph_wanted = epitaph
        self.epitaph = epitaph and hasattr(dm, "epitaph")
        self.cruelty = cruelty
        self.memory = RunMemory()
        self.session: str | None = None
        self.mismatched = None                    # the last session whose hello had the wrong protocol
        self.last_seen = 0.0                      # monotonic time of the game's last message
        # Everything the engine does this run, with the LLM calls behind it (in memory only if no dir).
        self.receipt = receipt or Receipt()
        self.receipt_seq: dict[str, int] = {}     # key -> receipt entry of the answer the game holds
        self.used_seq: dict[str, int] = {}        # key -> receipt entry of the answer the game last used
        self.answer_seq: dict[tuple[str, int], int] = {}   # (key, request id) -> its answer's receipt entry
        self.drawn_seqs: set[int] = set()         # answers the game has drawn from (marked applied)
        self.trim_stale: set[str] = set()         # keys already re-asked once for losing a budget race
        # Every roll the game made, by (hook, cause, owner): what the DM controls and what it doesn't.
        self.coverage: Counter = Counter()
        self.bg: set[asyncio.Task] = set()
        self.inflight: dict[str, tuple[int, asyncio.Task]] = {}
        self.inflight_kind: dict[str, str] = {}
        self.sem = PrioritySemaphore(max_concurrency)
        # Per decision kind: req, ok, late, superseded, timeout, error, replaced, applied, fallback.
        self.stats: dict[str, Counter] = defaultdict(Counter)
        self.usage = Counter()                    # in / cached / out tokens, cost, calls
        # Opus narrative once per run + Sonnet brief once per floor, fed to the DM's system prompt.
        self.campaign = Campaign(planner, self._set_campaign, self.stats, self.usage,
                                 self._record, on_brief=self._brief_written)
        self.memory.economy.cruelty = cruelty

    def _set_campaign(self, text: str) -> None:
        """The planners' narrative + floor brief, into the DM's and the agent's system prompts."""
        for model in (self.dm, self.agent):
            if hasattr(model, "set_campaign"):
                model.set_campaign(text)

    @property
    def decider(self):
        """Whoever writes the decks now: the agent in agent mode, else the DM."""
        return self.agent if self.agent is not None else self.dm

    def summary(self) -> None:
        """Log how many answers were requested, paid for, and actually used, per kind."""
        if not self.stats:
            return
        cols = ("req", "ok", "local", "late", "applied", "replaced", "reused", "superseded", "timeout", "error",
                "cooldown", "fallback")
        log.info("summary %-11s %s", "kind", " ".join(f"{c:>10}" for c in cols))
        for kind in sorted(k for k in self.stats if k not in ("gate", "agent")):
            log.info("        %-11s %s", kind, " ".join(f"{self.stats[kind][c]:>10}" for c in cols))
        g = self.stats.get("gate")
        if g:
            log.info("        gate        keep=%d (DM call saved) reask=%d error=%d", g["keep"], g["reask"], g["error"])
        a = self.stats.get("agent")
        if a:
            log.info("        agent       turns=%d calls=%d stacked=%d shuffled=%d kept=%d unchanged=%d pushed=%d "
                     "rejected=%d skipped=%d timeout=%d error=%d", a["turn"], a["calls"], a["stack"], a["shuffle"],
                     a["keep"], a["unchanged"], a["pushed"], a["rejected"], a["skipped"], a["timeout"], a["error"])
        u = self.usage
        if u["calls"]:
            log.info("        tokens in=%d (cached %d, %.0f%%) out=%d  cost=$%.4f over %d calls",
                     u["in"], u["cached"], 100 * u["cached"] / max(1, u["in"]), u["out"], u["cost"], u["calls"])
        for line in coverage_lines(self.coverage):
            log.info("        %s", line)

    def reconfigure(self, engine: "Engine") -> None:
        """Swap in other models mid-run (the app's Settings). The run, its receipt, memory and plans carry
        on; calls already in flight finish on the old models."""
        old = (self.dm, self.campaign.planner, self.gate, self.agent)
        self.dm, self.gate, self.late_grace_s = engine.dm, engine.gate, engine.late_grace
        self.agent = engine.agent
        if (old[3] is None) != (self.agent is None) and self.agent_send and self.session:
            task = asyncio.create_task(self._send_quietly(self.agent_send, {
                "t": "mode", "session": self.session, "agent": self.agent is not None}))
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)
        self.epitaph = self.epitaph_wanted and hasattr(engine.dm, "epitaph")
        self.sem = PrioritySemaphore(engine.concurrency)
        self.set_cruelty(engine.cruelty)
        self.campaign.planner = engine.planner
        self.campaign._push()
        if self.agent is None and self.agent_queue:
            # Agent mode switched off: what was waiting for a turn is asked one deck at a time.
            queued, self.agent_queue = list(self.agent_queue.values()), {}
            for msg in queued:
                self._spawn_run(msg, self.agent_send)
        if self.agent is None:
            self.agent_flags.clear()
        kept = (self.dm, engine.planner, self.gate, self.agent)
        replaced = [o for o in old if hasattr(o, "client") and o not in kept]
        if replaced:
            task = asyncio.create_task(_close_later(replaced))
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)
        engine.describe()

    async def handle(self, msg: dict, send: Send) -> None:
        self.last_seen = time.monotonic()
        self.agent_send = send
        t = msg.get("t")
        if t == "hello":
            if msg.get("proto") != bridge.PROTO:
                # Told once per session; the mod shows the player which side needs updating.
                if self.mismatched != msg.get("session"):
                    self.mismatched = msg.get("session")
                    log.error("the mod speaks protocol %s, this backend %d: update the %s",
                              msg.get("proto"), bridge.PROTO,
                              "backend" if isinstance(msg.get("proto"), int) and msg["proto"] > bridge.PROTO
                              else "mod")
                await send({"t": "welcome", "session": msg.get("session"), "proto": bridge.PROTO})
                return
            self.summary()
            self.session = str(msg.get("session"))
            if msg.get("run") != self.memory.run_id or msg.get("new_run"):
                self.memory.reset(msg.get("run"))
                self.campaign.reset()
                for _rid, task in list(self.inflight.values()):
                    task.cancel()                 # asked in the old run: don't spend DM slots on it
                self.inflight.clear()
                self._cancel_agent()
                self.push_id = 0
                self.stats.clear()                # the summary above was the last run's; this one starts at zero
                self.usage.clear()                # (cleared in place: the campaign counts into the same objects)
                self.receipt.start_run(msg.get("run"), fresh=bool(msg.get("new_run")))
                if not msg.get("new_run"):        # a continue: its plans are in its receipt, don't rewrite them
                    self.campaign.restore(*self.receipt.saved_plans())
                self.receipt_seq.clear()
                self.used_seq.clear()
                self.answer_seq.clear()
                self.drawn_seqs.clear()
                self.trim_stale.clear()
                self.coverage = Counter(self.receipt.coverage)   # a continue carries its tally on
                self.last_req.clear()
                if self.receipt.path:
                    log.info("receipt -> %s", self.receipt.path)
            self.memory.economy.cruelty = self.cruelty
            log.info("game connected  session=%s run=%s cruelty=%s", self.session, msg.get("run"), self.cruelty)
            # agent: the game asks every room's layout and clear at floor start (the agent writes ahead).
            await send({"t": "welcome", "session": self.session, "proto": bridge.PROTO, "cruelty": self.cruelty,
                        "agent": self.agent is not None})
        elif t == "prepare":
            self._prepare(msg, send)
        elif t == "req":
            self._start(msg, send)
        elif t == "drawn":
            await self._drawn(str(msg.get("key")), msg.get("id"), msg.get("outcome"), msg.get("cause"), send)
        elif t == "applied":                      # PROTO 1's whole-answer use: one draw of all of it
            await self._drawn(str(msg.get("key")), None, None, None, send)
        elif t == "abandon":
            # The player left a room whose deck was never used: nothing in it is promised any more.
            self.memory.forget(str(msg.get("key")))
        elif t == "effect":
            # What a used answer actually did in the game. It follows the answer's "applied".
            key = str(msg.get("key"))
            if self.receipt.add_effect(self.used_seq.get(key, self.receipt_seq.get(key)), msg.get("effect")):
                log.info("%-26s effect: %s", key, "; ".join(map(str, msg.get("effect") or [])))
            self.memory.on_effect(key, msg.get("effect"))      # the DM's memory of what it did
        elif t == "event" and msg.get("name") == "fallback":
            # The game froze waiting for this decision and gave up on it (the DM never answered, even
            # after re-asks): the game went ahead without it. Not news for the DM.
            data = msg.get("data") or {}
            self.stats[str(data.get("kind"))]["fallback"] += 1
            why = "no answer while the game waited" if data.get("asked") else "never asked"
            log.info("%-11s %-14s %s -> game went ahead without the DM", data.get("kind"), data.get("key"), why)
            self._record(str(data.get("kind")), "fallback", key=data.get("key"), result=f"DM gave up ({why})")
        elif t == "event" and msg.get("name") == "rolls":
            self._rolls(msg.get("data") or {})
        elif t == "event" and msg.get("name") in ("death", "victory"):
            self.memory.on_game_event(str(msg.get("name")), msg.get("data") or {})
            for _rid, task in list(self.inflight.values()):
                task.cancel()                     # the run is over: nothing still being decided can be used
            self.inflight.clear()
            self._cancel_agent()
            await self._run_over(str(msg["name"]), msg.get("data") or {}, send)
        elif t == "event":
            self.memory.on_game_event(str(msg.get("name")), msg.get("data") or {})
        elif t == "ping":
            await send({"t": "pong", "session": self.session, "id": msg.get("id")})

    def set_cruelty(self, value: int) -> None:
        """From the app's Settings; takes effect for the next decision."""
        cruelty = max(0, min(10, int(value)))
        if cruelty != self.cruelty:
            log.info("cruelty %d -> %d", self.cruelty, cruelty)
            self._record("cruelty", "set", result=f"{self.cruelty} -> {cruelty}")
            self.cruelty = cruelty
        self.dm.set_cruelty(cruelty)
        if self.agent is not None:
            self.agent.set_cruelty(cruelty)
        self.memory.economy.cruelty = cruelty

    # ------------------------------------------------------------------ decks
    def _observe(self, state: dict) -> None:
        """Every snapshot the game sends: a new floor starts its economy ledger (synchronously, before any
        of its draws can come in), then the planners, whose floor brief sees last floor against vanilla."""
        if state:
            self.last_state = state
        if self.memory.economy.observe(state):
            log.info("economy     new floor ledger (%d normal rooms)", self.memory.economy.rooms)
        self.campaign.observe(state, {**self.memory.digest(), "economy": self.memory.report()}, self.cruelty)

    def _brief_written(self, sig: tuple, text: str) -> None:
        """A floor brief is in: its ECONOMY block sets that floor's budgets."""
        words = parse_economy(text)
        self.memory.economy.set_words(sig, words)
        if words:
            log.info("economy     %s", " ".join(f"{f}={w}" for f, w in words.items()))

    async def _drawn(self, key: str, rid, outcome, cause, send: Send) -> None:
        """The game handed out one outcome of deck `key`. The first draw from an answer is its "applied";
        a family pushed over budget re-asks the other decks still promising it."""
        try:
            rid = int(rid) if rid is not None else None
        except (TypeError, ValueError):
            rid = None
        kind = (self.memory.issued.get(key) or {}).get("kind")
        first, overs, counted = self.memory.on_drawn(key, rid, outcome, cause)
        seq = self.answer_seq.get((key, rid)) if rid is not None else None
        if seq is None:
            seq = self.receipt_seq.get(key) if first else self.used_seq.get(key)
        if seq is not None and seq not in self.drawn_seqs:
            self.drawn_seqs.add(seq)
            self.stats[kind or kind_of(key)]["applied"] += 1
            self.receipt.update(seq, "applied")
            if self.receipt_seq.get(key) == seq:
                self.receipt_seq.pop(key, None)
        if seq is not None:
            self.used_seq[key] = seq
        self.receipt.add_drawn(seq, key, kind or kind_of(key), counted, cause)
        if overs:
            keys = self.memory.economy.stale_keys(overs, exclude=key)
            log.info("%-26s drew past the %s budget -> re-ask %s", key, "/".join(overs), ", ".join(keys) or "nothing")
            await self._restack(send, keys, "/".join(overs) + " over budget")

    def _rolls(self, data: dict) -> None:
        """The mod's roll audit: how many of the game's rolls each owner took, and which no stream did. With
        LOG_SPAWNS it carries samples of what spawns looked like (spawner, seed, cause): kept in the receipt."""
        rows, counts = [], data.get("counts")
        for c in counts if isinstance(counts, list) else []:
            if not isinstance(c, dict):
                continue
            try:
                n = int(c.get("n") or 0)
            except (TypeError, ValueError):
                continue
            row = (str(c.get("hook")), str(c.get("cause") or "-"), str(c.get("owner") or "uncontrolled"))
            if n > 0:
                self.coverage[row] += n
                rows.append([*row, n])
        if rows:
            free = sum(r[3] for r in rows if r[2] == "uncontrolled")
            restored = sum(r[3] for r in rows if r[2] == RESTORED)
            samples = data.get("samples") if isinstance(data.get("samples"), list) else None
            self._record("rolls", "tally", result=f"{sum(r[3] for r in rows) - free - restored} controlled, "
                                                  f"{free} uncontrolled" + (f", {restored} restored" if restored else ""),
                         rolls=rows, **({"samples": samples[:100]} if samples else {}))

    # ------------------------------------------------------------------ receipt
    def _record(self, what: str, status: str, **fields) -> int:
        """Add an entry to the receipt. Returns its seq."""
        return self.receipt.add(what, status, session=self.session, **fields)

    @staticmethod
    async def _send_quietly(send: Send, msg: dict) -> None:
        try:
            await send(msg)
        except Exception:                         # noqa: BLE001 - game gone; the file still has it
            pass

    # ---------------------------------------------------------- after-death screen
    async def _run_over(self, outcome: str, data: dict, send: Send) -> None:
        """Answer the game's death / victory with the DM's side of the stats, then its epitaph."""
        summary = epilogue.build(outcome, data, self.receipt, self.memory.run_id, self.campaign.narrative)
        summary["epitaph_pending"] = self.epitaph
        self._record("run_summary", outcome, result=epilogue.one_line(summary), summary=summary)
        log.info("run over\n%s", "\n".join("    " + line for line in epilogue.as_text(summary).splitlines()))
        session = self.session                    # the epitaph may land after the next run said hello
        await send(_summary_msg(session, summary))
        if self.epitaph:
            task = asyncio.create_task(self._write_epitaph(summary, session, send))
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)

    async def _write_epitaph(self, summary: dict, session: str | None, send: Send) -> None:
        self.stats["epitaph"]["req"] += 1
        t0 = time.perf_counter()
        call = None
        try:
            wait = self._cooldown_left()
            if 0 < wait < EPITAPH_TIMEOUT_S:      # rate-limited: a short cooldown is worth waiting out
                await asyncio.sleep(wait)
            async with self.sem:
                text, call = await asyncio.wait_for(self.dm.epitaph(epilogue.epitaph_prompt(summary)),
                                                    EPITAPH_TIMEOUT_S)
            summary["epitaph"] = epilogue.clean_epitaph(text)
            if not summary["epitaph"]:
                raise ValueError("epitaph was empty after cleaning")
        except Exception as e:                    # noqa: BLE001 - the screen just goes without
            self.stats["epitaph"]["error"] += 1
            log.warning("epitaph     failed (%s: %s)", type(e).__name__, error_line(e))
            self._record("epitaph", "error", ms=_ms(t0), **error_fields(e), llm=getattr(e, "call", None) or call)
            summary["epitaph"] = None
        else:
            self.stats["epitaph"]["ok"] += 1
            if (call or {}).get("usage"):
                self.usage.update({**call["usage"], "calls": 1})
            self._record("epitaph", "written", ms=_ms(t0), llm=call, result=summary["epitaph"])
            log.info('epitaph     %5dms  "%s"', _ms(t0), summary["epitaph"])
        summary["epitaph_pending"] = False
        await self._send_quietly(send, _summary_msg(session, summary))

    # ------------------------------------------------------------------ loading
    def _prepare(self, msg: dict, send: Send) -> None:
        """The game is on a loading screen (run start, or a new floor): start the planners now and say
        when this floor's plans are in, i.e. the run narrative and the floor brief. Safe to repeat (the
        game re-sends after a reconnect): each plan is only ever written once."""
        state = msg.get("state") or {}
        self._observe(state)
        session, floor = self.session, msg.get("floor")
        task = asyncio.create_task(self._announce_ready(session, floor, send, self.campaign.narrative_waiter(),
                                                        floor_signature(state)))
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    async def _announce_ready(self, session: str | None, floor, send: Send, narrative, sig) -> None:
        t0 = time.perf_counter()
        status = await narrative
        if status == "cancelled":
            return                                # a new run said hello; its own prepare will answer
        brief = await self.campaign.brief_waiter(sig)
        if brief == "cancelled":
            return
        if status == "ready" and brief not in ("ready", "none"):
            status = brief                        # the narrative is in but this floor's brief isn't
        log.info("floor %s plans %s after %dms (brief: %s) -> floor starts", floor, status, _ms(t0), brief)
        await self._send_quietly(send, {"t": "ready", "session": session, "floor": floor, "status": status})

    # ----------------------------------------------------------------- requests
    def _start(self, msg: dict, send: Send) -> None:
        key, rid, kind = str(msg["key"]), int(msg["id"]), msg.get("kind")
        if kind not in KINDS:
            log.warning("unknown kind %r", kind)
            return
        self.stats[kind]["req"] += 1
        self._observe(msg.get("state") or {})
        if self.agent is not None:
            old = self.agent_gating.get(key)
            if old and old[0] < rid and not old[1].done():
                old[1].cancel()                  # latest-wins: newer context supersedes
                self.stats[kind]["superseded"] += 1
            if self.memory.previous(key) and key not in self.agent_queue and key not in self.agent_inflight:
                self._spawn_gate(msg, send)      # a deck already written: the gate may keep it, no turn needed
            else:
                self._queue_agent(msg, send)
            return
        self._spawn_run(msg, send)

    def _spawn_run(self, msg: dict, send: Send) -> None:
        """One DM call for this request (classic mode)."""
        key, rid, kind = str(msg["key"]), int(msg["id"]), msg["kind"]
        old = self.inflight.get(key)
        if old and old[0] < rid and not old[1].done():
            old[1].cancel()                      # latest-wins: newer context supersedes
            self.stats[kind]["superseded"] += 1
            log.debug("%-11s %-14s superseded by a fresher request", kind, key)
        task = asyncio.create_task(self._run(msg, send))
        self.inflight[key] = (rid, task)
        self.inflight_kind[key] = kind
        task.add_done_callback(lambda _t, k=key, r=rid: self._cleanup(k, r))

    def _cleanup(self, key: str, rid: int) -> None:
        cur = self.inflight.get(key)
        if cur and cur[0] == rid:
            self.inflight.pop(key, None)

    async def _await_plans(self, state: dict) -> tuple[str, int]:
        """Wait for this floor's plans: (the brief's status, ms waited). Never answer against a brief that is
        about to replace itself. Superseding cancels this wait too, so it costs nothing. Like the slot queue,
        not the DM's time. "cancelled" / "moved": a new run (or floor) replaced what this was asked against."""
        tb = time.perf_counter()
        try:                                      # the brief waits for it too, but not if the floor has no signature
            narrative = await asyncio.wait_for(self.campaign.narrative_waiter(), NARRATIVE_WAIT_S)
        except asyncio.TimeoutError:
            narrative = "timeout"
        brief = narrative if narrative == "cancelled" else await self.campaign.brief_waiter(floor_signature(state))
        return brief, _ms(tb)

    def _request_ctx(self, kind: str, key: str, state: dict, ctx: dict) -> tuple[dict, int | None]:
        """The ctx the model sees for a request about `key`: the answer it replaces, the economy, and the
        kind's own limits. Also the kill drops left (drops only, else None)."""
        prev = self.memory.previous(key)
        if prev:                                  # a re-ask replaces this answer; show it so the model restates it
            ctx = {**ctx, "your_previous_answer": _compact(prev)}
        ctx = {**ctx, "economy": self.memory.economy.view(key)}
        kills_left = None
        if kind == "drops":                       # the cap clamp() holds kill drops to, so intents don't promise past it
            kills_left = kill_drops_left(state, self.cruelty, self.memory.counters)
            ctx["economy"]["kill_drops"] = {"cap": kill_drop_cap(state, self.cruelty), "left": kills_left}
            ctx["economy"]["verdict"] += f"; kill drops: {kills_left} left this floor"
        elif kind == "floor_plan":
            ctx["allowed_curses"] = max_curses(self.cruelty, mercy_active(state))
        elif kind == "item_choice" and ctx.get("candidates"):
            ctx["candidates"] = with_effects(ctx["candidates"])
        return ctx, kills_left

    async def _run(self, msg: dict, send: Send) -> None:
        key, rid, kind = str(msg["key"]), int(msg["id"]), msg["kind"]
        state, ctx = msg.get("state") or {}, msg.get("ctx") or {}
        brief, waited = await self._await_plans(state)
        if waited >= 50:
            log.info("%-11s %-14s waited %dms for the floor brief (%s)", kind, key, waited, brief)
        if brief in ("cancelled", "moved"):
            # Answering would be written with no campaign, and the game re-asks under the new one.
            self._record(kind, "superseded", ms=waited, key=key, req_id=rid, reason=brief)
            self.stats[kind]["superseded"] += 1
            return
        prev = self.memory.previous(key)
        asked_ctx = ctx                           # before the previous answer is mixed in: this is what the gate diffs
        ctx, kills_left = self._request_ctx(kind, key, state, ctx)
        deadline = max(MIN_DEADLINE_S, min(MAX_DEADLINE_S, float(msg.get("deadline_ms", 4000)) / 1000))
        digest = self.memory.digest(exclude=key, in_flight=self._in_flight(key))
        # What the receipt keeps of the request: the user message is exactly these, as JSON.
        rec = {"key": key, "req_id": rid, "deadline_ms": int(deadline * 1000),
               **({"brief_wait_ms": waited} if waited >= 50 else {}),
               "request": {"state": state, "ctx": ctx, "memory": digest}}
        t0 = time.perf_counter()
        base = {"t": "res", "session": self.session, "id": rid, "key": key, "kind": kind}
        raw: dict = {}
        call = None
        campaign = ""
        try:
            if kills_left == 0:
                # This floor's kill drops are spent: every entry would be clamped to nothing anyway, so the
                # deck is written here and the DM isn't paid for it.
                raw, late = _spent_drops(ctx), False
                campaign = getattr(self.dm, "campaign", "")
            else:
                if prev and not _deck_short(kind, prev, asked_ctx) \
                        and await self._gate_keeps(kind, key, state, asked_ctx, digest, prev, t0):
                    await self._reissue(kind, key, rid, prev, send, _ms(t0))
                    return
                # Rate-limited: hold the request until the model's cooldown is over, then make the call. A
                # cooldown longer than the deadline is answered "cooldown" at the deadline instead: answered at
                # once, the game would re-ask on the next frame and give up after HOLD_RETRIES. Like the slot
                # queue, not the DM's time.
                wait = self._cooldown_left()
                if wait:
                    if wait >= deadline:
                        await asyncio.sleep(deadline)
                        raise CoolingDown(self._cooldown_left())
                    log.info("%-11s %-14s rate-limited: holding %.1fs for the cooldown", kind, key, wait)
                    await asyncio.sleep(wait)
                # the deadline counts from here: queueing for a slot isn't the DM's time
                async with self.sem.slot(_priority(kind, ctx)):
                    campaign = getattr(self.dm, "campaign", "")   # what this answer is written against
                    raw, late = await self._decide(kind, key, state, ctx, digest, deadline, t0)
            usage = raw.pop("_usage", None)
            call = raw.pop("_call", None)
            decision, budget = self._clamp(kind, key, raw, state, ctx)
        except asyncio.CancelledError:
            # Superseded; the newer request will answer. A call already in flight may still be billed.
            self._record(kind, "superseded", ms=_ms(t0), **rec)
            return
        except CoolingDown as e:
            # Nothing was sent, so nothing is billed: the game re-asks if it is waiting.
            self.stats[kind]["cooldown"] += 1
            log.info("%-11s %-14s not sent: %s", kind, key, e)
            self._record(kind, "cooldown", ms=_ms(t0), result=str(e), **rec)
            await send({**base, "ok": False, "reason": "cooldown", "retry_ms": int(e.retry_in * 1000)})
            return
        except asyncio.TimeoutError:
            self.stats[kind]["timeout"] += 1
            log.warning("%-11s %-14s TIMEOUT after %.0fms -> the game re-asks if it is waiting", kind, key, _ms(t0))
            self._record(kind, "timeout", ms=_ms(t0), **rec)
            await send({**base, "ok": False, "reason": "timeout"})
            return
        except Exception as e:                    # noqa: BLE001 - never crash the bridge
            self.stats[kind]["error"] += 1
            log.exception("%s %s failed: %s", kind, key, error_line(e))
            self._record(kind, "error", ms=_ms(t0), **error_fields(e),
                         llm=getattr(e, "call", None) or call, raw=raw or None, **rec)
            await send({**base, "ok": False, "reason": type(e).__name__})
            return
        await self._issue(kind, key, rid, decision, budget, send, state=state, ctx=ctx, asked_ctx=asked_ctx,
                          digest=digest, rec=rec, raw=raw, call=call, usage=usage, ms=_ms(t0), late=late,
                          local=kills_left == 0, campaign=campaign)

    async def _reissue(self, kind: str, key: str, rid: int, prev: dict, send: Send, ms: int) -> None:
        """The same deck answers request `rid` too: what is left of it, and how many draws that already accounts
        for (the mod skips only draws made since)."""
        self.memory.reissue(key, rid)
        seq = self.receipt_seq.get(key, self.used_seq.get(key))
        if seq is not None:
            self.answer_seq[(key, rid)] = seq
        await send({"t": "res", "session": self.session, "id": rid, "key": key, "kind": kind, "ok": True, "ms": ms,
                    "decision": prev, "drawn_base": self.memory.drawn_total.get(key, 0)})

    def _clamp(self, kind: str, key: str, raw: dict, state: dict, ctx: dict):
        """(the legal answer, its budget). Trimmed against the economy as it is now, and committed in the same
        step by _issue: no other answer can land in between, so the first to finish takes the last coin."""
        budget = self.memory.economy.budget(key, kind, ctx)
        return clamp(kind, raw, state, ctx, self.cruelty, self.memory.counters, budget), budget

    async def _issue(self, kind: str, key: str, rid: int, decision: dict, budget, send: Send, *, state: dict,
                     ctx: dict, asked_ctx: dict, digest: dict, rec: dict, raw: dict, call: dict | None,
                     usage: dict | None, ms: int, late: bool = False, local: bool = False, campaign: str = "",
                     push: bool = False, turn: int | None = None) -> None:
        """Commit a clamped answer and send it to the game: as the answer to request `rid`, or (push) as an
        unasked rewrite of an open deck. `usage` is counted here only when this answer paid for its own call."""
        self.stats[kind]["ok"] += 1
        if local:
            self.stats[kind]["local"] += 1
        if late:
            self.stats[kind]["late"] += 1
        if push:
            self.stats[kind]["pushed"] += 1
        old_seq = self.receipt_seq.get(key)
        if self.memory.note_issued(key, kind, decision, ctx, rid):
            self.stats[kind]["replaced"] += 1    # paid for an answer the game never used
            self.receipt.update(old_seq, "replaced")
        self.receipt_seq[key] = self.answer_seq[(key, rid)] = self._record(
            kind, "ok", ms=ms, llm=call, raw=raw, decision=decision, **({"late": True} if late else {}),
            **({"local": "kill drops spent"} if local else {}), **({"push": True} if push else {}),
            **({"turn": turn} if turn is not None else {}), **rec)
        self.last_req[key] = {"state": state, "ctx": asked_ctx, "events": digest["recent_events"],
                              "campaign": campaign}
        tok = ""
        if usage:
            self.usage.update({**usage, "calls": 1})
            tok = f'  [{usage["in"]}/{usage["cached"]}c/{usage["out"]} tok]'
        log.info("%-11s %-14s %5dms%s%s  %s%s", kind, key, ms,
                 " LATE" if late else (" LOCAL" if local else (" PUSH" if push else "")), tok,
                 decision.get("intent", ""),
                 f'  "{decision["taunt"]}"' if decision.get("taunt") else "")
        if sum(c["ok"] for c in self.stats.values()) % 25 == 0:
            self.summary()
        if decision.get("guardrails"):
            log.info("%28s guardrails: %s", "", "; ".join(decision["guardrails"]))
        res = {"t": "res", "session": self.session, "id": rid, "key": key, "kind": kind, "ok": True, "ms": ms,
               "decision": decision}
        if push:                                  # the game didn't ask: these draws are already accounted for
            res.update(push=True, drawn_base=self.memory.drawn_total.get(key, 0))
        await send(res)
        if budget.trimmed and key not in self.trim_stale:
            # Lost a race for the budget: its deck may now be a worse story. One re-ask, which sees the
            # winner in your_open_decks (and the gate drops it if nothing meaningful changed).
            self.trim_stale.add(key)
            await self._restack(send, [key], "trimmed to the budget")

    async def _restack(self, send: Send, keys: list[str], why: str) -> None:
        """These decks' unused entries are out of date. The game re-asks them (`stale`); in agent mode the
        agent is woken to restack them itself."""
        if not keys:
            return
        if self.agent is not None:
            self._flag(keys, why, send)
            return
        await self._send_quietly(send, {"t": "stale", "session": self.session, "keys": keys, "why": why})

    def _cooldown_left(self) -> float:
        """Seconds until the deck writer's model may be called again after a 429 (0: it may now)."""
        cooldown = getattr(self.decider, "cooldown", None)
        return cooldown.remaining() if cooldown else 0.0

    def _deciding(self):
        """(key, (rid, task)) for every request being answered now: DM calls, agent turns, gate checks."""
        return itertools.chain(self.inflight.items(), self.agent_inflight.items(), self.agent_gating.items())

    def _in_flight(self, key: str | None) -> list[tuple[str, str]]:
        """Decisions being made right now (no answer yet), other than `key`."""
        out = []
        for k, (_rid, task) in self._deciding():
            if k != key and not task.done():
                out.append((k, self.inflight_kind.get(k, "?")))
        return out

    async def _decide(self, kind: str, key: str, state: dict, ctx: dict, digest: dict, deadline: float,
                      t0: float) -> tuple[dict, bool]:
        """(raw answer, whether it came after the deadline)."""
        return await self._within(self.dm.decide(kind, state, ctx, digest), deadline, f"{kind:<11} {key:<14}", t0)

    async def _within(self, coro, deadline: float, label: str, t0: float):
        """(the call's result, whether it came after the deadline). With a late grace, a call past its deadline
        keeps running and is delivered when it lands: the game holds the key as pending meanwhile (so it
        doesn't re-ask) and takes a late result whenever it arrives. Without one, the deadline is final."""
        call = asyncio.ensure_future(coro)
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(call), deadline), False
            except asyncio.TimeoutError:
                if not self.late_grace_s:
                    raise
            log.info("%s past its %.0fms deadline after %.0fms -> still waiting (up to %.0fs)",
                     label, deadline * 1000, _ms(t0), self.late_grace_s)
            return await asyncio.wait_for(asyncio.shield(call), self.late_grace_s), True
        finally:
            if not call.done():
                call.cancel()                     # timed out or superseded: stop the call (kills claude -p)

    async def _gate_keeps(self, kind: str, key: str, state: dict, ctx: dict, digest: dict, prev: dict,
                          t0: float) -> bool:
        """Ask the fast gate whether the answer already issued for `key` still fits. True = reuse it.
        Fails open: no gate, no earlier request to compare, a gate error or an unreadable reply all re-ask."""
        last = self.last_req.get(key)
        if self.gate is None or last is None:
            return False
        if ctx.get("owed"):                       # the game is frozen on this deck: the old answer didn't cover it
            return False
        campaign_changed = last["campaign"] != getattr(self.decider, "campaign", "")
        changes = diff({"state": last["state"], "ctx": last["ctx"]}, {"state": state, "ctx": ctx})
        fresh = new_events(last["events"], digest["recent_events"])
        if fresh:
            changes["new_events"] = fresh[-6:]
        call = None
        try:
            if not changes and not campaign_changed:
                reask, why = False, "nothing changed"     # no model needed to see that
            elif campaign_changed:
                reask, why = True, "campaign changed"     # a new brief is the reason to think again
            else:
                reask, why, call = await self.gate.should_reask(kind, _compact(prev), changes, campaign_changed,
                                                                plan=self.campaign.brief)
        except Exception as e:                    # noqa: BLE001 - the gate is an optimisation, never a blocker
            self.stats["gate"]["error"] += 1
            log.warning("%-11s %-14s gate failed (%s: %s) -> re-ask", kind, key, type(e).__name__, error_line(e))
            self._record("gate", "error", ms=_ms(t0), **error_fields(e), llm=getattr(e, "call", None), key=key)
            return False
        if call and call.get("usage"):
            self.usage.update({**call["usage"], "calls": 1})
        self.stats["gate"]["reask" if reask else "keep"] += 1
        if reask:
            self._record("gate", "reask", ms=_ms(t0), llm=call, key=key, result=why)
            log.info("%-11s %-14s gate: re-ask (%s)", kind, key, why)
            return False
        self.stats[kind]["reused"] += 1
        self._record("gate", "reused", ms=_ms(t0), llm=call, key=key, result=why)
        log.info("%-11s %-14s gate: keep (%s) -> reused answer, %d change(s) ignored", kind, key, why, len(changes))
        return True


    # ------------------------------------------------------------------ agent mode
    def _queue_agent(self, msg: dict, send: Send) -> None:
        """The request waits for the next agent turn; a newer one for the same key replaces it."""
        key, rid, kind = str(msg["key"]), int(msg["id"]), msg["kind"]
        self.agent_send = send
        old = self.agent_queue.get(key)
        if old and int(old["id"]) >= rid:
            return
        if old:
            self.stats[kind]["superseded"] += 1
            log.debug("%-11s %-14s superseded by a fresher request", kind, key)
        self.agent_queue[key] = msg
        self.agent_flags.pop(key, None)           # the request is its restack
        self._kick_agent(send, AGENT_BATCH_S[_priority(kind, msg.get("ctx") or {})])

    def _spawn_gate(self, msg: dict, send: Send) -> None:
        key, rid = str(msg["key"]), int(msg["id"])
        task = asyncio.create_task(self._agent_gate(msg, send))
        self.agent_gating[key] = (rid, task)
        self.inflight_kind[key] = msg["kind"]
        task.add_done_callback(lambda _t, k=key, r=rid: self._release(self.agent_gating, k, r))

    async def _agent_gate(self, msg: dict, send: Send) -> None:
        """A request for a deck the agent already wrote: the re-ask gate decides whether it still fits (answered
        here, no turn) or the agent should look again (it waits for the next turn), as it does for the DM."""
        key, rid, kind = str(msg["key"]), int(msg["id"]), msg["kind"]
        state, asked_ctx = msg.get("state") or {}, msg.get("ctx") or {}
        brief, waited = await self._await_plans(state)
        if brief in ("cancelled", "moved"):
            self._record(kind, "superseded", ms=waited, key=key, req_id=rid, reason=brief)
            self.stats[kind]["superseded"] += 1
            return
        t0 = time.perf_counter()
        prev = self.memory.previous(key)
        if prev and not _deck_short(kind, prev, asked_ctx):
            if _routine_reask(asked_ctx) and key not in self.agent_flags:
                self.stats[kind]["reused"] += 1
                self._record(kind, "reused", ms=_ms(t0), key=key, req_id=rid,
                             result=f"{asked_ctx.get('reason')}: the agent's deck stands")
                log.info("%-11s %-14s %s -> reused the agent's deck", kind, key, asked_ctx.get("reason"))
                await self._reissue(kind, key, rid, prev, send, _ms(t0))
                return
            digest = self.memory.digest(exclude=key, in_flight=self._in_flight(key))
            if await self._gate_keeps(kind, key, state, asked_ctx, digest, prev, t0):
                await self._reissue(kind, key, rid, prev, send, _ms(t0))
                return
        if self.agent is None:                    # agent mode was switched off meanwhile
            self._spawn_run(msg, send)
        else:
            self._queue_agent({**msg, "gated": True}, send)

    def _flag(self, keys: list[str], why: str, send: Send) -> None:
        """Open decks gone out of date with no request behind them: the agent is woken to restack them. Each
        answer is flagged once, so a restack that loses a budget race again is left as it is."""
        self.agent_send = send
        for k in keys:
            entry = self.memory.issued.get(k)
            if entry is None or k not in self.last_req or k in self.agent_queue \
                    or self.agent_flagged.get(k) == entry["at"]:
                continue
            self.agent_flagged[k] = entry["at"]
            self.agent_flags[k] = why
        if self.agent_flags:
            self._kick_agent(send, AGENT_FLAG_WAIT_S)

    def _kick_agent(self, send: Send, delay: float) -> None:
        """Start a turn after `delay`, so a burst of requests shares one. A turn already coming takes this one
        too, brought forward if this can't wait as long."""
        due = time.monotonic() + delay
        t = self.agent_timer
        if t is not None and not t.done():
            if due >= self.agent_due:
                return
            t.cancel()
        self.agent_due = due
        self.agent_timer = asyncio.create_task(self._agent_dispatch(send, delay))

    async def _agent_dispatch(self, send: Send, delay: float) -> None:
        """Start turns for everything waiting: the most urgent requests first, AGENT_MAX_KEYS to a turn. The
        DM slots (self.sem) cap how many run at once. A request for a deck a turn is still deciding waits for
        it; then, like any request for a deck already written, it goes to the gate before it may join a turn."""
        if delay:
            await asyncio.sleep(delay)
        while True:
            busy = {k for k, (_r, t) in self.agent_inflight.items() if not t.done()}
            for m in list(self.agent_queue.values()):
                key = str(m["key"])
                if key not in busy and not m.get("gated") and self.memory.previous(key):
                    del self.agent_queue[key]
                    self._spawn_gate(m, send)
            ready = [m for m in self.agent_queue.values() if str(m["key"]) not in busy]
            if not ready and not self.agent_flags:
                return
            queued = sorted(ready, key=lambda m: (_priority(m["kind"], m.get("ctx") or {}), int(m["id"])))
            asks = queued[:AGENT_MAX_KEYS]
            for m in asks:
                self.agent_queue.pop(str(m["key"]), None)
            flags, self.agent_flags = self.agent_flags, {}
            task = asyncio.create_task(self._agent_turn(asks, flags, send))
            self.agent_turns.add(task)
            task.add_done_callback(self.agent_turns.discard)
            for m in asks:
                key, rid = str(m["key"]), int(m["id"])
                self.agent_inflight[key] = (rid, task)
                self.inflight_kind[key] = m["kind"]
                task.add_done_callback(lambda _t, k=key, r=rid: self._release(self.agent_inflight, k, r))
            # After the releases above: what waited on these keys can go now.
            task.add_done_callback(lambda _t: self._kick_agent(send, 0.0) if self.agent_queue else None)

    @staticmethod
    def _release(table: dict[str, tuple[int, asyncio.Task]], key: str, rid: int) -> None:
        cur = table.get(key)
        if cur and cur[0] == rid:
            table.pop(key, None)

    def _cancel_agent(self) -> None:
        """A new run, or the run is over: nothing waiting for or being decided by the agent can be used."""
        if self.agent_timer is not None:
            self.agent_timer.cancel()
        for task in [*self.agent_turns, *(t for _rid, t in self.agent_gating.values())]:
            task.cancel()
        self.agent_gating.clear()
        self.agent_queue.clear()
        self.agent_flags.clear()
        self.agent_flagged.clear()
        self.agent_inflight.clear()

    def _open_deck(self, key: str, why: str | None, state: dict) -> dict:
        """An open deck as the agent sees it: what is left, what it was for, and how the player has changed
        since it was written. One flagged for a restack also shows what it was written against (an item
        ranking's candidates, a room's shape), to rewrite it from."""
        e = self.memory.issued[key]
        d = e["decision"]
        out = {"key": key, "kind": e["kind"], "left": list(e["left"]), "drawn": self.memory.drawn_total.get(key, 0),
               "intent": d.get("intent", ""), **({"scheme": d["scheme"]} if d.get("scheme") else {}),
               "age_s": round(time.monotonic() - e["at"])}
        moved = state_diff(self.last_req[key].get("state") or {}, state)
        if moved:
            out["since_written"] = moved
        if why:
            ctx = {k: v for k, v in self.last_req[key]["ctx"].items() if k not in _OPEN_CTX_HIDDEN}
            if ctx.get("candidates"):
                ctx["candidates"] = with_effects(ctx["candidates"])
            out.update(restack=why, context=ctx)
        return out

    async def _agent_turn(self, asks: list[dict], flags: dict[str, str], send: Send) -> None:
        """One agent call: every request in `asks` answered, and any open deck restacked, in one response."""
        newest = max(asks, key=lambda m: int(m["id"])) if asks else None
        state = (newest.get("state") if newest else None) or self.last_state
        brief, waited = await self._await_plans(state)
        if brief in ("cancelled", "moved"):
            for m in asks:
                self.stats[m["kind"]]["superseded"] += 1
                self._record(m["kind"], "superseded", ms=waited, key=str(m["key"]), req_id=int(m["id"]), reason=brief)
            return
        t0 = time.perf_counter()
        pending: dict[str, dict] = {}
        spent: list[dict] = []
        for m in asks:
            key, kind, asked_ctx = str(m["key"]), m["kind"], m.get("ctx") or {}
            st = m.get("state") or {}
            ctx, kills_left = self._request_ctx(kind, key, st, asked_ctx)
            if kind == "drops":                   # written to last several rooms, not just the one asked about
                ctx["wanted"] = AGENT_DROPS_DEPTH
            elif kind == "rocks":
                ctx["wanted"] = AGENT_ROCKS_DEPTH
            p = {"key": key, "rid": int(m["id"]), "kind": kind, "state": st, "ctx": ctx, "asked_ctx": asked_ctx,
                 "deadline_ms": m.get("deadline_ms", 4000),
                 # A deck the game draws from while the turn runs can be used up before a keep_deck lands.
                 "had_deck": self.memory.previous(key) is not None,
                 "drawn_at_turn": self.memory.drawn_total.get(key, 0)}
            if kills_left == 0:
                spent.append(p)
            else:
                pending[key] = p
        digest = self.memory.digest(in_flight=[(k, kd) for k, kd in self._in_flight(None) if k not in pending])
        for p in spent:                           # this floor's kill drops are spent: no call needed for these
            raw = _spent_drops(p["ctx"])
            decision, budget = self._clamp(p["kind"], p["key"], raw, p["state"], p["ctx"])
            await self._issue(p["kind"], p["key"], p["rid"], decision, budget, send, state=p["state"], ctx=p["ctx"],
                              asked_ctx=p["asked_ctx"], digest=digest, raw=raw, call=None, usage=None, ms=_ms(t0),
                              local=True, rec={"key": p["key"], "req_id": p["rid"],
                                               "request": {"state": p["state"], "ctx": p["ctx"]}})
        # The open decks it may restack: this floor's, none being decided elsewhere or waiting for a request of
        # their own. A deck from an earlier floor can't be drawn any more.
        me = asyncio.current_task()
        busy = set(self.agent_queue) | {k for k, (_r, t) in self._deciding() if t is not me and not t.done()}
        here = floor_sig(state or {})
        open_keys = [k for k, e in reversed(list(self.memory.issued.items()))
                     if k not in pending and k not in busy and k in self.last_req and e["left"]
                     and e["kind"] in AGENT_RESTACK_KINDS
                     and floor_sig(self.last_req[k].get("state") or {}) in (here, None)]
        open_keys.sort(key=lambda k: k not in flags)    # flagged first, then newest
        if not pending and not any(k in flags for k in open_keys):
            return                                # the flagged decks were used up or asked for meanwhile
        trigger = "; ".join([f'{k} asked ({p["asked_ctx"].get("reason") or "no reason given"})'
                             for k, p in pending.items()]
                            + [f"{k} needs a restack ({w})" for k, w in flags.items() if k in open_keys])
        turn = {"trigger": trigger,
                "pending": [{"key": k, "kind": p["kind"], "context": _turn_ctx(p["ctx"])} for k, p in pending.items()],
                "open_decks": [self._open_deck(k, flags.get(k), state) for k in open_keys],
                "state": state, "economy": self.memory.economy.view(None),
                "memory": {k: v for k, v in digest.items() if k != "your_open_decks"}}
        deadline = max(MIN_DEADLINE_S, min(MAX_DEADLINE_S, max(float(p["deadline_ms"]) for p in pending.values()) / 1000)) \
            if pending else MAX_DEADLINE_S
        priority = min((_priority(p["kind"], p["asked_ctx"]) for p in pending.values()), default=BACKGROUND)
        rec = {"trigger": trigger, "keys": list(pending), "open_decks": open_keys, "deadline_ms": int(deadline * 1000),
               **({"brief_wait_ms": waited} if waited >= 50 else {}), "request": turn}
        a = self.stats["agent"]
        a["req"] += 1
        call = None
        try:
            wait = self._cooldown_left()
            if wait:
                if wait >= deadline:
                    await asyncio.sleep(deadline)
                    raise CoolingDown(self._cooldown_left())
                log.info("%-11s %-14s rate-limited: holding %.1fs for the cooldown", "agent", "turn", wait)
                await asyncio.sleep(wait)
            async with self.sem.slot(priority):
                agent = self.agent
                if agent is None:                 # agent mode was switched off while this waited
                    for p in pending.values():
                        self._spawn_run({"t": "req", "id": p["rid"], "key": p["key"], "kind": p["kind"],
                                         "deadline_ms": p["deadline_ms"], "state": p["state"], "ctx": p["asked_ctx"]},
                                        send)
                    return
                campaign = getattr(agent, "campaign", "")
                (calls, call), late = await self._within(agent.act(build_turn_message(turn)), deadline,
                                                         f"{'agent':<11} {'turn':<14}", t0)
        except asyncio.CancelledError:
            for p in pending.values():
                self.stats[p["kind"]]["superseded"] += 1
            self._record("agent", "superseded", ms=_ms(t0), **rec)
            return
        except Exception as e:                    # noqa: BLE001 - never crash the bridge (cooldown and timeout too)
            await self._agent_failed(e, pending, rec, t0, call, send)
            return
        ms = _ms(t0)
        usage = (call or {}).get("usage")
        tok = ""
        if usage:                                 # one call, however many decks it wrote
            self.usage.update({**usage, "calls": 1})
            tok = f'  [{usage["in"]}/{usage["cached"]}c/{usage["out"]} tok]'
        a["turn"] += 1
        a["calls"] += len(calls)
        if late:
            a["late"] += 1
        summary = ", ".join(f'{c.get("tool")}({(c.get("args") or {}).get("key")})' for c in calls) or "no calls"
        turn_seq = self._record("agent", "turn", ms=ms, llm=call, result=summary, **({"late": True} if late else {}),
                                calls=[{"tool": c.get("tool"), "key": (c.get("args") or {}).get("key")} for c in calls],
                                **rec)
        log.info("%-11s %-14s %5dms%s%s  %d call(s): %s", "agent", "turn", ms, " LATE" if late else "", tok, len(calls),
                 summary)
        answered: set[str] = set()
        for c in calls:
            why = await self._agent_call(c, pending, set(open_keys), answered, state, digest, campaign, turn_seq, ms,
                                         late, send)
            if why:
                key = str((c.get("args") or {}).get("key"))
                a["rejected"] += 1
                self._record("agent", "rejected", key=key, tool=c.get("tool"), turn=turn_seq, result=why)
                log.info("%-11s %-14s rejected %s: %s", "agent", key, c.get("tool"), why)
        for key, p in pending.items():
            if key not in answered:
                a["skipped"] += 1
                self.stats[p["kind"]]["skipped"] += 1
                self._record(p["kind"], "skipped", key=key, req_id=p["rid"], turn=turn_seq,
                             result="the agent didn't decide it")
                log.info("%-11s %-14s skipped by the agent -> the game re-asks if it is waiting", p["kind"], key)
                await self._send_quietly(send, {"t": "res", "session": self.session, "id": p["rid"], "key": key,
                                                "kind": p["kind"], "ok": False, "reason": "agent_skipped"})

    async def _agent_failed(self, e: BaseException, pending: dict[str, dict], rec: dict, t0: float, call,
                            send: Send) -> None:
        """The turn's call failed: every request it held is answered "no decision", as _run answers one."""
        a = self.stats["agent"]
        if isinstance(e, CoolingDown):
            status, reason = "cooldown", "cooldown"
            log.info("%-11s %-14s not sent: %s", "agent", "turn", e)
            self._record("agent", "cooldown", ms=_ms(t0), result=str(e), **rec)
        elif isinstance(e, asyncio.TimeoutError):
            status, reason = "timeout", "timeout"
            log.warning("%-11s %-14s TIMEOUT after %.0fms -> the game re-asks if it is waiting", "agent", "turn", _ms(t0))
            self._record("agent", "timeout", ms=_ms(t0), **rec)
        else:
            status, reason = "error", type(e).__name__
            log.error("%-11s %-14s failed: %s", "agent", "turn", error_line(e), exc_info=e)
            self._record("agent", "error", ms=_ms(t0), **error_fields(e), llm=getattr(e, "call", None) or call, **rec)
        a[status] += 1
        for key, p in pending.items():
            self.stats[p["kind"]][status] += 1
            res = {"t": "res", "session": self.session, "id": p["rid"], "key": key, "kind": p["kind"], "ok": False,
                   "reason": reason}
            if isinstance(e, CoolingDown):
                res["retry_ms"] = int(e.retry_in * 1000)
            await self._send_quietly(send, res)

    async def _keep(self, kind: str, key: str, rid: int, prev: dict, why: str, turn_seq: int, ms: int,
                    send: Send) -> None:
        """The same deck answers request `rid`: what is left of it, and how many draws that already accounts for."""
        self.memory.reissue(key, rid)
        seq = self.receipt_seq.get(key, self.used_seq.get(key))
        if seq is not None:
            self.answer_seq[(key, rid)] = seq
        self.stats[kind]["reused"] += 1
        self.stats["agent"]["keep"] += 1
        self._record("agent", "kept", key=key, req_id=rid, turn=turn_seq, result=why)
        log.info("%-11s %-14s kept by the agent (%s)", kind, key, why or "no reason given")
        await send({"t": "res", "session": self.session, "id": rid, "key": key, "kind": kind, "ok": True, "ms": ms,
                    "decision": prev, "drawn_base": self.memory.drawn_total.get(key, 0)})

    async def _agent_call(self, c: dict, pending: dict[str, dict], open_keys: set[str], answered: set[str],
                          state: dict, digest: dict, campaign: str, turn_seq: int, ms: int, late: bool,
                          send: Send) -> str | None:
        """Apply one of the turn's tool calls, in order. None when it was applied, else why it wasn't."""
        tool, args = str(c.get("tool")), dict(c.get("args") or {})
        key = str(args.pop("key", "") or "")
        if key in answered:
            return "this deck was already decided this turn"
        p = pending.get(key)
        if p is not None:
            kind, rid, st, ctx, asked_ctx, push = p["kind"], p["rid"], p["state"], p["ctx"], p["asked_ctx"], False
        elif key in open_keys and key in self.memory.issued and key in self.last_req:
            entry = self.memory.issued[key]
            kind, rid, st, push = entry["kind"], 0, state, True
            asked_ctx = {**self.last_req[key]["ctx"], "reason": "restacked by the agent"}
            if kind in DECK_FIELD:                # a restack replaces what is left: as many entries (drops: a full deck)
                depth = {"drops": AGENT_DROPS_DEPTH, "rocks": AGENT_ROCKS_DEPTH}.get(kind, 0)
                asked_ctx.update(wanted=max(1, len(entry["left"]), depth),
                                 deck_left=list(entry["left"]))
            ctx, _kills_left = self._request_ctx(kind, key, st, asked_ctx)
        else:
            return "not a pending request or an open deck"
        a = self.stats["agent"]
        if tool == KEEP:
            if push:
                return "keep_deck is for pending requests (an open deck is kept by leaving it alone)"
            prev = self.memory.previous(key)
            if not prev:
                if p["had_deck"] and self.memory.drawn_total.get(key, 0) > p["drawn_at_turn"]:
                    # Used up while the turn ran: the keep was right, there is just nothing left to send. The
                    # game re-asks if it still needs the key.
                    a["keep"] += 1
                    self._record("agent", "kept", key=key, req_id=rid, turn=turn_seq,
                                 result="used up while the turn ran: " + str(args.get("why") or ""))
                    log.info("%-11s %-14s kept by the agent, already used up", kind, key)
                    await self._send_quietly(send, {"t": "res", "session": self.session, "id": rid, "key": key,
                                                    "kind": kind, "ok": False, "reason": "used_up"})
                    answered.add(key)
                    return None
                return "there is no deck to keep"
            await self._keep(kind, key, rid, prev, str(args.get("why") or ""), turn_seq, ms, send)
            answered.add(key)
            return None
        if tool == SHUFFLE:
            prev, field = self.memory.previous(key), DECK_FIELD.get(kind)
            if not prev or not field or not prev.get(field):
                return f"a {kind} deck has nothing to shuffle"
            raw = _compact(prev)
            items = list(raw.get(field) or [])
            raw[field] = [items[i] for i in shuffle_order(args.get("order"), len(items))]
            if isinstance(args.get("taunt"), str):
                raw["taunt"] = args["taunt"]
            action = "shuffle"
        elif stack_kind(tool) == kind:
            raw, action = args, "stack"
        elif stack_kind(tool):
            return f"{tool} can't write {key}, a {kind} deck"
        else:
            return f"unknown tool {tool!r}"
        try:
            decision, budget = self._clamp(kind, key, raw, st, ctx)
        except Exception as e:                    # noqa: BLE001 - one bad call doesn't sink the turn
            return f"its answer couldn't be made legal ({type(e).__name__}: {e})"
        prev = self.memory.previous(key)
        if prev and _same_deck(prev, decision):
            # Restacked to what it already holds: a keep, so the game isn't sent the same deck again.
            a["unchanged"] += 1
            if push:
                self._record("agent", "unchanged", key=key, turn=turn_seq, result=f"{tool}: the same deck")
                log.info("%-11s %-14s restacked to the same deck: left as it is", kind, key)
            else:
                await self._keep(kind, key, rid, prev, f"{tool}: the same deck", turn_seq, ms, send)
            answered.add(key)
            return None
        a[action] += 1
        if push:
            self.push_id -= 1
            rid = self.push_id
            a["pushed"] += 1
        await self._issue(kind, key, rid, decision, budget, send, state=st, ctx=ctx, asked_ctx=asked_ctx,
                          digest=digest, rec={"key": key, "req_id": rid, "request": {"state": st, "ctx": ctx}},
                          raw=raw, call=None, usage=None, ms=ms, late=late, campaign=campaign, push=push, turn=turn_seq)
        answered.add(key)
        return None


_DECK_TEXT = ("intent", "taunt", "scheme", "guardrails")


def _same_deck(prev: dict, decision: dict) -> bool:
    """Two answers that do the same thing in the game (their notes and taunt aside)."""
    def body(d: dict) -> dict:
        return {k: v for k, v in _compact(d).items() if k not in _DECK_TEXT}
    return body(prev) == body(decision)


# What an open deck's stored ctx shows the agent: not the live deck (`left` has that), nor the economy (the turn's).
_OPEN_CTX_HIDDEN = ("deck_left", "drawn_so_far", "economy", "your_previous_answer", "owed", "reason")


_DIFF_FIELDS = (("resources", "coins"), ("resources", "bombs"), ("resources", "keys"), ("hp", "red"),
                ("hp", "max_red"), ("hp", "soul"))


def state_diff(old: dict, new: dict) -> str:
    """How the player moved between two snapshots, only what changed: "coins 0->4, hp.red 6->3, +Libra (q1)"."""
    po, pn = old.get("player") or {}, new.get("player") or {}
    bits = []
    for group, field in _DIFF_FIELDS:
        a, b = (po.get(group) or {}).get(field), (pn.get(group) or {}).get(field)
        if a is not None and b is not None and a != b:
            bits.append(f"{field if group == 'resources' else group + '.' + field} {a}->{b}")
    items_o, items_n = po.get("items"), pn.get("items")
    if isinstance(items_o, list) and isinstance(items_n, list):
        bits += [f"+{i}" for i in items_n if i not in items_o] + [f"-{i}" for i in items_o if i not in items_n]
    return ", ".join(bits)


def _turn_ctx(ctx: dict) -> dict:
    """A pending request's ctx in a turn: the economy is the turn's, once; its kill drops note stays."""
    out = {k: v for k, v in ctx.items() if k != "economy"}
    kill_drops = (ctx.get("economy") or {}).get("kill_drops")
    if kill_drops:
        out["kill_drops"] = kill_drops
    return out


async def _close_later(models: list, after_s: float = 60.0) -> None:
    """Close replaced models' HTTP clients once the calls they still had running are done."""
    await asyncio.sleep(after_s)
    for m in models:
        if hasattr(m.client, "aclose"):
            try:
                await m.client.aclose()
            except Exception:                     # noqa: BLE001 - only tidying up
                pass


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# The roll audit's owner for a pickup the game spawned again as the player walked back into its room: no roll.
RESTORED = "restored"


def coverage_table(coverage: Counter) -> list[tuple[str, str, int, int]]:
    """The roll audit per hook and cause: (hook, cause, controlled, uncontrolled), most uncontrolled first.
    Restored pickups aren't rolls and are left out (restored_count has them)."""
    by: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for (hook, cause, owner), n in coverage.items():
        if owner == RESTORED:
            continue
        by[(hook, cause)]["uncontrolled" if owner == "uncontrolled" else "controlled"] += n
    rows = [(h, c, n["controlled"], n["uncontrolled"]) for (h, c), n in by.items()]
    return sorted(rows, key=lambda r: (-r[3], -r[2], r[0], r[1]))


def coverage_lines(coverage: Counter) -> list[str]:
    """The roll audit as log lines (Coordinator.summary)."""
    rows = coverage_table(coverage)
    if not rows:
        return []
    out = [f"rolls {'hook/cause':<34} {'controlled':>10} {'uncontrolled':>12}"]
    out += [f"      {h + '/' + c:<34} {ctl:>10} {free:>12}" for h, c, ctl, free in rows]
    restored = restored_count(coverage)
    if restored:
        out.append(f"      {'(not rolls) restored on re-entry':<34} {restored:>10}")
    return out


def restored_count(coverage: Counter) -> int:
    """Pickups the game spawned again as the player walked back into their room."""
    return sum(n for (_h, _c, owner), n in coverage.items() if owner == RESTORED)


def _summary_msg(session: str | None, summary: dict) -> dict:
    """The stats screen for the game; empty fields left out (the mod's json has no null)."""
    return {"t": "run_summary", "session": session, **{k: v for k, v in summary.items() if v is not None}}


def _compact(decision: dict) -> dict:
    """An issued decision as the model should see it again: its own fields, no engine detail."""
    out = {k: v for k, v in decision.items() if k not in ("guardrails", "champion_enum")}
    if out.get("extra_enemies"):
        out["extra_enemies"] = [{"enemy": e["key"], "count": e["count"]} for e in out["extra_enemies"]]
    if "cards" in out:
        out["cards"] = [c["key"] for c in out["cards"]]
    if "pills" in out:
        out["pills"] = [{"color": p["color"], "effect": p["effect"]} for p in out["pills"]]
    return out


@dataclass
class Engine:
    """The models behind the DM: what build_dm makes and Coordinator.reconfigure swaps in."""
    dm: object
    planner: object = None
    gate: object = None
    concurrency: int = 6
    late_grace: float = 0.0
    label: str = ""
    agent: object = None          # agent mode: writes every deck (the DM only writes the epitaph)
    cruelty: int = 6              # 0 (generous) .. 10 (merciless)

    def describe(self) -> None:
        log.info("Dungeon Master: %s%s", self.label, "  [agent mode: one call stacks every deck it needs]"
                 if self.agent is not None else "")
        if self.gate:
            log.info("Re-ask gate: %s", self.gate.model)
        else:
            log.info("Re-ask gate: off, every re-ask goes to the DM")
        if self.planner:
            log.info("Planners: narrative=%s  floor brief=%s", self.planner.narrative_model, self.planner.floor_model)


def build_dm(backend: str, api_key: str, *, cruelty: int, model: str | None = None,
             narrative_model: str | None = None, floor_model: str | None = None, planner_backend: str | None = None,
             gate_model: str = GATE_DEFAULT_MODEL, gate_threshold: float = GATE_THRESHOLD, no_gate: bool = False,
             no_planners: bool = False, claude_bin: str | None = None, google_key: str = "",
             mock_latency: tuple[float, float] = (0.25, 1.2), agent: bool = False) -> Engine:
    """The DM, planners and gate for a backend: "openrouter", "google", "claude-code" or "mock". The planners can run
    on another backend than the DM (planner_backend, default the DM's). agent: agent mode, an agent on the DM's
    backend and model writes the decks (the gate still screens its re-asks).
    ValueError, with a message for the player, when it can't be set up."""
    if backend == "mock":
        return Engine(MockDM(cruelty, latency=mock_latency), None if no_planners else MockPlanner(),
                      None if no_gate else MockGate(), label="MOCK (offline)",
                      agent=MockAgent(cruelty, latency=mock_latency) if agent else None, cruelty=cruelty)
    planner_backend = planner_backend or backend
    used = (backend, None if no_planners else planner_backend)
    if not api_key and "openrouter" in used:
        raise ValueError("No OpenRouter API key: add one, or use Claude Code or Google AI Studio instead.")
    if not google_key and "google" in used:
        raise ValueError("No Google AI Studio API key: add one in Settings.")
    try:
        dm, model = _make_dm(backend, model, cruelty, api_key, claude_bin, google_key)
        deck_agent = _make_agent(backend, model, cruelty, api_key, claude_bin, google_key) if agent else None
        if deck_agent is not None and hasattr(dm, "cooldown"):
            deck_agent.cooldown = dm.cooldown     # one model, one rate limit
        planner = None if no_planners else _make_planner(planner_backend, narrative_model, floor_model, api_key,
                                                         claude_bin, google_key)
    except SystemExit as e:                       # find_claude's message, for the CLI
        raise ValueError(str(e)) from None
    # The gate isn't a Claude model: it stays on OpenRouter, when there's a key for it.
    gate = OpenRouterGate(gate_model, gate_threshold, api_key=api_key) if api_key and not no_gate else None
    if backend == "claude-code":
        return Engine(dm, planner, gate, CLAUDE_CODE_CONCURRENCY, CLAUDE_CODE_LATE_GRACE_S,
                      f"{model} via Claude Code ({dm.runner.bin})", agent=deck_agent, cruelty=cruelty)
    return Engine(dm, planner, gate, label=f"{model} via {'Google AI Studio' if backend == 'google' else 'OpenRouter'}",
                  agent=deck_agent, cruelty=cruelty)


def _make_dm(backend: str, model: str | None, cruelty: int, api_key: str, claude_bin: str | None,
             google_key: str = ""):
    if backend == "claude-code":
        model = model or CLAUDE_CODE_DM_MODEL
        return ClaudeCodeDM(cruelty, model=model, claude_bin=claude_bin), model
    if backend == "google":
        model = model or GOOGLE_DEFAULT_MODEL
        return OpenRouterDM(cruelty, model=model, api_key=google_key, google=True), model
    model = model or OPENROUTER_DEFAULT_MODEL
    return OpenRouterDM(cruelty, model=model, api_key=api_key), model


def _make_agent(backend: str, model: str, cruelty: int, api_key: str, claude_bin: str | None, google_key: str = ""):
    if backend == "claude-code":
        return ClaudeCodeAgent(cruelty, model=model, claude_bin=claude_bin)
    if backend == "google":
        return OpenRouterAgent(cruelty, model=model, api_key=google_key, google=True)
    return OpenRouterAgent(cruelty, model=model, api_key=api_key)


def _make_planner(backend: str, narrative_model: str | None, floor_model: str | None, api_key: str,
                  claude_bin: str | None, google_key: str = ""):
    if backend == "claude-code":
        return ClaudeCodePlanner(narrative_model or CLAUDE_CODE_PLANNER_MODEL,
                                 floor_model or CLAUDE_CODE_PLANNER_MODEL, claude_bin=claude_bin)
    if backend == "google":
        return OpenRouterPlanner(narrative_model or GOOGLE_DEFAULT_MODEL, floor_model or GOOGLE_DEFAULT_MODEL,
                                 api_key=google_key, google=True)
    return OpenRouterPlanner(narrative_model or NARRATIVE_DEFAULT_MODEL, floor_model or FLOOR_DEFAULT_MODEL,
                             api_key=api_key)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--isaac-dir", type=Path, action="append",
                    help="the game's install folder, if it isn't found in your Steam libraries (repeatable)")
    ap.add_argument("--log", type=Path, action="append",
                    help="the game's log.txt, if it isn't in Documents/My Games/Binding of Isaac ... (repeatable)")
    ap.add_argument("--backend", choices=["openrouter", "google", "claude-code"],
                    default=os.environ.get("LLMDM_BACKEND", "openrouter"),
                    help="where the Claude calls go: OpenRouter, or your local Claude Code in headless mode")
    ap.add_argument("--claude-bin", default=None,
                    help="Claude Code executable for --backend claude-code (default: claude on PATH)")
    ap.add_argument("--model", default=os.environ.get("LLMDM_MODEL"),
                    help=f"DM model (default {OPENROUTER_DEFAULT_MODEL} on OpenRouter, {CLAUDE_CODE_DM_MODEL} on Claude Code)")
    ap.add_argument("--narrative-model", default=os.environ.get("LLMDM_NARRATIVE_MODEL"),
                    help="model that writes the run narrative (once per run)")
    ap.add_argument("--floor-model", default=os.environ.get("LLMDM_FLOOR_MODEL"),
                    help="model that writes the floor brief (once per floor)")
    ap.add_argument("--gate-model", default=os.environ.get("LLMDM_GATE_MODEL", GATE_DEFAULT_MODEL),
                    help="small OpenRouter decisions model that decides whether a re-asked decision needs a new DM call")
    ap.add_argument("--gate-threshold", type=float, default=GATE_THRESHOLD,
                    help="re-ask the DM when the gate's P(new answer needed) is at least this (lower = more re-asks)")
    ap.add_argument("--no-gate", action="store_true", help="always re-ask the DM (no fast gate)")
    ap.add_argument("--no-planners", action="store_true", help="no narrative / floor brief: the DM plays solo")
    ap.add_argument("--agent", action="store_true", default=os.environ.get("LLMDM_AGENT") == "1",
                    help="agent mode: each trigger is one call that stacks, shuffles or keeps every deck it needs to")
    ap.add_argument("--cruelty", type=int, default=6, help="0 (generous) .. 10 (merciless)")
    ap.add_argument("--mock", action="store_true", help="use the offline rule-based DM")
    ap.add_argument("--mock-latency", type=float, nargs=2, default=(0.25, 1.2), metavar=("MIN", "MAX"),
                    help="fake response time range for --mock, seconds")
    ap.add_argument("--receipts", type=Path, default=appdata.receipts_dir(),
                    help="folder for the per-run receipt (every decision and LLM call, as JSON lines)")
    ap.add_argument("--no-receipts", action="store_true", help="keep the receipt in memory only")
    ap.add_argument("--no-epitaph", action="store_true", help="no DM-written epitaph on the after-death stats screen")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        engine = build_dm("mock" if args.mock else args.backend, os.environ.get("OPENROUTER_API_KEY", ""),
                          google_key=os.environ.get("GOOGLE_API_KEY", ""), cruelty=args.cruelty, model=args.model, narrative_model=args.narrative_model,
                          floor_model=args.floor_model, gate_model=args.gate_model,
                          gate_threshold=args.gate_threshold, no_gate=args.no_gate, no_planners=args.no_planners,
                          claude_bin=args.claude_bin, mock_latency=tuple(args.mock_latency), agent=args.agent)
    except ValueError as e:
        raise SystemExit(f"{e} Set OPENROUTER_API_KEY, or run with --backend claude-code or --mock.")
    engine.describe()

    coord = Coordinator(engine.dm, args.cruelty, engine.planner, max_concurrency=engine.concurrency,
                        receipt=Receipt(None if args.no_receipts else args.receipts),
                        epitaph=not args.no_epitaph, gate=engine.gate, late_grace_s=engine.late_grace,
                        agent=engine.agent)

    isaac_dirs = args.isaac_dir or bridge.isaac_installs()
    logs = args.log or bridge.default_logs()
    if not isaac_dirs:
        log.warning("Isaac isn't installed in any Steam library found here: pass --isaac-dir.")
    if not logs:
        raise SystemExit("No Documents folder found for the game's log.txt: pass --log.")
    log.info("game: %s", ", ".join(map(str, isaac_dirs)) or "?")

    try:
        asyncio.run(bridge.serve(coord.handle, logs, isaac_dirs))
    except KeyboardInterrupt:
        coord.summary()


if __name__ == "__main__":
    main()
