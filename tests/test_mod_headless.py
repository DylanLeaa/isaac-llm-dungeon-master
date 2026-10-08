"""Headless run of the whole mod (main.lua + dm/*) on a fake Isaac API, against server.py --mock.

Walks a tiny floor: start room -> fight in a normal room -> treasure item -> consumables ->
a golden chest -> boss room, and checks the prefetch timing: the room-clear decision must be
requested *during* the fight (not at the end) and be ready when the last enemy dies.
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
                            "--mock-latency", "0.3", "0.9", "--receipts", str(Path(tmp) / "receipts")],
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
    # Minimal Mod Config Menu: records what the mod registers.
    mcm = lua.execute("""
        local M = { OptionType = { TEXT = 1, SPACE = 2, NUMBER = 3, TITLE = 9 }, settings = {} }
        function M.AddSetting(cat, sub, s) M.settings[#M.settings + 1] = { cat = cat, sub = sub, s = s } end
        return M""")
    g.ModConfigMenu = mcm
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
    # --- loading screen: input is blocked until the backend says the narrative is in ----
    Loading = modules["dm.loading"]
    assert Loading.active(), "a new run starts on the loading screen"
    assert Loading.blockInput(None, None, g.InputHook.IS_ACTION_PRESSED, 7) is False
    assert Loading.blockInput(None, None, g.InputHook.GET_ACTION_VALUE, 7) == 0
    assert Loading.blockInput(None, None, g.InputHook.IS_ACTION_PRESSED, g.ButtonAction.ACTION_PAUSE) is None
    wait(lambda: not Loading.active(), 5, "the narrative (mock) to land and the loading screen to end")
    assert Loading.blockInput(None, None, g.InputHook.IS_ACTION_PRESSED, 7) is None, "input is back"
    assert any(json.loads(l).get("t") == "prepare" for l in open(Path(bridge_dir) / "to_dm.jsonl") if l.strip())
    print("   loading screen held input until the narrative was in, then released it")

    # --- DM Settings menu: cruelty slider, applied live and saved -----------------
    menu = [mcm.settings[i] for i in range(1, len(mcm.settings) + 1)]
    general = [e for e in menu if e.sub == "General"]
    assert len(general) == 1, "DM Settings registered once (again on game start is a no-op)"
    entry = general[0]
    assert entry.cat == "DM Settings" and entry.s.CurrentSetting() == -1
    assert entry.s.Display() == "Cruelty: backend default"
    entry.s.OnChange(3)
    tick(3)
    to_dm = [json.loads(l) for l in open(Path(bridge_dir) / "to_dm.jsonl") if l.strip()]
    assert {"t": "settings", "cruelty": 3} in to_dm, "cruelty change should reach the backend"
    assert entry.s.Display() == "Cruelty: 3/10 (mischievous)" and json.loads(W.saved)["cruelty"] == 3
    print("   DM Settings -> cruelty 3 sent to backend and saved")
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

    def fight(enemies, n, key, dps):
        """Burn enemy HP at `dps`; return the HP left when the clear decision was requested."""
        requested_at_hp = None
        while True:
            for i in range(1, n + 1):
                e = enemies[i]
                if e.HitPoints > 0:
                    e.HitPoints = max(0, e.HitPoints - dps / 30)
                    break
            tick()
            hp = sum(enemies[i].HitPoints for i in range(1, n + 1))
            if requested_at_hp is None and Bridge.pendingInfo(key):
                requested_at_hp = hp
            if hp <= 0:
                return requested_at_hp

    # --- fight: burn 6 hp/s, watch when the clear decision gets requested -------
    fight_start = time.monotonic()
    requested_at_hp = fight(enemies, 3, "clear:1:85", 6)
    assert requested_at_hp is not None and requested_at_hp > 0, "clear was never prefetched"
    p90 = Bridge.latencyP90()
    print(f"4. clear decision requested with {requested_at_hp:.1f}hp left (~{requested_at_hp / 6:.1f}s to go);"
          f" p90 latency {p90:.0f}ms -> lead {1.4 * p90 + 400:.0f}ms")
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

    # --- consumables: floor plan, then a pill colour the plan doesn't cover ------
    def plan():
        res = Bridge.peek("cons:1")
        return to_py(res.decision) if res else None

    wait(plan, 6, "consumables plan")
    W.heldPill = 5
    wait(lambda: any(int(p["color"]) == 5 for p in (plan() or {}).get("pills") or []), 8,
         "a re-asked plan covering the held pill colour")
    pill5 = next(p for p in plan()["pills"] if int(p["color"]) == 5)
    rng = g.RNG()
    card = W.fire(MC.MC_GET_CARD, rng, 0, True, True, False)
    rune = W.fire(MC.MC_GET_CARD, rng, 0, True, True, True)
    print(f"7. consumables plan: card draw -> {card}, rune-only draw -> {rune}")
    assert card == g.Card.CARD_TOWER and rune == g.Card.RUNE_HAGALAZ
    trinket = W.fire(MC.MC_GET_TRINKET, 0, rng)
    golden = W.fire(MC.MC_GET_TRINKET, 32768, rng)
    print(f"   trinket draws -> {trinket}, golden {golden}; removed from pool: {W.removedTrinket}")
    assert trinket == plan()["trinkets"][0] and W.removedTrinket == golden - 32768
    assert golden == plan()["trinkets"][1] | 32768

    effect = W.fire(MC.MC_GET_PILL_EFFECT, 0, 5)
    W.fire(MC.MC_USE_PILL, effect)
    horse = W.fire(MC.MC_GET_PILL_EFFECT, 0, 5 | 2048)
    print(f"8. pill colour 5 -> {pill5['effect']} (effect {effect}), horse pill -> {horse}; saved: {W.saved}")
    assert effect == g.PillEffect[pill5["enum"]] and horse == effect
    assert '"5"' in W.saved
    assert json.loads(W.saved)["cruelty"] == 3, "pill save must not wipe the DM Settings"
    assert W.fire(MC.MC_GET_PILL_EFFECT, 3, 9) is None             # no plan for colour 9: vanilla
    W.fire(MC.MC_USE_PILL, 3)
    assert W.fire(MC.MC_GET_PILL_EFFECT, 3, 9) is None             # ...and it stays vanilla's

    # --- a golden chest shows up: its item pool gets ranked ---------------------
    golden_pool = g.ItemPoolType.POOL_GOLDEN_CHEST
    W.pickups = lua.table_from([W.makePickup(g.EntityType.ENTITY_PICKUP, g.PickupVariant.PICKUP_LOCKEDCHEST, 1)])
    wait(lambda: Bridge.peek(f"items:1:{golden_pool}"), 6, "golden chest pool ranking")
    W.pickups = lua.table_from([])
    print("9. golden chest in the room -> its pool was ranked before it was opened")

    # --- boss room -> plan next floor; the boss clear is a bonus on top ---------
    boss = lua.table_from([W.makeEnemy(100)])
    W.enemies = boss
    W.enter(86)
    wait(lambda: Bridge.peek("floor:1:next"), 6, "floor plan")
    fight(boss, 1, "clear:1:86", 30)
    ready = Bridge.peek("clear:1:86") is not None
    ret = W.fire(MC.MC_PRE_SPAWN_CLEAN_AWARD, g.RNG(), g.Vector(320, 280))
    print(f"10. boss dead; bonus decision ready={ready}; PRE_SPAWN_CLEAN_AWARD returned {ret}"
          " (the game keeps its own boss award)")
    assert ready and ret is None and Bridge.peek("clear:1:86") is None
    mask = W.fire(MC.MC_POST_CURSE_EVAL, 0)
    print(f"11. curse mask for floor 2 = {mask}")
    assert mask == 1

    # --- DM Settings > Receipt: what the engine did, browsable in the menu --------
    receipt = [e.s for e in menu if e.sub == "Receipt"]
    picker = next(s for s in receipt if s.Type == 3)

    def screen():
        return [s.Display() if callable(s.Display) else (s.Display or "") for s in receipt]

    wait(lambda: any("room_clear applied" in line for line in screen()), 5, "receipt entries in the menu")
    lines = screen()
    print("12. DM Settings > Receipt:\n      " + "\n      ".join(l for l in lines if l))
    assert any(" actions, " in l for l in lines), "totals shown"
    assert any(l.startswith("saved as ") for l in lines), "the receipt file is named"
    newest = picker.Display()
    picker.OnChange(2)
    assert picker.Display() != newest and picker.CurrentSetting() == 2, "the picker browses older entries"
    files = list((Path(bridge_dir) / "receipts").glob("*.jsonl"))
    assert len(files) == 1, files
    recs = [json.loads(l) for l in files[0].read_text().splitlines()]
    calls = [r for r in recs if r["type"] == "entry" and (r.get("llm") or {}).get("model") == "mock"]
    assert calls and all("request" in r for r in calls if r["what"] not in ("narrative", "floor_brief"))
    assert any(r["type"] == "update" and r["status"] == "applied" for r in recs)
    assert any(r.get("what") == "cruelty" and r.get("result") == "6 -> 3" for r in recs)
    print(f"    receipt file {files[0].name}: {len(recs)} records, {len(calls)} LLM calls")

    # --- death: the player's stats at once, then the DM's side and its epitaph ------
    gaper = lua.table_from({"Entity": lua.table_from({"Type": g.EntityType.ENTITY_GAPER, "Variant": 0})})
    W.fire(MC.MC_ENTITY_TAKE_DMG, g.Isaac.GetPlayer(0), 2, 0, gaper)
    W.fire(MC.MC_POST_GAME_END, True)
    last_run = [e.s for e in menu if e.sub == "Last run"]

    def last():
        return " | ".join(l for l in (s.Display() for s in last_run) if l)

    text = last()
    assert text.startswith("HERE LIES ISAAC") and "Reached: Basement I" in text and "Last hit by: gaper" in text
    assert "Rooms cleared: 2" in text and "Pills eaten: 2" in text and "tallying" in text, text
    death = [json.loads(l) for l in open(Path(bridge_dir) / "to_dm.jsonl") if '"death"' in l]
    assert death and death[0]["data"]["run"] == "TEST 1234" and death[0]["data"]["rows"][0] == ["Reached", "Basement I"]
    wait(lambda: "Extra enemies" in last(), 5, "the DM's side of the stats")
    wait(lambda: "The dice were mine" in last(), 5, "the DM's epitaph")
    tick(3)                                   # the overlay draws without errors
    assert "The dice were mine" in json.loads(W.saved)["last_run"]["epitaph"], "kept for the next session"
    print("13. death -> DM Settings > Last run:\n      " + last().replace(" | ", "\n      "))

    errors = [p for p in printed if "error" in p.lower()]
    assert not errors, errors
    print("\nALL HEADLESS CHECKS PASSED")


if __name__ == "__main__":
    main()
