"""Coordinator + memory bookkeeping: re-asks see the answer they replace, plans only count once applied."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from schemas import _clean_text  # noqa: E402
from bridge import PROTO  # noqa: E402
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

    assert handle({"t": "hello", "proto": PROTO, "session": "S", "run": "R", "cruelty": 9}) == 9 and dm.cruelty == 9
    assert handle({"t": "settings", "cruelty": 2}) == 2 and dm.cruelty == 2
    assert [m for m in sent if m["t"] == "settings_ok"][-1] == {"t": "settings_ok", "session": "S", "cruelty": 2}
    assert handle({"t": "settings"}) == 6 and dm.cruelty == 6        # menu's "backend default"
    assert handle({"t": "settings", "cruelty": 42}) == 10
    assert handle({"t": "settings", "cruelty": "lots"}) == 10        # garbage is ignored
    assert handle({"t": "hello", "proto": PROTO, "session": "S2", "run": "R"}) == 6  # reconnect without override


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
    _run_coord(coord, [{"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True},
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
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True}, send)
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.02)
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S", "run": "R2", "new_run": True}, send)
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


def _prepare_msgs(planner, msgs):
    """Feed messages to a coordinator; returns what it sent to the game."""
    coord = Coordinator(CampaignDM(2), cruelty=6, planner=planner)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        for m in msgs:
            await coord.handle(m, send)
        await asyncio.sleep(0.1)
        return coord

    return sent, coord, go


def test_prepare_answers_ready_once_the_narrative_is_written():
    planner = ScriptedPlanner(gated=True)
    sent, coord, go = _prepare_msgs(planner, [{"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True},
                                              {"t": "prepare", "floor": 1,
                                               "state": floor_req(1, 1, "Basement I")["state"]}])

    async def run():
        task = asyncio.create_task(go())
        await asyncio.sleep(0.05)
        assert not [m for m in sent if m["t"] == "ready"]      # still writing: the game keeps waiting
        planner.gate.set()
        await task

    asyncio.run(run())
    assert [m for m in sent if m["t"] == "ready"] == [{"t": "ready", "session": "S", "floor": 1, "status": "ready"}]
    assert len(planner.narratives) == 1


def test_prepare_twice_writes_one_narrative_and_a_late_request_adds_no_second():
    planner = ScriptedPlanner()
    state = floor_req(1, 1, "Basement I")["state"]
    sent, coord, go = _prepare_msgs(planner, [{"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True},
                                              {"t": "prepare", "state": state}, {"t": "prepare", "state": state},
                                              floor_req(1, 1, "Basement I")])
    asyncio.run(go())
    assert len(planner.narratives) == 1
    assert [m["status"] for m in sent if m["t"] == "ready"] == ["ready", "ready"]


def test_prepare_without_planner_is_ready_at_once():
    sent, coord, go = _prepare_msgs(None, [{"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True},
                                           {"t": "prepare", "state": {}}])
    asyncio.run(go())
    assert [m["status"] for m in sent if m["t"] == "ready"] == ["none"]


def test_prepare_reports_a_failed_narrative_and_ignores_an_abandoned_run():
    class Boom(ScriptedPlanner):
        async def narrative(self, state, cruelty):
            raise RuntimeError("boom")

    state = floor_req(1, 1, "Basement I")["state"]
    sent, coord, go = _prepare_msgs(Boom(), [{"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True},
                                             {"t": "prepare", "state": state}])
    asyncio.run(go())
    assert [m["status"] for m in sent if m["t"] == "ready"] == ["failed"]

    gated = ScriptedPlanner(gated=True)
    sent, coord, go = _prepare_msgs(gated, [{"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True},
                                            {"t": "prepare", "state": state},
                                            {"t": "hello", "proto": PROTO, "session": "S", "run": "R2", "new_run": True}])
    asyncio.run(go())
    assert not [m for m in sent if m["t"] == "ready"]            # the old run's wait is dropped


class SlowDM(ScriptedDM):
    """Answers after `delay` seconds; counts how many calls were cancelled."""

    def __init__(self, n, delay):
        super().__init__([{"intent": "slow", "rewards": [], "taunt": ""}] * n)
        self.delay, self.cancelled = delay, 0

    async def decide(self, kind, state, ctx, memory):
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return await super().decide(kind, state, ctx, memory)


def _ask(coord, msgs, settle):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        for m in msgs:
            await coord.handle(m, send)
        await asyncio.sleep(settle)

    asyncio.run(go())
    return [m for m in sent if m.get("t") == "res"]


def _fast(rid, key="clear:1:82"):
    return {**clear_req(rid), "key": key, "deadline_ms": 100}         # deadline floors at 0.4s


def test_late_answer_is_delivered_not_killed():
    dm = SlowDM(1, 0.6)
    res = _ask(Coordinator(dm, cruelty=6, late_grace_s=1.0), [_fast(1)], 0.9)
    assert [(r["ok"], r["decision"]["intent"]) for r in res] == [(True, "slow")]   # no timeout reply first
    assert dm.cancelled == 0


def test_without_grace_or_past_it_the_call_times_out_and_is_stopped():
    for grace in (0.0, 0.1):
        dm = SlowDM(1, 0.8)
        coord = Coordinator(dm, cruelty=6, late_grace_s=grace)
        res = _ask(coord, [_fast(1)], 0.9)
        assert [(r["ok"], r["reason"]) for r in res] == [(False, "timeout")]
        assert dm.cancelled == 1 and coord.stats["room_clear"]["timeout"] == 1


def test_waiting_for_a_slot_does_not_eat_the_deadline():
    dm = SlowDM(2, 0.3)
    coord = Coordinator(dm, cruelty=6, max_concurrency=1)
    res = _ask(coord, [_fast(1, "clear:1:1"), _fast(2, "clear:1:2")], 0.8)  # second waits 0.3s, then has 0.4s
    assert [r["ok"] for r in res] == [True, True]


# ------------------------------------------------- decisions wait for their floor's brief
class BriefGatedPlanner(ScriptedPlanner):
    """The narrative answers at once; each floor brief waits for `brief_gate`."""

    def __init__(self):
        super().__init__()
        self.brief_gate = asyncio.Event()

    async def floor_brief(self, state, narrative, digest, prev_brief, cruelty):
        await self.brief_gate.wait()
        return await super().floor_brief(state, narrative, digest, prev_brief, cruelty)


class SnapshotDM(CampaignDM):
    """Records the campaign each decision was made against."""

    campaign = ""

    def set_campaign(self, text):
        super().set_campaign(text)
        self.campaign = text

    async def decide(self, kind, state, ctx, memory):
        self.made_against = getattr(self, "made_against", []) + [self.campaign]
        return await super().decide(kind, state, ctx, memory)


def test_a_decision_waits_for_its_floor_brief_and_is_made_against_it():
    dm, planner = SnapshotDM(2), BriefGatedPlanner()
    coord = Coordinator(dm, cruelty=6, planner=planner)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.05)
        assert not dm.seen and not sent                  # held until the brief is in
        planner.brief_gate.set()
        await asyncio.sleep(0.05)
        await coord.handle(floor_req(2, 1, "Basement I"), send)   # brief already written: no wait
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert len(dm.made_against) == 2
    assert all("BRIEF for Basement I" in c for c in dm.made_against)


def test_a_new_run_drops_decisions_still_waiting_on_the_old_runs_brief():
    dm, planner = SnapshotDM(1), BriefGatedPlanner()
    coord = Coordinator(dm, cruelty=6, planner=planner)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True}, send)
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.05)
        assert not dm.seen and not [m for m in sent if m.get("t") == "res"]   # held for the brief
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S2", "run": "R2", "new_run": True}, send)
        planner.brief_gate.set()
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert not dm.seen                                   # never answered against an empty campaign
    assert not [m for m in sent if m.get("t") == "res"]


def test_a_brief_that_never_comes_only_holds_a_decision_for_the_brief_wait():
    import planners
    dm, planner = SnapshotDM(1), BriefGatedPlanner()
    coord = Coordinator(dm, cruelty=6, planner=planner)
    old, planners.BRIEF_WAIT_S = planners.BRIEF_WAIT_S, 0.1
    try:
        res = _ask(coord, [floor_req(1, 1, "Basement I")], 0.3)
    finally:
        planners.BRIEF_WAIT_S = old
    assert [r["ok"] for r in res] == [True]
    assert "still being prepared" in dm.made_against[0]


def test_floor_prepare_answers_ready_for_that_floor_once_its_brief_is_in():
    planner = BriefGatedPlanner()
    sent, coord, go = _prepare_msgs(planner, [{"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True},
                                              {"t": "prepare", "floor": 2,
                                               "state": floor_req(1, 2, "Caves I")["state"]}])

    async def run():
        task = asyncio.create_task(go())
        await asyncio.sleep(0.05)
        assert not [m for m in sent if m["t"] == "ready"]          # narrative in, brief still writing
        planner.brief_gate.set()
        await task

    asyncio.run(run())
    assert [m for m in sent if m["t"] == "ready"] == [{"t": "ready", "session": "S", "floor": 2, "status": "ready"}]


def test_the_gate_compares_against_the_campaign_a_call_started_with():
    dm = SlowDM(1, 0.1)
    dm.campaign = "brief A"
    coord = Coordinator(dm, cruelty=6)

    async def send(msg):
        pass

    async def go():
        await coord.handle(clear_req(1), send)
        await asyncio.sleep(0.03)
        dm.campaign = "brief B"                          # lands mid-call
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert coord.last_req["clear:1:82"]["campaign"] == "brief A"


def test_urgent_asks_get_the_next_slot_before_background_work():
    from server import BACKGROUND, NEXT_DOOR, URGENT, PrioritySemaphore, _priority
    assert _priority("room_clear", {}) == URGENT
    assert _priority("room_layout", {"reason": "player entered an adjacent room"}) == NEXT_DOOR
    assert _priority("item_choice", {"reason": "player is next to this room"}) == NEXT_DOOR
    assert _priority("item_choice", {"reason": "floor start"}) == BACKGROUND
    assert _priority("drops", {"reason": "player entered the room"}) == NEXT_DOOR
    for kind in ("cards", "trinkets", "pills"):
        assert _priority(kind, {"reason": "restock"}) == BACKGROUND
    for kind in ("cards", "trinkets", "pills", "item_choice"):
        assert _priority(kind, {"owed": [{"runes": True}]}) == URGENT, "the game is frozen on it"

    order = []

    async def go():
        sem = PrioritySemaphore(1)

        async def job(name, prio, hold=0.01):
            async with sem.slot(prio):
                order.append(name)
                await asyncio.sleep(hold)

        first = asyncio.create_task(job("busy", BACKGROUND, 0.05))
        await asyncio.sleep(0.01)
        jobs = [asyncio.create_task(job(n, p)) for n, p in
                [("items-1", BACKGROUND), ("items-2", BACKGROUND), ("cancelled", URGENT),
                 ("layout", NEXT_DOOR), ("clear", URGENT)]]
        await asyncio.sleep(0)
        jobs[2].cancel()                                 # a superseded waiter gives up its place
        await asyncio.gather(first, *jobs, return_exceptions=True)
        # every slot came back
        async with sem.slot(URGENT):
            order.append("after")

    asyncio.run(asyncio.wait_for(go(), 2))
    assert order == ["busy", "clear", "layout", "items-1", "items-2", "after"]


def test_planners_are_told_what_the_player_really_holds():
    from planners import loadout
    bare = {"player": {"character": "Isaac", "active_item": "none", "pocket_active": "none", "trinkets": "none",
                       "pocket": "none", "items": {}, "tainted": False},
            "save": {"items_unlocked": 120, "items_total": 719}}
    line = loadout(bare)
    assert "Isaac; active item: none;" in line and "items: none" in line and "120/719" in line
    assert "Tainted" not in line
    held = {"player": {"character": "Isaac", "tainted": True, "active_item": "The D6", "items": ["The D6 (q4)"]}}
    line = loadout(held)
    assert "Tainted Isaac" in line and "active item: The D6" in line and "unlocked" not in line
    assert "unknown" in loadout({})


def test_reconfigure_swaps_models_mid_run_and_keeps_the_run():
    """The app's Settings: a new DM takes over with the same run, plans, cruelty and receipt."""
    from receipt import Receipt
    from server import Engine
    import tempfile

    old_dm, planner = CampaignDM(1), ScriptedPlanner()
    coord = Coordinator(old_dm, cruelty=6, planner=planner, receipt=Receipt(Path(tempfile.mkdtemp())))
    new_dm = CampaignDM(1)
    new_dm.cruelties = []
    new_dm.set_cruelty = new_dm.cruelties.append
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S", "run": "R1", "new_run": True,
                            "cruelty": 8}, send)
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.05)
        path = coord.receipt.path
        coord.reconfigure(Engine(new_dm, ScriptedPlanner(), None, concurrency=2, late_grace=1.5, label="new"))
        await coord.handle(floor_req(2, 1, "Basement I"), send)
        await asyncio.sleep(0.05)
        return path

    path = asyncio.run(go())
    assert len(old_dm.seen) == 1 and len(new_dm.seen) == 1        # the second decision went to the new DM
    assert new_dm.cruelties == [8]                                 # told the run's cruelty, not the default
    assert "NARRATIVE for Isaac" in new_dm.campaigns[0] and "BRIEF for Basement I" in new_dm.campaigns[0]
    assert coord.late_grace_s == 1.5 and coord.gate is None
    assert coord.receipt.path == path and coord.memory.run_id == "R1"
    assert [m["ok"] for m in sent if m["t"] == "res"] == [True, True]

