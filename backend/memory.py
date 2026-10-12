"""Campaign memory: what the DM has done this run and what it is plotting.

Every request carries a compact digest of this, so Haiku can run multi-room
cons ("starve keys for three rooms, then drop two keys next to a troll bomb").
"""
from __future__ import annotations

from collections import deque
from typing import Any

from catalog import CHESTS, LUCK, VANILLA_DROP_RATE, VANILLA_PICKUPS_PER_CLEAR
from schemas import summarize_categories


class RunMemory:
    def __init__(self) -> None:
        self.reset(None)

    def reset(self, run_id: Any) -> None:
        self.run_id = run_id
        self.events: deque[str] = deque(maxlen=40)
        self.schemes: deque[str] = deque(maxlen=6)
        self.taunts: deque[str] = deque(maxlen=8)          # written, shown or not
        self.issued: dict[str, dict] = {}            # key -> {kind, decision}
        self.counters: dict[str, int] = {
            "troll_bombs_floor": 0, "clears": 0,
            "clears_since_key": 0, "clears_since_bomb": 0,
            "clears_since_coin": 0, "clears_since_heart": 0,
            "floor": 0,
        }
        # What the DM has given away, against vanilla: this floor, last floor and the whole run.
        self.gen_floor, self.gen_run = _new_gen(), _new_gen()
        self.gen_last_floor: dict | None = None

    # ------------------------------------------------------------ bookkeeping
    def previous(self, key: str) -> dict | None:
        """The answer already issued for `key` that the game hasn't used yet, if any."""
        entry = self.issued.get(key)
        return entry["decision"] if entry else None

    def note_issued(self, key: str, kind: str, decision: dict) -> bool:
        """Record an answer sent to the game. Returns True if it replaced an unused one."""
        replaced = key in self.issued
        self.issued[key] = {"kind": kind, "decision": decision}
        if len(self.issued) > 200:
            for k in list(self.issued)[:100]:
                self.issued.pop(k, None)
        # Every taunt written counts, shown or not, so the model stops recycling openers.
        _append_unique(self.taunts, decision.get("taunt"))
        return replaced

    def on_applied(self, key: str) -> None:
        """The game actually used decision `key`; update the ledgers."""
        entry = self.issued.pop(key, None)
        if not entry:
            return
        kind, d = entry["kind"], entry["decision"]
        # Only schemes the game acted on are real plans; re-asks and unused answers are noise.
        _append_unique(self.schemes, d.get("scheme"))
        c = self.counters
        if kind in ("room_clear", "room_layout"):
            for g in (self.gen_floor, self.gen_run):
                _count_gifts(g, kind, d)
        if kind == "room_clear":
            fam = summarize_categories(d.get("rewards", []))
            c["clears"] += 1
            for name, k in (("keys", "clears_since_key"), ("bombs", "clears_since_bomb"),
                            ("coins", "clears_since_coin"), ("hearts", "clears_since_heart")):
                c[k] = 0 if fam[name] else c[k] + 1
            c["troll_bombs_floor"] += sum(r["count"] for r in d.get("rewards", [])
                                          if r["reward"] == "troll_bomb")
            got = ", ".join(f'{r["count"]}x {r["reward"]}' for r in d.get("rewards", [])) or "nothing"
            self.events.append(f"clear reward: {got}")
        elif kind == "room_layout":
            c["troll_bombs_floor"] += int(d.get("layout_pickups") == "troll_bombs")
            c["troll_bombs_floor"] += d.get("bonus_pickups", []).count("troll_bomb")
            bits = []
            if d.get("extra_enemies"):
                bits.append("+" + ",".join(f'{e["count"]}x{e["key"]}' for e in d["extra_enemies"]))
            if d.get("layout_pickups") not in (None, "keep"):
                bits.append(d["layout_pickups"])
            if d.get("tinted_rocks"):
                bits.append(f'{d["tinted_rocks"]} tinted rock(s)')
            if d.get("hide_tinted_rocks"):
                bits.append("hid tinted rocks")
            if d.get("bonus_pickups"):
                bits.append("bait:" + ",".join(d["bonus_pickups"]))
            self.events.append("room dressed: " + ("; ".join(bits) or "untouched"))
        elif kind == "item_choice":
            self.events.append(f'item handed out from ranked list (top={d.get("order", ["?"])[0]})')
        elif kind == "floor_plan":
            self.events.append(f'floor plan: curses={d.get("add_curses")} treasure={d.get("treasure_distance")}')
        elif kind == "consumables":
            self.events.append("consumables plan in play: cards=" + ",".join(c["key"] for c in d.get("cards", []))
                               + " pills=" + ",".join(f'{p["color"]}:{p["effect"]}' for p in d.get("pills", [])))

    def on_game_event(self, name: str, data: dict) -> None:
        if name == "new_level":
            self.counters["troll_bombs_floor"] = 0
            if any(self.gen_floor.values()):
                self.gen_last_floor = _gen_view(self.gen_floor)
            self.gen_floor = _new_gen()
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
        elif name in ("card_drawn", "trinket_drawn"):
            self.events.append(f'{name.split("_")[0]} handed out: {data.get("name")}')
        elif name == "death":
            self.events.append("PLAYER DIED")
        else:
            self.events.append(f"{name}: {data}")

    def digest(self) -> dict:
        return {
            "recent_events": list(self.events)[-14:],
            "your_active_schemes": list(self.schemes)[-3:],
            "your_recent_taunts": list(self.taunts)[-6:],
            "ledger": dict(self.counters),
            "generosity": self.generosity(),
        }

    def generosity(self) -> dict:
        """What the DM has handed out, next to what vanilla would have, with a one-line verdict."""
        return {
            "this_floor": _gen_view(self.gen_floor),
            "last_floor": self.gen_last_floor,
            "run": _gen_view(self.gen_run),
            "vanilla": {"drop_rate": VANILLA_DROP_RATE, "pickups_per_clear": VANILLA_PICKUPS_PER_CLEAR,
                        "luck_per_run": "about 1"},
            "verdict": _verdict(self.gen_floor, self.gen_run),
        }


