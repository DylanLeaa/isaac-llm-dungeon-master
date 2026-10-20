"""Campaign memory: what the DM has done this run and what it is plotting.

Every request carries a compact digest of this, so Haiku can run multi-room
cons ("starve keys for three rooms, then drop two keys next to a troll bomb").

Answers are decks the mod draws from as the game rolls. Each draw comes back as a `drawn` message, so
the ledger counts what the game actually handed out, not whole plans. Open decks (written, not used up)
are shown to every other call in `your_open_decks`, and the floor's budget lives in `economy`.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

from catalog import BOMBS, COINS, HEARTS, KEYS
from economy import Economy, deck_outcomes, outcome_units

# Kinds whose answer is used whole, once: the first draw uses it up.
WHOLE = ("room_clear", "room_layout", "floor_plan")
KIND_OF_PREFIX = {"clear": "room_clear", "layout": "room_layout", "items": "item_choice", "curses": "floor_plan",
                  "cards": "cards", "trinkets": "trinkets", "pills": "pills", "drops": "drops",
                  "payout": "payout_plan", "rock": "rock"}
FAMILY_SETS = (("keys", KEYS), ("bombs", BOMBS), ("coins", COINS), ("hearts", HEARTS))
# The list each deck kind draws from.
DECK_FIELD = {"drops": "outcomes", "payout_plan": "schedule", "cards": "cards", "trinkets": "trinkets",
              "item_choice": "order"}
OPEN_DECKS_SHOWN = 8


def kind_of(key: str) -> str:
    return KIND_OF_PREFIX.get(key.split(":", 1)[0], "?")


class RunMemory:
    def __init__(self) -> None:
        self.economy = Economy()
        self.reset(None)

    def reset(self, run_id: Any) -> None:
        self.run_id = run_id
        self.events: deque[str] = deque(maxlen=40)
        self.schemes: deque[str] = deque(maxlen=6)
        self.taunts: deque[str] = deque(maxlen=8)          # written, shown or not
        self.issued: dict[str, dict] = {}            # key -> the deck the game holds (see note_issued)
        self.drawn_total: dict[str, int] = {}        # key -> every draw the game reported for it
        self.counters: dict[str, int] = {
            "troll_bombs_floor": 0, "clears": 0,
            "clears_since_key": 0, "clears_since_bomb": 0,
            "clears_since_coin": 0, "clears_since_heart": 0,
            "drops_floor": 0, "floor": 0,
        }
        self._since_clear: set[str] = set()          # families handed out since the last clear (any stream)
        cruelty = self.economy.cruelty
        self.economy.reset()
        self.economy.cruelty = cruelty

    # ------------------------------------------------------------ bookkeeping
    def previous(self, key: str) -> dict | None:
        """The answer already issued for `key` that the game hasn't used up, if any: for a deck, only what
        is left of it (the drawn entries stand, so they are never handed out again)."""
        entry = self.issued.get(key)
        if not entry:
            return None
        d, field = entry["decision"], DECK_FIELD.get(entry["kind"])
        if not field or not entry["drawn"]:
            return d
        left, keep = list(entry["left"]), []
        for item in d.get(field) or []:
            label = _item_label(entry["kind"], item)
            if label in left:
                left.remove(label)
                keep.append(item)
        return {**d, field: keep}

    def reissue(self, key: str, rid: int) -> None:
        """The same deck answers a newer request for `key` (the gate kept it)."""
        if key in self.issued:
            self.issued[key]["rid"] = rid

    def note_issued(self, key: str, kind: str, decision: dict, ctx: dict | None = None,
                    rid: int | None = None) -> bool:
        """Record an answer sent to the game and commit its outcomes to the economy (in the same step as
        clamp, so no other answer lands in between). True if it replaced one the game never drew from."""
        prev = self.issued.get(key)
        replaced = prev is not None and not prev["drawn"]
        self.issued[key] = {"kind": kind, "decision": decision, "rid": rid, "drawn": 0, "at": time.monotonic(),
                            "left": _left(kind, decision)}
        if len(self.issued) > 200:
            for k in list(self.issued)[:100]:
                self.issued.pop(k, None)
        self.economy.commit(key, kind, decision, ctx or {})
        # Every taunt written counts, shown or not, so the model stops recycling openers.
        _append_unique(self.taunts, decision.get("taunt"))
        return replaced

    def forget(self, key: str) -> None:
        """The game abandoned this deck (the player left the room): nothing in it is promised any more."""
        self.issued.pop(key, None)
        self.drawn_total.pop(key, None)
        self.economy.forget(key)

    def on_applied(self, key: str) -> None:
        """The game used decision `key` whole (the old `applied` message): a draw of all of it."""
        self.on_drawn(key, None, None, None)

    def on_drawn(self, key: str, rid: int | None, outcome: Any, cause: Any) -> tuple[bool, list[str], Any]:
        """The game handed out one outcome of deck `key`. Returns (whether it is the first draw from this
        answer, the economy families now over budget, the outcome as counted)."""
        entry = self.issued.get(key)
        kind = entry["kind"] if entry else kind_of(key)
        d = entry["decision"] if entry else {}
        if outcome is None or (isinstance(outcome, dict) and outcome.get("applied")):
            outcome = _whole(kind, d, outcome)
        first = bool(entry) and entry["drawn"] == 0 and (rid is None or entry["rid"] is None or rid == entry["rid"])
        self.drawn_total[key] = self.drawn_total.get(key, 0) + 1
        used_up = False
        if entry:
            entry["drawn"] += 1
            _take(entry["left"], _label(kind, outcome))
            used_up = kind in WHOLE or (not entry["left"] and kind != "pills")
            if used_up:
                self.issued.pop(key, None)           # a re-ask starts a new deck
        if first:
            # Only schemes the game acted on are real plans; re-asks and unused answers are noise.
            _append_unique(self.schemes, d.get("scheme"))
            self._first_event(kind, d)
        self._count(kind, outcome, cause)
        overs = self.economy.draw(key, outcome)
        if used_up:
            self.economy.forget(key)                 # what it didn't hand out (a mercy swap) isn't promised
        return first, overs, outcome

    def _count(self, kind: str, outcome: Any, cause: Any) -> None:
        c = self.counters
        rewards = _rewards(outcome)
        c["troll_bombs_floor"] += sum(n for name, n in rewards if name == "troll_bomb")
        if kind == "room_layout" and isinstance(outcome, dict):
            c["troll_bombs_floor"] += int(outcome.get("layout_pickups") == "troll_bombs")
        given = {fam for name, _ in rewards for fam, names in FAMILY_SETS if name in names}
        if kind == "room_clear":
            c["clears"] += 1
            given |= self._since_clear
            for fam, k in (("keys", "clears_since_key"), ("bombs", "clears_since_bomb"),
                           ("coins", "clears_since_coin"), ("hearts", "clears_since_heart")):
                c[k] = 0 if fam in given else c[k] + 1
            self._since_clear = set()
            got = ", ".join(f"{n}x {name}" for name, n in rewards) or "nothing"
            self.events.append(f"clear reward: {got}")
            return
        self._since_clear |= given
        if kind == "drops":
            real = [(name, n) for name, n in rewards if name != "nothing"]
            c["drops_floor"] += len(real)
            if real:
                self.events.append(f'{"breakable" if cause == "breakable" else "kill"} dropped: '
                                   + ", ".join(f"{n}x {name}" for name, n in real))
        elif kind in ("payout_plan", "rock") and rewards:
            what = "tinted rock paid" if kind == "rock" else ("chest held" if cause == "chest_open" else "machine paid")
            self.events.append(f"{what}: " + ", ".join(f"{n}x {name}" for name, n in rewards))
        elif kind == "item_choice" and isinstance(outcome, dict) and outcome.get("item") is not None:
            self.events.append(f'pedestal from your ranking: {outcome.get("name") or outcome["item"]}'
                               f' (q{outcome.get("quality", "?")})')
        elif kind in ("cards", "trinkets") and isinstance(outcome, dict):
            what = outcome.get("card") or outcome.get("name") or outcome.get("trinket")
            self.events.append(f'{kind[:-1]} handed out: {what}')

    def _first_event(self, kind: str, d: dict) -> None:
        if kind == "room_layout":
            bits = []
            if d.get("extra_enemies"):
                bits.append("+" + ",".join(f'{e["count"]}x{e["key"]}' for e in d["extra_enemies"]))
            if d.get("layout_pickups") not in (None, "keep"):
                bits.append(d["layout_pickups"])
            if d.get("tinted_rocks"):
                bits.append(f'{d["tinted_rocks"]} tinted rock(s)')
            if d.get("hide_tinted_rocks"):
                bits.append("hid tinted rocks")
            if d.get("tinted_rock_loot"):
                bits.append("tinted rocks hold:" + ",".join(d["tinted_rock_loot"]))
            if d.get("bonus_pickups"):
                bits.append("bait:" + ",".join(d["bonus_pickups"]))
            self.events.append("room dressed: " + ("; ".join(bits) or "untouched"))
        elif kind == "payout_plan":
            got = ", ".join(f'{r["count"]}x {r["reward"]}' for r in d.get("schedule", [])) or "vanilla"
            self.events.append(f"payout schedule in play for a machine/chest: {got}")
        elif kind == "floor_plan":
            self.events.append(f'floor plan: curses={d.get("add_curses")} treasure={d.get("treasure_distance")}')

    def on_game_event(self, name: str, data: dict) -> None:
        if name == "new_level":
            self.counters["troll_bombs_floor"] = 0
            self.counters["drops_floor"] = 0
            self.counters["floor"] = int(data.get("stage", self.counters["floor"]))
            self.events.append(f'--- entered {data.get("name", "new floor")} ---')
        elif name == "damage":
            self.events.append(f'player hit for {data.get("amount")} by {data.get("source", "?")}'
                               f' in room {data.get("room")}')
        elif name == "item_taken":
            self.events.append(f'player took item {data.get("name")} (q{data.get("quality")})')
        elif name == "room_cleared":
            self.events.append(f'cleared room {data.get("room")} in {data.get("seconds")}s,'
                               f' took {data.get("hits", 0)} hits')
        elif name == "pill_locked":
            who = "your pick" if data.get("by") == "dm" else "vanilla's pick"
            self.events.append(f'{data.get("look")} pills are now {data.get("effect")} for the run ({who})')
        elif name in ("spawn_log", "rolls"):
            pass                                  # diagnostics for the receipt, not for the DM's memory
        elif name == "death":
            self.events.append("PLAYER DIED")
        else:
            self.events.append(f"{name}: {data}")

    def digest(self, exclude: str | None = None, in_flight: list[tuple[str, str]] = ()) -> dict:
        """What every request sees. `exclude` is the key being decided: its own deck isn't "open elsewhere"."""
        now = time.monotonic()
        decks = [{"key": k, "kind": e["kind"], "left": e["left"][:6], "intent": e["decision"].get("intent", ""),
                  **({"scheme": e["decision"]["scheme"]} if e["decision"].get("scheme") else {}),
                  "age_s": round(now - e["at"])}
                 for k, e in self.issued.items() if k != exclude and e["left"]]
        return {
            "recent_events": list(self.events)[-14:],
            "your_active_schemes": list(self.schemes)[-3:],
            "your_recent_taunts": list(self.taunts)[-6:],
            "ledger": dict(self.counters),
            "your_open_decks": decks[-OPEN_DECKS_SHOWN:],
            "in_flight": [{"key": k, "kind": kind} for k, kind in in_flight if k != exclude],
        }

    def report(self) -> dict:
        """This floor, last floor and the run against vanilla (the planners' view)."""
        return self.economy.report()


