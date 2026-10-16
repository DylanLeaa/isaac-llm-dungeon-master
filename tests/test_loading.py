"""The loading screen (dm/loading.lua) on the fake Isaac API, with a scripted bridge and scheduler."""
from pathlib import Path

from lupa.lua53 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mod" / "llm_dungeon_master"

STUBS = """
local Bridge = { seen_ = false, ready_ = false }
function Bridge.seen() return Bridge.seen_ end
function Bridge.ready() return Bridge.ready_ end
function Bridge.holdPending() end
function Bridge.prepare() end
function Bridge.problem() return nil end
function Bridge.settled() return true end
local Sched = { floor_ = 0 }
function Sched.floor() return Sched.floor_ end
function Sched.floorReadyKeys() return {} end
local State = { addPausedFrames = function() end, snapshot = function() return {} end }
local Taunt = { show = function() end }
return Bridge, Sched, State, Taunt
"""


def load():
    lua = LuaRuntime(unpack_returned_tuples=True)
    W = lua.execute((ROOT / "tests" / "fake_isaac.lua").read_text(encoding="utf-8"))
    Loading = lua.execute((MOD / "dm" / "loading.lua").read_text(encoding="utf-8"))
    Bridge, Sched, State, Taunt = lua.execute(STUBS)
    cfg = lua.table_from({"LOADING_SCREEN": True, "LOADING_CONNECT_MS": 3000, "LOADING_TIMEOUT_MS": 90000,
                          "LOADING_FLOOR_TIMEOUT_MS": 60000})
    Loading.init(cfg, Bridge, State, Taunt, Sched)

    def frames(n, ms=33):
        for _ in range(n):
            W.now += ms
            W.frame += 1
            Loading.update()

    return Loading, Bridge, Sched, frames


def test_a_backend_started_after_the_game_still_gets_the_first_floors_screen():
    Loading, Bridge, Sched, frames = load()
    Loading.start()                            # new run, before this session's hello was answered
    Sched.floor_ = 1
    frames(5)
    assert not Loading.active(), "no backend seen yet: no screen"
    Bridge.seen_ = Bridge.ready_ = True         # the welcome lands
    frames(1)
    assert Loading.active(), "the screen goes up as soon as the backend answers"
    Loading.handle({"status": "ready", "floor": 1})
    frames(1)
    assert not Loading.active()


def test_no_backend_means_no_screen():
    Loading, Bridge, Sched, frames = load()
    Loading.start()
    Sched.floor_ = 1
    frames(120)                                # past LOADING_CONNECT_MS
    Bridge.seen_ = Bridge.ready_ = True         # a backend that turns up much later waits for the next floor
    frames(1)
    assert not Loading.active()
    Sched.floor_ = 2
    frames(1)
    assert Loading.active()


def test_a_backend_already_seen_raises_the_screen_at_once():
    Loading, Bridge, Sched, frames = load()
    Bridge.seen_ = True
    Loading.start()
    assert Loading.active()
