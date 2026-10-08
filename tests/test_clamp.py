"""Guardrail tests: whatever the model says, clamp() must return something legal and fair."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from schemas import clamp  # noqa: E402


def st(hp_red=6, soul=0, keys=1, bombs=1, coins=5, locked=0, bomb_targets=0):
    return {"player": {"hp": {"red": hp_red, "soul": soul}, "resources": {"keys": keys, "bombs": bombs, "coins": coins}},
            "level": {"needs": {"locked_doors_seen": locked, "bomb_targets": bomb_targets}}}


def test_layout_budget_and_unknown_enemy():
    raw = {"champion_chance": 5, "extra_enemies": [{"enemy": "boom_fly", "count": 3}, {"enemy": "mega_satan", "count": 3},
                                                   {"enemy": "host", "count": 3}],
           "tinted_rocks": 9, "layout_pickups": "troll_bombs", "bonus_pickups": ["troll_bomb", "dime", "nope"],
           "taunt": "Hello\nthere ☃ friend " + "x" * 100}
    out = clamp("room_layout", raw, st(), {}, cruelty=6, counters={})
    assert out["champion_chance"] <= 0.36
    assert sum(e["count"] for e in out["extra_enemies"]) <= 2          # round(6/3.3) = 2
    assert all(e["etype"].startswith("ENTITY_") for e in out["extra_enemies"])
    assert out["tinted_rocks"] == 2
    assert out["layout_pickups"] == "troll_bombs"
    assert out["bonus_pickups"] == ["dime"]                           # 2nd troll bomb over budget (1 + 6//4 = 2, one used)
    assert len(out["taunt"]) <= 70 and out["taunt"].isascii() and "\n" not in out["taunt"]


def test_mercy_blocks_trolls_and_caps_enemies():
    raw = {"champion_chance": 0.6, "extra_enemies": [{"enemy": "knight", "count": 3}], "tinted_rocks": 0,
           "layout_pickups": "troll_bombs", "taunt": ""}
    out = clamp("room_layout", raw, st(hp_red=1), {}, cruelty=10, counters={})
    assert out["layout_pickups"] == "to_pennies"
    assert out["champion_chance"] <= 0.10
    assert sum(e["count"] for e in out["extra_enemies"]) <= 1


def test_clear_drought_breaker_and_caps():
    raw = {"rewards": [{"reward": "penny", "count": 3}, {"reward": "golden_key", "count": 3},
                       {"reward": "troll_bomb", "count": 3}], "taunt": "lol"}
    out = clamp("room_clear", raw, st(keys=0, locked=2), {}, cruelty=8, counters={"clears_since_key": 0})
    total = sum(r["count"] for r in out["rewards"])
    assert total <= 4
    assert {"reward": "golden_key", "count": 1} in out["rewards"]
    # starved for keys long enough -> engine adds a key
    out2 = clamp("room_clear", {"rewards": [], "taunt": ""}, st(keys=0, locked=2), {}, cruelty=10,
                 counters={"clears_since_key": 7})
    assert {"reward": "key", "count": 1} in out2["rewards"]


def test_item_order_subset_and_complete():
    ctx = {"candidates": [{"id": 1}, {"id": 2}, {"id": 3}]}
    out = clamp("item_choice", {"order": [3, 99, "x", 3], "taunt": ""}, st(), ctx, cruelty=5, counters={})
    assert out["order"] == [3, 1, 2]


def test_floor_plan_curse_limits():
    raw = {"add_curses": ["darkness", "labyrinth", "unknown", "blind"], "treasure_distance": "moon", "taunt": ""}
    assert clamp("floor_plan", raw, st(), {}, cruelty=2, counters={})["add_curses"] == []
    assert clamp("floor_plan", raw, st(), {}, cruelty=5, counters={})["add_curses"] == ["darkness"]
    out = clamp("floor_plan", raw, st(), {}, cruelty=9, counters={})
    assert out["add_curses"] == ["darkness", "unknown"] and out["treasure_distance"] == "any"
    assert "unknown" not in clamp("floor_plan", raw, st(hp_red=1), {}, cruelty=9, counters={})["add_curses"]



def test_champion_color_and_boss_bonus_cap():
    out = clamp("room_layout", {"champion_color": "tiny", "taunt": ""}, st(), {}, cruelty=6, counters={})
    assert out["champion_color"] == "tiny" and out["champion_enum"] == "CHAMPION_TINY"
    out = clamp("room_layout", {"champion_color": "rainbow", "taunt": ""}, st(), {}, cruelty=6, counters={})
    assert out["champion_color"] == "random" and "champion_enum" not in out
    raw = {"rewards": [{"reward": "dime", "count": 3}, {"reward": "soul_heart", "count": 3}], "taunt": ""}
    bonus = clamp("room_clear", raw, st(), {"mode": "bonus"}, cruelty=6, counters={})
    assert sum(r["count"] for r in bonus["rewards"]) == 2      # the boss room keeps its own award too


def test_consumables_filter_and_bad_pill_cap():
    ctx = {"trinket_candidates": [{"id": 4}, {"id": 9}],
           "pill_colors_to_assign": [{"color": 1}, {"color": 2}, {"color": 3}],
           "pills_locked": [{"color": 7, "effect": "bad_trip"}]}
    raw = {"cards": ["tower", "suicide_king", "rune_jera"], "trinkets": [9, 99, 9, "x", 4],
           "pills": [{"color": 1, "effect": "health_down"}, {"color": 2, "effect": "amnesia"},
                     {"color": 3, "effect": "health_up"}, {"color": 8, "effect": "full_health"},
                     {"color": 1, "effect": "luck_up"}], "taunt": ""}
    out = clamp("consumables", raw, st(), ctx, cruelty=0, counters={})
    assert [c["key"] for c in out["cards"]] == ["tower", "rune_jera"]
    assert out["cards"][1] == {"key": "rune_jera", "enum": "RUNE_JERA", "cat": "rune"}
    assert out["trinkets"] == [9, 4]
    pills = {p["color"]: p for p in out["pills"]}
    assert set(pills) == {1, 2, 3}                              # colour 8 isn't open, colour 1 only once
    # cruelty 0: round(4 colours * 0.4) = 2 bad in the run; the locked Bad Trip uses one
    assert pills[1]["effect"] == "health_down" and pills[1]["phd"] == "PILLEFFECT_HEALTH_UP"
    assert pills[2]["effect"] == "see_forever"                  # over the cap -> its good counterpart
    assert pills[3] == {"color": 3, "effect": "health_up", "enum": "PILLEFFECT_HEALTH_UP",
                        "false_phd": "PILLEFFECT_HEALTH_DOWN"}
    cruel = clamp("consumables", raw, st(), ctx, cruelty=10, counters={})
    assert {p["color"]: p["effect"] for p in cruel["pills"]}[2] == "amnesia"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
