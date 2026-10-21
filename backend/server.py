"""LLM Dungeon Master backend.

Talks to the Isaac mod through files the game already writes, so the mod runs in Isaac's normal
sandbox: the mod's messages come in through the game's log.txt, ours go out through the mod's save
file (see bridge.py). There's nothing to set up in the game: this finds the game's files.

Run:  python server.py                 # Haiku 5.5 DM + Opus narrative + Sonnet floor briefs
      python server.py --no-planners  # Haiku alone
      python server.py --mock          # no API key needed
      python server.py --backend claude-code   # same DM through your local Claude Code (claude -p)
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
                         CLAUDE_CODE_PLANNER_MODEL, ClaudeCodeDM, ClaudeCodePlanner)
from dm_agent import OPENROUTER_DEFAULT_MODEL, MockDM, OpenRouterDM
from gate import GATE_DEFAULT_MODEL, GATE_THRESHOLD, MockGate, OpenRouterGate, diff, new_events
from memory import DECK_FIELD, RunMemory, kind_of
from planners import (FLOOR_DEFAULT_MODEL, NARRATIVE_DEFAULT_MODEL, NARRATIVE_WAIT_S, Campaign, MockPlanner, OpenRouterPlanner,
                      floor_signature, parse_economy)
from receipt import Receipt
from schemas import KINDS, clamp

log = logging.getLogger("llmdm")
Send = Callable[[dict], Awaitable[None]]

MAX_DEADLINE_S = 12.0      # never let a single decision hang longer than this (plus any late grace)
MIN_DEADLINE_S = 0.4
EPITAPH_TIMEOUT_S = 20.0   # the stats screen shows without it; it only fills in the last line

# Who gets the next free DM slot. A floor's start sends a burst of item rankings and card/trinket/pill
# decks; the room the player is about to walk into (or is about to clear) must not queue behind it, and a
# deck the game is frozen on (ctx.owed) goes first of all.
URGENT, NEXT_DOOR, BACKGROUND = 0, 1, 2


def _priority(kind: str, ctx: dict) -> int:
    if kind == "room_clear" or ctx.get("owed"):
        return URGENT
    if kind in ("room_layout", "payout_plan", "drops"):
        return NEXT_DOOR
    if kind == "item_choice" and ctx.get("reason") != "floor start":
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
                 receipt: Receipt | None = None, epitaph: bool = True, gate=None, late_grace_s: float = 0.0) -> None:
        self.dm = dm
        self.late_grace_s = late_grace_s          # how long past its deadline a call may still be delivered
        self.gate = gate                          # fast model that vetoes re-asks the DM would answer the same
        self.last_req: dict[str, dict] = {}       # key -> what the issued answer was written against
        self.epitaph_wanted = epitaph
        self.epitaph = epitaph and hasattr(dm, "epitaph")
        self.cruelty = self.default_cruelty = cruelty
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
        self.campaign = Campaign(planner, getattr(dm, "set_campaign", lambda _text: None), self.stats, self.usage,
                                 self._record, on_brief=self._brief_written)
        self.memory.economy.cruelty = cruelty

    def summary(self) -> None:
        """Log how many answers were requested, paid for, and actually used, per kind."""
        if not self.stats:
            return
        cols = ("req", "ok", "late", "applied", "replaced", "reused", "superseded", "timeout", "error", "fallback")
        log.info("summary %-11s %s", "kind", " ".join(f"{c:>10}" for c in cols))
        for kind in sorted(k for k in self.stats if k != "gate"):
            log.info("        %-11s %s", kind, " ".join(f"{self.stats[kind][c]:>10}" for c in cols))
        g = self.stats.get("gate")
        if g:
            log.info("        gate        keep=%d (DM call saved) reask=%d error=%d", g["keep"], g["reask"], g["error"])
        u = self.usage
        if u["calls"]:
            log.info("        tokens in=%d (cached %d, %.0f%%) out=%d  cost=$%.4f over %d calls",
                     u["in"], u["cached"], 100 * u["cached"] / max(1, u["in"]), u["out"], u["cost"], u["calls"])
        for line in coverage_lines(self.coverage):
            log.info("        %s", line)

    def reconfigure(self, engine: "Engine") -> None:
        """Swap in other models mid-run (the app's Settings). The run, its receipt, memory and plans carry
        on; calls already in flight finish on the old models."""
        old = (self.dm, self.campaign.planner, self.gate)
        self.dm, self.gate, self.late_grace_s = engine.dm, engine.gate, engine.late_grace
        self.epitaph = self.epitaph_wanted and hasattr(engine.dm, "epitaph")
        self.sem = PrioritySemaphore(engine.concurrency)
        self.dm.set_cruelty(self.cruelty)
        self.campaign.planner = engine.planner
        self.campaign.push = getattr(engine.dm, "set_campaign", lambda _text: None)
        self.campaign._push()
        replaced = [o for o in old if hasattr(o, "client") and o not in (self.dm, engine.planner, self.gate)]
        if replaced:
            task = asyncio.create_task(_close_later(replaced))
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)
        engine.describe()

    async def handle(self, msg: dict, send: Send) -> None:
        self.last_seen = time.monotonic()
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
            self._set_cruelty(msg.get("cruelty"))
            log.info("game connected  session=%s run=%s cruelty=%s", self.session, msg.get("run"), self.cruelty)
            await send({"t": "welcome", "session": self.session, "proto": bridge.PROTO, "cruelty": self.cruelty})
        elif t == "settings":
            # Changed from the in-game DM Settings menu; takes effect for the next decision.
            self._set_cruelty(msg.get("cruelty"))
            await send({"t": "settings_ok", "session": self.session, "cruelty": self.cruelty})
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
            await self._run_over(str(msg["name"]), msg.get("data") or {}, send)
        elif t == "event":
            self.memory.on_game_event(str(msg.get("name")), msg.get("data") or {})
        elif t == "ping":
            await send({"t": "pong", "session": self.session, "id": msg.get("id")})

    def _set_cruelty(self, value) -> None:
        """None (the game's 'backend default') means the --cruelty this server was started with."""
        try:
            cruelty = self.default_cruelty if value is None else max(0, min(10, int(value)))
        except (TypeError, ValueError):
            log.warning("ignoring bad cruelty %r", value)
            return
        if cruelty != self.cruelty:
            log.info("cruelty %d -> %d", self.cruelty, cruelty)
            self._record("cruelty", "set", result=f"{self.cruelty} -> {cruelty}")
            self.cruelty = cruelty
            self.dm.set_cruelty(cruelty)
        self.memory.economy.cruelty = self.cruelty

    # ------------------------------------------------------------------ decks
    def _observe(self, state: dict) -> None:
        """Every snapshot the game sends: a new floor starts its economy ledger (synchronously, before any
        of its draws can come in), then the planners, whose floor brief sees last floor against vanilla."""
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
            if keys:
                await self._send_quietly(send, {"t": "stale", "session": self.session, "keys": keys,
                                                "why": "/".join(overs) + " over budget"})

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
            samples = data.get("samples") if isinstance(data.get("samples"), list) else None
            self._record("rolls", "tally", result=f"{sum(r[3] for r in rows) - free} controlled, {free} uncontrolled",
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
            async with self.sem:
                text, call = await asyncio.wait_for(self.dm.epitaph(epilogue.epitaph_prompt(summary)),
                                                    EPITAPH_TIMEOUT_S)
            summary["epitaph"] = epilogue.clean_epitaph(text)
            if not summary["epitaph"]:
                raise ValueError("epitaph was empty after cleaning")
        except Exception as e:                    # noqa: BLE001 - the screen just goes without
            self.stats["epitaph"]["error"] += 1
            log.warning("epitaph     failed (%s: %s)", type(e).__name__, e)
            self._record("epitaph", "error", ms=_ms(t0), error=f"{type(e).__name__}: {e}",
                         llm=getattr(e, "call", None) or call)
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

    async def _run(self, msg: dict, send: Send) -> None:
        key, rid, kind = str(msg["key"]), int(msg["id"]), msg["kind"]
        state, ctx = msg.get("state") or {}, msg.get("ctx") or {}
        # Never answer against a brief that is about to replace itself: wait for this floor's plan.
        # Superseding cancels this wait too, so it costs nothing. Like the slot queue, not the DM's time.
        tb = time.perf_counter()
        try:                                      # the brief waits for it too, but not if the floor has no signature
            narrative = await asyncio.wait_for(self.campaign.narrative_waiter(), NARRATIVE_WAIT_S)
        except asyncio.TimeoutError:
            narrative = "timeout"
        brief = narrative if narrative == "cancelled" else await self.campaign.brief_waiter(floor_signature(state))
        waited = _ms(tb)
        if waited >= 50:
            log.info("%-11s %-14s waited %dms for the floor brief (%s)", kind, key, waited, brief)
        if brief in ("cancelled", "moved"):
            # A new run (or floor) replaced what this was asked against: answering would be written with
            # no campaign, and the game re-asks under the new one.
            self._record(kind, "superseded", ms=waited, key=key, req_id=rid, reason=brief)
            self.stats[kind]["superseded"] += 1
            return
        prev = self.memory.previous(key)
        asked_ctx = ctx                           # before the previous answer is mixed in: this is what the gate diffs
        if prev:                                  # a re-ask replaces this answer; show it so the model restates it
            ctx = {**ctx, "your_previous_answer": _compact(prev)}
        ctx = {**ctx, "economy": self.memory.economy.view(key)}
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
            if prev and not _deck_short(kind, prev, asked_ctx) \
                    and await self._gate_keeps(kind, key, state, asked_ctx, digest, prev, t0):
                # The same deck answers this request too: what is left of it, and how many draws that
                # already accounts for (the mod skips only draws made since).
                self.memory.reissue(key, rid)
                seq = self.receipt_seq.get(key, self.used_seq.get(key))
                if seq is not None:
                    self.answer_seq[(key, rid)] = seq
                await send({**base, "ok": True, "ms": _ms(t0), "decision": prev,
                            "drawn_base": self.memory.drawn_total.get(key, 0)})
                return
            # the deadline counts from here: queueing for a slot isn't the DM's time
            async with self.sem.slot(_priority(kind, ctx)):
                campaign = getattr(self.dm, "campaign", "")   # what this answer is written against
                raw, late = await self._decide(kind, key, state, ctx, digest, deadline, t0)
            usage = raw.pop("_usage", None)
            call = raw.pop("_call", None)
            # Trimmed against the economy as it is now, and committed in the same step below: no other
            # answer can land in between, so the first to finish takes the last coin.
            budget = self.memory.economy.budget(key, kind, ctx)
            decision = clamp(kind, raw, state, ctx, self.cruelty, self.memory.counters, budget)
        except asyncio.CancelledError:
            # Superseded; the newer request will answer. A call already in flight may still be billed.
            self._record(kind, "superseded", ms=_ms(t0), **rec)
            return
        except asyncio.TimeoutError:
            self.stats[kind]["timeout"] += 1
            log.warning("%-11s %-14s TIMEOUT after %.0fms -> the game re-asks if it is waiting", kind, key, _ms(t0))
            self._record(kind, "timeout", ms=_ms(t0), **rec)
            await send({**base, "ok": False, "reason": "timeout"})
            return
        except Exception as e:                    # noqa: BLE001 - never crash the bridge
            self.stats[kind]["error"] += 1
            log.exception("%s %s failed: %s", kind, key, e)
            self._record(kind, "error", ms=_ms(t0), error=f"{type(e).__name__}: {e}",
                         llm=getattr(e, "call", None) or call, raw=raw or None, **rec)
            await send({**base, "ok": False, "reason": type(e).__name__})
            return
        ms = _ms(t0)
        self.stats[kind]["ok"] += 1
        if late:
            self.stats[kind]["late"] += 1
        old_seq = self.receipt_seq.get(key)
        if self.memory.note_issued(key, kind, decision, ctx, rid):
            self.stats[kind]["replaced"] += 1    # paid for an answer the game never used
            self.receipt.update(old_seq, "replaced")
        self.receipt_seq[key] = self.answer_seq[(key, rid)] = self._record(
            kind, "ok", ms=ms, llm=call, raw=raw, decision=decision, **({"late": True} if late else {}), **rec)
        self.last_req[key] = {"state": state, "ctx": asked_ctx, "events": digest["recent_events"],
                              "campaign": campaign}
        tok = ""
        if usage:
            self.usage.update({**usage, "calls": 1})
            tok = f'  [{usage["in"]}/{usage["cached"]}c/{usage["out"]} tok]'
        log.info("%-11s %-14s %5dms%s%s  %s%s", kind, key, ms, " LATE" if late else "", tok, decision.get("intent", ""),
                 f'  "{decision["taunt"]}"' if decision.get("taunt") else "")
        if sum(c["ok"] for c in self.stats.values()) % 25 == 0:
            self.summary()
        if decision.get("guardrails"):
            log.info("%28s guardrails: %s", "", "; ".join(decision["guardrails"]))
        await send({**base, "ok": True, "ms": ms, "decision": decision})
        if budget.trimmed and key not in self.trim_stale:
            # Lost a race for the budget: its deck may now be a worse story. One re-ask, which sees the
            # winner in your_open_decks (and the gate drops it if nothing meaningful changed).
            self.trim_stale.add(key)
            await self._send_quietly(send, {"t": "stale", "session": self.session, "keys": [key],
                                            "why": "trimmed to the budget"})

    def _in_flight(self, key: str) -> list[tuple[str, str]]:
        """Decisions being made right now (no answer yet), other than `key`."""
        out = []
        for k, (_rid, task) in self.inflight.items():
            if k != key and not task.done():
                out.append((k, self.inflight_kind.get(k, "?")))
        return out

    async def _decide(self, kind: str, key: str, state: dict, ctx: dict, digest: dict, deadline: float,
                      t0: float) -> tuple[dict, bool]:
        """(raw answer, whether it came after the deadline). With a late grace, a call past its deadline
        keeps running and is delivered when it lands: the game holds the key as pending meanwhile (so it
        doesn't re-ask) and takes a late result whenever it arrives. Without one, the deadline is final."""
        call = asyncio.ensure_future(self.dm.decide(kind, state, ctx, digest))
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(call), deadline), False
            except asyncio.TimeoutError:
                if not self.late_grace_s:
                    raise
            log.info("%-11s %-14s past its %.0fms deadline after %.0fms -> still waiting (up to %.0fs)",
                     kind, key, deadline * 1000, _ms(t0), self.late_grace_s)
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
        campaign_changed = last["campaign"] != getattr(self.dm, "campaign", "")
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
            log.warning("%-11s %-14s gate failed (%s: %s) -> re-ask", kind, key, type(e).__name__, e)
            self._record("gate", "error", ms=_ms(t0), error=f"{type(e).__name__}: {e}",
                         llm=getattr(e, "call", None), key=key)
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


def coverage_table(coverage: Counter) -> list[tuple[str, str, int, int]]:
    """The roll audit per hook and cause: (hook, cause, controlled, uncontrolled), most uncontrolled first."""
    by: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for (hook, cause, owner), n in coverage.items():
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
    return out


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

    def describe(self) -> None:
        log.info("Dungeon Master: %s", self.label)
        if self.gate:
            log.info("Re-ask gate: %s", self.gate.model)
        else:
            log.info("Re-ask gate: off, every re-ask goes to the DM")
        if self.planner:
            log.info("Planners: narrative=%s  floor brief=%s", self.planner.narrative_model, self.planner.floor_model)


def build_dm(backend: str, api_key: str, *, cruelty: int, model: str | None = None,
             narrative_model: str | None = None, floor_model: str | None = None, planner_backend: str | None = None,
             gate_model: str = GATE_DEFAULT_MODEL, gate_threshold: float = GATE_THRESHOLD, no_gate: bool = False,
             no_planners: bool = False, claude_bin: str | None = None,
             mock_latency: tuple[float, float] = (0.25, 1.2)) -> Engine:
    """The DM, planners and gate for a backend: "openrouter", "claude-code" or "mock". The planners can run
    on another backend than the DM (planner_backend, default the DM's).
    ValueError, with a message for the player, when it can't be set up."""
    if backend == "mock":
        return Engine(MockDM(cruelty, latency=mock_latency), None if no_planners else MockPlanner(),
                      None if no_gate else MockGate(), label="MOCK (offline)")
    planner_backend = planner_backend or backend
    if not api_key and "openrouter" in (backend, None if no_planners else planner_backend):
        raise ValueError("No OpenRouter API key: add one, or use Claude Code instead.")
    try:
        dm, model = _make_dm(backend, model, cruelty, api_key, claude_bin)
        planner = None if no_planners else _make_planner(planner_backend, narrative_model, floor_model, api_key,
                                                         claude_bin)
    except SystemExit as e:                       # find_claude's message, for the CLI
        raise ValueError(str(e)) from None
    # The gate isn't a Claude model: it stays on OpenRouter, when there's a key for it.
    gate = OpenRouterGate(gate_model, gate_threshold, api_key=api_key) if api_key and not no_gate else None
    if backend == "claude-code":
        return Engine(dm, planner, gate, CLAUDE_CODE_CONCURRENCY, CLAUDE_CODE_LATE_GRACE_S,
                      f"{model} via Claude Code ({dm.runner.bin})")
    return Engine(dm, planner, gate, label=f"{model} via OpenRouter")


def _make_dm(backend: str, model: str | None, cruelty: int, api_key: str, claude_bin: str | None):
    if backend == "claude-code":
        model = model or CLAUDE_CODE_DM_MODEL
        return ClaudeCodeDM(cruelty, model=model, claude_bin=claude_bin), model
    model = model or OPENROUTER_DEFAULT_MODEL
    return OpenRouterDM(cruelty, model=model, api_key=api_key), model


def _make_planner(backend: str, narrative_model: str | None, floor_model: str | None, api_key: str,
                  claude_bin: str | None):
    if backend == "claude-code":
        return ClaudeCodePlanner(narrative_model or CLAUDE_CODE_PLANNER_MODEL,
                                 floor_model or CLAUDE_CODE_PLANNER_MODEL, claude_bin=claude_bin)
    return OpenRouterPlanner(narrative_model or NARRATIVE_DEFAULT_MODEL, floor_model or FLOOR_DEFAULT_MODEL,
                             api_key=api_key)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--isaac-dir", type=Path, action="append",
                    help="the game's install folder, if it isn't found in your Steam libraries (repeatable)")
    ap.add_argument("--log", type=Path, action="append",
                    help="the game's log.txt, if it isn't in Documents/My Games/Binding of Isaac ... (repeatable)")
    ap.add_argument("--backend", choices=["openrouter", "claude-code"],
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
                          cruelty=args.cruelty, model=args.model, narrative_model=args.narrative_model,
                          floor_model=args.floor_model, gate_model=args.gate_model,
                          gate_threshold=args.gate_threshold, no_gate=args.no_gate, no_planners=args.no_planners,
                          claude_bin=args.claude_bin, mock_latency=tuple(args.mock_latency))
    except ValueError as e:
        raise SystemExit(f"{e} Set OPENROUTER_API_KEY, or run with --backend claude-code or --mock.")
    engine.describe()

    coord = Coordinator(engine.dm, args.cruelty, engine.planner, max_concurrency=engine.concurrency,
                        receipt=Receipt(None if args.no_receipts else args.receipts),
                        epitaph=not args.no_epitaph, gate=engine.gate, late_grace_s=engine.late_grace)

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
