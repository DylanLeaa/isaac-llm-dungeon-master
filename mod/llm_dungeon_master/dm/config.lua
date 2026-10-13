-- LLM Dungeon Master - settings. Edit and restart the run (or `luamod llm_dungeon_master`).
return {
  -- "tcp"  : LuaSocket to the backend (fastest).  "file" : two .jsonl files in BRIDGE_DIR.
  -- "auto" : try tcp, fall back to file if the socket library can't be loaded.
  TRANSPORT = "auto",
  HOST = "127.0.0.1",
  PORT = 7337,
  -- Relative to the game's working directory (the install folder), or absolute.
  BRIDGE_DIR = "llmdm_bridge",

  -- nil = use the backend's --cruelty. 0 (generous) .. 10 (merciless).
  -- Only the starting value: once changed in Mod Config Menu > DM Settings, the saved value wins.
  CRUELTY = nil,

  -- What the DM controls. Anything switched off stays vanilla.
  CONTROL_ROOM_LAYOUT = true,
  CONTROL_ROOM_CLEAR  = true,
  CONTROL_ITEMS       = true,
  CONTROL_FLOOR       = true,
  CONTROL_CONSUMABLES = true,   -- which cards/trinkets drop, what each pill colour does

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
  -- How long after entering a room a late room_layout decision may still be applied.
  LATE_LAYOUT_GRACE_FRAMES = 20,
  ITEM_CANDIDATES = 6,
  TRINKET_CANDIDATES = 8,
  -- consumables requests per floor (floor start + re-asks for new pill colours / empty queues)
  MAX_CONSUMABLE_SENDS = 4,

  -- Characters per line in Mod Config Menu > DM Settings > Receipt. Lower it if lines get cut off.
  RECEIPT_WIDTH = 32,

  -- Every floor waits on a loading screen until the DM is ready for it: the run
  -- narrative (first floor), the floor brief, and answers to the floor's opening decisions.
  -- It gives up on its own: CONNECT if the backend isn't there, TIMEOUT (run start) or
  -- FLOOR_TIMEOUT (later floors) if the plans never land.
  LOADING_SCREEN = true,
  LOADING_CONNECT_MS = 3000,
  LOADING_TIMEOUT_MS = 90000,
  LOADING_FLOOR_TIMEOUT_MS = 60000,

  SHOW_TAUNTS = true,
  -- After-death stats (yours and the DM's, plus its epitaph) over the game-over screen; the map
  -- button hides it. Always kept under Mod Config Menu > DM Settings > Last run.
  SHOW_DEATH_SCREEN = true,
  SHOW_DEBUG_HUD = true,   -- small latency / connection readout in the corner
}
