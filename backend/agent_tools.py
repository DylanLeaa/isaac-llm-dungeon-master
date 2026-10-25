"""Agent mode's tools: what one agent turn can do to the decks, built from the decision schemas.

  stack_<kind>   write (or rewrite) a deck: the `decide_<kind>` schema plus the deck's `key`
  shuffle_deck   reorder what is left of a deck; nothing is added or removed, so the budget doesn't move
  keep_deck      answer a pending request with the deck as it stands

A turn answers with as many of these as it needs, applied in order against one budget.
"""
from __future__ import annotations

from typing import Any

from schemas import KINDS, TOOLS

STACK = "stack_"
SHUFFLE = "shuffle_deck"
KEEP = "keep_deck"

_KEY = {"type": "string",
        "description": "The deck's key, exactly as listed in `pending` or `open_decks` (e.g. 'drops:3')."}


def _stack_tool(kind: str) -> dict[str, Any]:
    t = TOOLS[kind]
    schema = t["input_schema"]
    return {
        "name": STACK + kind,
        "description": f"Stack the {kind} deck under `key`. {t['description']}",
        "input_schema": {**schema, "properties": {"key": _KEY, **schema["properties"]},
                         "required": ["key", *schema["required"]]},
    }


AGENT_TOOLS: dict[str, dict[str, Any]] = {
    **{STACK + k: _stack_tool(k) for k in KINDS},
    SHUFFLE: {
        "name": SHUFFLE,
        "description": "Reorder the unused entries of an open deck (or a pending request's current deck) "
                       "without changing what is in it: the floor's budget doesn't move. `order` lists indexes "
                       "into the deck's `left`, 0 first; any left out keep their order after the ones listed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": _KEY,
                "order": {"type": "array", "items": {"type": "integer", "minimum": 0},
                          "description": "Indexes into the deck's `left`, in the new draw order."},
                "taunt": {"type": "string", "maxLength": 60,
                          "description": "Optional new taunt for the deck. Leave out to keep its own."},
            },
            "required": ["key", "order"],
        },
    },
    KEEP: {
        "name": KEEP,
        "description": "Answer a pending request with its deck as it stands: nothing meaningful changed. Only "
                       "for keys in `pending` that already have a deck (context.your_previous_answer).",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": _KEY,
                "why": {"type": "string", "maxLength": 120, "description": "One short line: why it still fits."},
            },
            "required": ["key"],
        },
    },
}

# The same tools in the OpenAI-compatible shape (OpenRouter, Google AI Studio).
OPENAI_AGENT_TOOLS = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in AGENT_TOOLS.values()]


def stack_kind(tool: str) -> str | None:
    """The decision kind a `stack_<kind>` tool writes; None for any other tool."""
    if tool.startswith(STACK) and tool[len(STACK):] in KINDS:
        return tool[len(STACK):]
    return None


def shuffle_order(order: Any, n: int) -> list[int]:
    """`order` as a full permutation of range(n): bad or repeated indexes are dropped, any left out follow
    in their old order."""
    out: list[int] = []
    for i in order if isinstance(order, list) else []:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in out:
            out.append(i)
    return out + [i for i in range(n) if i not in out]
