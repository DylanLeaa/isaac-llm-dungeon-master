"""One real call per decision kind to Claude Haiku 5.5 via OpenRouter (needs OPENROUTER_API_KEY).

    python tests/live_smoke.py [--cruelty 8] [--planners]

--backend claude-code sends the same calls through your local Claude Code (`claude -p`) instead.

--planners first writes a real run narrative (Opus 5.5) and floor brief (Sonnet 5.5) and
gives them to the DM, so the decisions show whether Haiku follows the campaign.

Prints each clamped decision and its latency so you can sanity-check the DM's voice
and see whether your latency fits the prefetch lead times before launching the game.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from claude_code import ClaudeCodeDM, ClaudeCodePlanner  # noqa: E402
from dm_agent import OpenRouterDM  # noqa: E402
from memory import RunMemory  # noqa: E402
from planners import OpenRouterPlanner, campaign_text  # noqa: E402
from schemas import clamp  # noqa: E402

STATE = {
    "player": {"character": "Isaac", "hp": {"red": 3, "max_red": 6, "soul": 0},
               "resources": {"coins": 14, "bombs": 0, "keys": 0},
               "stats": {"damage": 3.5, "tears_delay": 10, "speed": 1.0, "luck": 0},
               "items": ["Sad Onion (q3)", "Cricket's Body (q3)"]},
    "level": {"stage": 2, "name": "Basement II", "normal_rooms_unvisited": 6,
              "needs": {"locked_doors_seen": 2, "secret_rooms_unfound": 1, "tinted_rocks_seen": 1,
                        "bomb_targets": 2, "shop_unvisited": False},
              "vanilla_expected_drops_rest_of_floor": {"keys": 0.3, "bombs": 0.3, "coins": 1.0}},
    "performance": {"half_hearts_lost_this_floor": 3, "rooms_cleared_this_floor": 4, "avg_clear_seconds": 11.2},
}
CASES = [
    ("room_layout", {"room_type": "default", "distance_from_start": 3}),
    ("room_clear", {"room_type": "default", "enemies_left": 1, "est_ms_until_clear": 1800}),
    ("item_choice", {"pool": 0, "room_type": "treasure", "candidates": [
        {"id": 1, "name": "The Sad Onion", "quality": 3}, {"id": 37, "name": "Mr. Boom", "quality": 0},
        {"id": 114, "name": "Mom's Knife", "quality": 4}, {"id": 46, "name": "Lucky Foot", "quality": 1}]}),
    ("floor_plan", {"current_stage": 2}),
    ("cards", {"reason": "restock", "wanted": 2, "deck_left": [], "drawn_so_far": []}),
    ("trinkets", {"reason": "restock", "wanted": 2, "deck_left": [], "drawn_so_far": [],
                  "trinket_candidates": [{"id": 1, "name": "Swallowed Penny"}, {"id": 19, "name": "Paper Clip"},
                                         {"id": 24, "name": "Butt Penny"}, {"id": 61, "name": "Left Hand"}]}),
    ("pills", {"reason": "a pill colour no answer covers is in play",
               "pills_locked": [{"color": 2, "look": "white_blue", "effect": "health_up"}],
               "pill_colors_to_assign": [{"color": 5, "look": "reddots_red"}, {"color": 9, "look": "orangedots_white"}]}),
    ("drops", {"reason": "player entered the room", "room_type": "default", "wanted": 5, "deck_left": [],
               "enemies": ["gaper", "gaper", "pooter"], "breakables": 1}),
]


async def plan(dm, planner, cruelty: int, digest: dict) -> None:
    t0 = time.perf_counter()
    narrative, u = await planner.narrative(STATE, cruelty)
    print(f"\n== narrative [{planner.narrative_model}]  ({time.perf_counter() - t0:.1f} s, "
          f"{u['in']}/{u['out']} tok, ${u['cost']:.4f})\n{narrative}")
    t0 = time.perf_counter()
    brief, u = await planner.floor_brief(STATE, narrative, digest, None, cruelty)
    print(f"\n== floor brief [{planner.floor_model}]  ({time.perf_counter() - t0:.1f} s, "
          f"{u['in']}/{u['out']} tok, ${u['cost']:.4f})\n{brief}")
    dm.set_campaign(campaign_text(narrative, brief, STATE["level"]["name"]))


async def main(cruelty: int, planners: bool, backend: str) -> None:
    if backend == "claude-code":
        dm, planner = ClaudeCodeDM(cruelty), ClaudeCodePlanner()
    else:
        dm, planner = OpenRouterDM(cruelty), OpenRouterPlanner()
    mem = RunMemory()
    mem.events.extend(["cleared room 84 in 9s, took 1 hits", "clear reward: 1x penny"])
    if planners:
        await plan(dm, planner, cruelty, mem.digest())
    for kind, ctx in CASES:
        t0 = time.perf_counter()
        raw = await dm.decide(kind, STATE, ctx, mem.digest())
        ms = (time.perf_counter() - t0) * 1000
        u = raw.get("_usage") or {}
        out = clamp(kind, raw, STATE, ctx, cruelty, mem.counters)
        print(f"\n== {kind}  ({ms:.0f} ms, {u.get('in', 0)} in / {u.get('cached', 0)} cached)\n"
              f"{json.dumps(out, indent=2)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cruelty", type=int, default=7)
    ap.add_argument("--planners", action="store_true", help="also run the Opus narrative + Sonnet floor brief")
    ap.add_argument("--backend", choices=["openrouter", "claude-code"], default="openrouter")
    args = ap.parse_args()
    asyncio.run(main(args.cruelty, args.planners, args.backend))
