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
from pathlib import Path
from typing import Awaitable, Callable

import bridge
import epilogue
from claude_code import (CLAUDE_CODE_CONCURRENCY, CLAUDE_CODE_DM_MODEL, CLAUDE_CODE_LATE_GRACE_S,
                         CLAUDE_CODE_PLANNER_MODEL, ClaudeCodeDM, ClaudeCodePlanner)
from dm_agent import OPENROUTER_DEFAULT_MODEL, MockDM, OpenRouterDM
from gate import GATE_DEFAULT_MODEL, GATE_THRESHOLD, MockGate, OpenRouterGate, diff, new_events
from memory import RunMemory
from planners import (FLOOR_DEFAULT_MODEL, NARRATIVE_DEFAULT_MODEL, Campaign, MockPlanner, OpenRouterPlanner,
                      floor_signature)
from receipt import Receipt
from schemas import KINDS, clamp

log = logging.getLogger("llmdm")
Send = Callable[[dict], Awaitable[None]]

MAX_DEADLINE_S = 12.0      # never let a single decision hang longer than this (plus any late grace)
MIN_DEADLINE_S = 0.4
EPITAPH_TIMEOUT_S = 20.0   # the stats screen shows without it; it only fills in the last line

# Who gets the next free DM slot. A floor's start sends a burst of item rankings and consumables;
# the room the player is about to walk into (or is about to clear) must not queue behind it.
URGENT, NEXT_DOOR, BACKGROUND = 0, 1, 2


