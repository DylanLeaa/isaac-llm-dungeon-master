"""The after-death stats screen: what the run came to, from both sides of the table.

The game sends its own side (character, floor reached, time, hits taken...) with the death or
victory event. This adds the DM's side, tallied from the decisions the game actually used (so a
continued run's receipt, replayed from its file, still counts), the con it was running and its
last word. The DM's epitaph for the run follows in a second message when it has been written.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from catalog import PILLS
from schemas import _clean_text, summarize_categories

EPITAPH_CHARS = 120
MAX_ROWS = 10


def run_title(narrative: str | None) -> str | None:
    """The TITLE: line of the Opus run narrative, if one was written."""
    m = re.search(r"^\s*TITLE:\s*(.+)$", narrative or "", re.M)
    return _clean_text(m.group(1), 48) if m else None


def _game_rows(rows: Any) -> list[list[str]]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list) and len(row) == 2:
            out.append([_clean_text(row[0], 20), _clean_text(row[1], 24)])
    return out[:MAX_ROWS]


def dm_side(applied: list[tuple[str, dict]], totals: dict, pills: Any) -> tuple[list[list[str]], str | None, str | None]:
    """Rows for the DM's column, plus its latest scheme and taunt among the decisions the game used."""
    c, curses, scheme, taunt = Counter(), [], None, None
    for kind, d in applied:
        if kind == "room_layout":
            c["rooms"] += 1
            c["enemies"] += sum(int(e.get("count", 0)) for e in d.get("extra_enemies", []))
            c["troll"] += int(d.get("layout_pickups") == "troll_bombs") + d.get("bonus_pickups", []).count("troll_bomb")
        elif kind == "room_clear":
            rewards = d.get("rewards", [])
            c.update(summarize_categories(rewards))
            c["troll"] += sum(int(r.get("count", 1)) for r in rewards if r.get("reward") == "troll_bomb")
        elif kind == "payout_plan":
            c.update(summarize_categories(d.get("schedule", [])))
        elif kind == "floor_plan":
            curses += [x for x in d.get("add_curses", []) if x not in curses]
        scheme = d.get("scheme") or scheme
        taunt = d.get("taunt") or taunt
    bad = sum(1 for p in pills if isinstance(p, dict) and p.get("by") == "dm"
              and PILLS.get(p.get("effect"), ("",))[0] == "bad") if isinstance(pills, list) else 0
    rows = [
        ["Rooms dressed", str(c["rooms"])],
        ["Extra enemies", str(c["enemies"])],
        ["Keys / bombs", f'{c["keys"]} / {c["bombs"]}'],
        ["Coins / hearts", f'{c["coins"]} / {c["hearts"]}'],
        ["Troll bombs", str(c["troll"])],
        ["Curses", ", ".join(curses) or "none"],
        ["Bad pills", str(bad)],
        ["DM gave up", str(totals.get("fallback", 0))],
        ["LLM bill", f'{totals.get("calls", 0)} calls ${totals.get("cost", 0):.2f}'],
    ]
    return rows, scheme, taunt


def build(outcome: str, game: dict, receipt, run_id: Any, narrative: str | None) -> dict:
    """The whole screen, as sent to the game (without the epitaph, which comes later)."""
    rows, scheme, taunt = dm_side(receipt.applied(), receipt.totals(), game.get("pills"))
    return {
        "run": game.get("run", run_id),
        "outcome": outcome,
        "character": _clean_text(game.get("character"), 24) or None,
        "title": run_title(narrative),
        "sections": [{"title": "YOU", "rows": _game_rows(game.get("rows"))},
                     {"title": "THE DM", "rows": rows}],
        "scheme": _clean_text(scheme, 160) or None,
        "taunt": _clean_text(taunt, 70) or None,
        "epitaph": None,
    }


def as_text(summary: dict) -> str:
    """The summary as plain lines, for the backend log and the epitaph prompt."""
    lines = [f'{summary.get("character") or "The player"}: {summary["outcome"]}'
             + (f' in "{summary["title"]}"' if summary.get("title") else "")]
    for sec in summary["sections"]:
        lines.append(sec["title"] + ": " + "; ".join(f"{label} {value}" for label, value in sec["rows"]))
    for key, label in (("scheme", "your scheme"), ("taunt", "your last taunt"), ("epitaph", "epitaph")):
        if summary.get(key):
            lines.append(f"{label}: {summary[key]}")
    return "\n".join(lines)


def one_line(summary: dict) -> str:
    """Short form for the receipt menu."""
    you = dict(map(tuple, summary["sections"][0]["rows"]))
    bits = [summary["outcome"]] + [f"{k.lower()} {you[k]}" for k in ("Reached", "Time") if k in you]
    return ", ".join(bits)


def epitaph_prompt(summary: dict) -> str:
    ending = ("died. Write the epitaph you'd carve on their tombstone"
              if summary["outcome"] == "death" else "won. Write your grudging toast")
    return json.dumps({
        "decision": "epitaph",
        "instructions": f"The run is over: the player {ending}. One or two sentences, at most "
                        f"{EPITAPH_CHARS} characters, plain ASCII, in your voice, about this run in particular. "
                        "Reply with the text only: no tool call, no quotes.",
        "run": as_text(summary),
    }, separators=(",", ":"))


def clean_epitaph(text: str) -> str:
    return _clean_text(str(text).strip().strip('"\''), EPITAPH_CHARS)
