# LLM Dungeon Master for The Binding of Isaac: Repentance

The RNG has been unplugged. Room-clear drops, what each kill drops, which item sits on the pedestal
(or comes out of a chest or a beggar), how a room is dressed (extra enemies, champion colours, tinted
"secret" rocks, layout pickups), which card or trinket turns up, what each pill colour does and what
curses the next floor carries are all decided by **Claude Haiku 5.5**, playing a gleeful, trolling tabletop
DM. It sees your hearts, keys, bombs, coins, luck, build and what the floor still demands of
you — and gives you twelve coins on a floor with no shop.

Two slower planners work above it. At the start of a run, **Claude Opus 5.5** writes the run's
narrative: a premise, an angle on your character, an arc across the chapters and some running gags.
At the start of every floor, **Claude Sonnet 5.5** writes a floor brief: the arc beat, which
resources to starve or flood, how to dress rooms, its stance on items, a multi-room set-piece
and a voice. Both go into Haiku's system prompt, so every decision serves the plan.

```
 Isaac (Lua mod)                                        backend (Python)
 ───────────────                                        ────────────────
 game callback ─▶ rolls.lua ─▶ causes.lua                 Campaign: Opus narrative (once per run)
 (the roll)       one owner     "why now": a kill,                  Sonnet brief + ECONOMY (per floor)
                  per roll      a chest, a slot use                     │ system prompt
                     │                                    Coordinator ── Haiku 5.5 (tool call)
                     ▼                                     latest-wins per key     │
 decks.lua ── "decide drops:3" + snapshot + deck ───────▶ clamp() + the floor's ◀──┘
   draw / owe / restock / rewrite the stale tail          economy (one budget for every deck)
 streams/*.lua ◀── clamped deck ───── bridge.lua ◀──────  RunMemory (open decks, schemes, ledger)
   what an outcome means; apply.lua spawns it  ── drawn ─▶  delivered, committed, bought
```

## Rolls, streams and decks

Every random roll the game makes is intercepted where it happens and owned by exactly one **stream**
([dm/rolls.lua](mod/llm_dungeon_master/dm/rolls.lua)). A pickup the game spawns on its own is a *spawn
roll*: [dm/causes.lua](mod/llm_dungeon_master/dm/causes.lua) says why (a kill, a chest opening, a slot
use, a tinted rock or a poop breaking), using the real spawner first and what just happened nearby only
without one. The stream that owns that cause cancels the game's spawn and pays its own outcome. What a
pickup turns out to be (a card, a trinket, a pedestal's item, a pill's effect) is an *identity roll*,
owned by its own stream. While a spawn that is being cancelled initialises, its identity rolls draw
nothing, so one physical roll is never governed by two decisions.

Each stream draws from a **deck** ([dm/decks.lua](mod/llm_dungeon_master/dm/decks.lua)): outcomes the DM
wrote in advance, one per roll. A request carries `wanted` (the deck's size), `deck_left` (what is
unused) and `drawn_so_far`, and its answer replaces the unused tail. The mod reports every draw to
the backend (`drawn`), so the ledger counts what was actually handed out. When the snapshot a deck was
written against moves (a hit, resources, an item), or another deck spends its budget (the backend's
`stale` push), the unused tail is rewritten. The drawn entries stand. A deck that is empty when the
game needs it is the one late-answer path (`Decks.owe`): the game is held until the answer lands.
Adding a randomness source is one file in `dm/streams/`.

## The floor's economy (one budget for every deck)

Each deck is decided on its own, but the backend sees all of them ([backend/economy.py](backend/economy.py)).
For each resource family (keys, bombs, coins in cents, hearts in half hearts, cards, pills, trinkets,
traps; luck per run; power, meaning item quality and stat pills, is shown but not trimmed) it keeps,
per floor, a **target**: what vanilla hands out on this floor's rooms, times the floor brief's word
for the family (`ECONOMY: keys=starve coins=flood ...`: starve 0.3, lean 0.6, vanilla 1, generous 1.5,
flood 2.2), shifted by cruelty. Alongside the target it keeps what has been **delivered** and what each
open deck has **committed**.
- Every request gets `ctx.economy`: per family the budget, what's given, what other decks promise
  and what's left, with a one-line verdict. `memory.your_open_decks` shows those decks (what's left in
  them, their intent and scheme), and `memory.in_flight` the decisions being made right now.