def _new_gen() -> dict[str, int]:
    return {"clears": 0, "clears_with_drop": 0, "clear_pickups": 0, "bait_pickups": 0,
            "luck": 0, "tinted_rocks": 0, "chests": 0}


def _count_gifts(g: dict[str, int], kind: str, d: dict) -> None:
    """Add one applied room_clear / room_layout decision to a generosity tally."""
    if kind == "room_clear":
        given = [(r["reward"], int(r.get("count", 1))) for r in d.get("rewards", [])]
        n = sum(c for _, c in given)
        g["clears"] += 1
        g["clears_with_drop"] += int(n > 0)
        g["clear_pickups"] += n
    else:
        given = [(r, 1) for r in d.get("bonus_pickups", [])]
        g["bait_pickups"] += len(given)
        g["tinted_rocks"] += int(d.get("tinted_rocks") or 0)
    g["luck"] += sum(LUCK.get(r, 0) * c for r, c in given)
    g["chests"] += sum(c for r, c in given if r in CHESTS)


def _gen_view(g: dict[str, int]) -> dict:
    c = g["clears"]
    return {
        "room_clears": c,
        "drop_rate": round(g["clears_with_drop"] / c, 2) if c else None,
        "pickups_from_clears": g["clear_pickups"],
        "vanilla_pickups_for_these_clears": round(c * VANILLA_PICKUPS_PER_CLEAR, 1),
        "bait_pickups_placed": g["bait_pickups"],
        "luck_given": g["luck"],
        "tinted_rocks_added": g["tinted_rocks"],
        "chests_given": g["chests"],
    }


def _verdict(floor: dict[str, int], run: dict[str, int]) -> str:
    """Plain-language read of the tallies, so the model doesn't have to do the arithmetic."""
    # Every placed pickup and added tinted rock (loot inside) is on top of what vanilla drops.
    given = floor["clear_pickups"] + floor["bait_pickups"] + floor["tinted_rocks"]
    excess = given - floor["clears"] * VANILLA_PICKUPS_PER_CLEAR
    if excess >= 3:
        out = f"this floor: {excess:.0f} pickups over vanilla - let the next clears drop nothing"
    elif excess >= 1:
        out = "this floor: above vanilla - hold back"
    elif excess <= -2:
        out = "this floor: below vanilla - room for a deliberate gift"
    else:
        out = "this floor: about vanilla"
    if run["luck"] >= 2:
        out += f"; run: {run['luck']} luck given already (vanilla ~1 a run) - no more lucky pennies"
    return out


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
