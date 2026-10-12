# LLM Dungeon Master for The Binding of Isaac: Repentance

The RNG has been unplugged. Room-clear drops, which item sits on the pedestal (or comes out of
a chest or a beggar), how a room is dressed (extra enemies, champion colours, tinted "secret"
rocks, layout pickups), which card or trinket turns up, what each pill colour does and what
curses the next floor carries are all decided by **Claude Haiku 5.5**, playing a gleeful, trolling tabletop
DM. It sees your hearts, keys, bombs, coins, luck, build and what the floor still demands of
you — and gives you twelve coins on a floor with no shop.

Two slower planners work above it. At the start of a run, **Claude Opus 5.5** writes the run's
narrative: a premise, an angle on your character, an arc across the chapters and some running gags.
At the start of every floor, **Claude Sonnet 5.5** writes a floor brief: the arc beat, which
resources to starve or flood, how to dress rooms, its stance on items, a multi-room set-piece
and a voice. Both go into Haiku's system prompt, so every decision serves the plan.

```
 Isaac (Lua mod)                                   backend (Python)
 ───────────────                                   ────────────────
                                                     Campaign: Opus narrative (once per run)
                                                               Sonnet brief (once per floor)
                                                                   │ system prompt
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

The planners start when the first request of a new run, or of a new floor (`stage` and
`stage_type` from the snapshot), reaches the backend. A new run opens on a **loading screen**: the mod
sends `prepare`, input is blocked and you can't be hurt until the backend answers `ready` (the narrative,
about 20–60s, is written or has failed). Enter skips it, and it gives up by itself after
`LOADING_CONNECT_MS` with no backend or `LOADING_TIMEOUT_MS` without a narrative (`LOADING_SCREEN = false`
turns it off). The time spent there isn't counted in the run's clock. Nothing else waits: until a floor's
brief (about 5–15s) lands, Haiku improvises. The old brief is dropped as soon as the floor changes. A
floor brief waits up to 25s for the run narrative, then goes ahead without it. If a planner
fails, it is logged and the DM carries on.

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
python server.py --no-planners            # Haiku only: no Opus narrative / Sonnet floor briefs
python server.py --narrative-model anthropic/claude-opus-5.5 --floor-model anthropic/claude-sonnet-5.5   # default is both sonnet

```
The console logs every decision with its latency, intent and taunt, and prints the run
narrative and each floor brief in full. It's fun on a second monitor. The planners add roughly
$0.05 per run (Opus) and $0.02–0.03 per floor (Sonnet).

**2. Mod**
- Copy `mod/llm_dungeon_master/` into `…/steamapps/common/The Binding of Isaac Rebirth/mods/`.
- Steam → Isaac → Properties → Launch Options: `--luadebug`.
  This is what allows the socket/file access. It lifts the mod sandbox for **every** enabled
  mod, so only run it with mods you trust, and remove it when you're done.
- Start a run. Bottom-left shows `DM tcp connected  avg …ms p90 …ms`.

If LuaSocket won't load on your install, set `TRANSPORT = "file"` in `dm/config.lua` and run
`python server.py --bridge file --bridge-dir "<Isaac install>/llmdm_bridge"` (create that folder).

**3. Check the DM's voice first (optional)**: `python tests/live_smoke.py --cruelty 8` makes one
real call per decision kind and prints the results and latencies. Add `--planners` to first
write a real narrative and floor brief and hand them to the DM.

## Configuration

`mod/llm_dungeon_master/dm/config.lua` toggles each control area, the prefetch lead factor,
refresh limits, taunts and the debug HUD. `ALLOW_LAYOUT_RESEED` lets the DM's treasure-room
distance wish reroll the floor via the `reseed` console command — off by default because
console commands disable achievements for that run.

## The receipt (what the engine did, and what it cost)

Every engine action is written to a per-run receipt: each decision with its request (snapshot,
context, memory digest), the model's raw tool call, the clamped result and the guardrails that
fired. It also gets the planners' narrative and briefs, fallbacks to vanilla, timeouts, errors,
superseded calls and cruelty changes. Each LLM call keeps its model, provider, generation id,
finish reason, raw response message, tokens and cost.

- **On disk**: `backend/receipts/<started>_run-<seed>.jsonl`, one JSON object per line
  (`--receipts DIR` moves it, `--no-receipts` keeps it in memory only). System prompts are
  stored once per file and referenced by hash. Later outcomes (`applied`, or `replaced` when a
  re-ask made an answer unused) are appended as `update` lines. Continuing a run resumes its
  file, even after a backend restart.
- **In game**: Mod Config Menu → **DM Settings → Receipt** shows run totals (actions, LLM calls,
  cost, tokens, cache hit rate, applied/replaced/vanilla/timeouts/errors), an entry browser
  (left/right steps back through the last 40 actions: model, latency, tokens, cost, intent,
  result, taunt, guardrails, errors) and the latest ten at a glance. The cruelty slider is on
  the **General** tab. If lines get cut off, lower `RECEIPT_WIDTH` in `dm/config.lua`.

## The after-death stats screen

When a run ends in death, the game-over screen gets the run's stats down either side of the death
note. The note (and the seed, EXIT and RESTART notes around it) is drawn after every mod callback,
so nothing can sit on top of it, and the strips beside it hold about eight lines a side. So the
screen is two pages that take turns every nine seconds (the map button turns the page): the stats,
then the DM's epitaph and its con. The note positions are measured constants at the top of
`dm/deathscreen.lua`; if text runs under a note, adjust `PAPER_W`, the `SEED_*` box or `EXIT_Y` /
`RESTART_Y`.

- **You**: floor reached, run time, rooms cleared, damage taken, items, pills eaten and
  what hit you last. These come from the mod, so they appear at once, even with the backend offline.
- **The DM**: rooms it dressed, extra enemies, keys/bombs/coins/hearts it dropped, troll bombs,
  curses, bad pills, events left to vanilla and the LLM bill. Only decisions the game actually
  used count. They are tallied from the receipt, so a continued run still counts in full.
- The run's title from the Opus narrative, the DM's last taunt, the long con it was running
  (its private `scheme`, revealed at last) and an **epitaph** Haiku writes for the run. The
  epitaph arrives a second or two after the rest (`--no-epitaph` turns it off).

`SHOW_DEATH_SCREEN = false` in `dm/config.lua` turns
the overlay off. The last finished run, including a victory, is kept in the save file under
Mod Config Menu → **DM Settings → Last run**. The backend also logs it and adds `run_summary` and
`epitaph` entries to the receipt.

## What's tested, and what isn't

- `tests/test_clamp.py` — guardrails.
- `tests/test_bridge_e2e.py` — the real `bridge.lua` under Lua 5.3 talking to the backend over
  the file bridge, plus the TCP server: superseding, timeouts, acks.
- `tests/test_mod_headless.py` — the **whole mod** (`main.lua` + `dm/*`) on a fake Isaac API
  against the mock backend: prefetch → room dressed on entry → clear decision requested mid-fight
  and ready at the last kill → DM-picked treasure item → card/rune/trinket draws and a locked pill
  colour → golden-chest pool ranked on sight → boss-room floor plan and bonus clear → curse mask
  → the DM Settings → Receipt menu filled from the backend, and the receipt file on disk → death:
  the stats screen, the DM's side and its epitaph in DM Settings → Last run.
- `tests/test_receipt.py` — the receipt: call records, applied/replaced updates, errors, planners,
  resuming a continued run.
- `tests/test_epilogue.py` — the after-death stats: only applied decisions count, the epitaph
  follows the stats and a failed one never leaves the screen waiting.

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