- `clamp()` trims each answer against the budget and the backend commits it in the same step, so two
  calls racing for the last coin are settled by which finishes first. The loser's coin becomes
  "nothing" (`penny -> nothing (coins over budget: clear:3:85 took the last one)`), and it is re-asked
  once to rewrite around it. Mercy and the drought breakers come after the trim and always win.
- Chests and tinted rocks count once opened or bombed. Machines are exchanges: what they pay is
  `bought`, never counted against the target, and a house edge (cruelty-scaled) caps what one pays
  back for what it was paid.
- A family pushed over budget anyway (a chest opened early) re-asks the other decks committed to it.

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

| Stream (key) → kind | Asked when (its forecast) | Rewritten when | The roll |
|---|---|---|---|
| `layout:<f>:<room>` → `room_layout` (enemies, champions, rocks, pickups) | you enter a room bordering an unvisited normal room | your HP / K-B-C / items change while you linger | `MC_POST_NEW_ROOM` of that room (held until the answer lands) |
| `clear:<f>:<room>` → `room_clear` (normal, miniboss and curse rooms; a bonus on top of the game's own award in boss rooms) | **estimated time-to-clear** (remaining enemy HP ÷ your recent DPS) < `1.4 × p90 latency + 400ms`, or the room is nearly dead | you get hit, pick something up, the kills drop something, or the economy moves, while there's still time | `MC_PRE_SPAWN_CLEAN_AWARD` (late: the game's award is skipped, held, and the DM's dropped after) |
| `drops:<f>` → `drops` (one entry per kill or breakable) | you enter a room with enemies (sized to them + `DROPS_SPARE`) | your state or the economy moves | `MC_POST_NPC_DEATH`, or a poop/fire/urn/skull breaking: the game's own drop is cancelled in `MC_PRE_ENTITY_SPAWN` (empty deck: the game's drop stands, reported) |
| `rock:<f>:<room>` (sticky, from the layout's `tinted_rock_loot`) | with the room's layout | — | a tinted rock breaking: its loot is cancelled and the DM's paid (no plan: the drops deck takes it) |
| `items:<f>:<pool>` → `item_choice` (ranked real candidates) | floor start, and again when you stand next to a treasure/shop/boss/devil/ultra secret/… room; chest and beggar pools when one shows up in your room | your state changes while you're next to it (`MAX_ITEM_SENDS`) | `MC_PRE_GET_COLLECTIBLE` (also D6 rerolls; late: the pedestal is swapped once the ranking lands) |
| `cards:<f>`, `trinkets:<f>` → `cards`, `trinkets` (`CARDS_DEPTH` / `TRINKETS_DEPTH` entries) | floor start, then whenever the deck runs out | — | `MC_GET_CARD`, `MC_GET_TRINKET` (late: the pickup is swapped once the answer lands) |
| `pills:<f>` → `pills` (sticky: an effect per colour in play) | a colour no answer covers is in play (held or on the floor) | — | the first swallow of a colour locks it for the run; the pill button holds until it's covered |
| `payout:<f>:<seed>` → `payout_plan` (per beggar/machine: one entry per paid use; per chest: its contents) | the machine or chest shows up in your room; again when one entry is left | never: a schedule is a story, it is only extended | a payment beside the machine, or the chest opening: the game's own payout is cancelled and the entry paid (even "nothing", even when the game wouldn't pay) |
| `curses:<f>` → `floor_plan` (curses, treasure distance) | you enter the boss room | boss dies | `MC_POST_CURSE_EVAL` of the next floor (late: curses changed once the plan lands) |

