"""Decision schemas (as Claude tools) and the hard guardrails applied to them.

The model is free to be as nasty as it likes *inside* the schema; `clamp()` is
the referee that keeps every decision legal for the game and keeps runs
beatable. The mod trusts whatever comes out of clamp().

Most kinds are decks: a short list of outcomes the mod draws from as the game rolls (ctx.wanted entries,
replacing the unconsumed ctx.deck_left). clamp() also holds every answer to the floor's economy
(economy.Budget): outcomes past a family's budget become "nothing".
"""
from __future__ import annotations

import re
from typing import Any

from catalog import (BOMBS, CARDS, CHAMPION_COLORS, COINS, CURSES, ENEMIES, HEARTS, KEYS,
                     LAYOUT_PICKUP_MODES, PAYMENT, PAYOUT_OUTCOMES, PILLS, REWARDS, SHOP_VALUE, TRAPS,
                     TREASURE_DISTANCE, VANILLA_PICKUPS_PER_CLEAR)
from economy import Budget, normal_rooms

KINDS = ("room_layout", "room_clear", "item_choice", "floor_plan", "cards", "trinkets", "pills", "drops",
         "payout_plan")
_DECK = ("context.wanted is how many entries to write; context.deck_left is what is still unused, and your "
         "answer replaces it; context.drawn_so_far is what was already handed out. ")

_TAUNT = {"type": "string", "maxLength": 60,
          "description": "Shown on screen. <=60 chars, plain ASCII, empty string for silence."}
_SCHEME = {"type": "string", "maxLength": 160,
           "description": "Private note to your future self: the long con you're setting up. Not shown to the player."}
_INTENT = {"type": "string", "maxLength": 120,
           "description": "One short sentence: what this decision is meant to do to the player."}

