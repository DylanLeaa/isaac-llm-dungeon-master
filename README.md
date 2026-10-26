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

How it works, briefly: the mod intercepts each random roll in the game and asks the backend what it should
turn out to be. The backend asks the models, checks the answer against a budget and the guardrails below, and the mod
carries it out. The game never decides without the DM. If the answer is late it waits on a loading screen, and with
no backend at all the run is plain vanilla. The internals (decks, the floor's economy, timing, agent mode, the
bridge between the mod and backend, the receipts and the tests) are in [TECHNICAL.md](TECHNICAL.md).

## Guardrails (the DM may be cruel, not unfair)

Haiku can choose anything inside the schema; `backend/schemas.py:clamp()` is the referee:

- `--cruelty 0..10` scales extra-enemy budget (0–3/room), champion chance cap (`min(0.6, 0.06 × cruelty)`),
  curse count (0–2) and troll-bomb budget per floor (none below cruelty 4; no curses below 3). Max one live
  troll bomb per room. The prompts list every one of these limits for the current cruelty, and a floor plan is
  told `allowed_curses`; a curse cut by them is noted in the guardrails. The app's **Settings → Cruelty** slider
  is saved. A run keeps the cruelty it started with, since its plans and budgets were made for it: a change
  applies from the next run (before any run it applies at once), and a continued run keeps its own.
- **Mercy**: at ≤1 heart, no troll bombs, ≤1 extra enemy, no Curse of the Unknown, and a half
  heart sneaks into the drop after a long heart drought.
- **Drought breaker**: 0 keys + locked doors seen + 7 key-less clears → a key is added (same for bombs).
- Item and trinket picks must come from the real candidates; unknown enemies/rewards/cards are dropped.
- **The economy**: every deck spends the floor's budget per resource family (see [TECHNICAL.md](TECHNICAL.md#the-floors-economy-one-budget-for-every-deck)); past it, an
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

`LLM Dungeon Master.exe` opens a window instead of a console. In
**Settings**, paste an OpenRouter key or switch on **Route billing through Claude Code**, then press **Save and apply**.
Changes apply to the running DM straight away, mid-run included. The window has four tabs:
- **Current run**: the receipt summary (LLM bill, tokens, decisions used / unused / the DM gave up on,
  what the DM did to you, its scheme, last taunt and epitaph), every entry live with its intent and
  effect, the selected entry
  in full, the run narrative and floor brief, and the **Deck view**: every deck the DM has written, per
  floor, in the order the game will draw it (the next payout, kill drop, pedestal, card...), with what has
  already been drawn and each deck's intent. Rebuilt from the receipt (`backend/deckview.py`).
- **History**: every past run's receipt in the same view. Its Deck view shows the decks as they stood
  when the row picked in the receipt was recorded.
- **Settings**: the key, the Claude Code switch, the models, the cruelty slider (for the next run), agent mode, and the files the backend found. **Models** picks
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

Developers can run the backend without the window, as a console server, with `--mock` for no key and
`--backend claude-code` for no OpenRouter key: see [TECHNICAL.md](TECHNICAL.md#the-console-server).

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

Agent mode (off by default) is **Settings → Mode → Agent mode**, or `python server.py --agent`. The DM then writes the
floor ahead in a few calls instead of answering one decision at a time: see
[TECHNICAL.md](TECHNICAL.md#agent-mode-the-floor-written-ahead-in-a-few-calls).

## Configuration

`mod/llm_dungeon_master/dm/config.lua` toggles each control area, the prefetch lead factor,
refresh limits, deck sizes (`CARDS_DEPTH`, `TRINKETS_DEPTH`, `DROPS_SPARE`), the roll audit
(`ROLL_REPORT_MS`, `LOG_SPAWNS`), how a cancelled spawn goes (`CANCEL_IN_PRE_SPAWN`), taunts, the debug
HUD and `SHOW_EFFECTS` (what the DM just did, as icons in the
bottom-right corner: "+1 bomb", "-1 penny", "+2 attack flies"). `ALLOW_LAYOUT_RESEED` lets the DM's treasure-room
distance wish reroll the floor via the `reseed` console command — off by default because
console commands disable achievements for that run.

## The receipt

Every decision the DM makes is written to a per-run receipt: the request, the model's raw answer, the result after the
guardrails, what it did in the game and what it cost. The app's **Current run** and **History** tabs show it, and the
files are in `%APPDATA%\LLM Dungeon Master\receipts\` (`backend/receipts/` from source). How to read a decision
and the file format are in [TECHNICAL.md](TECHNICAL.md#the-receipt-what-the-engine-did-and-what-it-cost).

## The after-death stats screen

When a run ends in death, the game-over screen shows the run's stats, then the DM's epitaph and its con, taking turns every nine seconds (the map button turns the page).

- **You**: floor reached, run time, rooms cleared, damage taken, items, pills eaten and
  what hit you last. These come from the mod, so they appear at once, even with the backend offline.
- **The DM**: rooms it dressed, extra enemies, keys/bombs/coins/hearts it dropped (from clears,
  kills, machines, chests and rocks), troll bombs, curses, bad pills, decisions it gave up on and the
  LLM bill. Only what the game actually drew counts. It is tallied from the receipt, so a continued
  run still counts in full.
- The run's title from the Opus narrative, the DM's last taunt, the long con it was running
  (its private `scheme`, revealed at last) and an **epitaph** Haiku writes for the run. The
  epitaph arrives a second or two after the rest (`--no-epitaph` turns it off).

`SHOW_DEATH_SCREEN = false` in `dm/config.lua` turns the overlay off.

## Known limits

- The map layout can't be authored directly without REPENTOGON; the DM controls it through curses and (optionally) rerolling the floor.
- Room dressing covers every room type except the floor's start room. Player 1 only. Greed mode is left vanilla. Golden pills stay random.
- Still vanilla: boss deaths' own drops, the colours of pills that drop, and a machine blown up, a beggar leaving, the fortune teller, blood donation and the confessional.

The full list is in [TECHNICAL.md](TECHNICAL.md#known-limits-vanilla-api).

## Disclaimer and license

This is an unofficial fan mod. It is not affiliated with, endorsed by or sponsored by Edmund McMillen,
Nicalis or Valhalla Games Studios. *The Binding of Isaac* and *The Binding of Isaac: Repentance* belong to
their respective owners. The mod ships no game assets and requires a legitimate copy of the game.
It is not affiliated with Anthropic either; it just calls the Claude API with your own key.

Released under the [MIT License](LICENSE).
