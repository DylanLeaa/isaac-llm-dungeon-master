"""Headless run of the whole mod (main.lua + dm/*) on a fake Isaac API, against server.py --mock.

Walks a tiny floor: start room -> fight in a normal room -> treasure item -> boss room,
and checks the prefetch timing: the room-clear decision must be requested *during* the
fight (not at the end) and be ready when the last enemy dies.
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lupa.lua53 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "mod" / "llm_dungeon_master"


def main():
    tmp = tempfile.mkdtemp()
    srv = subprocess.Popen([sys.executable, "server.py", "--mock", "--bridge", "file", "--bridge-dir", tmp,
                            "--mock-latency", "0.3", "0.9"],
                           cwd=ROOT / "backend", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.0)
    try:
        run(tmp)
    finally:
        srv.terminate()
        log = srv.communicate(timeout=5)[0]
        print("\n--- backend log ---\n" + log)


def run(bridge_dir):
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    printed = []
    g.print = lambda *a: (printed.append(" ".join(map(str, a))), print("   [lua]", *a))

    def to_py(v):
        if g.type(v) == "table":
            keys = list(v.keys())
            if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [to_py(v[i]) for i in range(1, len(keys) + 1)]
            return {str(k): to_py(v[k]) for k in keys}
        return v

    jsonmod = lua.table_from({"encode": lambda v: json.dumps(to_py(v)),
                              "decode": lambda s: lua.table_from(json.loads(s), recursive=True)})
    real_require = g.require

    def fake_require(name):
        if name == "json":
            return jsonmod
        if name == "socket":
            raise RuntimeError("no socket")
        return real_require(name)

    g.require = fake_require
    W = lua.execute(open(ROOT / "tests" / "fake_isaac.lua").read())

    modules = {}

    def include(name):
        if name not in modules:
            path = MOD / (name.replace(".", "/") + ".lua")
            mod = lua.execute(open(path).read())
            if name == "dm.config":
                mod.BRIDGE_DIR = bridge_dir
                mod.TRANSPORT = "auto"
            modules[name] = mod
        return modules[name]

    g.include = include
    lua.execute(open(MOD / "main.lua").read())
    Bridge, Sched = modules["dm.bridge"], modules["dm.scheduler"]
    MC = g.ModCallbacks
    t0 = time.monotonic()

    def tick(n=1):
        for _ in range(n):
            W.now = int((time.monotonic() - t0) * 1000)
            W.frame += 1
            W.fire(MC.MC_POST_RENDER)
            W.fire(MC.MC_POST_UPDATE)
            time.sleep(1 / 30)

    def wait(pred, secs, what):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            tick()
            if pred():
                return
        raise AssertionError("timed out waiting for " + what)

    # --- run start in room 84 ----------------------------------------------------
    W.cur = 84
    W.rooms[84].VisitedCount = 1
    W.fire(MC.MC_POST_GAME_STARTED, False)
    wait(Bridge.ready, 5, "backend welcome")
    print("1. connected:", Bridge.status())
    wait(lambda: Bridge.peek("layout:1:85") and Bridge.peek("items:1:0"), 6, "neighbour prefetch")
    print("2. prefetched before the player moved: layout for room 85, treasure pool ranking")

    # --- walk into room 85 with three 10hp enemies -------------------------------
    enemies = lua.table_from([W.makeEnemy(10), W.makeEnemy(10), W.makeEnemy(10)])
    W.enemies = enemies
    before = len(W.spawned) + len(W.removedGrid)
    W.enter(85)
    assert Bridge.peek("layout:1:85") is None, "layout decision should have been consumed"
    tick()
    changes = len(W.spawned) + len(W.removedGrid) + len(W.gridSpawned) - before
    champs = sum(1 for i in range(1, 4) if enemies[i].champion)
    print(f"3. room 85 dressed on entry: {changes} spawns/grid edits, {champs} champions")

    # --- fight: burn 6 hp/s, watch when the clear decision gets requested -------
    requested_at_hp = None
    fight_start = time.monotonic()
    while True:
        dt = 1 / 30
        dmg = 6 * dt
        for i in range(1, 4):
            e = enemies[i]
            if e.HitPoints > 0:
                e.HitPoints = max(0, e.HitPoints - dmg)
                break
        tick()
        hp = sum(enemies[i].HitPoints for i in range(1, 4))
        if requested_at_hp is None and Bridge.pendingInfo("clear:1:85"):
            requested_at_hp = hp
            p90 = Bridge.latencyP90()
            print(f"4. clear decision requested with {hp:.1f}hp left (~{hp / 6:.1f}s to go);"
                  f" p90 latency {p90:.0f}ms -> lead {1.4 * p90 + 400:.0f}ms")
        if hp <= 0:
            break
    assert requested_at_hp is not None and requested_at_hp > 0, "clear was never prefetched"
    assert requested_at_hp < 30, "requested too early (should wait for the ETA window)"
    ready = Bridge.peek("clear:1:85") is not None
    ret = W.fire(MC.MC_PRE_SPAWN_CLEAN_AWARD, g.RNG(), g.Vector(320, 280))
    print(f"5. last enemy died after {time.monotonic() - fight_start:.1f}s; decision ready={ready};"
          f" PRE_SPAWN_CLEAN_AWARD returned {ret}")
    assert ret is True and ready

    # --- treasure item -----------------------------------------------------------
    W.onPreGetCollectible = lambda pool, dec, seed: W.fire(MC.MC_PRE_GET_COLLECTIBLE, pool, dec, seed)
    pool = g.ItemPoolType.POOL_TREASURE
    got = W.fire(MC.MC_PRE_GET_COLLECTIBLE, pool, True, 99)
    q = {i: (i % 5) for i in range(1, 9)}
    print(f"6. treasure pedestal -> item {got} (quality {q.get(got)}); removed from pool: {W.removedItem}")
    assert got is not None and W.removedItem == got

    # --- boss room -> plan next floor -> curse eval ------------------------------
    W.enemies = lua.table_from([W.makeEnemy(100)])
    W.enter(86)
    wait(lambda: Bridge.peek("floor:1:next"), 6, "floor plan")
    mask = W.fire(MC.MC_POST_CURSE_EVAL, 0)
    print(f"7. boss room prefetched the next floor; curse mask for floor 2 = {mask}")
    assert mask == 1

    errors = [p for p in printed if "error" in p.lower()]
    assert not errors, errors
    print("\nALL HEADLESS CHECKS PASSED")


if __name__ == "__main__":
    main()