TOOLS: dict[str, dict[str, Any]] = {
    "room_layout": {
        "name": "decide_room_layout",
        "description": "Decide how an unvisited room is dressed before the player walks in.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "champion_chance": {"type": "number", "minimum": 0, "maximum": 0.6,
                                    "description": "Chance each normal enemy becomes a champion."},
                "champion_color": {"type": "string", "enum": ["random", *CHAMPION_COLORS],
                                   "description": "What every champion in this room becomes."},
                "extra_enemies": {
                    "type": "array", "maxItems": 3,
                    "items": {"type": "object", "properties": {
                        "enemy": {"type": "string", "enum": list(ENEMIES)},
                        "count": {"type": "integer", "minimum": 1, "maximum": 3}},
                        "required": ["enemy", "count"]},
                    "description": "Only applied in rooms that already have enemies."},
                "tinted_rocks": {"type": "integer", "minimum": 0, "maximum": 2,
                                 "description": "Turn this many ordinary rocks into tinted rocks. Each one holds loot (usually a soul heart): a gift to anyone who has or will get a bomb."},
                "hide_tinted_rocks": {"type": "boolean",
                                      "description": "Turn the room's existing tinted rocks back into plain rocks."},
                "tinted_rock_loot": {"type": "array", "maxItems": 2,
                                     "items": {"type": "string", "enum": [*REWARDS, "nothing"]},
                                     "description": "What EVERY tinted rock in this room drops when bombed (its own "
                                                    "and the ones you add), now or on a later visit, instead of the "
                                                    "game's loot. Leave out or empty for the game's own (usually a "
                                                    "soul heart). 'nothing' = an empty rock: a wasted bomb."},
                "layout_pickups": {"type": "string", "enum": list(LAYOUT_PICKUP_MODES)},
                "bonus_pickups": {"type": "array", "maxItems": 2,
                                  "items": {"type": "string", "enum": list(REWARDS)},
                                  "description": "Extra pickups placed in the room on entry (bait or charity)."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "champion_chance", "extra_enemies", "tinted_rocks",
                         "layout_pickups", "taunt"],
        },
    },
    "room_clear": {
        "name": "decide_room_clear",
        "description": "Decide the room-clear reward that drops when the last enemy dies. "
                       "When context.mode is 'bonus' (boss rooms) the game still drops its own reward "
                       "and the boss item; yours is extra. context.kills_dropped is what this room's kills "
                       "already dropped from your drops deck.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "rewards": {"type": "array", "maxItems": 3,
                            "items": {"type": "object", "properties": {
                                "reward": {"type": "string", "enum": list(REWARDS)},
                                "count": {"type": "integer", "minimum": 1, "maximum": 3}},
                                "required": ["reward", "count"]},
                            "description": "Empty list = nothing drops."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "rewards", "taunt"],
        },
    },
    "item_choice": {
        "name": "decide_item_choice",
        "description": "Rank candidate collectibles from one item pool. The first id is what the "
                       "player gets next from this pool; later ids are used for rerolls/extra pedestals. "
                       "The taunt appears when the first item from this pool spawns (possibly much later): "
                       "write it for that moment and never name the item.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "order": {"type": "array", "items": {"type": "integer"}, "minItems": 1,
                          "description": "Candidate ids, in the order they should be handed out."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "order", "taunt"],
        },
    },
    "floor_plan": {
        "name": "decide_floor_plan",
        "description": "Decide curses and layout preferences for the NEXT floor before it generates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "add_curses": {"type": "array", "maxItems": 2,
                               "items": {"type": "string", "enum": list(CURSES)}},
                "strip_vanilla_curses": {"type": "boolean",
                                         "description": "Remove curses the game rolled on its own."},
                "treasure_distance": {"type": "string", "enum": TREASURE_DISTANCE,
                                      "description": "Preferred distance from the start room to the treasure room."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "add_curses", "treasure_distance", "taunt"],
        },
    },
    "cards": {
        "name": "decide_cards",
        "description": "Write the next cards/runes the game hands out on this floor, from any source (drops, "
                       "chests, machines, rooms), in order. " + _DECK + "context.owed lists draws the game is "
                       "already waiting on (it is frozen until you answer): cover each one first. A runes_only "
                       "draw takes only a rune, runes false forbids runes, playing false forbids playing and "
                       "special cards; a draw takes the first card that fits. The taunt shows when the first "
                       "card of this deck is handed out.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "cards": {"type": "array", "maxItems": 5, "items": {"type": "string", "enum": list(CARDS)},
                          "description": "Handed out in this order."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "cards", "taunt"],
        },
    },
    "trinkets": {
        "name": "decide_trinkets",
        "description": "Write the next trinkets the game hands out on this floor, from any source, in order: "
                       "ids from context.trinket_candidates. " + _DECK + "context.owed counts trinkets already "
                       "on the floor waiting on you (the game is frozen): write at least that many. The taunt "
                       "shows when the first trinket of this deck is handed out.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "trinkets": {"type": "array", "maxItems": 4, "items": {"type": "integer"},
                             "description": "Ids from context.trinket_candidates, in hand-out order."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "trinkets", "taunt"],
        },
    },
    "pills": {
        "name": "decide_pills",
        "description": "Decide what each pill colour in context.pill_colors_to_assign does. A colour keeps its "
                       "effect for the rest of the run once it is swallowed (context.pills_locked lists those), "
                       "so a colour is a promise. context.owed lists colours the player is trying to swallow "
                       "right now (the game is frozen until you answer). The taunt shows when the first pill "
                       "of this answer is swallowed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "pills": {"type": "array", "maxItems": 13,
                          "items": {"type": "object", "properties": {
                              "color": {"type": "integer"},
                              "effect": {"type": "string", "enum": list(PILLS)}},
                              "required": ["color", "effect"]},
                          "description": "One entry per colour in context.pill_colors_to_assign."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "pills", "taunt"],
        },
    },
    "drops": {
        "name": "decide_drops",
        "description": "Write what each kill (and each poop, fire, urn or skull the player breaks) drops in "
                       "the room the player just entered, one entry per kill, in the order they happen. The "
                       "engine cancels the game's own drop for every one of them and drops your entry instead. "
                       + _DECK + "Vanilla drops nothing for most kills: 'nothing' is the normal entry. Keys, "
                       "bombs, coins and hearts here come out of the same floor budget as room-clear rewards "
                       "and chests (context.economy).",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "outcomes": {"type": "array", "maxItems": 8,
                             "items": {"type": "object", "properties": {
                                 "reward": {"type": "string", "enum": [*REWARDS, "nothing"]},
                                 "count": {"type": "integer", "minimum": 1, "maximum": 2}},
                                 "required": ["reward"]},
                             "description": "One per kill, in order."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "outcomes", "taunt"],
        },
    },
    "payout_plan": {
        "name": "decide_payout_plan",
        "description": "Fix what ONE beggar/machine/bum or ONE chest pays out, decided when it appears. "
                       "For a slot or beggar (context.source_class 'slot') `schedule` has one entry per "
                       "paid use, in order: what the 1st, 2nd, 3rd... use yields; an entry may be "
                       "'nothing' (it takes the payment and stiffs the player). For a chest "
                       "(source_class 'chest') `schedule` is everything it holds when opened. 'item' means "
                       "a pedestal item from the source's item pool (only if context.has_item_pool). A "
                       "machine is an exchange: what it pays back per payment is capped by a house edge, and "
                       "it never spends the floor's budget. A chest's contents count against the budget once "
                       "it is opened. Asked again with context.uses_so_far when a schedule runs low: extend "
                       "the story, the uses already paid stand.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "schedule": {"type": "array", "maxItems": 6,
                             "items": {"type": "object", "properties": {
                                 "reward": {"type": "string", "enum": [*REWARDS, *PAYOUT_OUTCOMES]},
                                 "count": {"type": "integer", "minimum": 1, "maximum": 2}},
                                 "required": ["reward"]}},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "schedule", "taunt"],
        },
    },
}


