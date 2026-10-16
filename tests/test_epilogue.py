"""The after-death stats screen: the DM's side is tallied from what the game used, the epitaph follows."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import epilogue  # noqa: E402
from receipt import Receipt  # noqa: E402
from bridge import PROTO  # noqa: E402
from server import Coordinator  # noqa: E402
from test_coordinator import clear_req  # noqa: E402

GAME = {"run": "SEED 1", "character": "Cain",
        "rows": [["Reached", "Caves II"], ["Time", "14:02"], ["Damage taken", "6 hearts"]],
        "pills": [{"effect": "bad_trip", "by": "dm"}, {"effect": "health_up", "by": "dm"},
                  {"effect": "range_down", "by": "vanilla"}]}


class EpitaphDM:
    def __init__(self, answers, epitaph):
        self.answers, self.epitaph_answer, self.prompts = list(answers), epitaph, []

    def set_cruelty(self, cruelty):
        pass

    async def decide(self, kind, state, ctx, memory):
        usage = {"in": 100, "cached": 80, "out": 20, "cost": 0.001}
        return {**self.answers.pop(0), "_usage": usage, "_call": {"model": "m", "usage": usage}}

    async def epitaph(self, prompt):
        self.prompts.append(json.loads(prompt))
        if isinstance(self.epitaph_answer, Exception):
            raise self.epitaph_answer
        return self.epitaph_answer, {"model": "m", "usage": {"in": 50, "cached": 40, "out": 30, "cost": 0.002}}


def _play(coord, sent):
    async def send(msg):
        sent.append(msg)

    async def go():
        await coord.handle({"t": "hello", "proto": PROTO, "session": "S", "run": "SEED 1", "new_run": True}, send)
        await coord.handle(clear_req(1), send)
        await asyncio.sleep(0.02)
        await coord.handle({"t": "applied", "key": "clear:1:82"}, send)
        await coord.handle(clear_req(2), send)               # answered but never used: not counted
        await asyncio.sleep(0.02)
        await coord.handle({"t": "event", "name": "fallback",
                            "data": {"kind": "room_layout", "key": "layout:1:9", "asked": True}}, send)
        await coord.handle({"t": "event", "name": "death", "data": GAME}, send)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    return [m for m in sent if m["t"] == "run_summary"]


def test_death_gets_dm_stats_then_epitaph(tmp_path):
    used = {"intent": "one key", "rewards": [{"reward": "key", "count": 1}, {"reward": "penny", "count": 2}],
            "taunt": "Door's that way.", "scheme": "starve bombs, flood keys"}
    unused = {"intent": "bombs", "rewards": [{"reward": "bomb", "count": 1}], "taunt": "Never shown."}
    dm = EpitaphDM([used, unused], '"Here lies Cain, who trusted a rabbit\'s foot."')
    coord = Coordinator(dm, cruelty=6, receipt=Receipt(tmp_path))
    first, second = _play(coord, [])

    assert first["session"] == "S" and first["run"] == "SEED 1" and first["outcome"] == "death"
    assert first["epitaph_pending"] is True and "epitaph" not in first, "no nulls go to the game"
    you, them = first["sections"]
    assert you == {"title": "YOU", "rows": GAME["rows"]}
    dm_rows = dict(map(tuple, them["rows"]))
    assert dm_rows["Keys / bombs"] == "1 / 0", "only the applied answer counts"
    assert dm_rows["Coins / hearts"] == "2 / 0"
    assert dm_rows["Bad pills"] == "1", "the DM's bad pills, not vanilla's"
    assert dm_rows["DM gave up"] == "1"
    assert dm_rows["LLM bill"] == "2 calls $0.00"
    assert first["taunt"] == "Door's that way." and first["scheme"] == "starve bombs, flood keys"

    assert second["epitaph"] == "Here lies Cain, who trusted a rabbit's foot." and second["epitaph_pending"] is False
    assert "Caves II" in dm.prompts[0]["run"] and "died" in dm.prompts[0]["instructions"]
    assert coord.usage["calls"] == 3 and coord.stats["epitaph"]["ok"] == 1

    recs = [json.loads(line) for line in coord.receipt.path.read_text(encoding="utf-8").splitlines()]
    entries = {r["what"]: r for r in recs if r["type"] == "entry"}
    assert entries["run_summary"]["status"] == "death" and entries["run_summary"]["summary"]["sections"]
    assert entries["run_summary"]["result"] == "death, reached Caves II, time 14:02"
    assert entries["epitaph"]["status"] == "written" and entries["epitaph"]["llm"]["usage"]["out"] == 30


def test_failed_epitaph_still_closes_the_screen(tmp_path):
    used = {"intent": "one key", "rewards": [{"reward": "key", "count": 1}], "taunt": ""}
    coord = Coordinator(EpitaphDM([used, used], TimeoutError("slow")), cruelty=6, receipt=Receipt(tmp_path))
    first, second = _play(coord, [])
    assert first["epitaph_pending"] is True
    assert second["epitaph_pending"] is False and "epitaph" not in second and "taunt" not in second
    assert coord.stats["epitaph"]["error"] == 1


def test_dm_without_epitaph_sends_one_summary(tmp_path):
    used = {"intent": "one key", "rewards": [{"reward": "key", "count": 1}], "taunt": "Hi."}
    coord = Coordinator(EpitaphDM([used, used], "x"), cruelty=6, receipt=Receipt(tmp_path), epitaph=False)
    (only,) = _play(coord, [])
    assert only["epitaph_pending"] is False


def test_tallies_and_title():
    applied = [
        ("room_layout", {"extra_enemies": [{"key": "gaper", "count": 2}, {"key": "fly", "count": 1}],
                         "layout_pickups": "troll_bombs", "bonus_pickups": ["troll_bomb", "key"]}),
        ("room_clear", {"rewards": [{"reward": "troll_bomb", "count": 1}, {"reward": "half_heart", "count": 2}]}),
        ("floor_plan", {"add_curses": ["darkness", "lost"]}),
        ("floor_plan", {"add_curses": ["darkness"], "taunt": "Lights out."}),
    ]
    rows, scheme, taunt = epilogue.dm_side(applied, {"fallback": 3, "calls": 40, "cost": 0.4321}, None)
    rows = dict(map(tuple, rows))
    assert rows["Rooms dressed"] == "1" and rows["Extra enemies"] == "3"
    assert rows["Troll bombs"] == "3" and rows["Coins / hearts"] == "0 / 2"
    assert rows["Curses"] == "darkness, lost" and rows["LLM bill"] == "40 calls $0.43"
    assert taunt == "Lights out." and scheme is None
    assert epilogue.run_title("PREMISE: x\nTITLE: Cain and the Cursed Rabbit's Foot\n") == \
        "Cain and the Cursed Rabbit's Foot"
    assert epilogue.run_title(None) is None
