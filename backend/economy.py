"""The floor's economy: one budget per resource family, shared by every deck the DM writes.

Each request is decided on its own, but the backend sees every deck. For each family (catalog.FAMILIES)
it keeps, per floor:
  target      the ceiling: what vanilla hands out on this floor's rooms, times the floor brief's word for
              the family (starve 0.3 .. flood 2.2), shifted by cruelty. A ceiling, not a quota: the DM may
              give less. Fixed for the floor; only delivered and committed move.
  delivered   what the game actually handed out (the mod's `drawn` messages)
  committed   per deck key: outcomes written but not drawn yet. A re-ask of a key replaces its own row.
  bought      what machines paid back for a payment: an exchange, it never counts against the target
  contingent  per key: chest contents and tinted-rock loot (charged only once opened or bombed), and
              machine schedules (shown to other calls, never reserved)
clamp() trims an answer against the budget (Budget) and the Coordinator commits it in the same
synchronous step, so two calls racing for the last coin are settled by which one finishes first. When a
family overshoots anyway (a chest opened early, a low vanilla estimate), `version` is bumped and the other
decks still committed to that family are re-asked (the backend's `stale` push).

Luck is budgeted per run. Power (item quality, stat pills) is shown against vanilla, never trimmed.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from catalog import (ECONOMY_WORDS, FAMILIES, SLACK, STAT_PILLS, UNITS, VANILLA_LUCK_PER_RUN, VANILLA_PER_FLOOR,
                     VANILLA_PER_ROOM)

FLOOR_TRIMMED = ("keys", "bombs", "coins", "hearts", "cards", "pills", "trinkets", "traps")
RUN_TRIMMED = ("luck",)
GIFT, CONTINGENT, EXCHANGE = "gift", "contingent", "exchange"
UNITS_NOTE = "coins in cents, hearts in half hearts, the rest by count (a golden key/bomb is 3)"


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _count(v: Any) -> int:
    return max(1, int(_num(v, 1)))


def units(name: Any, count: int = 1) -> dict[str, float]:
    """What `count` x reward `name` is worth, by family."""
    return {f: v * count for f, v in (UNITS.get(name) or {}).items()} if isinstance(name, str) else {}


def _add(into: dict, more: dict, sign: int = 1) -> None:
    for f, v in more.items():
        into[f] = into.get(f, 0) + sign * v


def outcome_units(outcome: Any) -> dict[str, float]:
    """What one drawn outcome handed out, by family."""
    out: dict[str, float] = {}
    if not isinstance(outcome, dict):
        return out
    for r in outcome.get("rewards") if isinstance(outcome.get("rewards"), list) else []:
        if isinstance(r, dict):
            _add(out, units(r.get("reward"), _count(r.get("count"))))
        else:
            _add(out, units(r))
    if isinstance(outcome.get("reward"), str):
        _add(out, units(outcome["reward"], _count(outcome.get("count"))))
    if outcome.get("item") is not None:
        out["power"] = out.get("power", 0) + _num(outcome.get("quality"))
    if outcome.get("effect") in STAT_PILLS:
        out["power"] = out.get("power", 0) + STAT_PILLS[outcome["effect"]]
    return out


def deck_outcomes(kind: str, decision: dict) -> list[tuple[str, int]]:
    """The reward outcomes a decision commits, in deck order. Identity decks (items, cards...) commit none:
    they decide which one, the drops, clears and chests decide how many."""
    if kind == "room_clear":
        rows = decision.get("rewards") or []
    elif kind == "drops":
        rows = decision.get("outcomes") or []
    elif kind == "payout_plan":
        rows = decision.get("schedule") or []
    elif kind == "room_layout":
        rows = [{"reward": r, "count": 1} for r in decision.get("bonus_pickups") or []]
    else:
        return []
    return [(r.get("reward"), _count(r.get("count"))) for r in rows if isinstance(r, dict)]


def mode_of(kind: str, ctx: dict) -> str:
    """Slots and beggars are exchanges, chests are gifts that count once opened, the rest are gifts."""
    if kind == "payout_plan":
        return CONTINGENT if ctx.get("source_class") == "chest" else EXCHANGE
    return GIFT


def rock_key(layout_key: str) -> str:
    """The tinted rocks of the room a layout key dresses: layout:3:85 -> rock:3:85."""
    return "rock:" + layout_key.split(":", 1)[1] if ":" in layout_key else layout_key


def floor_sig(state: dict) -> tuple | None:
    level = state.get("level") or {}
    if level.get("stage") is None:
        return None
    return (level.get("stage"), level.get("stage_type"))


def normal_rooms(state: dict) -> int:
    """Rooms on the floor that fight and drop like a normal room."""
    level = state.get("level") or {}
    types = level.get("room_types") if isinstance(level.get("room_types"), dict) else {}
    n = sum(int(_num(types.get(t))) for t in ("default", "miniboss"))
    if not n:
        n = round(0.6 * _num(level.get("rooms_total")))
    return n or 8


class Economy:
    def __init__(self) -> None:
        self.cruelty = 6
        self.reset()

    def reset(self) -> None:
        self.version = 0
        self.sig: tuple | None = None
        self.seen: set = set()
        self.rooms = 8
        self.words: dict[tuple | None, dict[str, str]] = {}
        self.modes: dict[str, str] = {}
        self.run_delivered: Counter = Counter()
        self.floors = 0
        self.last_floor: dict | None = None
        self._clear_floor()

    def _clear_floor(self) -> None:
        self.delivered: Counter = Counter()
        self.bought: Counter = Counter()
        self.committed: dict[str, Counter] = {}
        self.contingent: dict[str, Counter] = {}

    # ------------------------------------------------------------ floors
    def observe(self, state: dict) -> bool:
        """Every request's snapshot: a floor not seen before starts a new ledger. True if it did."""
        sig = floor_sig(state or {})
        if sig is None or sig == self.sig or sig in self.seen:
            return False
        if self.sig is not None:
            self.last_floor = self.report_floor()
        self.sig = sig
        self.seen.add(sig)
        self.floors += 1
        self.rooms = normal_rooms(state)
        self._clear_floor()
        self.modes = {}
        self.version += 1
        return True

    def set_words(self, sig: tuple | None, words: dict[str, str]) -> None:
        """The floor brief's ECONOMY block for floor `sig` (starve | lean | vanilla | generous | flood)."""
        self.words[tuple(sig) if sig else sig] = {f: w for f, w in words.items() if w in ECONOMY_WORDS}

    def word(self, family: str) -> str:
        return self.words.get(self.sig, {}).get(family, "vanilla")

    def vanilla(self, family: str) -> float:
        if family == "luck":
            return VANILLA_LUCK_PER_RUN
        return VANILLA_PER_ROOM.get(family, 0.0) * self.rooms + VANILLA_PER_FLOOR.get(family, 0.0)

    def target(self, family: str) -> float:
        c = self.cruelty
        if family == "traps":                       # the troll-bomb budget: none below cruelty 4
            return 0.0 if c < 4 else float(1 + c // 4)
        mult = ECONOMY_WORDS[self.word(family)]
        if family == "luck":                        # a run's worth, not shifted: one lucky penny is a lot
            return VANILLA_LUCK_PER_RUN * mult
        return self.vanilla(family) * mult * (1 + (5 - c) * 0.04)

    def slack(self, family: str) -> float:
        return SLACK.get(family, 1) * max(0.5, 1.5 - self.cruelty / 20)

    def cap(self, family: str) -> float:
        return self.target(family) + self.slack(family)

    def given(self, family: str) -> float:
        return self.run_delivered[family] if family in RUN_TRIMMED else self.delivered[family]

    def elsewhere(self, family: str, key: str | None) -> float:
        return sum(row.get(family, 0) for k, row in self.committed.items() if k != key)

    def blame(self, family: str, key: str | None) -> str:
        rows = [(row.get(family, 0), k) for k, row in self.committed.items() if k != key and row.get(family, 0) > 0]
        if rows:
            return f"{max(rows)[1]} took the last one"
        if family == "traps" and self.target(family) <= 0:
            return "none below cruelty 4"
        return "already given this " + ("run" if family in RUN_TRIMMED else "floor")

    # ------------------------------------------------------------ decks
    def budget(self, key: str, kind: str, ctx: dict) -> "Budget":
        return Budget(self, key, mode_of(kind, ctx))

    def commit(self, key: str, kind: str, decision: dict, ctx: dict) -> None:
        """Record a clamped answer's unconsumed outcomes. Replaces the key's own row, never adds to it."""
        mode = mode_of(kind, ctx)
        self.modes[key] = mode
        self.committed.pop(key, None)
        self.contingent.pop(key, None)
        tally: Counter = Counter()
        for name, count in deck_outcomes(kind, decision):
            _add(tally, units(name, count))
        if kind == "room_layout":
            rock = rock_key(key)
            self.modes[rock] = CONTINGENT
            loot: Counter = Counter()
            for name in decision.get("tinted_rock_loot") or []:
                _add(loot, units(name))
            if loot:
                self.contingent[rock] = loot
            else:
                self.contingent.pop(rock, None)
        if not tally:
            return
        (self.committed if mode == GIFT else self.contingent)[key] = tally

    def draw(self, key: str, outcome: Any) -> list[str]:
        """The game handed out `outcome` from deck `key`. Returns the families now over budget."""
        u = outcome_units(outcome)
        mode = self.modes.get(key) or (CONTINGENT if key.startswith(("rock:", "payout:")) else GIFT)
        row = (self.committed if mode == GIFT else self.contingent).get(key)
        for f, v in u.items():
            if row is not None:
                row[f] = max(0, row.get(f, 0) - v)
            if mode == EXCHANGE:
                self.bought[f] += v
                continue
            self.delivered[f] += v
            self.run_delivered[f] += v
        if mode == EXCHANGE:
            return []
        overs = [f for f, v in u.items() if v > 0 and f in FLOOR_TRIMMED + RUN_TRIMMED
                 and self.given(f) + self.elsewhere(f, None) > self.cap(f) + 1e-9]
        if overs:
            self.version += 1
        return overs

    def forget(self, key: str) -> None:
        """An abandoned or superseded deck: its unconsumed outcomes are no longer promised."""
        self.committed.pop(key, None)
        self.contingent.pop(key, None)

    def stale_keys(self, families: list[str], exclude: str | None = None) -> list[str]:
        """Decks still committed to any of `families` (other than `exclude`): they need a rewrite."""
        return sorted(k for k, row in self.committed.items()
                      if k != exclude and any(row.get(f, 0) > 0 for f in families))

    # ------------------------------------------------------------ what the model sees
    def view(self, key: str | None) -> dict:
        """ctx.economy for a request about `key`: per family the budget, what's given, what other decks
        promise and what's left, plus a one-line verdict. The key's own row is left out: it is what this
        answer rewrites."""
        out: dict[str, Any] = {}
        verdict = []
        for f in FAMILIES:
            if f == "power":
                out[f] = {"vanilla": round(self.vanilla(f), 1), "given": round(self.delivered[f], 1)}
                continue
            given, other, cap = self.given(f), self.elsewhere(f, key), self.cap(f)
            remaining = max(0.0, cap - given - other)
            e: dict[str, Any] = {"target": round(self.target(f), 1), "delivered": _tidy(given),
                                 "committed_elsewhere": _tidy(other), "remaining": round(remaining, 1)}
            promised = {k: _tidy(row[f]) for k, row in self.committed.items() if k != key and row.get(f, 0) > 0}
            if promised:
                e["promised_by"] = promised
            maybe = {k: _tidy(row[f]) for k, row in self.contingent.items() if k != key and row.get(f, 0) > 0}
            if maybe:
                e["contingent"] = maybe
            if self.bought[f]:
                e["bought"] = _tidy(self.bought[f])
            if f in RUN_TRIMMED:
                e["per"] = "run"
            out[f] = e
            if f in FLOOR_TRIMMED + RUN_TRIMMED:
                verdict.append(_verdict(f, given, other, promised, cap, remaining, self.word(f)))
        out["units"] = UNITS_NOTE
        out["verdict"] = "; ".join(verdict) or "nothing given or promised yet"
        return out

    def report_floor(self) -> dict:
        """This floor against vanilla, for the digest and the next floor's brief."""
        return {f: {"vanilla": round(self.vanilla(f), 1), "target": round(self.target(f), 1),
                    "delivered": _tidy(self.delivered[f]), **({"bought": _tidy(self.bought[f])} if self.bought[f] else {})}
                for f in FAMILIES if f != "luck"}

    def report(self) -> dict:
        return {"this_floor": self.report_floor(), "last_floor": self.last_floor,
                "run": {"luck_given": _tidy(self.run_delivered["luck"]), "luck_budget": round(self.cap("luck"), 1),
                        "power_given": _tidy(self.run_delivered["power"]), "floors": self.floors}}


def _tidy(v: float) -> float | int:
    return int(v) if float(v).is_integer() else round(v, 1)


def _verdict(f: str, given: float, other: float, promised: dict, cap: float, remaining: float, word: str) -> str:
    bits = [f"{f}: {_tidy(given)} given"]
    if other:
        top = max(promised, key=promised.get) if promised else "other decks"
        bits.append(f"{_tidy(other)} promised" + (f" by {top}" if len(promised) == 1 else " by other decks"))
    spend = "spend none" if remaining < 1 - 1e-9 else f"{math.floor(remaining + 1e-9)} left"
    return ", ".join(bits) + f", budget {round(cap, 1):g} ({word}) -> {spend}"


class Budget:
    """One answer's share of the economy, filled in deck order by clamp(). Outcomes over the cap shrink, or
    become nothing; mercy and the drought breakers are added after and always win."""

    def __init__(self, econ: Economy | None, key: str | None, mode: str = GIFT) -> None:
        self.econ, self.key, self.mode = econ, key, mode
        self.used: Counter = Counter()
        self.trimmed = False

    def allow(self, name: str, count: int) -> tuple[int, str | None]:
        """How many of `count` x `name` fit (and reserve them), with a guardrail note if fewer than asked."""
        per = UNITS.get(name) if isinstance(name, str) else None
        if self.econ is None or self.mode != GIFT or not per:
            return count, None
        e, n, short = self.econ, count, None
        for f, v in per.items():
            if v <= 0 or f not in FLOOR_TRIMMED + RUN_TRIMMED:
                continue
            fit = math.floor((e.cap(f) - e.given(f) - e.elsewhere(f, self.key) - self.used[f]) / v + 1e-9)
            if fit < n:
                n, short = max(0, fit), f
        for f, v in per.items():
            self.used[f] += v * n
        if n >= count:
            return count, None
        self.trimmed = True
        return n, f"{name} -> {'nothing' if n == 0 else n} ({short} over budget: {e.blame(short, self.key)})"
