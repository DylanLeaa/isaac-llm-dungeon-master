"""The engine's receipt: every decision and planner call is stored with its LLM call and shown to the game."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from dm_agent import LLMCallError  # noqa: E402
from receipt import Receipt  # noqa: E402
from bridge import PROTO  # noqa: E402
from server import Coordinator  # noqa: E402
from test_coordinator import CampaignDM, ScriptedPlanner, clear_req, floor_req  # noqa: E402

CALL = {"model": "anthropic/claude-haiku-5.5", "system": ["PERSONA", "CAMPAIGN"], "finish": "tool_calls",
        "response": {"tool_calls": [{"function": {"name": "room_clear", "arguments": "{}"}}]},
        "usage": {"in": 100, "cached": 80, "out": 20, "cost": 0.001}}


class CallDM:
    """Answers with a call record, like OpenRouterDM; the second call fails with one."""

    def __init__(self, answers):
        self.answers = list(answers)

    def set_cruelty(self, cruelty):
        pass

    async def decide(self, kind, state, ctx, memory):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {**answer, "_usage": CALL["usage"], "_call": json.loads(json.dumps(CALL))}


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_decisions_are_stored_with_their_call_and_pushed_to_the_game(tmp_path):
    answer = {"intent": "one key", "rewards": [{"reward": "key", "count": 1}], "taunt": "Door's that way."}
    dm = CallDM([answer, answer, LLMCallError("hit max_tokens", {**CALL, "finish": "length"})])
    coord = Coordinator(dm, cruelty=6, receipt=Receipt(tmp_path))
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S", "run": "SEED 1", "new_run": True}, send)
        for rid in (1, 2):                       # a re-ask: the first answer is replaced unused
            await coord.handle(clear_req(rid), send)
            await asyncio.sleep(0.02)
        await coord.handle({"t": "applied", "key": "clear:1:82"}, send)
        await coord.handle({"t": "event", "name": "fallback",
                            "data": {"kind": "room_layout", "key": "layout:1:9", "asked": True}}, send)
        await coord.handle(clear_req(3), send)  # fails: kept with what the model returned
        await asyncio.sleep(0.02)

    asyncio.run(go())
    recs = _records(coord.receipt.path)
    assert coord.receipt.path.name.endswith("_run-SEED1.jsonl")
    # System prompts are stored once and referenced by hash.
    assert [r["text"] for r in recs if r["type"] == "prompt"] == ["PERSONA", "CAMPAIGN"]
    entries = {r["seq"]: r for r in recs if r["type"] == "entry"}
    first, second, fallback, failed = entries[1], entries[2], entries[3], entries[4]
    assert first["llm"]["model"] == CALL["model"] and len(first["llm"]["system"]) == 2
    assert first["request"]["ctx"] == {"reason": "almost clear"} and "memory" in first["request"]
    assert first["decision"]["rewards"] == [{"reward": "key", "count": 1}] and first["raw"]["intent"] == "one key"
    assert "your_previous_answer" in second["request"]["ctx"]
    updates = [(r["ref"], r["status"]) for r in recs if r["type"] == "update"]
    assert updates == [(1, "replaced"), (2, "applied")]
    assert fallback["status"] == "fallback" and "llm" not in fallback
    assert failed["status"] == "error" and failed["llm"]["finish"] == "length"

    # The game got a sync on hello, then every entry and update as it happened.
    assert sent[1]["t"] == "receipt_sync" and sent[1]["entries"] == []
    pushed = [m["entry"] for m in sent if m["t"] == "receipt"]
    assert [(e["seq"], e["status"]) for e in pushed] == \
        [(1, "ok"), (1, "replaced"), (2, "ok"), (2, "applied"), (3, "fallback"), (4, "error")]
    shown = pushed[3]
    assert shown == {**shown, "what": "room_clear", "key": "clear:1:82", "model": "claude-haiku-5.5",
                     "tok": [100, 80, 20], "cost": 0.001, "result": "1x key", "taunt": "Door's that way."}
    assert "request" not in shown and "llm" not in shown        # the game never gets payloads
    totals = coord.receipt.totals()
    assert totals["calls"] == 3 and totals["applied"] == 1 and totals["replaced"] == 1
    assert totals["fallback"] == 1 and totals["error"] == 1 and abs(totals["cost"] - 0.003) < 1e-9


def test_planner_calls_are_on_the_receipt():
    dm, planner = CampaignDM(1), ScriptedPlanner()
    coord = Coordinator(dm, cruelty=6, planner=planner)

    async def send(msg):
        pass

    async def go():
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    written = {e["what"]: e for e in coord.receipt.snapshot()["entries"] if e["status"] == "written"}
    assert written["narrative"]["result"] == "NARRATIVE for Isaac"
    assert written["floor_brief"]["key"] == "Basement I (1.0)"


def test_continued_run_resumes_its_receipt_and_new_run_starts_fresh(tmp_path):
    dm = CallDM([{"intent": "a", "rewards": []}] * 3)
    sent = []

    async def send(msg):
        sent.append(msg)

    def session(coord, hello):
        async def go():
            await coord.handle(hello, send)
            await coord.handle(clear_req(len(sent)), send)
            await asyncio.sleep(0.02)
        asyncio.run(go())

    first = Coordinator(dm, cruelty=6, receipt=Receipt(tmp_path))
    session(first, {"t": "hello", "proto": PROTO, "session": "S1", "run": "R", "new_run": True})
    # Backend restarted, game continued the same run: same file, numbering and totals carry on.
    again = Coordinator(dm, cruelty=6, receipt=Receipt(tmp_path))
    session(again, {"t": "hello", "proto": PROTO, "session": "S2", "run": "R", "new_run": False})
    sync = [m for m in sent if m["t"] == "receipt_sync"][-1]
    assert again.receipt.path == first.receipt.path and [e["seq"] for e in sync["entries"]] == [1]
    assert again.receipt.totals()["calls"] == 2 and again.receipt.seq == 2
    # Same seed, new run: a new file.
    third = Coordinator(dm, cruelty=6, receipt=Receipt(tmp_path))
    third.receipt.start_run("R", fresh=True)
    assert third.receipt.seq == 0 and third.receipt.totals()["calls"] == 0
    assert third.receipt.path != first.receipt.path
