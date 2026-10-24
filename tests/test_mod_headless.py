"""Headless run of the whole mod (main.lua + dm/*) on a fake Isaac API, against server.py --mock.

Walks a tiny floor: start room -> fight in a normal room -> treasure item -> card, trinket and pill
decks -> a golden chest -> boss room -> kills that pop the drops deck -> machines and chests, and checks
the prefetch timing (the room-clear decision must be requested *during* the fight, not at the end, and
be ready when the last enemy dies) and the roll audit (nothing this walk covers went to vanilla).
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
    Bridge, Sched, Decks = modules["dm.bridge"], modules["dm.scheduler"], modules["dm.decks"]
    MC = g.ModCallbacks
    t0 = time.monotonic()

    def reqs(key):
        return [m for m in sent(game) if m.get("t") == "req" and m.get("key") == key]

    def drawn(key):
        return [m for m in sent(game) if m.get("t") == "drawn" and m.get("key") == key]

    def deck_of(key):
        res = Bridge.peek(key)
        return to_py(res.decision) if res else None

    def rolls_sent():
        return sum(1 for m in sent(game) if m.get("t") == "event" and m.get("name") == "rolls")

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
    assert Decks.left("layout", 85) == 0 and drawn("layout:1:85"), "the room's layout deck was drawn on entry"
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

    # --- card, trinket and pill decks: stocked from floor start, then a pill colour none covers -----
    def pills():
        return (deck_of("pills:1") or {}).get("pills") or []

    wait(lambda: deck_of("cards:1") and deck_of("trinkets:1"), 6, "the card and trinket decks")
    assert reqs("cards:1")[0]["ctx"]["wanted"] == 2 and reqs("cards:1")[0]["ctx"]["deck_left"] in ([], {})
    W.heldPill = 5
    wait(lambda: any(int(p["color"]) == 5 for p in pills()), 8, "a pills answer covering the held colour")
    pill5 = next(p for p in pills() if int(p["color"]) == 5)
    trinkets = list(deck_of("trinkets:1")["trinkets"])
    rng = g.RNG()
    card = W.fire(MC.MC_GET_CARD, rng, 0, True, True, False)
    rune = W.fire(MC.MC_GET_CARD, rng, 0, True, True, True)
    print(f"7. cards deck: card draw -> {card}, rune-only draw -> {rune}")
    assert card == g.Card.CARD_TOWER and rune == g.Card.RUNE_HAGALAZ
    assert [m["outcome"]["card"] for m in drawn("cards:1")] == ["tower", "rune_hagalaz"]
    trinket = W.fire(MC.MC_GET_TRINKET, 0, rng)
    golden = W.fire(MC.MC_GET_TRINKET, 32768, rng)
    print(f"   trinket draws -> {trinket}, golden {golden}; removed from pool: {W.removedTrinket}")
    assert trinket == trinkets[0] and W.removedTrinket == golden - 32768
    assert golden == trinkets[1] | 32768

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
    wait(lambda: Bridge.peek("curses:1"), 6, "floor plan")
    fight(boss, 1, "clear:1:86", 30)
    ready = Bridge.peek("clear:1:86") is not None
    ret = W.fire(MC.MC_PRE_SPAWN_CLEAN_AWARD, g.RNG(), g.Vector(320, 280))
    print(f"10. boss dead; bonus decision ready={ready}; PRE_SPAWN_CLEAN_AWARD returned {ret}"
          " (the game keeps its own boss award)")
    assert ready and ret is None and Decks.left("clear", 86) == 0
    mask = W.fire(MC.MC_POST_CURSE_EVAL, 0)
    print(f"11. curse mask for floor 2 = {mask}")
    assert mask == 1

    # --- holds: whatever the DM hasn't answered yet, the game freezes for instead of deciding ---
    Hold, Pills = modules["dm.loading"], modules["dm.streams.pills"]
    FREEZE = g.EntityFlag.FLAG_FREEZE
    W.rooms[87] = lua.table_from({"SafeGridIndex": 87, "GridIndex": 87, "VisitedCount": 0, "AwardSeed": 1,
                                  "Data": lua.table_from({"Type": g.RoomType.ROOM_DEFAULT, "Shape": 1, "Difficulty": 1})})
    W.adj[87] = lua.table_from([86])
    imp = W.makeEnemy(5)
    W.enemies = lua.table_from([imp])
    # The room's own penny, spawned as it loads (MC_PRE_ROOM_ENTITY_SPAWN), and a rock (a grid entity).
    W.roomEntities[87] = lua.table_from([lua.table_from([g.EntityType.ENTITY_PICKUP, g.PickupVariant.PICKUP_COIN, 1, 30]),
                                         lua.table_from([1000, 0, 0, 31])])
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
    assert Decks.left("layout", 87) == 0 and not imp.flags & FREEZE, "layout drawn, enemies free again"
    assert W.fire(MC.MC_PRE_NPC_UPDATE, imp) is None, "enemy AI runs again"
    print("12. undressed room: game held until its layout came in, then dressed it")

    imp.HitPoints = 0                              # one shot: no time to prefetch the reward
    tick()
    assert not Bridge.isPending("clear:1:87")
    ret = W.fire(MC.MC_PRE_SPAWN_CLEAN_AWARD, g.RNG(), g.Vector(320, 280))
    assert ret is True and Hold.holding("clear:1:87"), "the game's award is skipped and the game waits"
    assert Bridge.isPending("clear:1:87"), "the reward is asked for the moment the room clears"
    wait(lambda: not Hold.active(), 8, "the late room-clear reward")
    assert Decks.left("clear", 87) == 0 and drawn("clear:1:87"), "the late reward was drawn"
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
    assert Pills.blockPill(g.Isaac.GetPlayer(0), g.InputHook.IS_ACTION_PRESSED, g.ButtonAction.ACTION_PILLCARD) is False
    W.pillPressed = True
    assert Pills.blockPill(g.Isaac.GetPlayer(0), g.InputHook.IS_ACTION_TRIGGERED, g.ButtonAction.ACTION_PILLCARD) is False
    W.pillPressed = False
    assert Hold.holding("pills:1"), "pressing the pill button holds the game for the pills deck"
    owed = reqs("pills:1")[-1]["ctx"]["owed"]
    assert [int(o["color"]) for o in owed] == [7], owed
    wait(lambda: not Hold.active(), 8, "an answer covering the pill")
    assert any(int(p["color"]) == 7 for p in pills())
    assert Pills.blockPill(g.Isaac.GetPlayer(0), g.InputHook.IS_ACTION_PRESSED, g.ButtonAction.ACTION_PILLCARD) is None
    W.heldPill = 5
    print("15. uncovered pill: button held until the plan covered it, then free")

    # --- the drops deck: every kill pops it, and the game's own drop is cancelled ---------------
    PV, KEYS = g.PickupVariant, g.KeySubType
    Drops = modules["dm.streams.drops"]
    REWARD_VARIANT = {"penny": PV.PICKUP_COIN, "half_heart": PV.PICKUP_HEART, "key": PV.PICKUP_KEY,
                      "bomb": PV.PICKUP_BOMB}
    wait(lambda: Decks.left("cards", 1) > 0, 6, "the cards deck restocked")
    # Between fights the deck is topped up to a reserve: the next room's first kills have entries while
    # its own ask is on the way. A room the reserve covers isn't asked again on the way in.
    wait(lambda: Decks.left("drops", 1) >= 3 and not Bridge.isPending("drops:1"), 6, "the drops reserve")
    foes = [W.makeEnemy(10, 200, 200), W.makeEnemy(10, 420, 220), W.makeEnemy(10, 260, 340),
            W.makeEnemy(10, 500, 340), W.makeEnemy(10, 520, 360)]
    W.enemies = lua.table_from(foes)
    while Decks.left("drops", 1) > 0:              # spend the reserve: this room has more foes than it holds
        Decks.draw("drops", 1, lua.table())
    asks0 = len(reqs("drops:1"))
    W.enter(85)                                    # back in room 85: asked on the way in
    wait(lambda: len(reqs("drops:1")) > asks0 and not Bridge.isPending("drops:1"), 6, "the drops deck for this room")
    req = reqs("drops:1")[-1]
    assert req["ctx"]["wanted"] == 5 + 2 and req["ctx"]["enemies_left"] == 5, req["ctx"]
    foes = foes[:3]
    W.enemies = lua.table_from(foes)               # the last two wander off: three kills below
    deck = [dict(o) for o in deck_of("drops:1")["outcomes"]]
    assert Decks.left("drops", 1) == len(deck) >= 3, deck
    d0, n0 = len(drawn("drops:1")), len(W.spawned)
    # kill 1: its drop names the enemy as its spawner
    W.kill(foes[0])
    heart = W.spawnPickup(PV.PICKUP_HEART, g.HeartSubType.HEART_HALF, 200, 200, foes[0])
    assert heart.removed, "the kill's own drop is cancelled"
    # kill 2: a random pickup with no spawner, beside the kill (its variant chosen as it initialises)
    W.kill(foes[1])
    W.randomPickup = PV.PICKUP_KEY
    key = W.spawnPickup(0, 0, 430, 220)
    assert key.removed and key.Variant == PV.PICKUP_KEY, "a drop beside a kill is the kill's"
    # kill 3 drops a blank card. Regression: its card used to be drawn from the cards deck, then the
    # drop swallowed: the planned card was lost. A cancelled spawn's card is never drawn.
    cards0 = (len(drawn("cards:1")), Decks.left("cards", 1))
    W.kill(foes[2])
    card = W.spawnPickup(PV.PICKUP_TAROTCARD, 0, 260, 340, foes[2])
    assert card.removed and (len(drawn("cards:1")), Decks.left("cards", 1)) == cards0, \
        "a cancelled kill drop takes nothing from the cards deck"
    Apply = modules["dm.apply"]
    Apply.spawning = True                          # the DM's own card (a "card" entry) draws exactly once
    mine = W.spawnPickup(PV.PICKUP_TAROTCARD, 0, 260, 340)
    Apply.spawning = False
    assert not mine.removed and len(drawn("cards:1")) == cards0[0] + 1 and mine.SubType == g.Card.CARD_TOWER
    tick(2)
    assert len(drawn("drops:1")) == d0 + 3, "each kill drew one entry"
    paid = [s.v for s in list(W.spawned.values())[n0:]]
    want = [REWARD_VARIANT[e["reward"]] for e in deck[:3] for _ in range(int(e.get("count") or 1))
            if e["reward"] != "nothing"]
    assert paid == want, (paid, deck)
    kills = [l for m in sent(game) if m.get("t") == "effect" and m.get("key") == "drops:1" for l in m["effect"]]
    assert len(kills) == 3 and all("instead of the game's" in l for l in kills), kills
    # the clear: decided with what the kills dropped
    tick()
    ret = W.fire(MC.MC_PRE_SPAWN_CLEAN_AWARD, g.RNG(), g.Vector(320, 280))
    assert ret is True
    wait(lambda: not Hold.active(), 8, "the clear reward")
    paid_names = [e["reward"] for e in deck[:3] if e["reward"] != "nothing"]
    assert list(reqs("clear:1:85")[-1]["ctx"].get("kills_dropped") or []) == paid_names
    print("15b. kills drew the drops deck, the game's drops were cancelled:", kills)

    # --- an empty drops deck never leaves a kill to the game: its drop is cancelled, the game freezes, and the
    # entry is paid when the answer lands ---
    while Decks.left("drops", 1) > 0:
        Decks.draw("drops", 1, lua.table())
    foe = W.makeEnemy(10, 300, 300)
    W.enemies = lua.table_from([foe])
    d1, n1 = len(drawn("drops:1")), len(W.spawned)
    W.kill(foe)
    vheart = W.spawnPickup(PV.PICKUP_HEART, g.HeartSubType.HEART_FULL, 300, 300, foe)
    assert vheart.removed, "the game's own drop is cancelled even with the deck empty"
    # The hold starts on the next update; the floor's kill drops are spent by now, so the backend answers
    # locally (no mock latency) and the hold can end a frame later: look on the frame it starts.
    tick()
    assert Hold.holding("drops:1"), "the game freezes for the drops deck"
    wait(lambda: not Hold.holding("drops:1"), 8, "the drops answer for the empty-deck kill")
    tick(3)
    assert len(drawn("drops:1")) == d1 + 1, "the kill drew from the new answer"
    print("15b1. empty deck: the kill's drop was cancelled, the game froze until the DM answered")

    # --- a tinted rock holds what the room's layout planned, not the game's loot ---
    Rocks = modules["dm.streams.rocks"]
    Rocks.plan(85, "layout:1:85", lua.table_from(["key", "penny"]))
    tick()                                         # grid 22 (at 360,40) is seen standing
    n0 = len(W.spawned)
    W.gridState[22] = 2                            # bombed: the game drops its soul heart
    vanilla = W.spawnPickup(PV.PICKUP_HEART, g.HeartSubType.HEART_SOUL, 360, 40)
    assert vanilla.removed, "the game's loot from a planned rock is cancelled"
    tick(3)
    paid = [s.v for s in list(W.spawned.values())[n0:]]
    assert paid == [PV.PICKUP_KEY, PV.PICKUP_COIN], paid
    tick(10)
    assert len(W.spawned) == n0 + 2, "a rock pays once"
    reff = [l for m in sent(game) if m.get("t") == "effect" and m.get("key") == "layout:1:85" for l in m["effect"]]
    assert "tinted rock bombed: +1 key, +1 penny (instead of the game's 1 soul heart)" in reff, reff
    assert drawn("rock:1:85")[-1]["outcome"]["rewards"] == [{"reward": "key", "count": 1},
                                                           {"reward": "penny", "count": 1}]
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
            p = W.spawnPickup(PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY, 330, 330, beggar)
            frames(1)
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
    assert [m["cause"] for m in drawn(pkey)] == ["slot_use"] * 3
    print("15c. beggar paid on its schedule:", effects)

    # --- a golden chest's contents are fixed when it appears and paid when it opens. Regression: its item
    # was taken from the item ranking, and then the payout removed that pedestal and paid the schedule. ---
    chest = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_LOCKEDCHEST, 1, 400, 300)
    W.pickups = lua.table_from([chest])
    frames(60)                                     # the beggar is gone: its payout window can't claim the loot
    ckey = f"payout:1:{int(chest.InitSeed)}"
    gkey = f"items:1:{golden_pool}"
    wait(lambda: Bridge.peek(ckey) and Bridge.peek(gkey), 6, "the chest's contents and its pool's ranking")
    assert [r["reward"] for r in deck_of(ckey)["schedule"]] == ["item", "half_heart"]
    rank = list(deck_of(gkey)["order"])
    n0, g0 = len(W.spawned), len(drawn(gkey))
    chest.SubType = 0                              # opened: the game spawns its own item and a key
    W.spawnPool = golden_pool
    pedestal = W.spawnPickup(PV.PICKUP_COLLECTIBLE, 0, 410, 330, chest)
    W.spawnPool = None
    loot = W.spawnPickup(PV.PICKUP_KEY, KEYS.KEY_NORMAL, 410, 330, chest)
    assert pedestal.removed and loot.removed, "the chest's own contents are cancelled"
    frames(1)
    got = [(s.v, s.s) for s in list(W.spawned.values())[n0:]]
    assert got == [(PV.PICKUP_COLLECTIBLE, rank[0]), (PV.PICKUP_HEART, g.HeartSubType.HEART_HALF)], (got, rank)
    assert len(drawn(gkey)) == g0 + 1, "one draw from the pool: the schedule's item, not the game's pedestal"
    W.pickups = lua.table_from([])
    print("15d. golden chest paid its schedule; its item came from the pool's deck, once")

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
        loot = W.spawnPickup(PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY, x, 300)
        frames(1)
        assert loot.removed and [s.v for s in list(W.spawned.values())[n0:]] == [PV.PICKUP_HEART, PV.PICKUP_BOMB], \
            f"variant {c.Variant}: its own plan pays out, not claimed by its neighbour's"
    W.pickups = lua.table_from([])
    print("15d2. bomb chest and spiked chest side by side each paid their own scheduled contents")

    # --- a grab bag is a chest: its contents are scheduled ---
    bag = W.makePickup(g.EntityType.ENTITY_PICKUP, PV.PICKUP_GRAB_BAG, 1, 340, 300)
    W.pickups = lua.table_from([bag])
    bkey = f"payout:1:{int(bag.InitSeed)}"
    wait(lambda: Bridge.peek(bkey), 6, "the grab bag's contents")
    n0 = len(W.spawned)
    bag.removed = True                             # taken: gone, the game fills it with its own pickups
    spilled = W.spawnPickup(PV.PICKUP_COIN, g.CoinSubType.COIN_PENNY, 345, 300, bag)
    frames(1)
    assert spilled.removed and len(W.spawned) > n0, "the bag's own contents are replaced by its schedule"
    W.pickups = lua.table_from([])
    print("15e. grab bag paid its scheduled contents")

    # --- the roll audit: nothing this walk covers went to vanilla ---
    rolls0 = rolls_sent()
    wait(lambda: rolls_sent() > rolls0, 6, "the roll counts")
    tally = {}
    for m in sent(game):
        if m.get("t") == "event" and m.get("name") == "rolls":
            for c in m["data"]["counts"]:
                k = (c["hook"], c["cause"], c["owner"])
                tally[k] = tally.get(k, 0) + c["n"]
    covered = ("get_card", "get_trinket", "get_collectible", "clean_award", "npc_death", "slot_use", "chest_open")
    loose = {k: n for k, n in tally.items() if k[2] == "uncontrolled" and (
        k[0] in covered or (k[0] == "spawn" and k[1] in ("enemy_death", "chest_open", "slot_payout", "rock_break")))}
    assert not loose, loose
    assert tally.get(("room_entity", "coin", "layout"), 0) == 1, "a room's own pickups are counted as it loads"
    assert not [k for k in tally if k[0] == "pickup_selection"], "a cancelled spawn's selection is no roll"
    assert tally.get(("npc_death", "-", "drops"), 0) == 4 and tally.get(("spawn", "enemy_death", "drops"), 0) == 4, tally
    assert any(m.get("t") == "event" and m.get("name") == "rolls" and m["data"].get("samples") for m in sent(game)), \
        "LOG_SPAWNS samples go with the counts"
    print("15f. roll audit:", {f"{h}/{c}": n for (h, c, o), n in tally.items() if o != "uncontrolled"})

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
    audits = [r for r in recs if r.get("what") == "rolls"]
    assert audits and any(r.get("samples") for r in audits), "the roll audit is in the receipt"
    assert any(r["type"] == "drawn" and r["key"] == "drops:1" for r in recs), "every draw is in the receipt"
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
    # The boss pool (room 86 isn't next door) and the card/trinket decks go in the background.
    gated = sorted(Sched.floorReadyKeys().values())
    assert gated == ["items:2:0", "layout:2:85"], gated
    asks = [m["ctx"]["reason"] for m in sent(game) if m.get("t") == "req" and m.get("key") == "items:2:0"]
    assert asks == ["player is next to this room"], f"the gated pool is asked as next door (not floor start): {asks}"
    assert Bridge.isPending("items:2:2") or Bridge.settled("items:2:2"), "boss pool still asked, in the background"
    wait(lambda: Bridge.isPending("cards:2") or Bridge.settled("cards:2"), 2, "the cards deck asked, in the background")
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
        ("layout:2:", "items:2:", "cards:2", "trinkets:2", "pills:2", "drops:2"))]
    assert not early, f"floor 2 decisions made before its brief: {early}"
    print("17. floor 2: loading screen held until its brief and the start room's neighbours were in"
          " (layout:2:85, items:2:0); boss pool and decks in the background; none made before the brief")

    # --- the next floor is generated before its plan lands: its curses change once it does ---
    W.enter(86)
    assert Bridge.isPending("curses:2")
    W.curses = 0
    assert W.fire(MC.MC_POST_CURSE_EVAL, 0) is None, "nothing to apply yet"
    tick()
    assert Hold.holding("curses:2"), "the new floor holds for its plan"
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
