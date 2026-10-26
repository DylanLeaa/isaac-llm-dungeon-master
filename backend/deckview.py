"""The app's Deck view: every deck the DM has written this run, rebuilt from the receipt as of any row.

A deck (one key: "drops:3", "payout:3:1234", "items:3:0") is its latest answer and every draw the game
reported from the key. The draws stand; what's left is the latest answer's unused tail, worked out the way
the mod's decks.lua does it:
  - an answer replaces the unused tail, minus the entries drawn while it was being asked for (the mod
    skips that many from its front)
  - a card is drawn where it fits (its category); an item ranking or
    a trinket deck draws its first entry the player doesn't own, discarding the ones before it
  - drops, a floor's tinted rocks and payouts draw from the front; pills are sticky (a draw doesn't use an
    entry up), a room's clear and
    layout and a floor's curses are used whole, once
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import receipt as receipts

WHOLE = ("room_clear", "room_layout", "floor_plan")
STICKY = ("pills",)
# How the next draw is found: "front" takes the first entry, "match" the first entry matching the draw
# (others stay), "upto" the first matching one, discarding those before it (the player owned them).
DRAW = {"drops": "front", "rocks": "front", "payout_plan": "front", "cards": "match", "item_choice": "upto", "trinkets": "upto"}
ORDER = ("payout_plan", "drops", "room_clear", "item_choice", "cards", "trinkets", "pills", "rocks",
         "room_layout", "floor_plan")


@dataclass
class Slot:
    """One entry of a deck: drawn, skipped, or still to come."""
    label: str
    state: str                      # drawn | skipped | next | queued | waiting | used | set | locked | loaded
    ts: float | None = None         # when it was drawn
    cause: str = ""


@dataclass
class Deck:
    key: str
    kind: str
    floor: int
    title: str
    intent: str = ""
    taunt: str = ""
    versions: int = 0               # answers written for this key so far
    seq: int | None = None          # the receipt entry of the latest answer
    drawn: list[Slot] = field(default_factory=list)
    left: list[Slot] = field(default_factory=list)
    last: float = 0.0               # latest activity (answer or draw)
    closed: bool = False            # its floor is over: what's left was never drawn

    @property
    def spent(self) -> bool:
        """Nothing more to hand out."""
        if self.closed:
            return True
        if self.kind in STICKY:
            return False
        return not self.left or all(s.state == "used" for s in self.left)

    @property
    def status(self) -> str:
        if self.seq is None:
            return "no answer in the receipt"
        if self.closed and any(s.state == "unused" for s in self.left):
            return f"{sum(s.state == 'unused' for s in self.left)} never drawn"
        if self.kind == "pills":
            locked = sum(s.state == "locked" for s in self.left)
            return f"{locked} of {len(self.left)} colours locked"
        if self.kind in WHOLE:
            return "used" if self.spent else "waiting"
        out = f"{len(self.left)} left" if self.left else "used up"
        if self.versions > 1:
            out += f", rewritten {self.versions - 1}x"
        return out


@dataclass
class Floor:
    number: int
    name: str
    decks: list[Deck]


def build(r: receipts.Receipt, upto: int | None = None) -> list[Floor]:
    """Every deck in receipt `r` (kept with keep_full) as it stood when entry `upto` was recorded (None: now),
    grouped by floor, newest floor first. Within a floor: decks with something left first, payouts first."""
    cutoff = None
    if upto is not None and upto in r.full:
        cutoff = r.full[upto].get("ts")
    answers: dict[str, list[dict]] = {}
    names: dict[int, str] = {}
    for seq in sorted(r.full):
        e = r.full[seq]
        if upto is not None and seq > upto:
            break
        d, key = e.get("decision"), e.get("key")
        if not isinstance(d, dict) or not key:
            continue
        answers.setdefault(key, []).append(e)
        level = ((e.get("request") or {}).get("state") or {}).get("level") or {}
        if level.get("name"):
            names.setdefault(_floor_of(key), str(level["name"]))
    draws: dict[str, list[dict]] = {}
    for rec in r.draws:
        if cutoff is not None and (rec.get("ts") or 0) > cutoff:
            continue
        draws.setdefault(str(rec.get("key")), []).append(rec)

    decks = [_deck(key, answers.get(key, []), draws.get(key, [])) for key in {*answers, *draws}]
    floors: dict[int, list[Deck]] = {}
    for d in decks:
        floors.setdefault(d.floor, []).append(d)
    out = []
    now = max(floors, default=0)
    for n in sorted(floors, reverse=True):
        if n < now:
            for d in floors[n]:
                if d.key.startswith("curses:") and n == now - 1:
                    continue                      # the floor just started may still be waiting on its curses
                d.closed = True
                for s in d.left:
                    if s.state in ("next", "queued", "waiting"):
                        s.state = "unused"
        rank = {k: i for i, k in enumerate(ORDER)}
        floors[n].sort(key=lambda d: (d.spent, rank.get(d.kind, len(ORDER)), -d.last))
        out.append(Floor(n, names.get(n, ""), floors[n]))
    return out


def _deck(key: str, answers: list[dict], draws: list[dict]) -> Deck:
    cur = answers[-1] if answers else None
    kind = cur["what"] if cur else str((draws[0] if draws else {}).get("kind") or "?")
    ctx = ((cur or {}).get("request") or {}).get("ctx") or {}
    dec = (cur or {}).get("decision") or {}
    deck = Deck(key, kind, _floor_of(key), _title(kind, key, ctx), intent=str(dec.get("intent") or ""),
                taunt=str(dec.get("taunt") or ""), versions=len(answers), seq=(cur or {}).get("seq"),
                last=max([(cur or {}).get("ts") or 0] + [rec.get("ts") or 0 for rec in draws]))
    if cur is None:
        deck.drawn = [_drawn_slot(kind, rec, {}) for rec in draws]
        return deck
    # Every answer's names: an item ranking drawn from an older answer is named by that answer's candidates.
    names = _names([((a.get("request") or {}).get("ctx") or {}) for a in answers])
    entries = _entries(kind, dec, ctx, names)

    if kind in WHOLE:
        used = next((rec for rec in draws if (rec.get("ts") or 0) >= _asked_at(cur)), None)
        deck.left = [Slot(label, "used" if used else "waiting", (used or {}).get("ts"), _cause(used))
                     for _token, label in entries]
        return deck
    if kind in STICKY:
        for token, label in entries:
            hit = next((rec for rec in draws if kind == "pills" and _token(kind, rec.get("outcome")) == token), None)
            state = ("locked" if hit else "set") if kind == "pills" else "loaded"
            deck.left.append(Slot(label, state, (hit or {}).get("ts"), _cause(hit)))
        return deck

    # A deck: the draws stand, the latest answer's tail is what's left.
    asked, answered = _asked_at(cur), cur.get("ts") or 0
    # Drawn before the mod took this answer in: recorded before it, or still from an older answer.
    before = [rec for rec in draws if (rec.get("ts") or 0) < answered or rec.get("ref") not in (None, cur["seq"])]
    early = {id(rec) for rec in before}
    skip = sum(1 for rec in before if (rec.get("ts") or 0) >= asked)
    tail = entries[skip:]
    for rec in draws:
        slot = _drawn_slot(kind, rec, names)
        if id(rec) not in early:
            for gone in _take(DRAW.get(kind, "front"), tail, _token(kind, rec.get("outcome"))):
                deck.drawn.append(Slot(gone[1], "skipped", rec.get("ts"), "already owned"))
        deck.drawn.append(slot)
    deck.left = [Slot(label, "next" if i == 0 else "queued") for i, (_token, label) in enumerate(tail)]
    return deck


def _take(how: str, tail: list[tuple[Any, str]], token: Any) -> list[tuple[Any, str]]:
    """Remove the drawn entry from `tail`. Returns the entries discarded on the way (an "upto" deck's)."""
    if not tail:
        return []
    i = next((n for n, (t, _label) in enumerate(tail) if t == token), None) if how != "front" else 0
    if i is None:                                 # a mercy swap or an unknown draw: the front went
        i = 0
    if how != "upto":
        del tail[i]
        return []
    skipped = tail[:i]
    del tail[:i + 1]
    return skipped


def _asked_at(entry: dict) -> float:
    """When the request behind an answer reached the backend (before it waited for the floor brief)."""
    return (entry.get("ts") or 0) - ((entry.get("ms") or 0) + (entry.get("brief_wait_ms") or 0)) / 1000


def _floor_of(key: str) -> int:
    try:
        return int(key.split(":")[1])
    except (IndexError, ValueError):
        return 0


# ------------------------------------------------------------------ labels
def _names(ctxs: list[dict]) -> dict:
    """id -> label for the items, trinkets and pill colours the requests offered."""
    out: dict = {}
    for ctx in ctxs:
        for c in ctx.get("candidates") or []:
            if isinstance(c, dict) and "id" in c:
                q = f' (q{c["quality"]})' if c.get("quality") is not None else ""
                out[("item", _int(c["id"]))] = f'{c.get("name", c["id"])}{q}'
        for c in ctx.get("trinket_candidates") or []:
            if isinstance(c, dict) and "id" in c:
                out[("trinket", _int(c["id"]))] = str(c.get("name", c["id"]))
        for c in (ctx.get("pill_colors_to_assign") or []) + (ctx.get("pills_locked") or []):
            if isinstance(c, dict) and "color" in c and c.get("look"):
                out[("pill", _int(c["color"]))] = _words(c["look"])
    return out


def _entries(kind: str, d: dict, ctx: dict, names: dict) -> list[tuple[Any, str]]:
    """An answer's entries in draw order, as (what a draw of it reports, label)."""
    if kind in ("drops", "rocks", "payout_plan"):
        rows = d.get("schedule" if kind == "payout_plan" else "outcomes") or []
        return [((r.get("reward"), int(r.get("count") or 1)), _reward(r)) for r in rows if isinstance(r, dict)]
    if kind == "cards":
        return [(c.get("key"), _words(c.get("key"))) for c in d.get("cards") or [] if isinstance(c, dict)]
    if kind == "item_choice":
        return [(_int(i), names.get(("item", _int(i)), f"item {i}")) for i in d.get("order") or []]
    if kind == "trinkets":
        return [(_int(i), names.get(("trinket", _int(i)), f"trinket {i}")) for i in d.get("trinkets") or []]
    if kind == "pills":
        return [(_int(p.get("color")), f'{names.get(("pill", _int(p.get("color"))), "colour " + str(p.get("color")))}'
                 f' = {_words(p.get("effect"))}') for p in d.get("pills") or [] if isinstance(p, dict)]
    if kind == "room_clear":
        return [(None, ", ".join(_reward(r) for r in d.get("rewards") or []) or "nothing")]
    if kind in ("room_layout", "floor_plan"):
        return [(None, receipts.summarize(kind, d, ctx))]
    return []


def _token(kind: str, outcome: Any) -> Any:
    """What a draw's outcome matches in `_entries`."""
    if not isinstance(outcome, dict):
        return None
    if kind in ("drops", "rocks", "payout_plan"):
        return outcome.get("reward"), int(outcome.get("count") or 1)
    if kind == "cards":
        return outcome.get("card")
    if kind == "item_choice":
        return _int(outcome.get("item"))
    if kind == "trinkets":
        return _int(outcome.get("trinket"))
    if kind == "pills":
        return _int(outcome.get("pill"))
    return None


def _drawn_slot(kind: str, rec: dict, names: dict) -> Slot:
    o = rec.get("outcome") if isinstance(rec.get("outcome"), dict) else {}
    if kind in ("drops", "rocks", "payout_plan") and o.get("reward"):
        label = _reward(o)
    elif o.get("rewards") is not None:
        label = ", ".join(_reward(r) for r in o["rewards"] if isinstance(r, dict)) or "nothing"
    elif o.get("item") is not None:
        label = names.get(("item", _int(o["item"]))) or str(o.get("name") or f'item {o["item"]}')
    elif o.get("trinket") is not None:
        label = names.get(("trinket", _int(o["trinket"]))) or str(o.get("name") or f'trinket {o["trinket"]}')
    elif o.get("card"):
        label = _words(o["card"])
    elif o.get("pill") is not None:
        label = f'{names.get(("pill", _int(o["pill"])), "colour " + str(o["pill"]))} = {_words(o.get("effect"))}'
    else:
        label = "used"
    return Slot(label, "drawn", rec.get("ts"), _cause(rec))


def _reward(r: dict) -> str:
    name = r.get("reward")
    return "nothing" if name in (None, "nothing") else f'{int(r.get("count") or 1)}x {_words(name)}'


def _cause(rec: dict | None) -> str:
    return _words((rec or {}).get("cause") or "")


def _words(text: Any) -> str:
    return str(text or "").replace("_", " ")


def _int(v: Any) -> Any:
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _title(kind: str, key: str, ctx: dict) -> str:
    parts = key.split(":")
    where = parts[2] if len(parts) > 2 else ""
    room = _words(ctx.get("room_type") or "")
    room = f" ({room})" if room and room != "default" else ""
    if kind == "drops":
        return "Kill and break drops"
    if kind == "payout_plan":
        source = _words(ctx.get("source") or "machine / chest")
        return f"{source[:1].upper()}{source[1:]} ·{where[-4:]}"
    if kind == "room_clear":
        return f"Room {where} clear reward{room}"
    if kind == "room_layout":
        return f"Room {where} dressing{room}"
    if kind == "rocks":
        return "Tinted rocks"
    if kind == "item_choice":
        what = _words(ctx.get("room_type") or "")
        return f"{what[:1].upper()}{what[1:]} items (pool {ctx.get('pool', where)})" if what else f"Item pool {where}"
    return {"cards": "Cards", "trinkets": "Trinkets", "pills": "Pill colours",
            "floor_plan": "Next floor's curses"}.get(kind, key)
