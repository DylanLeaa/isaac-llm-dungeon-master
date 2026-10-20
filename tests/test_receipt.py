"""The engine's receipt: every decision and planner call is stored with its LLM call, and shown in the app."""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from dm_agent import LLMCallError  # noqa: E402
from receipt import Receipt, compact, load  # noqa: E402
from bridge import PROTO  # noqa: E402
from server import Coordinator  # noqa: E402
from test_coordinator import CampaignDM, ScriptedPlanner, _run_coord, clear_req, floor_req  # noqa: E402

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


def test_decisions_are_stored_with_their_call_and_what_they_did(tmp_path):
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
        await coord.handle({"t": "effect", "key": "clear:1:82", "effect": ["+1 key dropped"]}, send)
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
    ctx = first["request"]["ctx"]
    assert {k: v for k, v in ctx.items() if k != "economy"} == {"reason": "almost clear"} and "memory" in first["request"]
    assert "verdict" in ctx["economy"], "every request carries the floor's economy"
    assert first["decision"]["rewards"] == [{"reward": "key", "count": 1}] and first["raw"]["intent"] == "one key"
    assert "your_previous_answer" in second["request"]["ctx"]
    updates = [(r["ref"], r["status"]) for r in recs if r["type"] == "update"]
    assert updates == [(1, "replaced"), (2, "applied")]
    # What the applied answer did in the game, as the mod reported it.
    assert [(r["ref"], r["effect"]) for r in recs if r["type"] == "effect"] == [(2, ["+1 key dropped"])]
    assert fallback["status"] == "fallback" and "llm" not in fallback
    assert failed["status"] == "error" and failed["llm"]["finish"] == "length"

    # The game is never sent the receipt: the app shows it.
    assert not [m for m in sent if str(m.get("t")).startswith("receipt")]
    # The app's row: why it was asked, what the model meant, what was decided, what it did.
    shown = compact(load(coord.receipt.path).full[2])
    assert shown == {**shown, "what": "room_clear", "key": "clear:1:82", "status": "applied", "model": "claude-haiku-5.5",
                     "tok": [100, 80, 20], "cost": 0.001, "result": "1x key", "taunt": "Door's that way.",
                     "intent": "one key", "reason": "almost clear", "effect": ["+1 key dropped"]}
    assert "clamped" not in shown and "request" not in shown and "llm" not in shown
    totals = coord.receipt.totals()
    assert totals["calls"] == 3 and totals["applied"] == 1 and totals["replaced"] == 1
    assert totals["fallback"] == 1 and totals["error"] == 1 and abs(totals["cost"] - 0.003) < 1e-9


def test_planner_calls_are_on_the_receipt():
    dm, planner = CampaignDM(1), ScriptedPlanner()
    coord = Coordinator(dm, cruelty=6, planner=planner, receipt=Receipt(None, keep_full=True))

    async def send(msg):
        pass

    async def go():
        await coord.handle(floor_req(1, 1, "Basement I"), send)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    written = {e["what"]: compact(e) for e in coord.receipt.full.values() if e["status"] == "written"}
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
    assert again.receipt.path == first.receipt.path
    assert again.receipt.totals()["calls"] == 2 and again.receipt.seq == 2
    # Same seed, new run: a new file.
    third = Coordinator(dm, cruelty=6, receipt=Receipt(tmp_path))
    third.receipt.start_run("R", fresh=True)
    assert third.receipt.seq == 0 and third.receipt.totals()["calls"] == 0
    assert third.receipt.path != first.receipt.path


def _hello(session, run, new_run):
    return {"t": "hello", "proto": PROTO, "session": session, "run": run, "new_run": new_run}