# ------------------------------------------------- decks, draws and the floor's economy
FLOOR = {"stage": 1, "stage_type": 0, "name": "Basement I", "room_types": {"default": 10}}


def econ_req(rid, key, kind, ctx=None, keys=1):
    return {"t": "req", "id": rid, "key": key, "kind": kind, "deadline_ms": 4000,
            "state": {"player": {"hp": {"red": 6}, "resources": {"keys": keys, "bombs": 1, "coins": 0}},
                      "level": FLOOR},
            "ctx": {"reason": "test", **(ctx or {})}}


def _starve_keys(coord):
    coord.memory.economy.observe({"level": FLOOR})
    coord._brief_written((1, 0), "ARC BEAT: x\nECONOMY: keys=starve coins=starve\nROOMS: y")


def test_drawn_counts_what_was_handed_out_and_marks_the_answer_applied(tmp_path):
    from receipt import Receipt
    dm = ScriptedDM([{"intent": "a key", "rewards": [{"reward": "key", "count": 1}], "taunt": "", "scheme": "s1"},
                     {"intent": "kills", "outcomes": [{"reward": "nothing"}, {"reward": "bomb"}], "taunt": ""}])
    coord = Coordinator(dm, cruelty=6, receipt=Receipt(tmp_path))
    coord.receipt.start_run("R", fresh=True)
    coord.memory.counters["clears_since_bomb"] = 3
    _run_coord(coord, [econ_req(1, "drops:1", "drops", {"wanted": 2}), econ_req(2, "clear:1:85", "room_clear"),
                       {"t": "drawn", "key": "drops:1", "id": 1, "outcome": {"reward": "nothing"}, "cause": "enemy_death"},
                       {"t": "drawn", "key": "drops:1", "id": 1, "outcome": {"reward": "bomb", "count": 1},
                        "cause": "enemy_death"},
                       {"t": "drawn", "key": "clear:1:85", "id": 2,
                        "outcome": {"rewards": [{"reward": "key", "count": 1}]}}])
    c = coord.memory.counters
    assert c["clears"] == 1 and c["clears_since_key"] == 0
    assert c["clears_since_bomb"] == 0, "a bomb from a kill since the last clear counts for the drought"
    assert c["drops_floor"] == 1
    assert coord.stats["drops"]["applied"] == 1 and coord.stats["room_clear"]["applied"] == 1, "once per answer"
    assert coord.memory.economy.delivered["keys"] == 1 and coord.memory.economy.delivered["bombs"] == 1
    assert list(coord.memory.schemes) == ["s1"]
    assert any(e == "kill dropped: 1x bomb" for e in coord.memory.events)
    assert coord.receipt.drawn == [("drops", {"reward": "nothing"}), ("drops", {"reward": "bomb", "count": 1}),
                                   ("room_clear", {"rewards": [{"reward": "key", "count": 1}]})]
    assert sorted(coord.receipt.status.values()).count("applied") == 2
    assert "drops:1" not in coord.memory.issued, "a deck drawn to the end is used up"


