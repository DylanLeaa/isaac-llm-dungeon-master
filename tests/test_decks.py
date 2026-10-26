"""The deck store (dm/decks.lua) on the fake Isaac API, with a scripted bridge: draws, IOUs, refills,
sticky entries, stale-tail rewrites and the usable filter."""
from pathlib import Path

from lupa.lua53 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mod" / "llm_dungeon_master"

STUBS = """
local Bridge = { ready_ = true, results = {}, pending = {}, sent = {}, draws = {}, abandoned = {}, nextId = 0 }
function Bridge.ready() return Bridge.ready_ end
function Bridge.peek(key) return Bridge.results[key] end
function Bridge.isPending(key) return Bridge.pending[key] ~= nil end
function Bridge.request(kind, key, state, ctx, deadline)
  Bridge.nextId = Bridge.nextId + 1
  Bridge.pending[key] = Bridge.nextId
  Bridge.sent[#Bridge.sent + 1] = { kind = kind, key = key, ctx = ctx, id = Bridge.nextId }
  return Bridge.nextId
end
-- The backend answers the last request for key with decision (base: a reused answer's drawn_base).
function Bridge.answer(key, decision, base)
  Bridge.unasked = (Bridge.unasked or 1000) + 1
  local id = Bridge.pending[key] or Bridge.unasked      -- an answer nobody asked for here: its own id
  Bridge.pending[key] = nil
  Bridge.results[key] = { id = id, decision = decision, base = base }
end
function Bridge.drawn(key, id, outcome, cause) Bridge.draws[#Bridge.draws + 1] = { key = key, id = id, outcome = outcome, cause = cause } end
function Bridge.event() end
function Bridge.abandon(key) Bridge.abandoned[#Bridge.abandoned + 1] = key end
function Bridge.forget(key) Bridge.results[key], Bridge.pending[key] = nil, nil end
local State = { hash = "h1" }
function State.snapshot() return {} end
function State.materialHash() return State.hash end
local Loading = { holds = {} }
function Loading.hold(key, spec) Loading.holds[key] = spec end
local Taunt = { shown = {} }
function Taunt.show(t) Taunt.shown[#Taunt.shown + 1] = t end
return Bridge, State, Loading, Taunt
"""

DECK = """
local Decks, wants = ...
local function copy(d) local out = {} for i, e in ipairs(d.cards or {}) do out[i] = e end return out end
Decks.define("cards", {
  kind = "cards", key = function(f) return "cards:" .. f end,
  entries = copy,
  usable = function(c, roll)
    if c.locked then return "drop" end
    if roll.onlyRunes then return c.cat == "rune" end
    return true
  end,
  wanted = function() return 2 end,
  wants = function() return wants end,
  refresh = { max = 1, minMs = 0 },
  owedCtx = function(owed) local out = {} for i, o in ipairs(owed) do out[i] = { runes_only = o.roll.onlyRunes or false } end return out end,
  label = function(c) return c.key end,
  outcome = function(c) return { card = c.key } end,
})
Decks.define("pills", {
  kind = "pills", key = function(f) return "pills:" .. f end, sticky = true, deck = false,
  entries = function(d) return d.pills end,
  usable = function(e, roll) return e.color == roll.color end,
  outcome = function(e) return { pill = e.color } end,
})
"""


def load(wants=None):
    lua = LuaRuntime(unpack_returned_tuples=True)
    W = lua.execute((ROOT / "tests" / "fake_isaac.lua").read_text(encoding="utf-8"))
    Decks = lua.execute((MOD / "dm" / "decks.lua").read_text(encoding="utf-8"))
    Bridge, State, Loading, Taunt = lua.execute(STUBS)
    Decks.init(lua.table_from({}), Bridge, State, Loading, Taunt)
    define = lua.execute("return function(...) " + DECK + " end")
    define(Decks, lua.table_from([lua.table_from(w) for w in (wants or [])]))

    def t(*items, **kw):
        return lua.table_from([lua.table_from(i) for i in items]) if items else lua.table_from(kw)

    def update():
        W.now += 200
        Decks.update()

    return lua, W, Decks, Bridge, State, Loading, Taunt, t, update


def cards(lua, *keys, cat="tarot"):
    return lua.table_from({"cards": lua.table_from([lua.table_from({"key": k, "cat": "rune" if k.startswith("rune") else cat})
                                                   for k in keys]), "taunt": "first!"})


