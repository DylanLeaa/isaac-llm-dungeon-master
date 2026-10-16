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
   out: log.txt   in: the mod's save file            RunMemory (schemes, ledger)
 apply.lua      spawns / morphs / curses
```

## The timing model (asking late, but not too late)

Every decision is requested ahead of the moment the game needs it, with the freshest
snapshot available. When the answer isn't there in time, the game **never decides without the DM**:
it freezes on the loading screen (input blocked, enemies frozen, you can't be hurt) until the answer
lands, asking right then if it never asked. Rooms are dressed and rewards dropped late. Item pedestals
and cards/trinkets, which the game fills on the spot, are swapped for the DM's picks once they come.
A pill the plan doesn't cover won't go down until it does. A floor's curses change once its plan lands.
If the backend answers "no decision", it is asked again (`HOLD_RETRIES`). The freeze only ends without
the DM if the backend is gone (`LOADING_CONNECT_MS`) or `HOLD_TIMEOUT_MS` passes. Then the game is left as
it is (a skipped room-clear award stays skipped), and the receipt counts it under "DM gave up". With no
backend at all, the run is plain vanilla.

| Decision | Asked when | Refreshed when | Used at |
|---|---|---|---|
| `room_layout` (enemies, champions, rocks, pickups) | you enter a room bordering an unvisited normal room | your HP / K-B-C / items change while you linger | `MC_POST_NEW_ROOM` of that room (held until the answer lands) |
| `room_clear` (the drop; normal, miniboss and curse rooms. In boss rooms it is a bonus on top of the game's own award) | **estimated time-to-clear** (remaining enemy HP ÷ your recent DPS) < `1.4 × p90 latency + 400ms`, or the room is nearly dead | you get hit or pick something up and there's still time for a new answer | `MC_PRE_SPAWN_CLEAN_AWARD` (late: the game's award is skipped, held, and the DM's dropped after) |
| `item_choice` (ranked real candidates from the pool) | floor start, and again when you stand next to a treasure/shop/boss/devil/ultra secret/… room; chest and beggar pools when one shows up in your room | — | `MC_PRE_GET_COLLECTIBLE` (also catches D6 rerolls; late: the pedestal is swapped once the ranking lands) |
| `consumables` (next cards/runes, next trinkets, an effect per pill colour in play) | floor start | a pill colour the plan doesn't cover appears (held or on the floor), or the card/trinket queues run dry | `MC_GET_CARD`, `MC_GET_TRINKET` (late: the pickup is swapped), `MC_GET_PILL_EFFECT` (the pill button holds until it's covered) |
| `payout_plan` (per beggar/machine/chest: what each paid use yields, or what the chest holds) | the machine or chest shows up in your room; again when a schedule is used up | - | `MC_POST_PICKUP_INIT`: the game's own payout beside it is cancelled and the scheduled outcome paid (even "nothing", even when the game wouldn't pay) |
| `floor_plan` (curses, treasure distance) | you enter the boss room | boss dies | `MC_POST_CURSE_EVAL` of the next floor (late: curses changed once the plan lands) |

The planners start when the first request of a new run, or of a new floor (`stage` and
`stage_type` from the snapshot), reaches the backend. A new run opens on a **loading screen**: the mod
sends `prepare`, input is blocked and you can't be hurt until the backend answers `ready` (the narrative,
about 20–60s, is written or has failed). It can't be skipped, but it gives up by itself after
`LOADING_CONNECT_MS` with no backend or `LOADING_TIMEOUT_MS` without a narrative, and it doesn't come up at
all if no backend has answered since the game started (`LOADING_SCREEN = false` turns it off). The time spent there isn't counted in the run's clock. Nothing else waits: until a floor's
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

*The app (what players run).* `LLM Dungeon Master.exe` opens a window instead of a console. In
**Settings**, paste an OpenRouter key or switch on **Use Claude Code**, then press **Save and apply**.
Changes apply to the running DM straight away, mid-run included. The window has four tabs:
- **Current run**: the receipt summary (LLM bill, tokens, decisions used / unused / the DM gave up on,
  what the DM did to you, its scheme, last taunt and epitaph), every entry live with its intent and
  effect, the selected entry
  in full, and the run narrative and floor brief.
- **History**: every past run's receipt in the same view.
- **Settings**: the key and the Claude Code switch, plus the files the backend found.
- **Log**: what the console used to show.

The header shows whether the game is connected. Errors show in a dialog and the Log tab, and are
also written to the app's own log file.

The app keeps its files in `%APPDATA%\LLM Dungeon Master\`:
- `config.json`: the settings, with the key in plain text in your own user folder;
- `receipts\`: the run receipts;
- `backend.log`: the app's log, which is what to attach to a bug report.

A blank key falls back to `OPENROUTER_API_KEY`. Only one copy of the app runs at a time.
Windows SmartScreen may warn about an unsigned exe: choose **More info → Run anyway**.

Run the app from source with `python backend/app.py` (`--mock` for no key). To build the exe:
```powershell
powershell -ExecutionPolicy Bypass -File backend\build.ps1   # -> backend\dist\LLM Dungeon Master.exe
```

*The console server (development, and what the tests drive).*
```bash
cd backend
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...       # PowerShell: $env:OPENROUTER_API_KEY="sk-or-..."   cmd.exe: set OPENROUTER_API_KEY=sk-or-...
python server.py --cruelty 6              # finds the game by itself; model anthropic/claude-haiku-5.5 via OpenRouter
python server.py --mock                   # offline stand-in, no key needed
python server.py --no-planners            # Haiku only: no Opus narrative / Sonnet floor briefs
python server.py --narrative-model anthropic/claude-opus-5.5 --floor-model anthropic/claude-sonnet-5.5   # default is both sonnet
python server.py --backend claude-code    # same DM/planners through your local Claude Code (claude -p), no OpenRouter key