def test_a_concurrent_deck_sees_and_is_trimmed_by_what_another_promised():
    dm = ScriptedDM([{"intent": "a key", "rewards": [{"reward": "key", "count": 1}], "taunt": "", "scheme": "keys coming"},
                     {"intent": "keys", "outcomes": [{"reward": "key"}, {"reward": "key"}, {"reward": "nothing"}],
                      "taunt": ""}])
    coord = Coordinator(dm, cruelty=6)
    _starve_keys(coord)                         # keys: cap ~2.06 this floor
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(econ_req(1, "clear:1:85", "room_clear"), send)
        await asyncio.sleep(0.05)
        await coord.handle(econ_req(2, "drops:1", "drops", {"wanted": 3}), send)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    ctx, memory = dm.seen[1]
    keys = ctx["economy"]["keys"]
    assert keys["committed_elsewhere"] == 1 and keys["promised_by"] == {"clear:1:85": 1} and keys["remaining"] == 1.1
    assert "keys: 0 given, 1 promised by clear:1:85" in ctx["economy"]["verdict"]
    assert memory["your_open_decks"][0]["key"] == "clear:1:85" and memory["your_open_decks"][0]["scheme"] == "keys coming"
    drops = [m for m in sent if m.get("t") == "res" and m["key"] == "drops:1"][0]["decision"]
    assert [o["reward"] for o in drops["outcomes"]] == ["key", "nothing", "nothing"]
    assert "key -> nothing (keys over budget: clear:1:85 took the last one)" in drops["guardrails"]
    assert [m for m in sent if m.get("t") == "stale"] == [
        {"t": "stale", "session": None, "keys": ["drops:1"], "why": "trimmed to the budget"}]


