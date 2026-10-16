-- LLM Dungeon Master
-- Every "random" event worth trolling about is decided by an LLM (Claude Haiku 5.5)
-- running in a local backend, reached through the game's log and this mod's save file.
local mod = RegisterMod("LLM Dungeon Master", 1)

local cfg    = include("dm.config")
local SaveFile = include("dm.savefile")
local Bridge = include("dm.bridge")
local State  = include("dm.state")
local Taunt  = include("dm.taunt")
local Apply  = include("dm.apply")
local Sched  = include("dm.scheduler")
local Settings = include("dm.settings")
local Receipt = include("dm.receipt")
local Cons   = include("dm.consumables")
local Death  = include("dm.deathscreen")
local Loading = include("dm.loading")

Taunt.enabled = cfg.SHOW_TAUNTS
Apply.init(Taunt)
Cons.init(cfg, Bridge, State, Taunt)
Sched.init(cfg, Bridge, State, Apply, Taunt, Cons)

local game = Game()

-- A bug in this mod must never take the run down with it: log once per message, fall back to vanilla.
local seenErrors = {}
local function safe(name, fn)
  return function(...)
    local ok, ret = pcall(fn, ...)
    if ok then return ret end
    local msg = tostring(ret)
    if not seenErrors[msg] then
      seenErrors[msg] = true
      Isaac.DebugString("[LLMDM] error in " .. name .. ": " .. msg)
      print("[LLMDM] error in " .. name .. ": " .. msg)
    end
    return nil
  end
end

-- Saved DM Settings (cruelty) go out with the first hello; the backend's receipt fills the menu.
SaveFile.init(mod)
Settings.init(SaveFile, cfg, Bridge, Receipt, Death)
Bridge.setReceiptHandler(Receipt.handle)
Death.init(cfg, Bridge, State, Cons, Settings, Receipt.wrap)
Bridge.setSummaryHandler(Death.handle)
Loading.init(cfg, Bridge, State, Taunt, Sched)
Bridge.setReadyHandler(Loading.handle)

-- Connect as soon as the mod loads so the DM is warm by the time a run starts.
Bridge.init(cfg, SaveFile, "menu")

local runSeed
-- POST_NEW_LEVEL / POST_NEW_ROOM fire for a run's first room before POST_GAME_STARTED, while the
-- scheduler still holds the last run's floor counter: answering them would ask the DM about this
-- floor under a stale key, then again after the reset. Ignore them until the run has started;
-- game_started replays both. Frame 0 is the start of a fresh game (also a restart, which skips
-- PRE_GAME_EXIT); later floors and reseeds happen past it.
local started = false
local function live() return started and game:GetFrameCount() > 0 end

mod:AddCallback(ModCallbacks.MC_POST_GAME_STARTED, safe("game_started", function(_, continued)
  Settings.registerMenu()   -- in case Mod Config Menu loaded after this mod
  Settings.reload()
  local seed = game:GetSeeds():GetStartSeedString()
  runSeed = seed
  Bridge.init(cfg, SaveFile, seed)
  Bridge.setRun(seed, not continued)
  State.resetRun()
  Sched.resetRun()
  Death.startRun(seed)
  Loading.start()
  -- Pill colours the DM fixed earlier in this run must stay fixed after a continue.
  Cons.onLock = function(locked)
    local pills = {}                       -- string keys: colours are a sparse int table
    for c, e in pairs(locked) do pills[tostring(c)] = e end
    Settings.save({ seed = seed, pills = pills })
  end
  if continued then
    local saved = Settings.load()
    if saved.seed == seed then Cons.restore(saved.pills) end
    local stats = saved.run_stats            -- so the stats screen counts the whole run
    if type(stats) == "table" and stats.seed == seed then State.restoreRun(stats) end
  end
  -- POST_NEW_ROOM / POST_NEW_LEVEL already fired for the first room (and were ignored); replay them.
  started = true
  Sched.onNewRoom()
  Sched.onNewLevel()
end))

