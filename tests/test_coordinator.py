"""Coordinator + memory bookkeeping: re-asks see the answer they replace, plans only count once applied."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from schemas import _clean_text  # noqa: E402
from server import Coordinator  # noqa: E402


class ScriptedDM:
    """Returns canned answers in order and records the ctx each call saw."""

    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def set_cruelty(self, cruelty):
        pass

    async def decide(self, kind, state, ctx, memory):
        self.seen.append((ctx, memory))
        return {**self.answers.pop(0), "_usage": {"in": 100, "cached": 80, "out": 20, "cost": 0.001}}


def clear_req(rid):
    return {"t": "req", "id": rid, "key": "clear:1:82", "kind": "room_clear", "deadline_ms": 4000,
            "state": {"player": {"hp": {"red": 6}, "resources": {"keys": 0, "bombs": 1, "coins": 0}}},
            "ctx": {"reason": "almost clear"}}


def test_reask_sees_previous_answer_and_schemes_wait_for_apply():
    dm = ScriptedDM([
        {"intent": "one bomb", "rewards": [{"reward": "bomb", "count": 1}], "taunt": "Two bombs. Fun.",
         "scheme": "bomb bait"},
        {"intent": "still one bomb", "rewards": [{"reward": "bomb", "count": 1}], "taunt": "two bombs, fun!",
         "scheme": "Bomb bait"},
    ])
    coord = Coordinator(dm, cruelty=6)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(clear_req(1), send)
        await asyncio.sleep(0.05)
        await coord.handle(clear_req(2), send)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert "your_previous_answer" not in dm.seen[0][0]
    prev = dm.seen[1][0]["your_previous_answer"]
    assert prev["rewards"] == [{"reward": "bomb", "count": 1}] and "guardrails" not in prev
    # Unapplied schemes stay out of memory; equivalent taunts are stored once.
    assert dm.seen[1][1]["your_active_schemes"] == []
    assert len(coord.memory.taunts) == 1
    assert coord.stats["room_clear"]["replaced"] == 1
    assert coord.usage["cached"] == 160 and coord.usage["calls"] == 2

    asyncio.run(coord.handle({"t": "applied", "key": "clear:1:82"}, send))
    assert list(coord.memory.schemes) == ["Bomb bait"]
    assert coord.stats["room_clear"]["applied"] == 1


def test_fallback_event_is_counted_not_remembered():
    coord = Coordinator(ScriptedDM([]), cruelty=6)

    async def send(msg):
        pass

    asyncio.run(coord.handle({"t": "event", "name": "fallback",
                              "data": {"kind": "room_clear", "key": "clear:1:9", "asked": False}}, send))
    assert coord.stats["room_clear"]["fallback"] == 1
    assert not coord.memory.events


def test_clean_text_cuts_on_word_boundary():
    assert _clean_text("Two bombs, one key, and a shop you have not found. Good luck in the dark", 70) \
        == "Two bombs, one key, and a shop you have not found. Good luck in the"
    assert _clean_text("x" * 100, 70) == "x" * 70
