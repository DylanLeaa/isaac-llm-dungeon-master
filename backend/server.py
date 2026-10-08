"""LLM Dungeon Master backend.

Speaks newline-delimited JSON with the Isaac mod over either
  * TCP on 127.0.0.1:7337 (mod uses LuaSocket; needs --luadebug), or
  * two append-only files in a shared folder (mod uses io; also needs --luadebug).

Run:  python server.py                 # Haiku 5.5, TCP
      python server.py --mock          # no API key needed
      python server.py --bridge file --bridge-dir "C:/.../The Binding of Isaac Rebirth/llmdm_bridge"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from dm_agent import OPENROUTER_DEFAULT_MODEL, MockDM, OpenRouterDM
from memory import RunMemory
from schemas import KINDS, clamp

log = logging.getLogger("llmdm")
Send = Callable[[dict], Awaitable[None]]

MAX_DEADLINE_S = 12.0      # never let a single decision hang longer than this
MIN_DEADLINE_S = 0.4


class Coordinator:
    """Routes requests to the DM, enforces latest-wins per key, applies guardrails."""

    def __init__(self, dm, cruelty: int, max_concurrency: int = 6) -> None:
        self.dm = dm
        self.cruelty = cruelty
        self.memory = RunMemory()
        self.session: str | None = None
        self.inflight: dict[str, tuple[int, asyncio.Task]] = {}
        self.sem = asyncio.Semaphore(max_concurrency)
        self.stats = {"ok": 0, "timeout": 0, "error": 0, "superseded": 0}

    async def handle(self, msg: dict, send: Send) -> None:
        t = msg.get("t")
        if t == "hello":
            self.session = str(msg.get("session"))
            if msg.get("run") != self.memory.run_id or msg.get("new_run"):
                self.memory.reset(msg.get("run"))
            if msg.get("cruelty") is not None:
                self.cruelty = int(msg["cruelty"])
                self.dm.set_cruelty(self.cruelty)
            log.info("game connected  session=%s run=%s cruelty=%s", self.session, msg.get("run"), self.cruelty)
            await send({"t": "welcome", "session": self.session, "cruelty": self.cruelty})
        elif t == "req":
            self._start(msg, send)
        elif t == "applied":
            self.memory.on_applied(str(msg.get("key")))
        elif t == "event":
            self.memory.on_game_event(str(msg.get("name")), msg.get("data") or {})
        elif t == "ping":
            await send({"t": "pong", "session": self.session, "id": msg.get("id")})

    # ----------------------------------------------------------------- requests
    def _start(self, msg: dict, send: Send) -> None:
        key, rid, kind = str(msg["key"]), int(msg["id"]), msg.get("kind")
        if kind not in KINDS:
            log.warning("unknown kind %r", kind)
            return
        old = self.inflight.get(key)
        if old and old[0] < rid and not old[1].done():
            old[1].cancel()                      # latest-wins: newer context supersedes
            self.stats["superseded"] += 1
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
        deadline = max(MIN_DEADLINE_S, min(MAX_DEADLINE_S, float(msg.get("deadline_ms", 4000)) / 1000))
        t0 = time.perf_counter()
        base = {"t": "res", "session": self.session, "id": rid, "key": key, "kind": kind}
        try:
            async with self.sem:
                raw = await asyncio.wait_for(
                    self.dm.decide(kind, state, ctx, self.memory.digest()), timeout=deadline)
            decision = clamp(kind, raw, state, ctx, self.cruelty, self.memory.counters)
        except asyncio.CancelledError:
            return                                # superseded; the newer request will answer
        except asyncio.TimeoutError:
            self.stats["timeout"] += 1
            log.warning("%-11s %-14s TIMEOUT after %.0fms -> game falls back to vanilla",
                        kind, key, (time.perf_counter() - t0) * 1000)
            await send({**base, "ok": False, "reason": "timeout"})
            return
        except Exception as e:                    # noqa: BLE001 - never crash the bridge
            self.stats["error"] += 1
            log.exception("%s %s failed: %s", kind, key, e)
            await send({**base, "ok": False, "reason": type(e).__name__})
            return
        ms = int((time.perf_counter() - t0) * 1000)
        self.stats["ok"] += 1
        self.memory.note_issued(key, kind, decision)
        log.info("%-11s %-14s %5dms  %s%s", kind, key, ms, decision.get("intent", ""),
                 f'  "{decision["taunt"]}"' if decision.get("taunt") else "")
        if decision.get("guardrails"):
            log.info("%28s guardrails: %s", "", "; ".join(decision["guardrails"]))
        await send({**base, "ok": True, "ms": ms, "decision": decision})


# ---------------------------------------------------------------- transports
async def serve_tcp(coord: Coordinator, host: str, port: int) -> None:
    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("tcp client %s connected", peer)
        lock = asyncio.Lock()

        async def send(obj: dict) -> None:
            data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
            async with lock:
                writer.write(data)
                await writer.drain()

        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("bad json from game: %r", line[:200])
                    continue
                await coord.handle(msg, send)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            log.info("tcp client %s disconnected", peer)
            writer.close()

    server = await asyncio.start_server(on_client, host, port, limit=2**22)
    log.info("listening on %s:%d", host, port)
    async with server:
        await server.serve_forever()


async def serve_files(coord: Coordinator, bridge_dir: Path, poll_s: float = 0.03) -> None:
    """Game appends to to_dm.jsonl; we append to from_dm.jsonl. Both tagged by session."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    inbox, outbox = bridge_dir / "to_dm.jsonl", bridge_dir / "from_dm.jsonl"
    inbox.touch()
    outbox.touch()
    log.info("file bridge at %s", bridge_dir)
    pos, buf = inbox.stat().st_size, b""     # ignore stale requests from old sessions

    async def send(obj: dict) -> None:
        with open(outbox, "ab") as f:
            f.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    while True:
        size = inbox.stat().st_size
        if size < pos:                            # game truncated: new session
            pos, buf = 0, b""
        if size > pos:
            with open(inbox, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            pos += len(chunk)
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("t") == "hello":
                    outbox.write_bytes(b"")      # fresh response log per session
                await coord.handle(msg, send)
        await asyncio.sleep(poll_s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bridge", choices=["tcp", "file"], default="tcp")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7337)
    ap.add_argument("--bridge-dir", type=Path, default=Path("llmdm_bridge"))
    ap.add_argument("--model", default=os.environ.get("LLMDM_MODEL", OPENROUTER_DEFAULT_MODEL),
                    help="OpenRouter model slug")
    ap.add_argument("--cruelty", type=int, default=6, help="0 (generous) .. 10 (merciless)")
    ap.add_argument("--mock", action="store_true", help="use the offline rule-based DM")
    ap.add_argument("--mock-latency", type=float, nargs=2, default=(0.25, 1.2), metavar=("MIN", "MAX"),
                    help="fake response time range for --mock, seconds")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.mock:
        dm = MockDM(args.cruelty, latency=tuple(args.mock_latency))
        log.info("Dungeon Master: MOCK (offline)")
    else:
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise SystemExit("Set OPENROUTER_API_KEY (or run with --mock).")
        dm = OpenRouterDM(args.cruelty, model=args.model)
        log.info("Dungeon Master: %s via OpenRouter  cruelty=%d", args.model, args.cruelty)

    async def run() -> None:
        coord = Coordinator(dm, args.cruelty)
        if args.bridge == "tcp":
            await serve_tcp(coord, args.host, args.port)
        else:
            await serve_files(coord, args.bridge_dir)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