mod:AddCallback(ModCallbacks.MC_POST_NEW_ROOM, safe("new_room", function() if live() then Sched.onNewRoom() end end))
mod:AddCallback(ModCallbacks.MC_POST_NEW_LEVEL, safe("new_level", function() if live() then Sched.onNewLevel() end end))

local lastQueued
mod:AddCallback(ModCallbacks.MC_POST_UPDATE, safe("update", function()
  Apply.update()
  Sched.update()
  -- Item pickup detection (for the DM's memory).
  local p = Isaac.GetPlayer(0)
  local q = p.QueuedItem and p.QueuedItem.Item
  local id = q and q.ID or nil
  if id and id ~= lastQueued then
    local info = State.onItemTaken(id)
    if info then Bridge.event("item_taken", { name = info.name, quality = info.quality }) end
  end
  lastQueued = id
end))

mod:AddCallback(ModCallbacks.MC_POST_RENDER, safe("render", function()
  Bridge.update()           -- pumped from render so it keeps flowing while paused
  Loading.update()
  Loading.render()
  Taunt.render()
  Death.render()
  if cfg.SHOW_DEBUG_HUD and not Death.active() then
    Taunt.renderDebug(Bridge.status(), math.floor(Bridge.latencyAvg()), math.floor(Bridge.latencyP90()),
      Sched.debugCounts())
  end
end))

mod:AddCallback(ModCallbacks.MC_INPUT_ACTION, Loading.blockInput)

mod:AddCallback(ModCallbacks.MC_PRE_SPAWN_CLEAN_AWARD, safe("clean_award", function(_, rng, pos)
  return Sched.onCleanAward(rng, pos)
end))

mod:AddCallback(ModCallbacks.MC_PRE_GET_COLLECTIBLE, safe("get_collectible", function(_, pool, decrease, seed)
  return Sched.onGetCollectible(pool, decrease, seed)
end))

mod:AddCallback(ModCallbacks.MC_GET_CARD, safe("get_card", function(_, rng, card, playing, runes, onlyRunes)
  return Cons.onGetCard(rng, card, playing, runes, onlyRunes)
end))

mod:AddCallback(ModCallbacks.MC_GET_TRINKET, safe("get_trinket", function(_, trinket, rng)
  return Cons.onGetTrinket(trinket, rng)
end))

mod:AddCallback(ModCallbacks.MC_GET_PILL_EFFECT, safe("get_pill_effect", function(_, effect, color)
  return Cons.onGetPillEffect(effect, color)
end))

mod:AddCallback(ModCallbacks.MC_USE_PILL, safe("use_pill", function(_, effect)
  Cons.onUsePill(effect)
  State.onPillUsed()
end))

mod:AddCallback(ModCallbacks.MC_POST_CURSE_EVAL, safe("curse_eval", function(_, curses)
  return Sched.onCurseEval(curses)
end))

mod:AddCallback(ModCallbacks.MC_ENTITY_TAKE_DMG, safe("take_dmg", function(_, ent, amount, flags, source)
  local p = ent:ToPlayer()
  if not p then return nil end
  local src = source and source.Entity
  State.onPlayerHit(amount, State.hitSourceName(src))
  Bridge.event("damage", {
    amount = amount, room = game:GetLevel():GetCurrentRoomDesc().SafeGridIndex,
    source = src and (src.Type .. "." .. src.Variant) or "environment",
  })
  return nil
end), EntityType.ENTITY_PLAYER)

mod:AddCallback(ModCallbacks.MC_POST_GAME_END, safe("game_end", function(_, gameOver)
  Death.onGameEnd(gameOver)    -- tells the backend too, which answers with the DM's side
end))

-- Quitting to the menu mid-run: keep the run's totals so a continue picks them up.
mod:AddCallback(ModCallbacks.MC_PRE_GAME_EXIT, safe("game_exit", function(_, shouldSave)
  started = false
  if not (shouldSave and runSeed) then return end
  local stats = State.saveRun()
  stats.seed = runSeed
  Settings.save({ run_stats = stats })
end))