# ----------------------------------------------------------------- guardrails
def _clean_text(s: Any, limit: int) -> str:
    s = str(s or "")
    s = s.encode("ascii", "ignore").decode()       # Isaac's font is ASCII only
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    space = cut.rfind(" ")
    if space >= limit * 0.6:                        # end on a whole word unless that loses too much
        cut = cut[:space]
    return cut.rstrip(" ,;:-")


def _hp_half_hearts(state: dict) -> int:
    hp = (state.get("player") or {}).get("hp") or {}
    return int(hp.get("red", 0)) + int(hp.get("soul", 0)) + 2 * int(hp.get("eternal", 0)) \
        + 2 * int(hp.get("bone", 0))


def mercy_active(state: dict) -> bool:
    """Player is one or two hits from death: the DM may sneer, not execute."""
    return _hp_half_hearts(state) <= 2


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def kill_drop_cap(state: dict, cruelty: int) -> int:
    """How many kills and breaks may drop something this floor. Rare in vanilla: scaled from the clear
    rate and cruelty."""
    return round(normal_rooms(state) * VANILLA_PICKUPS_PER_CLEAR * max(0.5, 1.5 - 0.08 * cruelty))


def kill_drops_left(state: dict, cruelty: int, counters: dict) -> int:
    return max(0, kill_drop_cap(state, cruelty) - int(counters.get("drops_floor", 0)))


def traps_per_floor(cruelty: int) -> int:
    """Troll bombs (and other traps) a floor may hold: none below cruelty 4."""
    return 0 if cruelty < 4 else 1 + cruelty // 4


def extra_enemies_per_room(cruelty: int) -> int:
    return round(cruelty / 3.3)                         # 0..3


def max_curses(cruelty: int, mercy: bool = False) -> int:
    """How many curses a floor plan may add."""
    n = 2 if cruelty >= 7 else (1 if cruelty >= 3 else 0)
    return min(n, 1) if mercy else n


def _wanted(ctx: dict, default: int, cap: int) -> int:
    """How many entries a deck answer may hold: context.wanted, within the schema's maximum."""
    return max(1, min(cap, _int(ctx.get("wanted"), default)))


