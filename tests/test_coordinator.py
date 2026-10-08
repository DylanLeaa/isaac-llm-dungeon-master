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


def test_cruelty_follows_hello_and_settings():
    class DM(ScriptedDM):
        def set_cruelty(self, cruelty):
            self.cruelty = cruelty

    dm = DM([])
    coord = Coordinator(dm, cruelty=6)
    sent = []

    async def send(msg):
        sent.append(msg)

    def handle(msg):
        asyncio.run(coord.handle(msg, send))
        return coord.cruelty

    assert handle({"t": "hello", "session": "S", "run": "R", "cruelty": 9}) == 9 and dm.cruelty == 9
    assert handle({"t": "settings", "cruelty": 2}) == 2 and dm.cruelty == 2
    assert sent[-1] == {"t": "settings_ok", "session": "S", "cruelty": 2}
    assert handle({"t": "settings"}) == 6 and dm.cruelty == 6        # menu's "backend default"
    assert handle({"t": "settings", "cruelty": 42}) == 10
    assert handle({"t": "settings", "cruelty": "lots"}) == 10        # garbage is ignored
    assert handle({"t": "hello", "session": "S2", "run": "R"}) == 6  # reconnect without override


def test_clean_text_cuts_on_word_boundary():
    assert _clean_text("Two bombs, one key, and a shop you have not found. Good luck in the dark", 70) \
        == "Two bombs, one key, and a shop you have not found. Good luck in the"
    assert _clean_text("x" * 100, 70) == "x" * 70


class ScriptedPlanner:
    """Records planner calls; each call blocks until its gate is opened (or answers at once)."""

    narrative_model = floor_model = "scripted"

    def __init__(self, fail_brief=False, gated=False):
        self.narratives, self.briefs = [], []
        self.fail_brief, self.gated = fail_brief, gated
        self.gate = asyncio.Event() if gated else None

    async def narrative(self, state, cruelty):
        self.narratives.append(state)
        if self.gate:
            await self.gate.wait()
        return f"NARRATIVE for {state['player']['character']}", {"in": 50, "cached": 0, "out": 30, "cost": 0.01}

    async def floor_brief(self, state, narrative, digest, prev_brief, cruelty):
        self.briefs.append({"stage": state["level"]["stage"], "narrative": narrative, "prev": prev_brief})
        if self.fail_brief:
            raise RuntimeError("boom")
        return f"BRIEF for {state['level']['name']}", {}


class CampaignDM(ScriptedDM):
    def __init__(self, n):
        super().__init__([{"intent": "x", "rewards": [], "taunt": ""}] * n)
        self.campaigns = []

    def set_campaign(self, text):
        self.campaigns.append(text)


def floor_req(rid, stage, name, stage_type=0):
    msg = clear_req(rid)
    msg["key"] = f"clear:{stage}:{rid}"
    msg["state"] = {**msg["state"], "player": {**msg["state"]["player"], "character": "Isaac"},
                    "level": {"stage": stage, "stage_type": stage_type, "name": name}}
    return msg


def _run_coord(coord, msgs, settle=0.05):
    async def send(msg):
        pass

    async def go():
        for m in msgs:
            await coord.handle(m, send)
            await asyncio.sleep(settle)

    asyncio.run(go())


def test_narrative_once_per_run_and_brief_once_per_floor():
    dm, planner = CampaignDM(5), ScriptedPlanner()
    coord = Coordinator(dm, cruelty=6, planner=planner)
    _run_coord(coord, [{"t": "hello", "session": "S", "run": "R1", "new_run": True},
                       floor_req(1, 1, "Basement I"), floor_req(2, 1, "Basement I"),
                       floor_req(3, 2, "Caves I"), floor_req(4, 2, "Caves I"),
                       floor_req(5, 2, "Caves I")])
    assert len(planner.narratives) == 1
    assert [b["stage"] for b in planner.briefs] == [1, 2]
    # The brief sees the narrative and the previous floor's brief.
    assert planner.briefs[1] == {"stage": 2, "narrative": "NARRATIVE for Isaac", "prev": "BRIEF for Basement I"}
    final = dm.campaigns[-1]
    assert "NARRATIVE for Isaac" in final and "BRIEF for Caves I" in final
    assert "Basement I\n" not in final
    assert coord.stats["narrative"]["ok"] == 1 and coord.stats["floor_brief"]["ok"] == 2
    assert coord.usage["cost"] >= 0.01


def test_floor_change_clears_old_brief_at_once():
    dm, planner = CampaignDM(2), ScriptedPlanner()
    coord = Coordinator(dm, cruelty=6, planner=planner)

    async def send(msg):
        pass

    async def go():
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.05)
        assert "BRIEF for Basement I" in dm.campaigns[-1]
        await coord.handle(floor_req(2, 2, "Caves I"), send)
        # Pushed synchronously, before the new brief is written.
        assert "BRIEF for Basement I" not in dm.campaigns[-1]
        assert "FLOOR BRIEF for Caves I: still being prepared" in dm.campaigns[-1]
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert "BRIEF for Caves I" in dm.campaigns[-1]


def test_new_run_discards_late_narrative_from_old_run():
    dm, planner = CampaignDM(2), ScriptedPlanner(gated=True)
    coord = Coordinator(dm, cruelty=6, planner=planner)

    async def send(msg):
        pass

    async def go():
        await coord.handle({"t": "hello", "session": "S", "run": "R1", "new_run": True}, send)
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.02)
        await coord.handle({"t": "hello", "session": "S", "run": "R2", "new_run": True}, send)
        planner.gate.set()                       # the old run's narrative "arrives" now
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert coord.campaign.narrative is None
    assert all("NARRATIVE for" not in c for c in dm.campaigns)
    assert coord.stats["narrative"]["ok"] == 0


def test_planner_failure_never_breaks_decisions():
    dm, planner = CampaignDM(1), ScriptedPlanner(fail_brief=True)
    coord = Coordinator(dm, cruelty=6, planner=planner)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert sent and sent[-1]["ok"] is True
    assert coord.stats["floor_brief"]["error"] == 1
    assert "NARRATIVE for Isaac" in dm.campaigns[-1] and "still being prepared" in dm.campaigns[-1]
