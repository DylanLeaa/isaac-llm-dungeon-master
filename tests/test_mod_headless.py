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
sys.path.insert(0, str(ROOT / "backend"))
from bridge import LogTail  # noqa: E402


def sent(game):
    """Every message the mod has written to the game's log so far."""
    tail = LogTail(game["log"])
    tail.pos = 0
    return tail.messages()


def fake_game(tmp):
    """A game folder the backend can find: log.txt (empty, as at launch) and the mod's save folder."""
    tmp = Path(tmp)
    save = tmp / "isaac" / "data" / "llm_dungeon_master" / "save1.dat"
    save.parent.mkdir(parents=True)
    (tmp / "log.txt").write_bytes(b"")
    return {"log": tmp / "log.txt", "save": save,
            "args": ["--log", str(tmp / "log.txt"), "--isaac-dir", str(tmp / "isaac")]}


def main():
    tmp = tempfile.mkdtemp()
    game = fake_game(tmp)
    # The backend logs to a file: a pipe nobody reads until the end fills up and stalls it mid-run.
    out = open(Path(tmp) / "backend.log", "w")
    srv = subprocess.Popen([sys.executable, "server.py", "--mock", *game["args"],
                            "--mock-latency", "0.3", "0.9", "--receipts", str(Path(tmp) / "receipts")],
                           cwd=ROOT / "backend", stdout=out, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.0)
    try:
        run(tmp, game)
    finally:
        srv.terminate()
        srv.wait(timeout=5)
        out.close()
        print("\n--- backend log ---\n" + (Path(tmp) / "backend.log").read_text())