def clamp(kind: str, raw: dict, state: dict, ctx: dict, cruelty: int, counters: dict,
          budget: Budget | None = None) -> dict:
    """The legal, fair version of `raw`. With a `budget` (the floor's economy, less this key's own row),
    gifts past a family's budget are trimmed, in deck order; mercy and the drought breakers come after."""
    cruelty = max(0, min(10, int(cruelty)))
    budget = budget or Budget(None, None)
    mercy = mercy_active(state)
    floor_trolls = int(counters.get("troll_bombs_floor", 0))
    troll_budget = 0 if mercy else max(0, traps_per_floor(cruelty) - floor_trolls)
    out: dict[str, Any] = {
        "intent": _clean_text(raw.get("intent"), 120),
        "taunt": _clean_text(raw.get("taunt"), 70),
        "scheme": _clean_text(raw.get("scheme"), 160),
    }
    notes: list[str] = []

    def afford(name: str, n: int) -> int:
        n, note = budget.allow(name, n)
        if note:
            notes.append(note)
        return n

    if kind == "room_layout":
        cap = min(0.6, 0.06 * cruelty)
        if mercy:
            cap = min(cap, 0.10)
        out["champion_chance"] = round(max(0.0, min(cap, float(raw.get("champion_chance") or 0))), 3)
        color = raw.get("champion_color")
        out["champion_color"] = color if color in CHAMPION_COLORS else "random"
        if color in CHAMPION_COLORS:
            out["champion_enum"] = CHAMPION_COLORS[color][0]

        budget_n = extra_enemies_per_room(cruelty)
        if mercy:
            budget_n = min(budget_n, 1)
        if ctx.get("first_room_of_floor"):
            budget_n = 0
        extras, spent = [], 0
        for e in raw.get("extra_enemies") or []:
            spec = ENEMIES.get(e.get("enemy"))
            if not spec or spent >= budget_n:
                continue
            n = max(1, min(int(e.get("count") or 1), budget_n - spent))
            spent += n
            extras.append({"key": e["enemy"], "etype": spec["etype"], "variant": spec["variant"], "count": n})
        out["extra_enemies"] = extras

        out["tinted_rocks"] = max(0, min(2, int(raw.get("tinted_rocks") or 0)))
        out["hide_tinted_rocks"] = bool(raw.get("hide_tinted_rocks")) and cruelty >= 3

        mode = raw.get("layout_pickups") if raw.get("layout_pickups") in LAYOUT_PICKUP_MODES else "keep"
        if mode == "troll_bombs" and troll_budget <= 0:
            mode = "to_pennies"
            notes.append("troll_bombs->to_pennies (budget/mercy)")
        if mode == "troll_bombs":
            troll_budget -= 1
        out["layout_pickups"] = mode

        bonus = []
        room_trolls = int(mode == "troll_bombs")               # at most one live bomb per room
        for r in (raw.get("bonus_pickups") or [])[:2]:
            if r not in REWARDS:
                continue
            if r == "troll_bomb":
                if troll_budget <= 0 or room_trolls:
                    notes.append("extra troll_bomb dropped")
                    continue
                troll_budget -= 1
                room_trolls += 1
            if afford(r, 1):
                bonus.append(r)
        out["bonus_pickups"] = bonus

        # What every tinted rock in the room drops. Empty = the game's own loot. Only charged when bombed.
        asked = raw.get("tinted_rock_loot") if isinstance(raw.get("tinted_rock_loot"), list) else []
        loot: list[str] = []
        for r in [r for r in asked if isinstance(r, str) and r in REWARDS][:2]:
            if r == "troll_bomb":
                if troll_budget <= 0 or r in loot:
                    notes.append("tinted rock troll_bomb dropped (budget/mercy)")
                    continue
                troll_budget -= 1
            loot.append(r)
        if not loot and "nothing" in asked:
            if mercy or cruelty < 3:
                notes.append("empty tinted rocks -> the game's loot (cruelty/mercy)")
            else:
                loot = ["nothing"]
        out["tinted_rock_loot"] = loot

    elif kind == "room_clear":
        cap = 2 if ctx.get("mode") == "bonus" else 4       # boss rooms keep their own reward too
        rewards, total = [], 0
        for r in (raw.get("rewards") or [])[:3]:
            name = r.get("reward")
            if name not in REWARDS:
                continue
            n = max(1, min(3, int(r.get("count") or 1)))
            if name == "troll_bomb":
                n = min(n, troll_budget, 1)                     # one live bomb per clear
                troll_budget -= n
                if n <= 0:
                    notes.append("troll_bomb dropped (budget/mercy)")
                    continue
            if name in ("golden_key", "golden_bomb", "eternal_heart"):
                n = 1
            n = min(n, cap - total)
            if n <= 0:
                break
            n = afford(name, n)
            if n <= 0:
                continue
            total += n
            rewards.append({"reward": name, "count": n})
        # Drought breaker: the house may starve you, but not soft-lock you.
        res = (state.get("player") or {}).get("resources") or {}
        need = (state.get("level") or {}).get("needs") or {}
        names = {r["reward"] for r in rewards}
        if (int(res.get("keys", 0)) == 0 and int(need.get("locked_doors_seen", 0)) > 0
                and int(counters.get("clears_since_key", 0)) >= 7 and not (names & KEYS)):
            rewards.append({"reward": "key", "count": 1})
            notes.append("drought breaker: key")
        elif (int(res.get("bombs", 0)) == 0 and int(need.get("bomb_targets", 0)) > 0
              and int(counters.get("clears_since_bomb", 0)) >= 9 and not (names & BOMBS)):
            rewards.append({"reward": "bomb", "count": 1})
            notes.append("drought breaker: bomb")
        if mercy and not (names & HEARTS) and int(counters.get("clears_since_heart", 0)) >= 5:
            rewards.append({"reward": "half_heart", "count": 1})
            notes.append("mercy: half heart")
        out["rewards"] = rewards

    elif kind == "drops":
        wanted = _wanted(ctx, 8, 8)
        left = kill_drops_left(state, cruelty, counters)
        outcomes = []
        for r in (raw.get("outcomes") or [])[:wanted]:
            name = r.get("reward") if isinstance(r, dict) else None
            n = max(1, min(2, _int(r.get("count") if isinstance(r, dict) else 1, 1)))
            if name not in REWARDS:
                name = "nothing"                           # every entry is one kill: keep its place
            if name == "troll_bomb":
                if troll_budget <= 0:
                    notes.append("troll_bomb -> nothing (budget/mercy)")
                    name = "nothing"
                else:
                    n, troll_budget = 1, troll_budget - 1
            elif name in TRAPS and mercy:
                notes.append(f"{name} -> nothing (mercy)")
                name = "nothing"
            if name in ("golden_key", "golden_bomb", "eternal_heart"):
                n = 1
            if name != "nothing":
                if left <= 0:
                    notes.append(f"{name} -> nothing (this floor's kill drops are spent)")
                    name = "nothing"
                else:
                    n = afford(name, n)
                    if n <= 0:
                        name, n = "nothing", 1
                    else:
                        left -= 1
            outcomes.append({"reward": name, "count": n if name != "nothing" else 1})
        out["outcomes"] = outcomes

    elif kind == "payout_plan":
        out["schedule"] = _clamp_schedule(raw.get("schedule") or [], ctx, cruelty, mercy, troll_budget, notes)

    elif kind == "item_choice":
        cands = [int(c["id"]) for c in ctx.get("candidates", []) if "id" in c]
        seen, order = set(), []
        for i in raw.get("order") or []:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if i in cands and i not in seen:
                order.append(i)
                seen.add(i)
        order += [c for c in cands if c not in seen]       # never return an empty pool
        out["order"] = order

    elif kind == "floor_plan":
        allowed = max_curses(cruelty, mercy)
        add = []
        for c in raw.get("add_curses") or []:
            if c not in CURSES or c in add:
                continue
            if mercy and c == "unknown":
                notes.append("unknown -> none (mercy)")         # don't hide HP from a dying player
            elif len(add) >= allowed:
                notes.append(f"{c} -> none (no curses below cruelty 3)" if allowed == 0
                             else f"{c} -> none (at most {allowed} curse{'s' if allowed > 1 else ''} at this cruelty)")
            else:
                add.append(c)
        out["add_curses"] = add
        out["strip_vanilla_curses"] = bool(raw.get("strip_vanilla_curses"))
        td = raw.get("treasure_distance")
        out["treasure_distance"] = td if td in TREASURE_DISTANCE else "any"

    elif kind == "cards":
        out["cards"] = [{"key": c, "enum": CARDS[c][0], "cat": CARDS[c][1]}
                        for c in (raw.get("cards") or [])[:_wanted(ctx, 5, 5)] if c in CARDS]

    elif kind == "trinkets":
        cands = {int(c["id"]) for c in ctx.get("trinket_candidates", []) if "id" in c}
        trinkets: list[int] = []
        for i in raw.get("trinkets") or []:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if i in cands and i not in trinkets and len(trinkets) < _wanted(ctx, 4, 4):
                trinkets.append(i)
        out["trinkets"] = trinkets

    elif kind == "pills":
        out["pills"] = _clamp_pills(raw.get("pills") or [], ctx, cruelty, notes)

    else:
        raise ValueError(f"unknown decision kind {kind!r}")

    if notes:
        out["guardrails"] = notes
    return out