def test_continued_run_reuses_its_plans_and_only_plans_new_floors(tmp_path):
    first_planner = ScriptedPlanner()
    first = Coordinator(CampaignDM(5), cruelty=6, planner=first_planner, receipt=Receipt(tmp_path))
    _run_coord(first, [_hello("S1", "R", True), floor_req(1, 1, "Basement I")])
    assert len(first_planner.narratives) == 1 and len(first_planner.briefs) == 1
    # Backend restarted, the game continued the run on the same floor, then went down a floor.
    planner, dm = ScriptedPlanner(), CampaignDM(5)
    again = Coordinator(dm, cruelty=6, planner=planner, receipt=Receipt(tmp_path))
    _run_coord(again, [_hello("S2", "R", False), floor_req(2, 1, "Basement I"), floor_req(3, 2, "Basement II")])
    assert planner.narratives == [], "the run's narrative is reused, not written again"
    reused = next(c for c in dm.campaigns if "BRIEF for Basement I" in c)
    assert "NARRATIVE for Isaac" in reused
    assert again.stats["narrative"]["reused"] == 1 and again.stats["floor_brief"]["reused"] == 1
    # Only the new floor gets a brief, written after the reused one.
    assert planner.briefs == [{"stage": 2, "narrative": "NARRATIVE for Isaac", "prev": "BRIEF for Basement I"}]
    assert "BRIEF for Basement II" in dm.campaigns[-1]
    # The same seed as a new run plans from scratch.
    planner = ScriptedPlanner()
    third = Coordinator(CampaignDM(5), cruelty=6, planner=planner, receipt=Receipt(tmp_path))
    _run_coord(third, [_hello("S3", "R", True), floor_req(1, 1, "Basement I")])
    assert len(planner.narratives) == 1 and len(planner.briefs) == 1


def test_continued_run_without_a_narrative_writes_one(tmp_path):
    class NoNarrative(ScriptedPlanner):
        async def narrative(self, state, cruelty):
            raise RuntimeError("boom")

    first = Coordinator(CampaignDM(5), cruelty=6, planner=NoNarrative(), receipt=Receipt(tmp_path))
    _run_coord(first, [_hello("S1", "R", True), floor_req(1, 1, "Basement I")])
    planner = ScriptedPlanner()
    again = Coordinator(CampaignDM(5), cruelty=6, planner=planner, receipt=Receipt(tmp_path))
    _run_coord(again, [_hello("S2", "R", False), floor_req(2, 1, "Basement I")])
    assert len(planner.narratives) == 1
    assert planner.briefs == [], "the floor's brief was written, so it is still reused"


def test_a_brief_from_before_floor_was_recorded_still_restores(tmp_path):
    (tmp_path / "20260101-000000_run-R.jsonl").write_text(json.dumps(
        {"type": "entry", "seq": 1, "run": "R", "what": "floor_brief", "status": "written",
         "key": "Caves II (4.1)", "text": "OLD BRIEF"}) + "\n", encoding="utf-8")
    rec = Receipt(tmp_path)
    rec.start_run("R", fresh=False)
    assert rec.saved_plans() == (None, ((4, 1), "Caves II", "OLD BRIEF"))


