-- LLM Dungeon Master
-- Every "random" event worth trolling about is decided by an LLM (Claude Haiku 5.5)
-- running in a local backend, reached through the game's log and this mod's save file.
--
-- Every roll the game makes goes through dm/rolls.lua to exactly one stream (dm/streams/*.lua), which
-- draws the DM's answer from its deck (dm/decks.lua). dm/causes.lua says why the game rolled.
local mod = RegisterMod("LLM Dungeon Master", 1)

local cfg    = include("dm.config")
local SaveFile = include("dm.savefile")
local Bridge = include("dm.bridge")
local State  = include("dm.state")
local Taunt  = include("dm.taunt")
local Apply  = include("dm.apply")
local Sched  = include("dm.scheduler")
local Settings = include("dm.settings")
local Death  = include("dm.deathscreen")
local Loading = include("dm.loading")
local EffectHud = include("dm.effecthud")
local Rolls  = include("dm.rolls")
local Causes = include("dm.causes")
local Decks  = include("dm.decks")

-- The streams, in the order their hooks run: a room is dressed before its drops are asked for, and
-- a planned tinted rock is the rock deck's before the drops deck can take it as a breakable.
local D = { cfg = cfg, Bridge = Bridge, State = State, Apply = Apply, Taunt = Taunt, Loading = Loading,
            Rolls = Rolls, Causes = Causes, Decks = Decks, Sched = Sched }
D.Layout   = include("dm.streams.layout")
D.Rocks    = include("dm.streams.rocks")
D.Drops    = include("dm.streams.drops")
D.Clear    = include("dm.streams.clear")
D.Items    = include("dm.streams.items")
D.Payouts  = include("dm.streams.payouts")
D.Cards    = include("dm.streams.cards")
D.Trinkets = include("dm.streams.trinkets")
D.Pills    = include("dm.streams.pills")
D.Curses   = include("dm.streams.curses")
local STREAMS = { D.Layout, D.Rocks, D.Drops, D.Clear, D.Items, D.Payouts, D.Cards, D.Trinkets, D.Pills, D.Curses }

Taunt.enabled = cfg.SHOW_TAUNTS
Apply.init(Taunt)
EffectHud.init(cfg, Apply)
Causes.init()
Rolls.init(cfg, Bridge, Apply, Causes)
Decks.init(cfg, Bridge, State, Loading, Taunt)
for _, s in ipairs(STREAMS) do s.init(D) end
Sched.init(D, STREAMS)
local Pills = D.Pills

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

SaveFile.init(mod)
Settings.init(SaveFile)
Death.init(cfg, Bridge, State, Pills, Settings)
Bridge.setSummaryHandler(Death.handle)
Loading.init(cfg, Bridge, State, Taunt, Sched)
Bridge.setReadyHandler(Loading.handle)
Bridge.setEffectHandler(EffectHud.push)
Bridge.setStaleHandler(Decks.markStale)

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
  Settings.reload()
  local seed = game:GetSeeds():GetStartSeedString()
  runSeed = seed
  Bridge.init(cfg, SaveFile, seed)
  Bridge.setRun(seed, not continued)
  State.resetRun()
  Sched.resetRun()
  Death.startRun(seed)
  Loading.start()
  EffectHud.clear()
  -- Pill colours the DM fixed earlier in this run must stay fixed after a continue.
  Pills.onLock = function(locked)
    local pills = {}                       -- string keys: colours are a sparse int table
    for c, e in pairs(locked) do pills[tostring(c)] = e end
    Settings.save({ seed = seed, pills = pills })
  end
  if continued then
    local saved = Settings.load()
    if saved.seed == seed then Pills.restore(saved.pills) end
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
  Loading.updateHolds()     -- after the decks, which may have started one
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
  EffectHud.render(Loading.active() or Death.active())
  Death.render()
  if cfg.SHOW_DEBUG_HUD and not Death.active() then
    Taunt.renderDebug(Bridge.status(), math.floor(Bridge.latencyAvg()), math.floor(Bridge.latencyP90()),
      Sched.debugCounts())
  end
end))

mod:AddCallback(ModCallbacks.MC_INPUT_ACTION, Loading.blockInput)
mod:AddCallback(ModCallbacks.MC_PRE_NPC_UPDATE, safe("hold_npc", Loading.holdNpc))
mod:AddCallback(ModCallbacks.MC_INPUT_ACTION, safe("block_pill", function(_, ent, hook, action)
  return Pills.blockPill(ent, hook, action)
end))

-- Every roll the game makes: spawns, kills, the clear award, pedestals, cards, trinkets, pills, curses.
Rolls.register(mod, safe)

mod:AddCallback(ModCallbacks.MC_USE_PILL, safe("use_pill", function(_, effect)
  Causes.notePlayerUse()
  Pills.onUsePill(effect)
  State.onPillUsed()
end))
for _, name in ipairs({ "MC_USE_CARD", "MC_USE_ITEM" }) do
  if ModCallbacks[name] then
    mod:AddCallback(ModCallbacks[name], safe(name:lower(), function() Causes.notePlayerUse() end))
  end
end

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