def test_an_overspend_re_asks_the_other_decks_committed_to_that_family():
    dm = ScriptedDM([{"intent": "kills", "outcomes": [{"reward": "key"}], "taunt": ""},
                     {"intent": "loaded", "schedule": [{"reward": "key", "count": 2}, {"reward": "key", "count": 2}],
                      "taunt": ""}])
    coord = Coordinator(dm, cruelty=6)
    _starve_keys(coord)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(econ_req(1, "drops:1", "drops", {"wanted": 1}), send)
        await asyncio.sleep(0.05)
        await coord.handle(econ_req(2, "payout:1:7", "payout_plan", {"source_class": "chest", "source": "chest"}), send)
        await asyncio.sleep(0.05)
        e = coord.memory.economy
        assert e.contingent["payout:1:7"]["keys"] == 4 and e.delivered["keys"] == 0, "a chest counts once opened"
        version = e.version
        for _ in range(2):
            await coord.handle({"t": "drawn", "key": "payout:1:7", "id": 2, "cause": "chest_open",
                                "outcome": {"reward": "key", "count": 2}}, send)
        assert e.delivered["keys"] == 4 and e.version > version

    asyncio.run(go())
    stale = [m for m in sent if m.get("t") == "stale"]
    assert stale and stale[0]["keys"] == ["drops:1"] and "keys over budget" in stale[0]["why"]


