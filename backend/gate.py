"""The reflex layer: a small, fast model that decides whether a re-asked decision is worth a real DM call.

The game re-asks a key (same room, same pedestal) whenever its context moves. Most of those moves are
noise to the DM (a coin picked up, a different `reason` string), and the answer it already issued still
fits. The gate sees only what changed, and the answer it would be reusing, and says keep or re-ask.
It fails open: any error, timeout or unreadable reply means "re-ask", so the DM never gets worse.
"""
from __future__ import annotations

import json
import logging
from typing import Any


log = logging.getLogger("llmdm")

GATE_URL = "https://openrouter.ai/api/alpha/decisions"
GATE_DEFAULT_MODEL = "inception/mercury-decide:free"
GATE_TIMEOUT_S = 2.5
GATE_THRESHOLD = 0.35        # re-ask when P(needs a new answer) reaches this; low, because a stale answer costs more

# Present in every snapshot but meaningless to the DM.
_IGNORED = {"frame", "reason", "avg_clear_seconds", "difficulty", "seconds_in_room", "est_ms_until_clear"}
_MAX_DIFFS = 30

GATE_QUESTION = {
    "type": "noul",
    "instructions": (
        "A game-master AI already answered a decision in a roguelike, and the game has asked again. "
        "`earlier_answer` is what it said; `changes_since` is everything that moved since. Does the game-master "
        "need to think again? Yes if: a resource the answer plays on (keys, bombs, coins, hearts) crossed zero or "
        "went from scarce to plentiful or back; health changed enough to change how cruel the answer can be; an "
        "item was taken or lost that changes the right choice; the room, floor or candidates changed; "
        "`campaign_changed` is true; or the earlier answer refers to something no longer true. No if the changes "
        "are small, cosmetic or unrelated (a coin or two, timers, counters, an unrelated room cleared). "
        "`plan` is the floor brief the game-master is following, if any. If the plan already dictates the earlier "
        "answer and still dictates it after the changes, it still fits, even after a big swing. A change that "
        "trips one of the plan's own conditions (for example a health threshold) is a reason to think again."),
    "criteria": {"true": "The earlier answer no longer fits; a new one is needed",
                 "false": "The earlier answer still fits"},
}


def _flatten(obj: Any, path: str = "") -> dict[str, Any]:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _IGNORED:
                continue
            out.update(_flatten(v, f"{path}.{k}" if path else str(k)))
        return out
    return {path: obj}


def diff(old: Any, new: Any) -> dict[str, str]:
    """Dotted paths that changed between two snapshots, as 'old -> new'. Capped; volatile fields skipped."""
    a, b = _flatten(old), _flatten(new)
    out: dict[str, str] = {}
    for path in sorted(set(a) | set(b)):
        if a.get(path) != b.get(path):
            out[path] = f"{_short(a.get(path))} -> {_short(b.get(path))}"
            if len(out) >= _MAX_DIFFS:
                out["..."] = "more changes omitted"
                break
    return out


def _short(v: Any) -> str:
    s = json.dumps(v, separators=(",", ":"), default=str)
    return s if len(s) <= 80 else s[:77] + "..."


def new_events(old: list, new: list) -> list:
    """Events in the memory digest that were not there when the earlier answer was written."""
    seen = set(old or [])
    return [e for e in (new or []) if e not in seen]


class OpenRouterGate:
    """OpenRouter's typed-decisions endpoint: one yes/no question, answered as a probability."""

    def __init__(self, model: str = GATE_DEFAULT_MODEL, threshold: float = GATE_THRESHOLD,
                 api_key: str | None = None) -> None:
        import os

        import httpx
        self.client = httpx.AsyncClient(timeout=GATE_TIMEOUT_S, headers={
            "Authorization": f"Bearer {api_key or os.environ['OPENROUTER_API_KEY']}",
            "X-OpenRouter-Title": "LLM Dungeon Master",
        })
        self.model = model
        self.threshold = threshold

    async def should_reask(self, kind: str, previous: dict, changes: dict, campaign_changed: bool,
                           plan: str | None = None) -> tuple[bool, str, dict]:
        """(reask, why, call_record). Raises on transport errors or a missing answer: the caller re-asks."""
        state = json.dumps({"decision": kind, "campaign_changed": campaign_changed, "plan": plan or None,
                            "earlier_answer": previous, "changes_since": changes}, separators=(",", ":"))
        resp = await self.client.post(GATE_URL, json={
            "model": self.model, "state": state, "questions": {"needs_new_answer": GATE_QUESTION}})
        resp.raise_for_status()
        data = resp.json()
        p = float(data["answers"]["needs_new_answer"]["noul"])
        u = data.get("usage") or {}
        record = {"model": data.get("model") or self.model, "system": [], "user": state, "response": data.get("answers"),
                  "usage": {"in": int(u.get("prompt_tokens") or 0), "cached": 0,
                            "out": int(u.get("completion_tokens") or 0), "cost": float(u.get("cost") or 0.0)}}
        return p >= self.threshold, f"P(new answer needed)={p:.2f}", record


class MockGate:
    """Offline stand-in: keep unless a resource crossed zero."""

    model = "mock"

    async def should_reask(self, kind: str, previous: dict, changes: dict, campaign_changed: bool,
                           plan: str | None = None) -> tuple[bool, str, dict]:
        crossed = any(p.startswith("state.player.resources") for p in changes)
        return crossed, "mock: resource changed" if crossed else "mock: nothing that matters",\
            {"model": "mock", "finish": "stop"}