def test_draw_then_empty_owes_and_the_refill_settles_it():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load()
    Bridge.answer("cards:1", cards(lua, "tower"))
    c, _ = Decks.draw("cards", 1, t(cause="pedestal"))
    assert c.key == "tower" and Bridge.draws[1].outcome.card == "tower" and Bridge.draws[1].id == 1001
    assert list(Taunt.shown.values()) == ["first!"], "the taunt shows on the answer's first draw"
    got = []
    assert Decks.draw("cards", 1, t())[0] is None, "used up"
    assert Decks.owe("cards", 1, t(onlyRunes=True), lambda e, gave: got.append((e and e.key, gave)))
    assert Loading.holds["cards:1"] is None
    update()                                       # held from the update, never inside the game's callback
    hold = Loading.holds["cards:1"]
    req = Bridge.sent[len(Bridge.sent)]
    assert req.key == "cards:1" and req.ctx.owed[1].runes_only is True and req.ctx.wanted == 2
    assert hold.stale == 1001, "the answer already in had nothing for it: wait for a newer one"
    Bridge.answer("cards:1", cards(lua, "fool", "rune_hagalaz"))
    hold.apply()
    assert got == [("rune_hagalaz", False)], "the IOU takes the first entry that fits its draw"
    assert Decks.left("cards", 1) == 1 and Bridge.draws[len(Bridge.draws)].outcome.card == "rune_hagalaz"


def test_an_iou_that_gives_up_leaves_the_game_its_own():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load()
    got = []
    Decks.owe("cards", 1, t(), lambda e, gave: got.append((e, gave)))
    Decks.holdNow("cards", 1)
    Loading.holds["cards:1"].giveUp()
    assert got == [(None, True)]


def test_sticky_entries_are_looked_up_not_used_up():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load()
    Bridge.answer("pills:1", lua.table_from({"pills": t({"color": 3, "effect": "a"}, {"color": 5, "effect": "b"})}))
    for _ in range(2):
        e, _ = Decks.draw("pills", 1, t(color=5))
        assert e.effect == "b"
    assert Decks.left("pills", 1) == 2 and len(Bridge.draws) == 2
    assert Decks.peek("pills", 1, t(color=4))[0] is None


def test_the_usable_filter_skips_or_drops():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load()
    d = cards(lua, "tower", "fool", "rune_jera")
    d.cards[2].locked = True                       # never usable (still locked): discarded
    Bridge.answer("cards:1", d)
    e, _ = Decks.draw("cards", 1, t(onlyRunes=True))
    assert e.key == "rune_jera", "a rune-only draw skips the cards that don't fit"
    assert [Decks.get("cards", 1).entries[i].key for i in (1,)] == ["tower"] and Decks.left("cards", 1) == 1


def test_restock_and_a_stale_tail_rewrite_within_its_budget():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load(wants=[{"arg": 1, "n": 1, "reason": "restock"}])
    update()
    assert Bridge.sent[1].ctx.reason == "restock", "an empty wanted deck is asked for"
    update()
    assert len(Bridge.sent) == 1, "not twice while it is on its way"
    Bridge.answer("cards:1", cards(lua, "tower", "fool"))
    update()
    assert len(Bridge.sent) == 1, "stocked, and written against the state as it is"
    State.hash = "h2"                              # the player got hit: the unused tail is stale
    update()
    assert len(Bridge.sent) == 2 and Bridge.sent[2].ctx.reason.startswith("refresh")
    assert list(Bridge.sent[2].ctx.deck_left.values()) == ["tower", "fool"]
    Bridge.answer("cards:1", cards(lua, "sun", "moon"))
    State.hash = "h3"
    update()
    assert len(Bridge.sent) == 2, "refresh.max rewrites per restock"
    Decks.markStale(lua.table_from(["cards:1"]))   # the backend: another deck spent the budget
    update()
    assert len(Bridge.sent) == 2, "...a stale push is a rewrite too"
    Decks.draw("cards", 1, t())
    Decks.draw("cards", 1, t())
    update()
    assert len(Bridge.sent) == 3 and Bridge.sent[3].ctx.reason == "restock", "used up: restocked"


def test_an_answer_skips_what_was_drawn_since_it_was_asked():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load()
    Bridge.answer("cards:1", cards(lua, "tower", "fool", "sun"))
    Decks.request("cards", 1, "refresh")           # asked with [tower, fool, sun] left...
    Decks.draw("cards", 1, t())                    # ...then tower is handed out before the answer lands
    Bridge.answer("cards:1", cards(lua, "tower", "moon", "star"))
    assert Decks.peek("cards", 1, t())[0].key == "moon", "the answer's first place was already drawn"
    # A reused answer (the backend's gate kept it) is what was left when it was sent: base = draws counted.
    Decks.request("cards", 1, "refresh")
    Bridge.answer("cards:1", cards(lua, "moon", "star"), 1)
    assert Decks.peek("cards", 1, t())[0].key == "moon"


