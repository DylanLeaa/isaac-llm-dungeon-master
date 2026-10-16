"""The on-screen effect list (dm/effecthud.lua) on the fake Isaac API: what each effect line shows, and where."""
from pathlib import Path
from types import SimpleNamespace

from lupa.lua53 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mod" / "llm_dungeon_master"
SCREEN_W, SCREEN_H = 480, 270           # the fake's screen


def load(**cfg):
    lua = LuaRuntime(unpack_returned_tuples=True)
    W = lua.execute((ROOT / "tests" / "fake_isaac.lua").read_text(encoding="utf-8"))
    Apply = lua.execute((MOD / "dm" / "apply.lua").read_text(encoding="utf-8"))
    Hud = lua.execute((MOD / "dm" / "effecthud.lua").read_text(encoding="utf-8"))
    Hud.init(lua.table_from(cfg), Apply)
    t = SimpleNamespace(lua=lua, W=W, Apply=Apply, Hud=Hud)

    def shown(line):
        toks = Hud.parse(line)
        return [(toks[i].text, toks[i].icon) for i in range(1, len(toks) + 1)]

    def push(*lines):
        Hud.push(lua.table_from(list(lines)))

    def frames(n, ms=33, hidden=False):
        """n rendered frames; what they drew."""
        W.drawn = lua.table()
        for _ in range(n):
            W.now += ms
            Hud.render(hidden)
        return [dict(W.drawn[i]) for i in range(1, len(W.drawn) + 1)]

    t.shown, t.push, t.frames = shown, push, frames
    return t


def test_counted_things_get_their_icon_and_count():
    t = load()
    count, shown = t.Apply.count, t.shown
    assert shown(count("+", 1, "bomb") + " dropped") == [("+1", "bomb")]
    assert shown(count("-", 2, "penny") + " (would have dropped, not replaced)") == [("-2", "penny")]
    assert shown(count("+", 2, "attack_fly") + " spawned") == [("+2", "attack fly")]
    assert shown(count("-", 1, "golden chest") + " (dropped as the room cleared, not replaced)") == \
        [("-1", "locked chest")]
    assert shown(count("+", 1, "grab_bag") + " placed") == [("+1", "grab bag")]
    assert shown("Slot Machine use 2: " + count("+", 3, "coin") + " (scheduled)") == [("+3", "penny")]
    # a line that trades one thing for another: both, on the one row
    assert shown("-2 pennies -> +2 bombs (layout pickups)") == [("-2", "penny"), ("+2", "bomb")]
    assert shown("-1 key, -2 hearts (layout pickups removed)") == [("-1", "key"), ("-2", "red heart")]


def test_things_without_an_icon_keep_their_name():
    t = load()
    shown = t.shown
    assert shown("+2 champions (red, 15% of 6)") == [("+2 champions", None)]
    assert shown(t.Apply.count("+", 1, "tinted_rock") + " (not enough rocks)") == [("+1 tinted rock", None)]
    assert shown("+curse of darkness") == [("+curse of darkness", None)]
    assert shown("-curse of lost (the game's, stripped)") == [("-curse of lost", None)]
    t.W.missingSprites["gfx/217.000_dip.anm2"] = True     # an animation file the game doesn't have
    assert shown(t.Apply.count("+", 1, "dip") + " spawned") == [("+1 dip", None)]


def test_lines_with_nothing_to_count_are_left_to_the_receipt():
    t = load()
    for line in ("nothing dropped", "no change", "item offered: Bum Friend (q1)", "card handed out: tower",
                 "pill blue-blue = speed up", "+0 champions (red, 15% of 6)", "1 tinted rocks hidden",
                 "not applied: the player left the room", "the DM never answered: 2 swallowed drop(s) returned",
                 "treasure room far: ignored (layout rerolls are off)", "no curses",
                 "Beggar use 1: nothing (scheduled)"):
        assert t.shown(line) == [], line


def test_rows_stack_up_from_above_the_pocket_item_then_fade():
    t = load()
    t.push("+1 bomb dropped", "+1 key dropped", "nothing dropped")
    bomb, key = t.frames(1)
    assert (bomb["file"], key["file"]) == ("gfx/005.041_bomb.anm2", "gfx/005.031_key.anm2")
    assert bomb["y"] < key["y"], "one line each, the newest at the bottom"
    assert key["y"] <= SCREEN_H - 48 and bomb["x"] <= SCREEN_W - 14, "clear of the pocket item in the corner"
    assert bomb["a"] < 1, "fading in"
    assert t.frames(60)[-1]["a"] == 1, "then fully shown"
    assert t.frames(100)[-1]["a"] < 0.5, "fading out after ~4 s"
    assert t.frames(30) == [], "and gone"


def test_hud_offset_moves_it_in_from_the_corner():
    t = load()
    t.push("+1 bomb dropped")
    plain = t.frames(1)[0]
    t.lua.globals().Options.HUDOffset = 1
    moved = t.frames(1)[0]
    assert moved["x"] < plain["x"] and moved["y"] < plain["y"]


def test_the_clock_waits_while_the_screen_is_covered():
    t = load()
    t.push("+1 bomb dropped")
    assert t.frames(300, hidden=True) == [], "10 s under the loading screen: not drawn"
    assert len(t.frames(1)) == 1, "and still there to be seen once it's gone"
    t.W.paused = True
    assert t.frames(5) == [], "not drawn over the pause menu"


def test_six_rows_at_once_the_rest_wait_their_turn():
    t = load()
    t.push(*[f"+{n} bombs dropped" for n in range(2, 12)])
    assert len(t.frames(1)) == 6
    t.frames(155)                                   # the first six fade out...
    assert len(t.frames(1)) == 4, "...and the other four come in"


def test_a_new_run_starts_empty():
    t = load()
    t.push("+1 bomb dropped")
    t.Hud.clear()
    assert t.frames(5) == []


def test_off_in_the_config_shows_nothing():
    t = load(SHOW_EFFECTS=False)
    t.push("+1 bomb dropped")
    assert t.frames(5) == []
