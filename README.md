# LLM Dungeon Master for The Binding of Isaac: Repentance

The RNG has been unplugged. Room-clear drops, which item sits on the pedestal (or comes out of
a chest or a beggar), how a room is dressed (extra enemies, champion colours, tinted "secret"
rocks, layout pickups), which card or trinket turns up, what each pill colour does and what
curses the next floor carries are all decided by **Claude Haiku 5.5**, playing a gleeful, trolling tabletop
DM. It sees your hearts, keys, bombs, coins, luck, build and what the floor still demands of
you — and gives you twelve coins on a floor with no shop.

```
 Isaac (Lua mod)                                   backend (Python)
 ───────────────                                   ────────────────
 scheduler.lua  ── "decide clear:3:85" + snapshot ─▶ Coordinator ── Haiku 5.5 (tool call)
   decides WHEN to ask                                 latest-wins per key     │
 bridge.lua     ◀─ clamped decision ───────────────  clamp() guardrails ◀──────┘
   non-blocking TCP / file                             RunMemory (schemes, ledger)
 apply.lua      spawns / morphs / curses
```

## The timing model (asking late, but not too late)

Every decision is requested ahead of the moment the game needs it, with the freshest
snapshot available, and the game **never waits**: if the answer isn't there in time, vanilla RNG
decides that one event.