def _rewards(outcome: Any) -> list[tuple[str, int]]:
    """(reward, count) pairs in a drawn outcome."""
    if not isinstance(outcome, dict):
        return []
    out = []
    for r in outcome.get("rewards") if isinstance(outcome.get("rewards"), list) else []:
        if isinstance(r, dict) and isinstance(r.get("reward"), str):
            out.append((r["reward"], max(1, int(r.get("count") or 1))))
        elif isinstance(r, str):
            out.append((r, 1))
    if isinstance(outcome.get("reward"), str):
        out.append((outcome["reward"], max(1, int(outcome.get("count") or 1))))
    return out


def _whole(kind: str, d: dict, outcome: Any) -> dict:
    """The outcome of a decision used whole, from the decision itself."""
    base = dict(outcome) if isinstance(outcome, dict) else {}
    if kind == "room_clear":
        return {"rewards": d.get("rewards", []), **base}
    if kind == "room_layout":
        return {"rewards": [{"reward": r, "count": 1} for r in d.get("bonus_pickups", [])],
                "layout_pickups": d.get("layout_pickups"), **base}
    return base or {"applied": True}


def _label(kind: str, outcome: Any) -> str:
    """One deck entry, as `your_open_decks` lists it."""
    if not isinstance(outcome, dict):
        return "?"
    if isinstance(outcome.get("reward"), str):
        n = int(outcome.get("count") or 1)
        return outcome["reward"] if outcome["reward"] == "nothing" else f'{n}x {outcome["reward"]}'
    for k in ("card", "trinket", "item"):
        if outcome.get(k) is not None:
            return str(outcome[k])
    if outcome.get("pill") is not None:
        return f'{outcome["pill"]}={outcome.get("effect")}'
    return "?"