The planners start when the first request of a new run, or of a new floor (`stage` and
`stage_type` from the snapshot), reaches the backend. A new run, and every new floor, opens on a
**loading screen**: the mod sends `prepare`, input is blocked and you can't be hurt until the backend
answers `ready` (the narrative, about 20–60s, on the first floor; the floor brief, about 5–15s) and the
rooms next to the start room have their answers (their layouts, and the item ranking for any special
room next door). The rest of the floor (its other item pools, the card/trinket/pill decks) is asked in
the background while you play. The screen can't be skipped, but it gives up by itself after
`LOADING_CONNECT_MS` with no backend or `LOADING_TIMEOUT_MS` / `LOADING_FLOOR_TIMEOUT_MS` without its
plans, and it doesn't come up at all if no backend has answered since the game started
(`LOADING_SCREEN = false` turns it off). The time spent there isn't counted in the run's clock. Decisions
are never written against a stale brief: a request waits up to 45s for its floor's brief. A floor brief
waits up to 25s for the run narrative, then goes ahead without it. A planner's call is sent again after
a 5xx (twice, backing off) and waits out a 429 that asks for 20s or less. If it still fails, it is logged
and the DM carries on.

Provider errors on decisions: a 5xx is sent again once after a short backoff. A 429 puts the DM's model in
a cooldown, for the provider's Retry-After (Google's `retryDelay`) or else an exponential backoff (2s, 4s,
8s... with jitter, reset by a call that goes through). Nothing is sent during it: a request is held until
the cooldown ends and then made, or, if the cooldown outlasts its deadline, answered `cooldown` at the
deadline (receipt status `cooldown`, nothing billed) so the game's re-ask lands after it. Every error in
the receipt keeps the provider's response body (`body`, up to 2000 characters).

Requests for the same key supersede each other: the backend cancels the stale Haiku call and
the mod only replaces a result with one for a newer request. The p90 latency is measured live,
so the lead time adapts to your connection.

## Guardrails (the DM may be cruel, not unfair)

Haiku can choose anything inside the schema; `backend/schemas.py:clamp()` is the referee:

- `--cruelty 0..10` scales extra-enemy budget (0–3/room), champion chance cap, curse count (0–2)
  and troll-bomb budget per floor (none below cruelty 4; no curses below 3). Max one live troll bomb
  per room. The planners' prompts list these limits for the current cruelty, and a floor plan is told
  `allowed_curses`; a curse cut by them is noted in the guardrails. With
  Mod Config Menu (Pure) installed,
  **DM Settings → Cruelty** changes it in-game (even mid-run), overrides `--cruelty` and is saved.
- **Mercy**: at ≤1 heart, no troll bombs, ≤1 extra enemy, no Curse of the Unknown, and a half
  heart sneaks into the drop after a long heart drought.
- **Drought breaker**: 0 keys + locked doors seen + 7 key-less clears → a key is added (same for bombs).
- Item and trinket picks must come from the real candidates; unknown enemies/rewards/cards are dropped.
- **The economy**: every deck spends the floor's budget per resource family (see above); past it, an
  outcome becomes "nothing". Kill drops also have a per-floor count (from vanilla's clear rate and
  cruelty), shown to the DM as `ctx.economy.kill_drops`; once it is spent, a drops deck is written as
  all "nothing" by the backend, with no model call (`local` in the summary and the receipt). Machines keep a house edge: over its uses one pays back at most `1 - 0.05 × cruelty` of
  what it was paid (at shop prices), plus one small win, and at most one item.
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
  in full, the run narrative and floor brief, and the **Deck view**: every deck the DM has written, per
  floor, in the order the game will draw it (the next payout, kill drop, pedestal, card...), with what has
  already been drawn and each deck's intent. Rebuilt from the receipt (`backend/deckview.py`).
- **History**: every past run's receipt in the same view. Its Deck view shows the decks as they stood
  when the row picked in the receipt was recorded.