def test_abandon_tells_the_backend_only_about_an_unused_answer():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load()
    Bridge.answer("cards:1", cards(lua, "tower"))
    Decks.sync("cards", 1)
    Decks.abandon("cards", 1)
    assert list(Bridge.abandoned.values()) == ["cards:1"] and Bridge.peek("cards:1") is None
    Bridge.answer("cards:2", cards(lua, "tower"))
    Decks.draw("cards", 2, t())
    Decks.abandon("cards", 2)
    assert list(Bridge.abandoned.values()) == ["cards:1"], "a deck already drawn from was used, not abandoned"


def push(lua, Bridge, key, decision, wire, base):
    """The backend restacked `key` unasked (agent mode): a result with a local id newer than any request."""
    Bridge.unasked = (Bridge.unasked or 1000) + 1
    Bridge.results[key] = lua.table_from({"id": Bridge.unasked, "wire": wire, "decision": decision, "base": base,
                                          "pushed": True})


def test_a_pushed_deck_replaces_the_tail_and_its_draws_carry_the_pushed_id():
    lua, W, Decks, Bridge, State, Loading, Taunt, t, update = load(wants=[{"arg": 1, "n": 1}])
    Bridge.answer("cards:1", cards(lua, "tower", "fool", "sun"))
    Decks.draw("cards", 1, t())                    # tower: the backend has counted it...
    Decks.draw("cards", 1, t())                    # ...but not fool, drawn while the push was on its way
    push(lua, Bridge, "cards:1", cards(lua, "moon", "star", "world"), -1, 1)
    c, _ = Decks.draw("cards", 1, t())
    assert c.key == "star", "the push's first entry stands for the draw it hadn't seen"
    assert Bridge.draws[len(Bridge.draws)].id == -1, "reported under the backend's id for the push"
    update()
    assert len(Bridge.sent) == 0, "written against the state as it is: no refresh"
    State.hash = "h2"
    update()
    assert len(Bridge.sent) == 1 and Bridge.sent[1].ctx.reason.startswith("refresh"), "a later move still rewrites it"


def test_the_bridge_gives_a_push_a_local_id_so_only_a_later_request_replaces_it():
    lua = LuaRuntime(unpack_returned_tuples=True)
    W = lua.execute((ROOT / "tests" / "fake_isaac.lua").read_text(encoding="utf-8"))
    lua.execute("package.preload['json'] = function() return { encode = function() return '{}' end } end")
    Bridge = lua.execute((MOD / "dm" / "bridge.lua").read_text(encoding="utf-8"))
    inbox = lua.table_from({"boot": 1, "msgs": lua.table_from([])})
    store = lua.eval("function(inbox) return { save = function() end, poll = function() return inbox end } end")(inbox)
    Bridge.init(lua.table_from({"POLL_MS": 0}), store, "R")      # session "R-0": the fake clock starts at 0
    msgs = []

    def deliver(**msg):
        msgs.append(lua.table_from({"session": "R-0", **msg}))
        inbox.msgs = lua.table_from([lua.table_from([i + 1, m]) for i, m in enumerate(msgs)])
        W.now += 100
        Bridge.update()

    deliver(t="welcome", proto=3)
    assert Bridge.ready()
    asked = Bridge.request("cards", "cards:1", lua.table_from({}), lua.table_from({}), 4000)
    deliver(t="res", push=True, id=-1, key="cards:1", kind="cards", ok=True, drawn_base=0,
            decision=lua.table_from({"intent": "pushed"}))
    r = Bridge.peek("cards:1")
    assert r.wire == -1 and r.pushed and r.id > asked and r.decision.intent == "pushed"
    assert Bridge.isPending("cards:1"), "the request on its way is left alone"
    deliver(t="res", id=asked, key="cards:1", kind="cards", ok=True, decision=lua.table_from({"intent": "older"}))
    assert Bridge.peek("cards:1").decision.intent == "pushed", "asked before the push: older than it"
    assert not Bridge.isPending("cards:1")
    later = Bridge.request("cards", "cards:1", lua.table_from({}), lua.table_from({}), 4000)
    deliver(t="res", id=later, key="cards:1", kind="cards", ok=True, decision=lua.table_from({"intent": "newer"}))
    assert Bridge.peek("cards:1").decision.intent == "newer" and Bridge.peek("cards:1").wire is None