def _clamp_schedule(raw: list, ctx: dict, cruelty: int, mercy: bool, troll_budget: int,
                    notes: list[str]) -> list[dict]:
    """A beggar/machine's per-use payouts, or a chest's contents, held to the same rules as a clear.
    A machine also keeps a house edge: over its uses it pays back at most this share of what it was paid
    (shop prices), plus one small win up front; at most one 'item' jackpot, outside the edge."""
    chest = ctx.get("source_class") == "chest"
    has_item = bool(ctx.get("has_item_pool"))
    limit = 4 if chest else 6
    max_nothing = 0 if cruelty < 3 else 1 + cruelty // 4    # consecutive stiffed uses; 0 = it always pays
    payment = PAYMENT.get(str(ctx.get("source")), 1)
    ratio = 1.0 - 0.05 * cruelty
    out, run, pickups, paid, items = [], 0, 0, 0.0, 0
    for r in raw[:limit] if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        name = r.get("reward")
        if name == "item" and (not has_item or items >= 1):
            continue
        if name not in REWARDS and name not in PAYOUT_OUTCOMES:
            continue
        try:
            n = max(1, min(2, int(r.get("count") or 1)))
        except (TypeError, ValueError):
            n = 1
        if not chest and name not in ("nothing", "item"):
            allowed = ratio * payment * (len(out) + 1) + 5
            if paid + SHOP_VALUE.get(name, 5) * n > allowed + 1e-9:
                notes.append(f"use {len(out) + 1}: {name} -> nothing (house edge)")
                name, n = "nothing", 1
        if name == "nothing":
            if mercy:
                name, n = "half_heart", 1
                notes.append("mercy: nothing->half_heart")
            elif chest and (cruelty < 5 or out):
                continue                                    # an empty chest is a one-line joke, never a pattern
            else:
                run += 1
                if run > max_nothing:
                    notes.append("nothing->penny (it has to pay sometimes)")
                    name, n, run = "penny", 1, 0
        if name != "nothing":
            run = 0
        if name == "item":
            n, items = 1, items + 1
        elif name == "troll_bomb":
            n = min(n, troll_budget, 1)
            troll_budget -= n
            if n <= 0:
                notes.append("troll_bomb dropped (budget/mercy)")
                continue
        elif name in ("golden_key", "golden_bomb", "eternal_heart"):
            n = 1
        if chest:
            n = min(n, 4 - pickups)
            if n <= 0:
                break
            pickups += n if name != "nothing" else 0
        if name != "item":
            paid += SHOP_VALUE.get(name, 5) * n
        out.append({"reward": name, "count": n})
    return out