def test_machine_winnings_are_bought_and_never_trim_the_drops():
    dm = ScriptedDM([{"intent": "pays", "schedule": [{"reward": "key"}, {"reward": "key"}], "taunt": ""},
                     {"intent": "kills", "outcomes": [{"reward": "key"}, {"reward": "key"}], "taunt": ""}])
    coord = Coordinator(dm, cruelty=6)
    _starve_keys(coord)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(econ_req(1, "payout:1:9", "payout_plan", {"source_class": "slot", "source": "key_master"}),
                           send)
        await asyncio.sleep(0.05)
        for _ in range(2):
            await coord.handle({"t": "drawn", "key": "payout:1:9", "id": 1, "cause": "slot_use",
                                "outcome": {"reward": "key", "count": 1}}, send)
        await coord.handle(econ_req(2, "drops:1", "drops", {"wanted": 2}), send)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    e = coord.memory.economy
    assert e.bought["keys"] == 2 and e.delivered["keys"] == 0
    assert dm.seen[1][0]["economy"]["keys"]["bought"] == 2
    drops = [m for m in sent if m.get("t") == "res" and m["key"] == "drops:1"][0]["decision"]
    assert [o["reward"] for o in drops["outcomes"]] == ["key", "key"], "the machine's keys were paid for"


def test_abandoning_a_deck_frees_what_it_promised():
    dm = ScriptedDM([{"intent": "a key", "rewards": [{"reward": "key", "count": 1}], "taunt": ""}])
    coord = Coordinator(dm, cruelty=6)
    _run_coord(coord, [econ_req(1, "clear:1:85", "room_clear")])
    assert coord.memory.economy.elsewhere("keys", None) == 1
    _run_coord(coord, [{"t": "abandon", "key": "clear:1:85"}])
    assert coord.memory.economy.elsewhere("keys", None) == 0 and "clear:1:85" not in coord.memory.issued