```
`--backend claude-code` (or `LLMDM_BACKEND=claude-code`) runs every Claude call as a headless
`claude -p` process: no tools, settings or MCP servers, and our system prompt in place of Claude
Code's. It bills however Claude Code is signed in (subscription or API key). The defaults are
`claude-haiku-5-5` for the DM and `claude-sonnet-5-5` for the planners, and `--model` and the
planner flags take Claude model IDs. The re-ask gate isn't a Claude model, so it stays on OpenRouter
when `OPENROUTER_API_KEY` is set and is off otherwise. Each call starts a fresh process, takes
two API turns for its structured answer, and Haiku thinks before answering under Claude Code, so
decisions take about 7–11 s instead of 2–3 s even at the `--effort low` this backend uses for them. To
fit the game's 10–12 s deadlines it runs at most 3 decisions at once, starts each deadline when its
call does, and lets a call run up to 20 s past its deadline and still deliver: the game keeps a late
answer and uses it if it still needs one. Use `--claude-bin` if `claude` isn't on PATH.
`python tests/live_smoke.py --backend claude-code` checks it.
The console (or the app's Log tab) logs every decision with its latency, intent and taunt, and
prints the run narrative and each floor brief in full. It's fun on a second monitor. The planners add roughly
$0.05 per run (Opus) and $0.02–0.03 per floor (Sonnet).

**2. Mod**
- Copy `mod/llm_dungeon_master/` into `…/steamapps/common/The Binding of Isaac Rebirth/mods/`.
  No launch options: the mod runs in the game's normal sandbox.
- Start the game with the backend running. Bottom-left shows `DM connected, link …ms  avg …ms p90 …ms`
  (`link` is the round trip of the connection alone; `avg` / `p90` include the LLM).

The backend looks for the game in your Steam libraries and its `log.txt` under
`Documents/My Games/Binding of Isaac …`. If it can't find them, pass `--isaac-dir "<Isaac install>"`
and/or `--log "<path to log.txt>"`. With no backend running, the game plays as vanilla: there's no
loading screen, and the DM says once per run that it isn't there.

**How the mod talks to the backend.** Isaac's sandbox gives a mod no sockets or file access, so
the link goes through two files the game writes anyway ([backend/bridge.py](backend/bridge.py),
[dm/bridge.lua](mod/llm_dungeon_master/dm/bridge.lua)):
- *Mod → backend*: each message is written with `Isaac.DebugString`, so it lands in `log.txt` as
  `[LLMDM:<n>:<i>/<k>]<json>`, split over several lines when it's long. The backend tails the log.
- *Backend → mod*: the backend writes its messages, numbered, into the mod's save file
  (`<install>/data/llm_dungeon_master/save1.dat`, under `"dm"`). The mod reads it every `POLL_MS`
  and acks what it has handled. The mod's own data in that file (settings, pill locks, run stats) is
  kept as is. The mod puts its session id in the file before its hello, and the backend finds the file
  by looking for it, so the save slot and the Workshop folder name don't matter.
- Hello and welcome carry a protocol version (`PROTO` in both files). If they differ, the mod
  doesn't play, and it tells the player whether the backend or the mod needs updating.

**3. Check the DM's voice first (optional)**: `python tests/live_smoke.py --cruelty 8` makes one
real call per decision kind and prints the results and latencies. Add `--planners` to first
write a real narrative and floor brief and hand them to the DM.

## Configuration

`mod/llm_dungeon_master/dm/config.lua` toggles each control area, the prefetch lead factor,
refresh limits, taunts, the debug HUD and `SHOW_EFFECTS` (what the DM just did, as icons in the
bottom-right corner: "+1 bomb", "-1 penny", "+2 attack flies"). `ALLOW_LAYOUT_RESEED` lets the DM's treasure-room
distance wish reroll the floor via the `reseed` console command — off by default because
console commands disable achievements for that run.

## The receipt (what the engine did, and what it cost)

Every engine action is written to a per-run receipt: each decision with its request (snapshot,
context, memory digest), the model's raw tool call, the clamped result and the guardrails that
fired. It also gets the planners' narrative and briefs, holds the DM gave up on, timeouts, errors,
superseded calls and cruelty changes. Each LLM call keeps its model, provider, generation id,
finish reason, raw response message, tokens and cost.

In the app, each decision reads across as:
- **Asked because**: why the game asked (`player is next to this room`, `almost clear`,
  `refresh: player state changed while lingering`).
- **Intent**: what the model said it is trying to do to the player (its `intent` field).
- **Result**: what it decided, after the guardrails (`1x key`, `+2 gaper; champions 30% red`). When the
  guardrails changed what the model asked for, the difference follows:
  `[clamped: champion chance 60% -> 18%; extra enemies 3x gaper -> 1x gaper]`.
- **Effect**: what that actually did in the game, as the mod saw it when it applied the decision:
  `+1 bomb dropped`, `-1 key, -1 heart -> +2 pennies (layout pickups)`, `+2 champions (red, 30% of 5)`,
  `+curse of darkness`, `item offered: Tech X (q4)`, `pill blue_blue = speed down`. Rankings and
  consumables plans are used a bit at a time, so they gain a line per use (each pedestal, card, trinket
  or pill). A decision that was never used has no effect.

- **On disk**: `<started>_run-<seed>.jsonl` in `backend/receipts/` (from source) or
  `%APPDATA%\LLM Dungeon Master\receipts\` (the exe), one JSON object per line
  (`--receipts DIR` moves it, `--no-receipts` keeps it in memory only). System prompts are
  stored once per file and referenced by hash. Later outcomes (`applied`, or `replaced` when a
  re-ask made an answer unused) are appended as `update` lines, and what an applied decision did as
  `effect` lines. Continuing a run resumes its file, even after a backend restart.
- **In the app**: the Current run and History tabs (see Setup). The game itself no longer shows the
  receipt.

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
  curses, bad pills, decisions it gave up on and the LLM bill. Only decisions the game actually
  used count. They are tallied from the receipt, so a continued run still counts in full.
- The run's title from the Opus narrative, the DM's last taunt, the long con it was running
  (its private `scheme`, revealed at last) and an **epitaph** Haiku writes for the run. The
  epitaph arrives a second or two after the rest (`--no-epitaph` turns it off).

`SHOW_DEATH_SCREEN = false` in `dm/config.lua` turns
the overlay off. The last finished run, including a victory, is kept in the save file under
Mod Config Menu → **DM Settings → Last run** (if lines get cut off there, lower `MENU_WIDTH` in
`dm/config.lua`). The backend also logs it and adds `run_summary` and
`epitaph` entries to the receipt.

## What's tested, and what isn't

- `tests/test_clamp.py` — guardrails.
- `tests/test_bridge_e2e.py` — the real `bridge.lua` and `savefile.lua` under Lua 5.3 talking to the
  backend through a fake game folder (log.txt + save file), plus a Python stand-in for the mod:
  superseding, timeouts, chunked messages, acks, protocol mismatch, and each side's data surviving the
  other's writes.
- `tests/test_mod_headless.py` — the **whole mod** (`main.lua` + `dm/*`) on a fake Isaac API
  against the mock backend: prefetch → room dressed on entry → clear decision requested mid-fight
  and ready at the last kill → DM-picked treasure item → card/rune/trinket draws and a locked pill
  colour → golden-chest pool ranked on sight → boss-room floor plan and bonus clear → curse mask
  → the receipt file on disk, with what each used decision did → death:
  the stats screen, the DM's side and its epitaph in DM Settings → Last run.
- `tests/test_receipt.py` — the receipt: call records, applied/replaced updates, effects, errors,
  planners, resuming a continued run.
- `tests/test_epilogue.py` — the after-death stats: only applied decisions count, the epitaph
  follows the stats and a failed one never leaves the screen waiting.

These run without the game, against a fake Isaac API and a fake game folder. In the game, errors are
caught per callback and printed once to the console/`log.txt`, and that event falls back to vanilla.

## Known limits (vanilla API)

- **Map layout** can't be authored directly without REPENTOGON; the DM controls it via curses and
  (optionally) rerolling the floor. With REPENTOGON's level-generation callbacks this could become
  true room placement — `floor_plan` is the place to add it.
- Room dressing only touches normal rooms (not shops, bosses, challenge rooms). Player 1 only.
  Greed mode is left vanilla. Golden pills stay random.
- Beggars, machines and chests follow a per-instance `payout_plan` schedule (`dm/payouts.lua`). A "use"
  is spotted by the player's coins/keys/bombs dropping beside a machine, and an opening by the chest
  changing, so unusual machines (fortune teller, blood donation, confessional) are left to the game.
- Enemy-death drops and natural spawns in rooms whose clear the DM decides are swallowed
  (`dm/dropcatcher.lua`) and handed to the `room_clear` request as `suppressed_drops`; what the DM
  doesn't put back shows as e.g. `-1 key (would have dropped, not replaced)` in the effect. They come
  back at once if the DM never answers. `CONTROL_DROPS`, `DROPS_NATURAL` and `CONTROL_PAYOUTS` switch
  these off. Other room types (shops, challenge rooms...) still drop as the game does.
- How the game spawns those pickups (spawner, timing, slot animation) is classified, not guaranteed:
  with `LOG_SPAWNS` on, each pickup spawn is reported to the backend as a `spawn_log` event so the
  classification can be checked against a real run.
- Enemy IDs are referenced by `EntityType` enum name, so anything missing on your version is skipped.

## Extending

Add a decision kind in four places: a tool schema + clamp branch (`schemas.py`), a mock answer
(`dm_agent.py`), a request trigger (`scheduler.lua`, or its own module like `consumables.lua`)
and an applier (`apply.lua`). Good next ones: devil vs angel room (`Level:InitializeDevilAngelRoom`),
swapping layout enemies (`MC_PRE_ROOM_ENTITY_SPAWN`).