def _clamp_pills(raw: list, ctx: dict, cruelty: int, notes: list[str]) -> list[dict]:
    """Pill colours the DM may still assign, with a cruelty-scaled cap on how many are bad.

    The cap counts the whole run (colours already swallowed included), since a colour's
    effect is permanent. Over-cap bad pills become their good counterpart.
    """
    open_colors = {int(c["color"]) for c in ctx.get("pill_colors_to_assign", []) if "color" in c}
    locked = ctx.get("pills_locked") or []
    bad = sum(1 for p in locked if PILLS.get(p.get("effect"), ("",))[0] == "bad")
    max_bad = round((len(locked) + len(open_colors)) * min(1.0, 0.4 + 0.06 * cruelty))
    out, seen = [], set()
    for p in raw:
        try:
            color = int(p.get("color"))
        except (TypeError, ValueError, AttributeError):
            continue
        effect = p.get("effect")
        if color not in open_colors or color in seen or effect not in PILLS:
            continue
        if PILLS[effect][0] == "bad":
            if bad >= max_bad:
                notes.append(f"pill {color}: {effect}->{PILLS[effect][2]} (bad-pill cap)")
                effect = PILLS[effect][2]
            else:
                bad += 1
        seen.add(color)
        kind, _, counter = PILLS[effect]
        entry = {"color": color, "effect": effect, "enum": _pill_enum(effect)}
        if counter and kind == "bad":
            entry["phd"] = _pill_enum(counter)            # what PHD turns it into when swallowed
        elif counter and kind == "good":
            entry["false_phd"] = _pill_enum(counter)
        out.append(entry)
    return out


def _pill_enum(effect: str) -> str:
    return "PILLEFFECT_" + effect.upper()


