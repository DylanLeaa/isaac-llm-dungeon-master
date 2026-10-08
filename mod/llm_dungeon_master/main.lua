-- LLM Dungeon Master
-- Every "random" event worth trolling about is decided by an LLM (Claude Haiku 5.5)
-- running in a local backend. Requires the --luadebug launch option (sockets / io).
local mod = RegisterMod("LLM Dungeon Master", 1)

local cfg    = include("dm.config")
local Bridge = include("dm.bridge")
local State  = include("dm.state")
local Taunt  = include("dm.taunt")
local Apply  = include("dm.apply")
local Sched  = include("dm.scheduler")
local Settings = include("dm.settings")
local Cons   = include("dm.consumables")

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

-- Saved DM Settings (cruelty) go out with the first hello.
Settings.init(mod, cfg, Bridge)

-- Connect as soon as the mod loads so the DM is warm by the time a run starts.
Bridge.init(cfg, "menu")

mod:AddCallback(ModCallbacks.MC_POST_GAME_STARTED, safe("game_started", function(_, continued)
  Settings.registerMenu()   -- in case Mod Config Menu loaded after this mod
  local seed = game:GetSeeds():GetStartSeedString()
  Bridge.init(cfg, seed)
  Bridge.setRun(seed, not continued)
  State.resetRun()
  Sched.resetRun()
  -- Pill colours the DM fixed earlier in this run must stay fixed after a continue.
  Cons.onLock = function(locked)
    local pills = {}                       -- string keys: colours are a sparse int table
    for c, e in pairs(locked) do pills[tostring(c)] = e end
    Settings.save({ seed = seed, pills = pills })
  end
  if continued then
    local saved = Settings.load()
    if saved.seed == seed then Cons.restore(saved.pills) end
  end
  -- POST_NEW_ROOM / POST_NEW_LEVEL already fired for the first room; replay them.
  Sched.onNewRoom()
  Sched.onNewLevel()
end))

mod:AddCallback(ModCallbacks.MC_POST_NEW_ROOM, safe("new_room", function() Sched.onNewRoom() end))
mod:AddCallback(ModCallbacks.MC_POST_NEW_LEVEL, safe("new_level", function() Sched.onNewLevel() end))

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
  Taunt.render()
  if cfg.SHOW_DEBUG_HUD then
    Taunt.renderDebug(Bridge.status(), math.floor(Bridge.latencyAvg()), math.floor(Bridge.latencyP90()),
      Sched.debugCounts())
  end
end))

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
end))

mod:AddCallback(ModCallbacks.MC_POST_CURSE_EVAL, safe("curse_eval", function(_, curses)
  return Sched.onCurseEval(curses)
end))

mod:AddCallback(ModCallbacks.MC_ENTITY_TAKE_DMG, safe("take_dmg", function(_, ent, amount, flags, source)
  local p = ent:ToPlayer()
  if not p then return nil end
  State.onPlayerHit(amount)
  local src = source and source.Entity
  Bridge.event("damage", {
    amount = amount, room = game:GetLevel():GetCurrentRoomDesc().SafeGridIndex,
    source = src and (src.Type .. "." .. src.Variant) or "environment",
  })
  return nil
end), EntityType.ENTITY_PLAYER)

mod:AddCallback(ModCallbacks.MC_POST_GAME_END, safe("game_end", function(_, gameOver)
  Bridge.event(gameOver and "death" or "victory", {})
end))