def _item_label(kind: str, item: Any) -> str:
    """One entry of a deck's list, labelled like `_left`."""
    if kind in ("drops", "payout_plan") and isinstance(item, dict):
        return _label(kind, {"reward": item.get("reward"), "count": int(item.get("count") or 1)})
    if kind == "cards" and isinstance(item, dict):
        return str(item.get("key"))
    return str(item)


def _left(kind: str, d: dict) -> list[str]:
    """A fresh deck's entries, labelled like `_label`."""
    if kind in ("drops", "payout_plan"):
        return [_label(kind, {"reward": name, "count": n}) for name, n in deck_outcomes(kind, d)]
    if kind == "room_clear":
        return [", ".join(f'{r["count"]}x {r["reward"]}' for r in d.get("rewards", [])) or "nothing"]
    if kind == "room_layout":
        return ["dressing" + (": bait " + ",".join(d["bonus_pickups"]) if d.get("bonus_pickups") else "")]
    if kind == "cards":
        return [c["key"] for c in d.get("cards", [])]
    if kind == "trinkets":
        return [str(t) for t in d.get("trinkets", [])]
    if kind == "pills":
        return [f'{p["color"]}={p["effect"]}' for p in d.get("pills", [])]
    if kind == "item_choice":
        return [str(i) for i in d.get("order", [])]
    if kind == "floor_plan":
        return ["curses " + (",".join(d.get("add_curses", [])) or "none")]
    return []


def _take(left: list[str], label: str) -> None:
    if label in left:
        left.remove(label)
    elif left:
        left.pop(0)


def _norm(text: str) -> str:
    return " ".join("".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace()).split())


def _append_unique(dq: deque, text: str | None) -> None:
    """Append `text` unless an equivalent entry is already there (moves it to the end instead)."""
    if not text:
        return
    n = _norm(text)
    for old in list(dq):
        if _norm(old) == n:
            dq.remove(old)
    dq.append(text)


__all__ = ["RunMemory", "kind_of", "outcome_units"]