def run(tmp, game):
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
        return real_require(name)

    g.require = fake_require
    W = lua.execute(open(ROOT / "tests" / "fake_isaac.lua").read())
    W.logPath, W.savePath = str(game["log"]), str(game["save"])

    modules = {}

    def include(name):
        if name not in modules:
            path = MOD / (name.replace(".", "/") + ".lua")
            mod = lua.execute(open(path).read())
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
    # The title screen: the backend answers the hello the mod sent when it loaded.
    wait(Bridge.ready, 5, "backend welcome at the title screen")
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
    assert any(m.get("t") == "prepare" for m in sent(game))
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
    settings = [m for m in sent(game) if m.get("t") == "settings"]
    assert settings and settings[-1]["cruelty"] == 3, "cruelty change should reach the backend"
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

    # --- holds: whatever the DM hasn't answered yet, the game freezes for instead of deciding ---
    Hold, Cons = modules["dm.loading"], modules["dm.consumables"]
    FREEZE = g.EntityFlag.FLAG_FREEZE
    W.rooms[87] = lua.table_from({"SafeGridIndex": 87, "GridIndex": 87, "VisitedCount": 0, "AwardSeed": 1,
                                  "Data": lua.table_from({"Type": g.RoomType.ROOM_DEFAULT, "Shape": 1, "Difficulty": 1})})
    W.adj[87] = lua.table_from([86])
    imp = W.makeEnemy(5)
    W.enemies = lua.table_from([imp])
    W.enter(87)                                    # never prefetched (not next to anywhere the player stood)
    assert Hold.active() and Hold.holding("layout:1:87"), "walking into an undressed room holds the game"
    assert Bridge.isPending("layout:1:87"), "...and asks for the room right then"
    assert Hold.blockInput(None, None, g.InputHook.IS_ACTION_PRESSED, 7) is False
    assert W.fire(MC.MC_PRE_NPC_UPDATE, imp) is True, "enemy AI is skipped from the room's first frame"
    tick()
    assert imp.flags & FREEZE, "enemies stand still while the game waits"
    imp.flags = 0                                  # the game cleared the freeze status itself
    tick()
    assert imp.flags & FREEZE, "a freeze the game cleared is put back"
    wait(lambda: not Hold.active(), 8, "the late room layout")
    assert Bridge.peek("layout:1:87") is None and not imp.flags & FREEZE, "layout taken, enemies free again"
    assert W.fire(MC.MC_PRE_NPC_UPDATE, imp) is None, "enemy AI runs again"
    print("12. undressed room: game held until its layout came in, then dressed it")

    imp.HitPoints = 0                              # one shot: no time to prefetch the reward
    tick()
    assert not Bridge.isPending("clear:1:87")
    ret = W.fire(MC.MC_PRE_SPAWN_CLEAN_AWARD, g.RNG(), g.Vector(320, 280))
    assert ret is True and Hold.holding("clear:1:87"), "the game's award is skipped and the game waits"
    assert Bridge.isPending("clear:1:87"), "the reward is asked for the moment the room clears"
    wait(lambda: not Hold.active(), 8, "the late room-clear reward")
    assert Bridge.peek("clear:1:87") is None, "the late reward was taken"
    print("13. instant clear: award held until the DM's reward came in")

    devil = g.ItemPoolType.POOL_DEVIL              # never ranked
    assert W.fire(MC.MC_PRE_GET_COLLECTIBLE, devil, True, 5) is None
    pedestal = W.makePickup(g.EntityType.ENTITY_PICKUP, g.PickupVariant.PICKUP_COLLECTIBLE, 2)
    W.fire(MC.MC_POST_PICKUP_INIT, pedestal)
    tick()
    assert Hold.holding(f"items:1:{devil}"), "a pedestal from an unranked pool holds the game"
    wait(lambda: not Hold.active(), 8, "the late devil pool ranking")
    order = list(to_py(Bridge.peek(f"items:1:{devil}").decision)["order"])
    assert pedestal.morphed and pedestal.SubType == order[0], (pedestal.SubType, order)
    print(f"14. unranked pedestal: held, then swapped to the DM's pick {pedestal.SubType}")

    W.heldPill = 7                                 # a colour no plan has covered
    assert Cons.blockPill(g.Isaac.GetPlayer(0), g.InputHook.IS_ACTION_PRESSED, g.ButtonAction.ACTION_PILLCARD) is False
    W.pillPressed = True
    assert Cons.blockPill(g.Isaac.GetPlayer(0), g.InputHook.IS_ACTION_TRIGGERED, g.ButtonAction.ACTION_PILLCARD) is False
    W.pillPressed = False
    assert Hold.holding("cons:1"), "pressing the pill button holds the game for the plan"
    wait(lambda: not Hold.active(), 8, "a plan covering the pill")
    assert any(int(p["color"]) == 7 for p in plan()["pills"])
    assert Cons.blockPill(g.Isaac.GetPlayer(0), g.InputHook.IS_ACTION_PRESSED, g.ButtonAction.ACTION_PILLCARD) is None
    W.heldPill = 5
    print("15. uncovered pill: button held until the plan covered it, then free")

    # --- drops the game would scatter mid-fight are swallowed and handed to the room clear -------
    KEYS, SPAWN0 = g.KeySubType, len(W.spawned)
    foe = W.makeEnemy(10)
    W.enemies = lua.table_from([foe])
    W.enter(85)                                    # a room the DM decides the clear of, armed on entry
    PV = g.PickupVariant
    heart = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_HEART, g.HeartSubType.HEART_HALF)
    key = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_KEY, KEYS.KEY_NORMAL)
    coin = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY, 500, 100)
    mine = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY)
    item = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_COLLECTIBLE, 2)
    W.fire(MC.MC_POST_NPC_DEATH, foe)              # a kill: the heart and key it drops are swallowed
    for p in (heart, key, coin, item):
        W.fire(MC.MC_POST_PICKUP_INIT, p)
    assert heart.removed and key.removed and coin.removed, "enemy-death and natural drops are swallowed"
    assert not item.removed, "item pedestals are never touched"
    # loot from a tinted rock the player bombs is theirs, even mid-fight (grid 22 stands at 360,40)
    rock_loot = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_HEART, g.HeartSubType.HEART_SOUL, 360, 40)
    tick()                                         # the rock is seen standing
    W.gridState[22] = 2
    W.fire(MC.MC_POST_PICKUP_INIT, rock_loot)
    assert not rock_loot.removed, "a broken tinted rock's loot is never swallowed"
    W.gridState[22] = None
    g.Apply = modules["dm.apply"]
    modules["dm.apply"].spawning = True            # the mod's own spawn must pass through
    W.fire(MC.MC_POST_PICKUP_INIT, mine)
    modules["dm.apply"].spawning = False
    assert not mine.removed
    foe.HitPoints = 0
    tick()
    ret = W.fire(MC.MC_PRE_SPAWN_CLEAN_AWARD, g.RNG(), g.Vector(320, 280))
    assert ret is True and Hold.holding("clear:1:85")
    W.frame += 1                                   # the last kill's fallout, a frame after the clear
    fallout = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_TRINKET, 133)
    W.fire(MC.MC_POST_PICKUP_INIT, fallout)
    assert fallout.removed, "a drop just after the clear is still swallowed, into the clear's tally"
    wait(lambda: not Hold.active(), 8, "the clear reward that sees the swallowed drops")
    req = [m for m in sent(game) if m.get("t") == "req" and m.get("key") == "clear:1:85"][-1]
    assert req["ctx"]["suppressed_drops"] == {"half_heart": 1, "key": 1, "penny": 1}, req["ctx"]
    eff = [m for m in sent(game) if m.get("t") == "effect" and m.get("key") == "clear:1:85"][-1]["effect"]
    assert any("would have dropped, not replaced" in line for line in eff), eff
    assert any("trinket" in line for line in eff), eff
    W.frame += 30
    late = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_HEART, g.HeartSubType.HEART_HALF)
    W.fire(MC.MC_POST_PICKUP_INIT, late)
    assert not late.removed, "once the clear has settled, drops pass through"
    # The reward already out when the fallout lands: it is lost, and the clear says so.
    Catcher, lost = modules["dm.dropcatcher"], []
    Catcher.arm(999, True)
    entry = Catcher.close(6)
    Catcher.settle(entry, lambda name: lost.append(name))
    W.frame += 2
    fallout2 = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_TRINKET, 133)
    W.fire(MC.MC_POST_PICKUP_INIT, fallout2)
    assert fallout2.removed and len(lost) == 1 and entry.total == 0, (lost, entry.total)
    W.frame += 10
    late2 = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_TRINKET, 133)
    W.fire(MC.MC_POST_PICKUP_INIT, late2)
    assert not late2.removed and len(lost) == 1, "the trail ends a few frames after the clear"
    # The room's own pickups spawn as it loads, before the catcher is armed: adopted once armed,
    # unless the layout already removed or morphed them.
    W.frame += 10
    loaded = [W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_KEY, KEYS.KEY_NORMAL),
              W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_BOMB, 1),
              W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY)]
    for p in loaded:
        W.fire(MC.MC_POST_PICKUP_INIT, p)
    assert not any(p.removed for p in loaded), "nothing is catching yet: they stay"
    Catcher.arm(998, True)
    loaded[1].removed = True                       # the layout removed this one
    loaded[2].Variant, loaded[2].SubType = PV.PICKUP_HEART, 2   # and morphed this one
    assert Catcher.adopt(modules["dm.bridge"]) == 1 and loaded[0].removed
    assert dict(Catcher.counts().items()) == {"key": 1}, dict(Catcher.counts().items())
    Catcher.release(Catcher.close())
    stale = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_KEY, KEYS.KEY_NORMAL)
    W.fire(MC.MC_POST_PICKUP_INIT, stale)
    W.frame += 5
    Catcher.arm(997, True)                         # a later room: last room's strays aren't its layout
    assert Catcher.adopt(modules["dm.bridge"]) == 0 and not stale.removed
    Catcher.close()
    print("15b. kill drops swallowed, reward decided with them, shortfall reported:", eff, "| lost after the reward:", list(lost))

    # --- a tinted rock holds what the room's layout planned, not the game's loot ---
    Rocks = modules["dm.rockloot"]
    Rocks.plan(85, "layout:1:85", lua.table_from(["key", "penny"]))
    tick()                                         # grid 22 (at 360,40) is seen standing
    n0 = len(W.spawned)
    W.gridState[22] = 2                            # bombed: the game drops its soul heart
    vanilla = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_HEART, g.HeartSubType.HEART_SOUL, 360, 40)
    W.fire(MC.MC_POST_PICKUP_INIT, vanilla)
    assert vanilla.removed, "the game's loot from a planned rock is cancelled"
    tick(3)
    paid = [s.v for s in list(W.spawned.values())[n0:]]
    assert paid == [PV.PICKUP_KEY, PV.PICKUP_COIN], paid
    tick(10)
    assert len(W.spawned) == n0 + 2, "a rock pays once"
    reff = [l for m in sent(game) if m.get("t") == "effect" and m.get("key") == "layout:1:85" for l in m["effect"]]
    assert "tinted rock bombed: +1 key, +1 penny (instead of the game's 1 soul heart)" in reff, reff
    W.gridState[22] = None
    print("15b2. tinted rock paid the DM's loot instead of the game's:", reff[-1])

    # --- a beggar follows its payout schedule (key, nothing, penny); the game's own payout is cancelled ---
    beggar = W.makePickup(g.EntityType.ENTITY_SLOT, 4, 0, 330, 300)
    W.pickups = lua.table_from([beggar])
    W.coins = 3
    pkey = f"payout:1:{int(beggar.InitSeed)}"
    wait(lambda: Bridge.peek(pkey), 6, "the beggar's payout schedule")

    def frames(n):
        for _ in range(n):
            W.frame += 1
            W.fire(MC.MC_POST_UPDATE)

    def payout(vanilla):
        """The player pays; the game (if `vanilla`) pays its own penny beside the beggar."""
        W.coins -= 1
        frames(10)
        if vanilla:
            p = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY, 330, 330)
            W.fire(MC.MC_POST_PICKUP_INIT, p)
            return p
    SPAWN0 = len(W.spawned)
    v1 = payout(True)
    assert v1.removed and W.spawned[SPAWN0 + 1].v == PV.PICKUP_KEY, "use 1: the game's penny is cancelled, a key is paid"
    n1 = len(W.spawned)
    v2 = payout(True)
    assert v2.removed and len(W.spawned) == n1, "use 2 is scheduled as 'nothing': the game's payout is cancelled"
    payout(False)
    frames(100)
    assert W.spawned[len(W.spawned)].v == PV.PICKUP_COIN, "use 3: the game paid nothing, the scheduled penny is paid anyway"
    effects = [l for m in sent(game) if m.get("t") == "effect" and m.get("key") == pkey for l in m["effect"]]
    assert any("use 1" in l and "+1 key" in l for l in effects) and any("use 2: nothing" in l for l in effects), effects
    print("15c. beggar paid on its schedule:", effects)

    # --- a golden chest's contents are fixed when it appears and paid when it opens ---
    chest = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_LOCKEDCHEST, 1, 400, 300)
    W.pickups = lua.table_from([chest])
    frames(60)                                     # the beggar is gone: its payout window can't claim the loot
    ckey = f"payout:1:{int(chest.InitSeed)}"
    wait(lambda: Bridge.peek(ckey), 6, "the chest's contents")
    n0 = len(W.spawned)
    chest.SubType = 0                              # opened
    W.fire(MC.MC_POST_PICKUP_UPDATE, chest)
    loot = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_KEY, KEYS.KEY_NORMAL, 410, 330)
    W.fire(MC.MC_POST_PICKUP_INIT, loot)
    assert loot.removed and [s.v for s in list(W.spawned.values())[n0:]] == [PV.PICKUP_HEART, PV.PICKUP_BOMB]
    W.pickups = lua.table_from([])
    print("15d. chest paid its scheduled contents instead of the game's")

    # --- chests with no item pool are scheduled too, and two side by side each pay their own plan:
    # the spiked chest opens inside the bomb chest's payout window, its loot within reach of both ---
    stone = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_BOMBCHEST, 1, 400, 300)
    spiked = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_SPIKEDCHEST, 1, 460, 300)
    W.pickups = lua.table_from([stone, spiked])
    for c in (stone, spiked):
        wait(lambda: Bridge.peek(f"payout:1:{int(c.InitSeed)}"), 6, "the chest's contents")
    for c, x in ((stone, 400), (spiked, 460)):
        n0 = len(W.spawned)
        c.SubType = 0                              # opened (the bomb chest blown open)
        W.fire(MC.MC_POST_PICKUP_UPDATE, c)
        loot = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY, x, 300)
        W.fire(MC.MC_POST_PICKUP_INIT, loot)
        assert loot.removed and [s.v for s in list(W.spawned.values())[n0:]] == [PV.PICKUP_HEART, PV.PICKUP_BOMB], \
            f"variant {c.Variant}: its own plan pays out, not swallowed by its neighbour's"
    W.pickups = lua.table_from([])
    print("15d2. bomb chest and spiked chest side by side each paid their own scheduled contents")

    # --- a grab bag is a chest: its contents are scheduled, and its spawns are never classed as natural ---
    bag = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_GRAB_BAG, 1, 340, 300)
    W.pickups = lua.table_from([bag])
    bkey = f"payout:1:{int(bag.InitSeed)}"
    wait(lambda: Bridge.peek(bkey), 6, "the grab bag's contents")
    n0 = len(W.spawned)
    bag.removed = True                             # taken: gone, the game fills it with its own pickups
    spilled = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY, 345, 300)
    spilled.SpawnerEntity = bag
    W.fire(MC.MC_POST_PICKUP_INIT, spilled)
    assert spilled.removed and len(W.spawned) > n0, "the bag's own contents are replaced by its schedule"
    W.pickups = lua.table_from([])
    print("15e. grab bag paid its scheduled contents")

    # --- the receipt: on disk for the app, no longer in the game's menu ----------------
    assert not [e for e in menu if e.sub == "Receipt"], "the receipt lives in the app, not the menu"
    files = list((Path(tmp) / "receipts").glob("*.jsonl"))
    assert len(files) == 1, files
    recs = [json.loads(l) for l in files[0].read_text().splitlines()]
    calls = [r for r in recs if r["type"] == "entry" and (r.get("llm") or {}).get("model") == "mock"]
    assert calls and all("request" in r for r in calls if r["what"] not in ("narrative", "floor_brief", "gate"))
    assert any(r["type"] == "update" and r["status"] == "applied" for r in recs)
    effects = [line for r in recs if r["type"] == "effect" for line in r["effect"]]
    assert any(line.endswith(" dropped") for line in effects), effects      # what each used decision did
    assert any(line.startswith("item offered: ") for line in effects), effects
    icons = {W.drawn[i].file for i in range(1, len(W.drawn) + 1)}
    assert any(f.startswith("gfx/005.") for f in icons), "used decisions show in the corner, as pickup icons"
    assert any(r.get("what") == "cruelty" and r.get("result") == "6 -> 3" for r in recs)
    print(f"16. receipt file {files[0].name}: {len(recs)} records, {len(calls)} LLM calls, "
          f"{len(effects)} effect lines")

    # --- floor 2: a loading screen again, held until the brief and the start room's neighbours are in ---
    W.stage = 2
    for idx in (84, 85, 71, 86):
        W.rooms[idx].VisitedCount = 0
    W.enemies = lua.table_from([])
    W.cur = 84
    W.rooms[84].VisitedCount = 1
    W.fire(MC.MC_POST_NEW_LEVEL)
    W.fire(MC.MC_POST_NEW_ROOM)
    tick()
    assert Loading.active(), "a new floor starts on the loading screen"
    assert Loading.blockInput(None, None, g.InputHook.IS_ACTION_PRESSED, 7) is False
    Loading.handle(lua.table_from({"status": "ready", "floor": 1}))           # a stale ready from floor 1
    tick()
    assert Loading.active(), "a ready for another floor doesn't end this floor's wait"
    # Only the start room's neighbours gate the screen: room 85's layout and treasure room 71's pool.
    # The boss pool (room 86 isn't next door) and the consumables plan go in the background.
    gated = sorted(Sched.floorReadyKeys().values())
    assert gated == ["items:2:0", "layout:2:85"], gated
    asks = [m["ctx"]["reason"] for m in sent(game) if m.get("t") == "req" and m.get("key") == "items:2:0"]
    assert asks == ["player is next to this room"], f"the gated pool is asked as next door (not floor start): {asks}"
    assert Bridge.isPending("items:2:2") or Bridge.settled("items:2:2"), "boss pool still asked, in the background"
    wait(lambda: Bridge.isPending("cons:2") or Bridge.settled("cons:2"), 2, "consumables asked, in the background")
    wait(lambda: not Loading.active(), 8, "floor 2's plans and the start room's neighbours")
    opening = [k for k in ("layout:2:85", "items:2:0") if Bridge.settled(k)]
    assert opening == ["layout:2:85", "items:2:0"], opening
    prepares = [m for m in sent(game) if m.get("t") == "prepare"]
    assert prepares[-1]["floor"] == 2 and prepares[-1]["state"]["level"]["stage"] == 2
    # Empty slots are spelled out, so a planner can't fill them from the character's usual loadout.
    who = prepares[-1]["state"]["player"]
    assert (who["active_item"], who["pocket_active"], who["trinkets"]) == ("none",) * 3, who
    assert who["pocket"] == "pill: reddots_red", who      # the pill picked up earlier on floor 1
    assert who["player_type"] == "isaac" and who["tainted"] is False, who
    assert prepares[-1]["state"]["save"] == {"items_unlocked": 7, "items_total": 8}, prepares[-1]["state"]["save"]
    recs = [json.loads(l) for l in files[0].read_text().splitlines()]
    brief2 = next(i for i, r in enumerate(recs) if r.get("what") == "floor_brief" and "Basement II" in r.get("key", ""))
    early = [r["key"] for r in recs[:brief2] if r.get("type") == "entry" and str(r.get("key", "")).startswith(
        ("layout:2:", "items:2:", "cons:2"))]
    assert not early, f"floor 2 decisions made before its brief: {early}"
    print("17. floor 2: loading screen held until its brief and the start room's neighbours were in"
          " (layout:2:85, items:2:0); boss pool and consumables in the background; none made before the brief")

    # --- the next floor is generated before its plan lands: its curses change once it does ---
    W.enter(86)
    assert Bridge.isPending("floor:2:next")
    W.curses = 0
    assert W.fire(MC.MC_POST_CURSE_EVAL, 0) is None, "nothing to apply yet"
    tick()
    assert Hold.holding("floor:2:next"), "the new floor holds for its plan"
    wait(lambda: not Hold.active(), 8, "the late floor plan")
    assert W.curses == 1, W.curses
    print("18. late floor plan: held, then the floor's curses were changed to the DM's")

    # --- death: the player's stats at once, then the DM's side and its epitaph ------
    gaper = lua.table_from({"Entity": lua.table_from({"Type": g.EntityType.ENTITY_GAPER, "Variant": 0})})
    W.fire(MC.MC_ENTITY_TAKE_DMG, g.Isaac.GetPlayer(0), 2, 0, gaper)
    W.fire(MC.MC_POST_GAME_END, True)
    last_run = [e.s for e in menu if e.sub == "Last run"]

    def last():
        return " | ".join(l for l in (s.Display() for s in last_run) if l)

    text = last()
    assert text.startswith("HERE LIES ISAAC") and "Reached: Basement II" in text and "Last hit by: gaper" in text
    assert "Rooms cleared: 4" in text and "Pills eaten: 2" in text and "tallying" in text, text
    death = [m for m in sent(game) if m.get("t") == "event" and m.get("name") == "death"]
    assert death and death[0]["data"]["run"] == "TEST 1234" and death[0]["data"]["rows"][0] == ["Reached", "Basement II"]
    wait(lambda: "Extra enemies" in last(), 5, "the DM's side of the stats")
    wait(lambda: "The dice were mine" in last(), 5, "the DM's epitaph")
    tick(3)                                   # the overlay draws without errors
    assert "The dice were mine" in json.loads(W.saved)["last_run"]["epitaph"], "kept for the next session"
    print("19. death -> DM Settings > Last run:\n      " + last().replace(" | ", "\n      "))

    errors = [p for p in printed if "error" in p.lower()]
    assert not errors, errors
    print("\nALL HEADLESS CHECKS PASSED")


if __name__ == "__main__":
    main()
