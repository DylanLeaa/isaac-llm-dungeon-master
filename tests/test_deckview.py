"""The app's Deck view: decks rebuilt from a receipt, live and as of a picked row."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import deckview  # noqa: E402
from receipt import Receipt  # noqa: E402


class Run:
    """A receipt written by hand: answers and draws, at given times."""

    def __init__(self):
        self.r = Receipt(None, keep_full=True)
        self.seq = 0

    def answer(self, kind, key, decision, ts, ms=0, ctx=None, floor="Basement I"):
        self.seq += 1
        self.r.ingest({"type": "entry", "seq": self.seq, "ts": ts, "what": kind, "status": "ok", "key": key,
                       "ms": ms, "decision": decision,
                       "request": {"state": {"level": {"name": floor}}, "ctx": ctx or {}}})
        return self.seq

    def draw(self, key, kind, outcome, ts, ref=None, cause="enemy_death"):
        self.r.apply_drawn({"type": "drawn", "ref": ref, "key": key, "kind": kind, "outcome": outcome,
                            "cause": cause, "ts": ts})


def deck(floors, key):
    return next(d for f in floors for d in f.decks if d.key == key)


def labels(slots):
    return [(s.label, s.state) for s in slots]


def drops(*names):
    return {"intent": "tax him", "outcomes": [{"reward": n, "count": 1} for n in names]}


def test_drops_deck_live_order_and_as_of_a_row():
    run = Run()
    a = run.answer("drops", "drops:1", drops("penny", "nothing", "key"), ts=10)
    run.draw("drops:1", "drops", {"reward": "penny", "count": 1}, ts=11, ref=a)
    later = run.answer("cards", "cards:1", {"cards": [{"key": "hermit"}]}, ts=12)

    d = deck(deckview.build(run.r), "drops:1")
    assert labels(d.drawn) == [("1x penny", "drawn")]
    assert labels(d.left) == [("nothing", "next"), ("1x key", "queued")]
    assert d.status == "2 left" and d.intent == "tax him"

    # As of the answer's own row: nothing drawn yet. The card deck didn't exist then.
    floors = deckview.build(run.r, upto=a)
    d = deck(floors, "drops:1")
    assert d.drawn == [] and [s.label for s in d.left] == ["1x penny", "nothing", "1x key"]
    assert all(x.key != "cards:1" for f in floors for x in f.decks)
    assert deck(deckview.build(run.r, upto=later), "cards:1").left[0].label == "hermit"


def test_a_rewrite_skips_what_was_drawn_while_it_was_asked_for():
    run = Run()
    a = run.answer("drops", "drops:1", drops("penny", "nothing"), ts=10)
    run.draw("drops:1", "drops", {"reward": "penny", "count": 1}, ts=11, ref=a)   # before the re-ask
    run.draw("drops:1", "drops", {"reward": "nothing", "count": 1}, ts=13, ref=a)  # while it was asked
    b = run.answer("drops", "drops:1", drops("bomb", "coin", "heart"), ts=15, ms=3000)   # asked at 12
    d = deck(deckview.build(run.r), "drops:1")
    assert [s.label for s in d.drawn] == ["1x penny", "nothing"]
    assert labels(d.left) == [("1x coin", "next"), ("1x heart", "queued")]
    assert d.status == "2 left, rewritten 1x"
    run.draw("drops:1", "drops", {"reward": "half_heart", "count": 1}, ts=16, ref=b)   # a mercy swap: the front
    assert [s.label for s in deck(deckview.build(run.r), "drops:1").left] == ["1x heart"]


def test_an_item_ranking_discards_what_the_player_owned():
    run = Run()
    ctx = {"room_type": "treasure", "pool": 0,
           "candidates": [{"id": 1, "name": "Sad Onion", "quality": 3}, {"id": 2, "name": "Lunch", "quality": 1},
                          {"id": 3, "name": "Dinner", "quality": 1}]}
    a = run.answer("item_choice", "items:1:0", {"order": [1, 2, 3]}, ts=10, ctx=ctx)
    run.draw("items:1:0", "item_choice", {"item": 2, "name": "Lunch"}, ts=11, ref=a, cause="pedestal")
    d = deck(deckview.build(run.r), "items:1:0")
    assert d.title == "Treasure items (pool 0)"
    assert labels(d.drawn) == [("Sad Onion (q3)", "skipped"), ("Lunch (q1)", "drawn")]
    assert labels(d.left) == [("Dinner (q1)", "next")]


def test_whole_and_sticky_decks():
    run = Run()
    layout = run.answer("room_layout", "layout:1:85", {"tinted_rock_loot": ["soul_heart", "nothing"]}, ts=10)
    clear = run.answer("room_clear", "clear:1:85", {"rewards": [{"reward": "key", "count": 1}]}, ts=11)
    pills = run.answer("pills", "pills:1", {"pills": [{"color": 3, "effect": "bad_trip"},
                                                      {"color": 4, "effect": "health_up"}]},
                       ts=12, ctx={"pill_colors_to_assign": [{"color": 3, "look": "blue_blue"}]})
    assert labels(deck(deckview.build(run.r), "clear:1:85").left) == [("1x key", "waiting")]
    run.draw("layout:1:85", "room_layout", {"applied": True}, ts=13, ref=layout, cause="room_enter")
    run.draw("clear:1:85", "room_clear", {"rewards": [{"reward": "key", "count": 1}]}, ts=14, ref=clear)
    run.draw("pills:1", "pills", {"pill": 3, "effect": "bad_trip"}, ts=15, ref=pills, cause="swallowed")
    run.draw("rock:1:85", "rock", {"rewards": [{"reward": "soul_heart", "count": 1}]}, ts=16, cause="rock_break")

    floors = deckview.build(run.r)
    clear_deck = deck(floors, "clear:1:85")
    assert labels(clear_deck.left) == [("1x key", "used")] and clear_deck.spent and clear_deck.status == "used"
    p = deck(floors, "pills:1")
    assert labels(p.left) == [("blue blue = bad trip", "locked"), ("colour 4 = health up", "set")]
    assert p.status == "1 of 2 colours locked" and not p.spent
    rock = deck(floors, "rock:1:85")
    assert rock.title == "Room 85 tinted rocks" and rock.left[0].label == "soul heart, nothing"
    assert rock.status == "paid 1x"
    assert floors[0].decks[-1].spent                # spent decks sort last


def test_a_floor_that_is_over_never_draws_its_leftovers():
    run = Run()
    run.answer("payout_plan", "payout:1:99991234", {"schedule": [{"reward": "penny", "count": 2}]}, ts=10,
               ctx={"source": "beggar"})
    run.answer("floor_plan", "curses:1", {"add_curses": ["blind"]}, ts=11)
    run.answer("drops", "drops:2", drops("bomb"), ts=12, floor="Basement II")
    floors = deckview.build(run.r)
    assert [(f.number, f.name) for f in floors] == [(2, "Basement II"), (1, "Basement I")]
    beggar = deck(floors, "payout:1:99991234")
    assert beggar.title == "Beggar ·1234"
    assert beggar.spent and beggar.status == "1 never drawn" and beggar.left[0].state == "unused"
    assert deck(floors, "curses:1").left[0].state == "waiting"   # the new floor may still wait on it