def test_listener_and_load_mirror_the_file(tmp_path):
    """What the app sees: events as the run is recorded, and the whole file read back."""
    answer = {"intent": "one key", "rewards": [{"reward": "key", "count": 1}], "taunt": "Door's that way."}
    coord = Coordinator(CallDM([answer]), cruelty=6, receipt=Receipt(tmp_path))
    events = []
    coord.receipt.listener = lambda event, data: events.append((event, dict(data)))

    async def send(msg):
        pass

    async def go():
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S", "run": "SEED 1", "new_run": True}, send)
        await coord.handle(clear_req(1), send)
        await asyncio.sleep(0.02)
        await coord.handle({"t": "applied", "key": "clear:1:82"}, send)
        await coord.handle({"t": "effect", "key": "clear:1:82", "effect": ["+1 key dropped"]}, send)
        await coord.handle({"t": "effect", "key": "nobody:1", "effect": ["lost"]}, send)   # never asked: ignored

    asyncio.run(go())
    assert [e[0] for e in events] == ["run", "entry", "update", "drawn", "effect"]
    assert events[3][1]["outcome"] == {"rewards": answer["rewards"]} and events[3][1]["ref"] == 1
    assert events[4][1] == {"seq": 1, "effect": ["+1 key dropped"]}
    assert events[0][1]["run"] == "SEED 1" and events[0][1]["fresh"] and events[0][1]["path"] == coord.receipt.path
    assert events[1][1]["what"] == "room_clear"
    assert events[2][1]["seq"] == 1 and events[2][1]["status"] == "applied" and events[2][1]["ts"] > 0

    r = load(coord.receipt.path)
    assert r.run_id == "SEED 1" and r.path is None and r.source == coord.receipt.path
    assert r.full[1]["status"] == "applied" and r.full[1]["decision"]["rewards"] == answer["rewards"]
    assert r.full[1]["applied_ts"] == events[2][1]["ts"] >= r.full[1]["ts"]
    assert compact(r.full[1])["applied_time"] == time.strftime("%H:%M:%S", time.localtime(events[2][1]["ts"]))
    assert r.full[1]["effect"] == ["+1 key dropped"]
    assert r.prompt_text == {h: t for h, t in zip(r.full[1]["llm"]["system"], ["PERSONA", "CAMPAIGN"])}
    assert r.totals() == coord.receipt.totals()
    assert r.run_summary() is None


def test_run_summary_and_plans_from_a_receipt(tmp_path):
    rec = Receipt(tmp_path, keep_full=True)
    rec.start_run("R", fresh=True)
    rec.add("narrative", "written", text="TITLE: The Long Con\nACT 1: ...")
    rec.add("floor_brief", "written", key="Basement I", text="BRIEF 1")
    rec.add("floor_brief", "discarded", key="Basement I", text="NOT THIS")
    rec.add("run_summary", "death", summary={"outcome": "death", "sections": [], "epitaph": None})
    rec.add("epitaph", "written", result="Here lies Isaac.")
    for r in (rec, load(rec.path)):
        assert r.plans() == ("TITLE: The Long Con\nACT 1: ...", "BRIEF 1")
        assert r.run_summary() == {"outcome": "death", "sections": [], "epitaph": "Here lies Isaac."}


def test_effects_accumulate():
    """A ranking or consumables plan is used gradually: each use adds its line."""
    rec = Receipt(None, keep_full=True)
    seq = rec.add("item_choice", "ok", decision={"intent": "bait", "order": [1]})
    rec.update(seq, "applied")
    for i in range(3):
        assert rec.add_effect(seq, [f"item offered: #{i}"])
    assert not rec.add_effect(seq, ["", None, {"x": 1}])               # nothing usable: no record
    assert not rec.add_effect(99, ["+1 key dropped"])                   # no such entry
    assert compact(rec.full[seq])["effect"] == ["item offered: #0", "item offered: #1", "item offered: #2"]


def test_the_app_row_shows_what_the_guardrails_changed():
    raw = {"intent": "swarm", "champion_chance": 0.6, "champion_color": "red",
           "extra_enemies": [{"enemy": "gaper", "count": 3}], "layout_pickups": "keep", "taunt": "",
           "tinted_rocks": 0}
    decision = {"intent": "swarm", "champion_chance": 0.18, "champion_color": "red",
                "extra_enemies": [{"key": "gaper", "count": 1}], "tinted_rocks": 0, "layout_pickups": "keep",
                "bonus_pickups": []}
    row = compact({"seq": 1, "what": "room_layout", "status": "ok", "raw": raw, "decision": decision,
                   "request": {"ctx": {"reason": "player is next to this room"}}})
    assert row["clamped"] == ["champion chance 60% -> 18%", "extra enemies 3x gaper -> 1x gaper"]
    assert row["reason"] == "player is next to this room"