class GatedDM(ScriptedDM):
    """Each key's answer waits for its own event, so a test decides which call finishes first."""

    def __init__(self, answers):
        super().__init__([])
        self.by_key, self.gates = answers, {}

    async def decide(self, kind, state, ctx, memory):
        key = state["key"]
        self.seen.append((ctx, memory))
        await self.gates.setdefault(key, asyncio.Event()).wait()
        return dict(self.by_key[key].pop(0))


def test_two_concurrent_decks_race_for_the_last_coin():
    import math
    for first, second in (("clear:1:85", "drops:1"), ("drops:1", "clear:1:85")):
        dm = GatedDM({"clear:1:85": [{"intent": "a penny", "rewards": [{"reward": "penny", "count": 1}], "taunt": ""},
                                     {"intent": "again", "rewards": [], "taunt": ""}],
                      "drops:1": [{"intent": "a penny", "outcomes": [{"reward": "penny"}, {"reward": "nothing"}],
                                   "taunt": ""},
                                  {"intent": "again", "outcomes": [{"reward": "nothing"}], "taunt": ""}]})
        coord = Coordinator(dm, cruelty=6)
        _starve_keys(coord)
        e = coord.memory.economy
        e.delivered["coins"] = math.floor(e.cap("coins")) - 1       # one coin left on the floor
        sent = []

        async def send(msg):
            sent.append(msg)

        def req(rid, key):
            m = econ_req(rid, key, "room_clear" if key.startswith("clear") else "drops", {"wanted": 2})
            m["state"]["key"] = key
            return m

        async def go():
            await coord.handle(req(1, "clear:1:85"), send)
            await coord.handle(req(2, "drops:1"), send)
            await asyncio.sleep(0.02)
            assert all(c["economy"]["coins"]["remaining"] >= 1 for c, _ in dm.seen), "both were asked with a coin left"
            for key in (first, second):
                dm.gates.setdefault(key, asyncio.Event()).set()
                await asyncio.sleep(0.02)
            loser = [m for m in sent if m.get("t") == "stale"]
            assert loser == [{"t": "stale", "session": None, "keys": [second], "why": "trimmed to the budget"}]
            await coord.handle(req(3, second), send)               # the mod re-asks the loser
            await asyncio.sleep(0.02)

        asyncio.run(go())
        res = {m["key"]: m["decision"] for m in sent if m.get("t") == "res" and m["id"] in (1, 2)}
        coins = [r for r in res["clear:1:85"]["rewards"] if r["reward"] == "penny"] + \
                [o for o in res["drops:1"]["outcomes"] if o["reward"] == "penny"]
        assert len(coins) == 1, (first, res)
        assert f"penny -> nothing (coins over budget: {first} took the last one)" in res[second]["guardrails"]
        assert e.elsewhere("coins", None) == 1
        reask_memory = dm.seen[-1][1]
        assert first in [d["key"] for d in reask_memory["your_open_decks"]], "the re-ask sees the winner's deck"


def test_a_kept_deck_is_served_as_what_is_left_of_it():
    from gate import MockGate
    dm = ScriptedDM([{"intent": "kills", "outcomes": [{"reward": "key"}, {"reward": "nothing"}, {"reward": "penny"}],
                      "taunt": ""}])
    coord = Coordinator(dm, cruelty=6, gate=MockGate())
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle(econ_req(1, "drops:1", "drops", {"wanted": 3}), send)
        await asyncio.sleep(0.05)
        await coord.handle({"t": "drawn", "key": "drops:1", "id": 1, "outcome": {"reward": "key", "count": 1}}, send)
        await coord.handle(econ_req(2, "drops:1", "drops", {"wanted": 3}), send)    # nothing changed: kept
        await asyncio.sleep(0.05)
        await coord.handle({"t": "drawn", "key": "drops:1", "id": 2, "outcome": {"reward": "nothing"}}, send)

    asyncio.run(go())
    kept = [m for m in sent if m.get("t") == "res"][-1]
    assert len(dm.seen) == 1 and coord.stats["drops"]["reused"] == 1
    assert [o["reward"] for o in kept["decision"]["outcomes"]] == ["nothing", "penny"], "the drawn key isn't served again"
    assert kept["drawn_base"] == 1
    assert coord.stats["drops"]["applied"] == 1, "one answer, however many requests it served"
    assert coord.memory.issued["drops:1"]["left"] == ["1x penny"]
