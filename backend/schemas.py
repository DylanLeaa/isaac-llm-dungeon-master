"""Decision schemas (as Claude tools) and the hard guardrails applied to them.

The model is free to be as nasty as it likes *inside* the schema; `clamp()` is
the referee that keeps every decision legal for the game and keeps runs
beatable. The mod trusts whatever comes out of clamp().
"""
from __future__ import annotations

import re
from typing import Any

from catalog import (BOMBS, CARDS, CHAMPION_COLORS, COINS, CURSES, ENEMIES, HEARTS, KEYS,
                     LAYOUT_PICKUP_MODES, PILLS, REWARDS, TRAPS, TREASURE_DISTANCE)

KINDS = ("room_layout", "room_clear", "item_choice", "floor_plan", "consumables")

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
                       "and the boss item; yours is extra.",
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
    "consumables": {
        "name": "decide_consumables",
        "description": "Plan the random consumables for this floor: the next cards/runes and trinkets the "
                       "game hands out (from any source: your drops, chests, layouts, machines), and the "
                       "effect of each pill colour in context.pill_colors_to_assign. A pill colour keeps "
                       "its effect for the rest of the run once it is swallowed. The taunt appears the "
                       "first time anything from this plan is used.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": _INTENT,
                "cards": {"type": "array", "maxItems": 5, "items": {"type": "string", "enum": list(CARDS)},
                          "description": "Handed out in this order. Draws that only allow runes (or "
                                         "forbid them) take the first card that fits."},
                "trinkets": {"type": "array", "maxItems": 4, "items": {"type": "integer"},
                             "description": "Ids from context.trinket_candidates, in hand-out order."},
                "pills": {"type": "array", "maxItems": 13,
                          "items": {"type": "object", "properties": {
                              "color": {"type": "integer"},
                              "effect": {"type": "string", "enum": list(PILLS)}},
                              "required": ["color", "effect"]},
                          "description": "One entry per colour in context.pill_colors_to_assign."},
                "taunt": _TAUNT, "scheme": _SCHEME,
            },
            "required": ["intent", "cards", "trinkets", "pills", "taunt"],
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


def clamp(kind: str, raw: dict, state: dict, ctx: dict, cruelty: int, counters: dict) -> dict:
    cruelty = max(0, min(10, int(cruelty)))
    mercy = mercy_active(state)
    floor_trolls = int(counters.get("troll_bombs_floor", 0))
    troll_budget = 0 if (mercy or cruelty < 4) else max(0, 1 + cruelty // 4 - floor_trolls)
    out: dict[str, Any] = {
        "intent": _clean_text(raw.get("intent"), 120),
        "taunt": _clean_text(raw.get("taunt"), 70),
        "scheme": _clean_text(raw.get("scheme"), 160),
    }
    notes: list[str] = []

    if kind == "room_layout":
        cap = min(0.6, 0.06 * cruelty)
        if mercy:
            cap = min(cap, 0.10)
        out["champion_chance"] = round(max(0.0, min(cap, float(raw.get("champion_chance") or 0))), 3)
        color = raw.get("champion_color")
        out["champion_color"] = color if color in CHAMPION_COLORS else "random"
        if color in CHAMPION_COLORS:
            out["champion_enum"] = CHAMPION_COLORS[color][0]

        budget = round(cruelty / 3.3)                       # 0..3 extra enemies
        if mercy:
            budget = min(budget, 1)
        if ctx.get("first_room_of_floor"):
            budget = 0
        extras, spent = [], 0
        for e in raw.get("extra_enemies") or []:
            spec = ENEMIES.get(e.get("enemy"))
            if not spec or spent >= budget:
                continue
            n = max(1, min(int(e.get("count") or 1), budget - spent))
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
            bonus.append(r)
        out["bonus_pickups"] = bonus

    elif kind == "room_clear":
        cap = 2 if ctx.get("mode") == "bonus" else 4             # boss rooms keep their own reward too
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
        max_curses = 2 if cruelty >= 7 else (1 if cruelty >= 3 else 0)
        if mercy:
            max_curses = min(max_curses, 1)
        add = []
        for c in raw.get("add_curses") or []:
            if c in CURSES and c not in add and len(add) < max_curses:
                if mercy and c == "unknown":
                    continue                                    # don't hide HP from a dying player
                add.append(c)
        out["add_curses"] = add
        out["strip_vanilla_curses"] = bool(raw.get("strip_vanilla_curses"))
        td = raw.get("treasure_distance")
        out["treasure_distance"] = td if td in TREASURE_DISTANCE else "any"

    elif kind == "consumables":
        out["cards"] = [{"key": c, "enum": CARDS[c][0], "cat": CARDS[c][1]}
                        for c in (raw.get("cards") or [])[:5] if c in CARDS]
        cands = {int(c["id"]) for c in ctx.get("trinket_candidates", []) if "id" in c}
        trinkets: list[int] = []
        for i in raw.get("trinkets") or []:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if i in cands and i not in trinkets and len(trinkets) < 4:
                trinkets.append(i)
        out["trinkets"] = trinkets
        out["pills"] = _clamp_pills(raw.get("pills") or [], ctx, cruelty, notes)

    else:
        raise ValueError(f"unknown decision kind {kind!r}")

    if notes:
        out["guardrails"] = notes
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