def clamp_diff(kind: str, raw: Any, out: Any) -> list[str]:
    """What clamp() changed: "asked -> got" for every field the model didn't get as asked.

    Fields the model left out (their defaults) don't count. Text fields (intent, taunt, scheme)
    are only tidied, so they never do either.
    """
    if not isinstance(raw, dict) or not isinstance(out, dict):
        return []
    diffs: list[str] = []

    def num(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def counted(items: Any, name: str) -> str:
        bits = [f'{int(num(i.get("count"), 1))}x {i.get(name)}' for i in items or [] if isinstance(i, dict)]
        return ", ".join(bits) or "none"

    def listed(items: Any) -> str:
        return ", ".join(str(i) for i in items or []) or "none"

    def check(label: str, asked: str, got: str) -> None:
        if asked != got:
            diffs.append(f"{label} {asked} -> {got}")

    if kind == "room_layout":
        if "champion_chance" in raw:
            check("champion chance", f'{round(100 * num(raw["champion_chance"]))}%',
                  f'{round(100 * num(out.get("champion_chance")))}%')
        if raw.get("champion_color") not in (None, "random"):
            check("champion color", str(raw["champion_color"]), str(out.get("champion_color")))
        if "extra_enemies" in raw:
            check("extra enemies", counted(raw["extra_enemies"], "enemy"), counted(out.get("extra_enemies"), "key"))
        if "tinted_rocks" in raw:
            check("tinted rocks", str(int(num(raw["tinted_rocks"]))), str(out.get("tinted_rocks")))
        if raw.get("hide_tinted_rocks"):
            check("hide tinted rocks", "yes", "yes" if out.get("hide_tinted_rocks") else "no")
        if raw.get("tinted_rock_loot"):
            check("tinted rock loot", listed(raw["tinted_rock_loot"]), listed(out.get("tinted_rock_loot")))
        if raw.get("layout_pickups") not in (None, "keep"):
            check("layout pickups", str(raw["layout_pickups"]), str(out.get("layout_pickups")))
        if "bonus_pickups" in raw:
            check("bonus pickups", listed(raw["bonus_pickups"]), listed(out.get("bonus_pickups")))
    elif kind == "room_clear":
        check("rewards", counted(raw.get("rewards"), "reward"), counted(out.get("rewards"), "reward"))
    elif kind == "payout_plan":
        check("schedule", counted(raw.get("schedule"), "reward"), counted(out.get("schedule"), "reward"))
    elif kind == "item_choice":
        valid = {str(i) for i in out.get("order") or []}
        ignored = [str(i) for i in raw.get("order") or [] if str(i).strip() not in valid]
        if ignored:
            diffs.append("ignored ids not in the pool: " + ", ".join(ignored))
    elif kind == "floor_plan":
        check("curses", listed(raw.get("add_curses")), listed(out.get("add_curses")))
        if raw.get("treasure_distance") not in (None, "any"):
            check("treasure", str(raw["treasure_distance"]), str(out.get("treasure_distance")))
    elif kind == "drops":
        check("outcomes", counted(raw.get("outcomes"), "reward"), counted(out.get("outcomes"), "reward"))
    elif kind == "cards":
        check("cards", listed(raw.get("cards")), listed(c["key"] for c in out.get("cards") or []))
    elif kind == "trinkets":
        check("trinkets", listed(raw.get("trinkets")), listed(out.get("trinkets")))
    elif kind == "pills":
        asked ={str(p.get("color")): p.get("effect") for p in raw.get("pills") or [] if isinstance(p, dict)}
        got = {str(p["color"]): p["effect"] for p in out.get("pills") or []}
        for color, effect in asked.items():
            if got.get(color) != effect:
                diffs.append(f"pill {color} {effect} -> {got.get(color, 'dropped')}")
    return diffs


def summarize_categories(rewards: list[dict]) -> dict[str, int]:
    """Count rewards by resource family (used to update drought counters)."""
    fam = {"keys": 0, "bombs": 0, "coins": 0, "hearts": 0, "traps": 0}
    for r in rewards:
        n, name = int(r.get("count", 1)), r.get("reward")
        if name in KEYS: fam["keys"] += n
        if name in BOMBS: fam["bombs"] += n
        if name in COINS: fam["coins"] += n
        if name in HEARTS: fam["hearts"] += n
        if name in TRAPS: fam["traps"] += n
    return fam