def _priority(kind: str, ctx: dict) -> int:
    if kind == "room_clear":
        return URGENT
    if kind == "room_layout":
        return NEXT_DOOR
    if kind == "item_choice" and ctx.get("reason") != "floor start":
        return NEXT_DOOR
    return BACKGROUND


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
        self.epitaph = epitaph and hasattr(dm, "epitaph")
        self.cruelty = self.default_cruelty = cruelty
        self.memory = RunMemory()
        self.session: str | None = None
        self.mismatched = None                    # the last session whose hello had the wrong protocol
        self.send: Send | None = None             # the game's connection, for pushing receipt entries
        # Everything the engine does this run, with the LLM calls behind it (in memory only if no dir).
        self.receipt = receipt or Receipt()
        self.receipt_seq: dict[str, int] = {}     # key -> receipt entry of the answer the game holds
        self.bg: set[asyncio.Task] = set()
        self.inflight: dict[str, tuple[int, asyncio.Task]] = {}
        self.sem = PrioritySemaphore(max_concurrency)
        # Per decision kind: req, ok, late, superseded, timeout, error, replaced, applied, fallback.
        self.stats: dict[str, Counter] = defaultdict(Counter)
        self.usage = Counter()                    # in / cached / out tokens, cost, calls
        # Opus narrative once per run + Sonnet brief once per floor, fed to the DM's system prompt.
        self.campaign = Campaign(planner, getattr(dm, "set_campaign", lambda _text: None), self.stats, self.usage,
                                 self._record)

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

    async def handle(self, msg: dict, send: Send) -> None:
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
            self.session, self.send = str(msg.get("session")), send
            if msg.get("run") != self.memory.run_id or msg.get("new_run"):
                self.memory.reset(msg.get("run"))
                self.campaign.reset()
                self.receipt.start_run(msg.get("run"), fresh=bool(msg.get("new_run")))
                self.receipt_seq.clear()
                self.last_req.clear()
                if self.receipt.path:
                    log.info("receipt -> %s", self.receipt.path)
            self._set_cruelty(msg.get("cruelty"))
            log.info("game connected  session=%s run=%s cruelty=%s", self.session, msg.get("run"), self.cruelty)
            await send({"t": "welcome", "session": self.session, "proto": bridge.PROTO, "cruelty": self.cruelty})
            await send({"t": "receipt_sync", "session": self.session, **self.receipt.snapshot()})
        elif t == "settings":
            # Changed from the in-game DM Settings menu; takes effect for the next decision.
            self._set_cruelty(msg.get("cruelty"))
            await send({"t": "settings_ok", "session": self.session, "cruelty": self.cruelty})
        elif t == "prepare":
            self._prepare(msg, send)
        elif t == "req":
            self._start(msg, send)
        elif t == "applied":
            entry = self.memory.issued.get(str(msg.get("key")))
            if entry:
                self.stats[entry["kind"]]["applied"] += 1
            self.memory.on_applied(str(msg.get("key")))
            self._push_receipt(self.receipt.update(self.receipt_seq.pop(str(msg.get("key")), None), "applied"))
        elif t == "event" and msg.get("name") == "fallback":
            # The game needed a decision and had none: vanilla RNG decided. Not news for the DM.
            data = msg.get("data") or {}
            self.stats[str(data.get("kind"))]["fallback"] += 1
            why = "no answer in time" if data.get("asked") else "never asked: prefetch fired too late"
            log.info("%-11s %-14s %s -> vanilla", data.get("kind"), data.get("key"), why)
            self._record(str(data.get("kind")), "fallback", key=data.get("key"), result=f"vanilla RNG ({why})")
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

    # ------------------------------------------------------------------ receipt
    def _record(self, what: str, status: str, **fields) -> int:
        """Add an entry to the receipt and show it in the game's menu. Returns its seq."""
        entry = self.receipt.add(what, status, session=self.session, **fields)
        self._push_receipt(entry)
        return entry["seq"]

    def _push_receipt(self, entry: dict | None) -> None:
        if entry is None or self.send is None:
            return
        # A copy: the receipt updates its entries in place (applied / replaced) before this is sent.
        msg = {"t": "receipt", "session": self.session, "entry": dict(entry), "totals": self.receipt.totals()}
        task = asyncio.create_task(self._send_quietly(self.send, msg))
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

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
        self.campaign.observe(state, self.memory.digest(), self.cruelty)
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
        self.campaign.observe(msg.get("state") or {}, self.memory.digest(), self.cruelty)
        old = self.inflight.get(key)
        if old and old[0] < rid and not old[1].done():
            old[1].cancel()                      # latest-wins: newer context supersedes
            self.stats[kind]["superseded"] += 1
            log.debug("%-11s %-14s superseded by a fresher request", kind, key)
        task = asyncio.create_task(self._run(msg, send))
        self.inflight[key] = (rid, task)
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
        brief = await self.campaign.brief_waiter(floor_signature(state))
        waited = _ms(tb)
        if waited >= 50:
            log.info("%-11s %-14s waited %dms for the floor brief (%s)", kind, key, waited, brief)
        prev = self.memory.previous(key)
        asked_ctx = ctx                           # before the previous answer is mixed in: this is what the gate diffs
        if prev:                                  # a re-ask replaces this answer; show it so the model restates it
            ctx = {**ctx, "your_previous_answer": _compact(prev)}
        deadline = max(MIN_DEADLINE_S, min(MAX_DEADLINE_S, float(msg.get("deadline_ms", 4000)) / 1000))
        digest = self.memory.digest()
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
            if prev and await self._gate_keeps(kind, key, state, asked_ctx, digest, prev, t0):
                await send({**base, "ok": True, "ms": _ms(t0), "decision": prev})
                return
            # the deadline counts from here: queueing for a slot isn't the DM's time
            async with self.sem.slot(_priority(kind, ctx)):
                campaign = getattr(self.dm, "campaign", "")   # what this answer is written against
                raw, late = await self._decide(kind, key, state, ctx, digest, deadline, t0)
            usage = raw.pop("_usage", None)
            call = raw.pop("_call", None)
            decision = clamp(kind, raw, state, ctx, self.cruelty, self.memory.counters)
        except asyncio.CancelledError:
            # Superseded; the newer request will answer. A call already in flight may still be billed.
            self._record(kind, "superseded", ms=_ms(t0), **rec)
            return
        except asyncio.TimeoutError:
            self.stats[kind]["timeout"] += 1
            log.warning("%-11s %-14s TIMEOUT after %.0fms -> game falls back to vanilla", kind, key, _ms(t0))
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
        if self.memory.note_issued(key, kind, decision):
            self.stats[kind]["replaced"] += 1    # paid for an answer the game never used
            self._push_receipt(self.receipt.update(old_seq, "replaced"))
        self.receipt_seq[key] = self._record(kind, "ok", ms=ms, llm=call, raw=raw, decision=decision,
                                             **({"late": True} if late else {}), **rec)
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


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


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
    ap.add_argument("--receipts", type=Path, default=Path(__file__).resolve().parent / "receipts",
                    help="folder for the per-run receipt (every decision and LLM call, as JSON lines)")
    ap.add_argument("--no-receipts", action="store_true", help="keep the receipt in memory only (still shown in game)")
    ap.add_argument("--no-epitaph", action="store_true", help="no DM-written epitaph on the after-death stats screen")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    concurrency, late_grace = 6, 0.0
    if args.mock:
        dm = MockDM(args.cruelty, latency=tuple(args.mock_latency))
        planner = None if args.no_planners else MockPlanner()
        gate = None if args.no_gate else MockGate()
        log.info("Dungeon Master: MOCK (offline)")
    elif args.backend == "claude-code":
        model = args.model or CLAUDE_CODE_DM_MODEL
        dm = ClaudeCodeDM(args.cruelty, model=model, claude_bin=args.claude_bin)
        planner = None if args.no_planners else ClaudeCodePlanner(
            args.narrative_model or CLAUDE_CODE_PLANNER_MODEL, args.floor_model or CLAUDE_CODE_PLANNER_MODEL,
            claude_bin=args.claude_bin)
        # The gate isn't a Claude model: it stays on OpenRouter, when there's a key for it.
        gate = None
        if not args.no_gate:
            if os.environ.get("OPENROUTER_API_KEY"):
                gate = OpenRouterGate(args.gate_model, args.gate_threshold)
            else:
                log.info("Re-ask gate: off (no OPENROUTER_API_KEY), every re-ask goes to the DM")
        concurrency, late_grace = CLAUDE_CODE_CONCURRENCY, CLAUDE_CODE_LATE_GRACE_S
        log.info("Dungeon Master: %s via Claude Code (%s)  cruelty=%d", model, dm.runner.bin, args.cruelty)
    else:
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise SystemExit("Set OPENROUTER_API_KEY (or run with --mock or --backend claude-code).")
        model = args.model or OPENROUTER_DEFAULT_MODEL
        dm = OpenRouterDM(args.cruelty, model=model)
        planner = None if args.no_planners else OpenRouterPlanner(
            args.narrative_model or NARRATIVE_DEFAULT_MODEL, args.floor_model or FLOOR_DEFAULT_MODEL)
        gate = None if args.no_gate else OpenRouterGate(args.gate_model, args.gate_threshold)
        log.info("Dungeon Master: %s via OpenRouter  cruelty=%d", model, args.cruelty)
    if gate:
        log.info("Re-ask gate: %s", gate.model)
    if planner:
        log.info("Planners: narrative=%s  floor brief=%s", planner.narrative_model, planner.floor_model)

    coord = Coordinator(dm, args.cruelty, planner, max_concurrency=concurrency,
                        receipt=Receipt(None if args.no_receipts else args.receipts),
                        epitaph=not args.no_epitaph, gate=gate, late_grace_s=late_grace)

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