| Decision | Asked when | Refreshed when | Used at |
|---|---|---|---|
| `room_layout` (enemies, champions, rocks, pickups) | you enter a room bordering an unvisited normal room | your HP / K-B-C / items change while you linger | `MC_POST_NEW_ROOM` of that room (or ≤20 frames later if the answer is late) |
| `room_clear` (the drop; normal, miniboss and curse rooms. In boss rooms it is a bonus on top of the game's own award) | **estimated time-to-clear** (remaining enemy HP ÷ your recent DPS) < `1.4 × p90 latency + 400ms`, or the room is nearly dead | you get hit or pick something up and there's still time for a new answer | `MC_PRE_SPAWN_CLEAN_AWARD` |
| `item_choice` (ranked real candidates from the pool) | floor start, and again when you stand next to a treasure/shop/boss/devil/ultra secret/… room; chest and beggar pools when one shows up in your room | — | `MC_PRE_GET_COLLECTIBLE` (also catches D6 rerolls) |
| `consumables` (next cards/runes, next trinkets, an effect per pill colour in play) | floor start | a pill colour the plan doesn't cover appears (held or on the floor), or the card/trinket queues run dry | `MC_GET_CARD`, `MC_GET_TRINKET`, `MC_GET_PILL_EFFECT` |
| `floor_plan` (curses, treasure distance) | you enter the boss room | boss dies | `MC_POST_CURSE_EVAL` of the next floor |

Requests for the same key supersede each other: the backend cancels the stale Haiku call and
the mod only replaces a result with one for a newer request. The p90 latency is measured live,
so the lead time adapts to your connection.

## Guardrails (the DM may be cruel, not unfair)

Haiku can choose anything inside the schema; `backend/schemas.py:clamp()` is the referee:

- `--cruelty 0..10` scales extra-enemy budget (0–3/room), champion chance cap, curse count (0–2)
  and troll-bomb budget per floor. Max one live troll bomb per room. With
  Mod Config Menu (Pure) installed,
  **DM Settings → Cruelty** changes it in-game (even mid-run), overrides `--cruelty` and is saved.
- **Mercy**: at ≤1 heart, no troll bombs, ≤1 extra enemy, no Curse of the Unknown, and a half
  heart sneaks into the drop after a long heart drought.
- **Drought breaker**: 0 keys + locked doors seen + 7 key-less clears → a key is added (same for bombs).
- Item and trinket picks must come from the real candidates; unknown enemies/rewards/cards are dropped.
- **Pills**: a colour's effect is locked the first time it's swallowed (saved with the run, so
  continues keep it). Bad pills are capped per run (40% of colours at cruelty 0, all at 10);
  PHD / Virgo / Lucky Foot and False PHD still flip them, and Bad Trip at ≤1 heart becomes Full Health.
  Suicide King isn't on the card menu. Boss-room bonuses are capped at 2 pickups.
- Taunts are trimmed to 70 ASCII chars (Isaac's font).

## Setup

**1. Backend**
```bash
cd backend
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...       # PowerShell: $env:OPENROUTER_API_KEY="sk-or-..."   cmd.exe: set OPENROUTER_API_KEY=sk-or-...
python server.py --cruelty 6              # TCP on 127.0.0.1:7337, model anthropic/claude-haiku-5.5 via OpenRouter
python server.py --mock                   # offline stand-in, no key needed

```
The console logs every decision with its latency, intent and taunt — fun on a second monitor.

**2. Mod**
- Copy `mod/llm_dungeon_master/` into `…/steamapps/common/The Binding of Isaac Rebirth/mods/`.
- Steam → Isaac → Properties → Launch Options: `--luadebug`.
  This is what allows the socket/file access. It lifts the mod sandbox for **every** enabled
  mod, so only run it with mods you trust, and remove it when you're done.
- Start a run. Bottom-left shows `DM tcp connected  avg …ms p90 …ms`.

If LuaSocket won't load on your install, set `TRANSPORT = "file"` in `dm/config.lua` and run
`python server.py --bridge file --bridge-dir "<Isaac install>/llmdm_bridge"` (create that folder).

**3. Check the DM's voice first (optional)**: `python tests/live_smoke.py --cruelty 8` makes one
real call per decision kind and prints the results and latencies.

## Configuration

`mod/llm_dungeon_master/dm/config.lua` toggles each control area, the prefetch lead factor,
refresh limits, taunts and the debug HUD. `ALLOW_LAYOUT_RESEED` lets the DM's treasure-room
distance wish reroll the floor via the `reseed` console command — off by default because
console commands disable achievements for that run.

## What's tested, and what isn't

- `tests/test_clamp.py` — guardrails.
- `tests/test_bridge_e2e.py` — the real `bridge.lua` under Lua 5.3 talking to the backend over
  the file bridge, plus the TCP server: superseding, timeouts, acks.
- `tests/test_mod_headless.py` — the **whole mod** (`main.lua` + `dm/*`) on a fake Isaac API
  against the mock backend: prefetch → room dressed on entry → clear decision requested mid-fight
  and ready at the last kill → DM-picked treasure item → card/rune/trinket draws and a locked pill
  colour → golden-chest pool ranked on sight → boss-room floor plan and bonus clear → curse mask.

These run without the game. The Isaac API calls follow the official docs, but the mod has not
been run inside Isaac yet, so expect a first-session shakedown; errors are caught per callback,
printed once to the console/`log.txt`, and that event falls back to vanilla.

## Known limits (vanilla API)

- **Map layout** can't be authored directly without REPENTOGON; the DM controls it via curses and
  (optionally) rerolling the floor. With REPENTOGON's level-generation callbacks this could become
  true room placement — `floor_plan` is the place to add it.
- Room dressing only touches normal rooms (not shops, bosses, challenge rooms). Player 1 only.
  Greed mode is left vanilla. Golden pills stay random.
- Beggars and machines: the DM picks *which item* they pay out, not *whether* they pay.
- Chest contents other than items (the pickups inside) are still vanilla.
- Enemy IDs are referenced by `EntityType` enum name, so anything missing on your version is skipped.

## Extending

Add a decision kind in four places: a tool schema + clamp branch (`schemas.py`), a mock answer
(`dm_agent.py`), a request trigger (`scheduler.lua`, or its own module like `consumables.lua`)
and an applier (`apply.lua`). Good next ones: devil vs angel room (`Level:InitializeDevilAngelRoom`),
random pickup variants everywhere (`MC_POST_PICKUP_SELECTION`), swapping layout enemies
(`MC_PRE_ROOM_ENTITY_SPAWN`), chest pickup contents and beggar payouts.