- **Settings**: the key, the Claude Code switch, the models, and the files the backend found. **Models** picks
  three models from OpenRouter's live list, with their prices. **Major** writes the run narrative and floor briefs,
  **Minor** makes every decision (only tool-capable models are listed), and the **re-ask gate** uses one of
  OpenRouter's typed-decisions models. The list is sorted by intelligence (the Artificial Analysis index) by default.
  Click a column heading to sort by it, and filter by name, provider, or scored models only. With Claude Code on,
  its models are pinned to the top of the Major and Minor lists, so either role can run there. **Default** goes back
  to Haiku / Sonnet / Mercury. The list is cached in `models.json`, so the pickers work offline.
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
prints the run narrative and each floor brief in full. Its summary table counts one run (it restarts
when a new run says hello), and a death or victory cancels the decisions still being made. It's fun on a second monitor. The planners add roughly
$0.05 per run (Opus) and $0.02–0.03 per floor (Sonnet).

**2. Mod**
- The app (the exe, or `python backend/app.py`) installs the mod itself: at start it copies its own
  `mod/llm_dungeon_master/` into `…/steamapps/common/The Binding of Isaac Rebirth/mods/` of every
  install it finds, and updates it when it differs, so mod and backend always match. It leaves the
  folder alone when it is a symlink or junction (a link to your checkout) and when a Workshop copy
  (`mods/llm_dungeon_master_<id>`) is there. The console server doesn't install anything: copy the
  folder by hand. No launch options: the mod runs in the game's normal sandbox.
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
refresh limits, deck sizes (`CARDS_DEPTH`, `TRINKETS_DEPTH`, `DROPS_SPARE`), the roll audit
(`ROLL_REPORT_MS`, `LOG_SPAWNS`), how a cancelled spawn goes (`CANCEL_IN_PRE_SPAWN`), taunts, the debug
HUD and `SHOW_EFFECTS` (what the DM just did, as icons in the
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
  `+curse of darkness`, `item offered: Tech X (q4)`, `pill blue_blue = speed down`,
  `kill: +1 penny (instead of the game's key)`. Decks are used a bit at a time, so they gain a line per
  use (each kill, pedestal, card, trinket, pill or machine use). A decision that was never used has no
  effect.

- **On disk**: `<started>_run-<seed>.jsonl` in `backend/receipts/` (from source) or
  `%APPDATA%\LLM Dungeon Master\receipts\` (the exe), one JSON object per line
  (`--receipts DIR` moves it, `--no-receipts` keeps it in memory only). System prompts are
  stored once per file and referenced by hash. Later outcomes (`applied`, or `replaced` when a
  re-ask made an answer unused) are appended as `update` lines, what an applied decision did as
  `effect` lines, and every outcome the game drew from a deck as a `drawn` line. Continuing a run
  resumes its file, even after a backend restart.
- **The roll audit**: the mod counts every roll by hook, cause and owner and sends the counts every
  `ROLL_REPORT_MS` (`rolls` entries in the receipt, with samples of what each spawn looked like when
  `LOG_SPAWNS` is on). The backend log's summary and the app's Current run tab show controlled vs.
  uncontrolled (vanilla) rolls per hook and cause, the most uncontrolled first.
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
- **The DM**: rooms it dressed, extra enemies, keys/bombs/coins/hearts it dropped (from clears,
  kills, machines, chests and rocks), troll bombs, curses, bad pills, decisions it gave up on and the
  LLM bill. Only what the game actually drew counts. It is tallied from the receipt, so a continued
  run still counts in full.
- The run's title from the Opus narrative, the DM's last taunt, the long con it was running
  (its private `scheme`, revealed at last) and an **epitaph** Haiku writes for the run. The
  epitaph arrives a second or two after the rest (`--no-epitaph` turns it off).

`SHOW_DEATH_SCREEN = false` in `dm/config.lua` turns
the overlay off. The last finished run, including a victory, is kept in the save file under
Mod Config Menu → **DM Settings → Last run** (if lines get cut off there, lower `MENU_WIDTH` in
`dm/config.lua`). The backend also logs it and adds `run_summary` and
`epitaph` entries to the receipt.

## What's tested, and what isn't

- `tests/test_clamp.py` — guardrails: every kind, the budget trim, the house edge.
- `tests/test_decks.py` — the deck store: draw, empty → owe → refill → fixup, sticky entries, stale
  rewrites within their budget, the usable filter, skipping entries drawn while an answer was on its way.
- `tests/test_coordinator.py` — also the economy: `drawn` → the ledger and the receipt, a deck trimmed
  by what another promised, an overspend re-asking the other decks, machines paid for (`bought`), and
  two concurrent decks racing for the last coin in either order.
- `tests/test_bridge_e2e.py` — the real `bridge.lua` and `savefile.lua` under Lua 5.3 talking to the
  backend through a fake game folder (log.txt + save file), plus a Python stand-in for the mod:
  superseding, timeouts, chunked messages, acks, protocol mismatch, and each side's data surviving the
  other's writes.
- `tests/test_mod_headless.py` — the **whole mod** (`main.lua` + `dm/*`) on a fake Isaac API
  against the mock backend: prefetch → room dressed on entry → clear decision requested mid-fight
  and ready at the last kill → DM-picked treasure item → card/rune/trinket draws and a locked pill
  colour → golden-chest pool ranked on sight → boss-room floor plan and bonus clear → curse mask
  → kills popping the drops deck with the game's drops cancelled (a cancelled card drop takes nothing
  from the cards deck) → tinted rock, beggar, chests and a grab bag paying their schedules (a golden
  chest's item drawn once, from its pool) → the roll audit with nothing covered left to vanilla → the
  receipt file on disk → death: the stats screen, the DM's side and its epitaph in DM Settings → Last run.
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
- Beggars, machines and chests follow a per-instance `payout_plan` schedule (`dm/streams/payouts.lua`).
  A "use" is spotted by the player's coins/keys/bombs dropping beside a machine, and an opening by the
  chest changing (`dm/causes.lua`). Left to the game, and reported by the roll audit: a machine blown
  up, a beggar leaving, the fortune teller, blood donation and the confessional.
- **The roll audit decides what is left.** How the vanilla API reports each source (whether kill drops,
  chest contents, slot payouts and rock/poop loot carry their spawner and seed, which way of cancelling
  a spawn is safe, the room-entity IDs of rocks and layout pickups) has to be read off a real run with
  `LOG_SPAWNS` on: the open questions, and how the mod copes with either answer, are at the top of
  `dm/rolls.lua`. Until then the mod removes a cancelled pickup as it initialises
  (`CANCEL_IN_PRE_SPAWN = false`), which is what it did before decks.
- Still vanilla, and counted as uncontrolled: boss deaths' own drops, shop stock, the colours of
  pills that drop (`MC_POST_PICKUP_SELECTION` is only counted), what a pickup the game spawns for no
  cause the tracker knows (`unknown`) turns out to be, and golden pills. Layout pickups and tinted rocks
  are changed after the room loads rather than at `MC_PRE_ROOM_ENTITY_SPAWN` (counted there, not yet
  replaced). These are the next streams to add, in the order the audit ranks them.
- `CONTROL_DROPS`, `DROPS_NATURAL` and `CONTROL_PAYOUTS` switch the drops deck, its breakables and the
  machines off.
- Enemy IDs are referenced by `EntityType` enum name, so anything missing on your version is skipped.

## Extending

A new randomness source is one stream file in `dm/streams/`: its deck (`Decks.define`: key, entries,
usable, context, forecast via `wants`, refresh, what a draw reports) and the roll it owns
(`Rolls.on` for an identity roll, `Rolls.claimSpawns` for a spawn cause, `Causes.on` for an event).
A new decision kind also needs a tool schema + clamp branch (`schemas.py`, with `economy.deck_outcomes`
if it hands out resources), a mock answer (`dm_agent.py`) and its line in `prompts.py`. Good next ones,
from the roll audit: shop stock, dropped pill colours, layout pickups at `MC_PRE_ROOM_ENTITY_SPAWN`,
devil vs angel room (`Level:InitializeDevilAngelRoom`).
