-- LLM Dungeon Master - settings. Edit and restart the run (or `luamod llm_dungeon_master`).
return {
  -- The link to the backend: we write to the game's log.txt and read its answers from this mod's
  -- save file every POLL_MS. Messages longer than LOG_CHUNK chars go over several log lines.
  POLL_MS = 30,
  LOG_CHUNK = 900,

  -- nil = use the backend's --cruelty. 0 (generous) .. 10 (merciless).
  -- Only the starting value: once changed in Mod Config Menu > DM Settings, the saved value wins.
  CRUELTY = nil,

  -- What the DM controls. Anything switched off stays vanilla.
  CONTROL_ROOM_LAYOUT = true,
  CONTROL_ROOM_CLEAR  = true,
  CONTROL_ITEMS       = true,
  CONTROL_FLOOR       = true,
  CONTROL_CONSUMABLES = true,   -- which cards/trinkets drop, what each pill colour does (three decks)
  CONTROL_PAYOUTS     = true,   -- what each beggar / machine use and each chest pays out (a fixed schedule)
  CONTROL_DROPS       = true,   -- the drops deck: what each kill drops (the game's own drop is cancelled)
  DROPS_NATURAL       = true,   -- ...and each poop, fire, urn or skull broken (and tinted rocks with no plan)
  -- How a cancelled spawn goes: false removes the pickup as it initialises (its card/trinket/item isn't
  -- drawn meanwhile); true replaces it with a puff of smoke in MC_PRE_ENTITY_SPAWN, so it never exists.
  -- Which one works for every source is the roll audit's question (see dm/rolls.lua).
  CANCEL_IN_PRE_SPAWN = false,
  -- The roll audit: every random roll the game makes is counted by hook, cause and owner (controlled or
  -- vanilla) and sent every ROLL_REPORT_MS. LOG_SPAWNS adds what each pickup spawn looked like (its
  -- spawner, seed and cause; capped per run), which is what calibrates the cause tracker.
  ROLL_REPORT_MS      = 3000,
  LOG_SPAWNS          = true,

  -- Re-roll the floor (console `reseed`) until the treasure room distance matches the
  -- DM's preference. Uses a console command, which disables achievements for the run.
  ALLOW_LAYOUT_RESEED = false,
  MAX_RESEEDS = 4,

  -- Prefetch timing ---------------------------------------------------------
  -- Room-clear rewards are requested when the estimated time-to-clear drops below
  --   LEAD_FACTOR * p90 backend latency + LEAD_PAD_MS
  -- i.e. as late as possible while still landing before the last enemy dies.
  LEAD_FACTOR = 1.4,
  LEAD_PAD_MS = 400,
  -- A pending prefetch is re-sent with a fresh snapshot when the game state changes
  -- materially (damage taken, resources changed) and at least this long has passed.
  REFRESH_MIN_MS = 1500,
  MAX_REFRESHES = 2,
  -- Hard cap on room_layout requests per room per floor (first ask + refreshes). A room the
  -- player keeps walking past is only re-asked when their state changed since the last ask.
  MAX_LAYOUT_SENDS = 3,
  MAX_ITEM_SENDS = 3,      -- item_choice requests per pool per floor (re-asked when the player's state changes)
  ITEM_CANDIDATES = 6,
  TRINKET_CANDIDATES = 8,
  -- Deck sizes: how many entries a deck is kept stocked with, ahead of the game's draws.
  CARDS_DEPTH = 2,
  TRINKETS_DEPTH = 2,
  DROPS_SPARE = 2,         -- drops entries asked beyond the room's enemies (splitters, poops)
  -- Requests per floor for each card / trinket / pill deck (restocks, new pill colours, rewrites).
  MAX_DECK_SENDS = 8,
  MAX_DROPS_SENDS = 40,    -- drops: one ask per fighting room, plus restocks and rewrites
  -- payout_plan requests per machine (first ask + re-asks once its schedule runs low)
  MAX_PAYOUT_SENDS = 4,

  -- Characters per line in Mod Config Menu > DM Settings > Last run. Lower it if lines get cut off.
  MENU_WIDTH = 32,

  -- Every floor waits on a loading screen until the DM is ready for it: the run
  -- narrative (first floor), the floor brief, and answers for the rooms next to the start room.
  -- It gives up on its own: CONNECT if the backend isn't there, TIMEOUT (run start) or
  -- FLOOR_TIMEOUT (later floors) if the plans never land.
  LOADING_SCREEN = true,
  LOADING_CONNECT_MS = 3000,
  LOADING_TIMEOUT_MS = 90000,
  LOADING_FLOOR_TIMEOUT_MS = 60000,
  -- When the game needs a decision the DM hasn't answered yet (a room as you walk in, the reward
  -- as the last enemy dies, a pedestal, a pill), it freezes on the same screen until it has. If the
  -- backend answers "no decision" it is asked again, HOLD_RETRIES times. The freeze ends without the
  -- DM only if the backend is gone (LOADING_CONNECT_MS) or HOLD_TIMEOUT_MS passes.
  HOLD_RETRIES = 2,
  HOLD_TIMEOUT_MS = 45000,

  SHOW_TAUNTS = true,
  -- What the DM just did ("+1 bomb", "-1 penny", "+2 attack flies"), as icons in the bottom-right
  -- corner for a few seconds.
  SHOW_EFFECTS = true,
  -- After-death stats (yours and the DM's, plus its epitaph) over the game-over screen; the map
  -- button hides it. Always kept under Mod Config Menu > DM Settings > Last run.
  SHOW_DEATH_SCREEN = true,
  SHOW_DEBUG_HUD = true,   -- small latency / connection readout in the corner
}
