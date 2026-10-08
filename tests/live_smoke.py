"""One real call per decision kind to Claude Haiku 5.5 via OpenRouter (needs OPENROUTER_API_KEY).

    python tests/live_smoke.py [--cruelty 8]

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
from dm_agent import OpenRouterDM  # noqa: E402
from memory import RunMemory  # noqa: E402
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
]


async def main(cruelty: int) -> None:
    dm, mem = OpenRouterDM(cruelty), RunMemory()
    mem.events.extend(["cleared room 84 in 9s, took 1 hits", "clear reward: 1x penny"])
    for kind, ctx in CASES:
        t0 = time.perf_counter()
        raw = await dm.decide(kind, STATE, ctx, mem.digest())
        ms = (time.perf_counter() - t0) * 1000
        out = clamp(kind, raw, STATE, ctx, cruelty, mem.counters)
        print(f"\n== {kind}  ({ms:.0f} ms)\n{json.dumps(out, indent=2)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cruelty", type=int, default=7)
    asyncio.run(main(ap.parse_args().cruelty))
