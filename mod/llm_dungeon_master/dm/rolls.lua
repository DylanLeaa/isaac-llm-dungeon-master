-- The roll registry: every callback where the game makes a random choice comes through here, and
-- each roll goes to exactly one owner, a stream (dm/streams/*.lua) that draws the DM's answer from its deck.
--
--   game callback --> rolls.lua --> causes.lua --> decks.lua --> streams/<x>.lua --> apply.lua
--   (the roll)        one router     "why now"      draw / owe    what it means in the game
--
-- Two kinds of roll:
--   spawn rolls     MC_PRE_ENTITY_SPAWN of a pickup the game spawns on its own. causes.lua says why (a
--                   kill, a chest opening, a slot paying, a rock breaking...), and the first stream that
--                   claims that cause owns it: it cancels the game's spawn and pays its own entry instead.
--   identity rolls  what a pickup turns out to be: MC_GET_CARD, MC_GET_TRINKET, MC_PRE_GET_COLLECTIBLE,
--                   MC_GET_PILL_EFFECT. Also the room-clear award, the floor's curses and a kill itself.
-- The mod's own spawns (Apply.spawning) are never spawn rolls, but their identity rolls still go to the
-- streams (a "card" reward is the cards deck's next card). While a spawn that is being cancelled
-- initialises, its identity rolls return nil without drawing: no answer is spent on a pickup that will
-- never exist. That is what keeps one physical roll from being governed by two decisions.
--
-- Coverage: every roll is counted by hook, cause and owner, and a roll no stream took is counted
-- "uncontrolled". The counts go to the backend every ROLL_REPORT_MS (the `rolls` event), with LOG_SPAWNS
-- samples of what each spawn looked like.
--
-- ROLL AUDIT (phase 0): what the vanilla API does here has to be read off a real run with LOG_SPAWNS on
-- (the receipt's "rolls" entries carry the samples). Until then the mod assumes, and copes with, both
-- answers to each question:
--   * spawner / seed: kill drops, chest contents, slot payouts and rock/poop loot may or may not carry
--     SpawnerEntity. causes.lua prefers the spawner and falls back to what just happened nearby.
--   * InitSeed of the spawned pickup == the seed MC_PRE_ENTITY_SPAWN saw: matched by seed, and failing
--     that by the frame and variant.
--   * cancelling: replacing the spawn with an effect (CANCEL_IN_PRE_SPAWN) or removing the pickup in
--     MC_POST_PICKUP_INIT. The second is what the mod did before decks and is the default.
--   * an item pool rolled before the pedestal spawns (a chest choosing its item first): payouts.lua
--     suppresses those draws while a chest or beggar of that pool is paying out.
--   * room-entity IDs for rocks, tinted rocks and layout pickups in MC_PRE_ROOM_ENTITY_SPAWN: only
--     observed (counted) so far; layouts still change the room after it loads.
-- What the first real run (2026-10-20, 4 floors) showed:
--   * kill drops carry SpawnerEntity (the enemy); a boss's heart drop carries the boss; chest contents
--     and breakable loot have none (causes.lua's "what just happened nearby" fallback found them).
--   * every spawn had a seed.
--   * a layout pickup fires MC_PRE_ROOM_ENTITY_SPAWN and then MC_PRE_ENTITY_SPAWN on the same frame, with
--     no spawner and the same variant: matched here and counted once, as room_entity.
--   * a boss room's pedestal spawns as clean_award, variant 100: its item is the get_collectible roll.
local Rolls = {}

local game = Game()
local cfg, Bridge, Apply, Causes

local claimers = {}       -- ordered {name, fn(info) -> "cancel" | "keep" | nil}: who owns a spawn roll
local handlers = {}       -- hook -> {owner, fn}: who owns an identity roll
local pending = {}        -- spawns being cancelled, until their pickup initialises: {seed, frame, variant, ...}
local nexts = {}          -- one-shot claims on the next pickup of a variant this frame (an IOU's pickup)
local tally = {}          -- "hook|cause|owner" -> rolls since the last report
local samples = {}        -- LOG_SPAWNS: what recent spawns looked like
local sampled = 0
local gridSeen = {}       -- room-entity grid types already sampled (one sample each: they're all alike)
local roomEnts = {}       -- pickups the room's layout file is placing: {variant, frame}, until they spawn
local lastReport = -1e9
local SAMPLE_CAP = 300    -- per run
local POOF = 15           -- EffectVariant.POOF01

Rolls.current = nil       -- the spawn whose pickup is initialising right now (PRE_ENTITY_SPAWN .. POST_PICKUP_INIT)

function Rolls.init(config, bridge, apply, causes)
  cfg, Bridge, Apply, Causes = config, bridge, apply, causes
end

function Rolls.reset()
  pending, nexts, tally, samples, sampled, Rolls.current = {}, {}, {}, {}, 0, nil
  gridSeen, roomEnts = {}, {}
end

-- ----------------------------------------------------------------- owners
--- A stream that may own spawn rolls: fn(info) returns "cancel" (the game's spawn is cancelled and the
--- stream pays its own), "keep" (the stream owns it and lets it stand) or nil (not its roll).
function Rolls.claimSpawns(name, fn) claimers[#claimers + 1] = { name = name, fn = fn } end

--- The one owner of an identity roll: fn(...) returns (what the callback returns, status), where status
--- is "dm" (decided by the DM, now or once its answer lands), "vanilla" (left to the game: counted
--- uncontrolled) or nil (not a roll: our own candidate peek, a cancelled spawn's identity).
function Rolls.on(hook, owner, fn) handlers[hook] = { owner = owner, fn = fn } end

--- The next pickup of `variant` to initialise this frame goes to fn(pickup) (a pickup the game made while
--- its deck was empty: the deck's IOU swaps it once the answer lands).
function Rolls.claimNext(variant, fn)
  nexts[#nexts + 1] = { variant = variant, frame = game:GetFrameCount(), fn = fn }
end

--- Inside the initialisation of a spawn that is being cancelled: identity rolls must not draw.
function Rolls.suppressed()
  local c = Rolls.current
  return c ~= nil and c.cancel and c.frame == game:GetFrameCount()
end

--- The cause of the spawn initialising right now, if any.
function Rolls.cause()
  local c = Rolls.current
  return c and c.frame == game:GetFrameCount() and c.cause or nil
end

function Rolls.count(hook, cause, owner, n)
  local k = hook .. "|" .. tostring(cause or "-") .. "|" .. (owner or "uncontrolled")
  tally[k] = (tally[k] or 0) + (n or 1)
end

local function sample(row)
  if not cfg.LOG_SPAWNS or sampled >= SAMPLE_CAP then return end
  sampled = sampled + 1
  samples[#samples + 1] = row
end

local function entName(e)
  return e and (tostring(e.Type) .. "." .. tostring(e.Variant)) or "none"
end

-- ----------------------------------------------------------------- the hooks
local function preSpawn(etype, variant, subtype, pos, _vel, spawner, seed)
  if etype ~= EntityType.ENTITY_PICKUP then return nil end
  local f = game:GetFrameCount()
  if Apply.spawning then
    Rolls.current = { seed = seed, frame = f, mod = true, variant = variant }
    return nil
  end
  -- A pickup the room's layout file placed (MC_PRE_ROOM_ENTITY_SPAWN just counted it): not a roll of its
  -- own. The layout owns it, and what a random one resolves to (MC_POST_PICKUP_SELECTION).
  if not spawner then
    for i, r in ipairs(roomEnts) do
      if f - r.frame <= 1 and (r.variant == variant or r.variant == 0 or variant == 0) then
        table.remove(roomEnts, i)
        Rolls.current = { seed = seed, frame = f, variant = variant, owner = r.owner, cause = "room_entity" }
        return nil
      end
    end
  end
  local info = { variant = variant, subtype = subtype, pos = pos, spawner = spawner, seed = seed, frame = f }
  info.cause, info.source = Causes.of(info)
  local owner, verdict
  for _, c in ipairs(claimers) do
    verdict = c.fn(info)
    if verdict then owner = c.name; break end
  end
  -- A pedestal no stream claimed (a boss's, a room's): where it stands isn't random, and its item is the
  -- MC_PRE_GET_COLLECTIBLE roll, counted there.
  if owner or variant ~= PickupVariant.PICKUP_COLLECTIBLE then Rolls.count("spawn", info.cause, owner) end
  sample({ hook = "spawn", variant = variant, subtype = subtype, cause = info.cause, owner = owner or "uncontrolled",
           spawner = entName(spawner), seed = seed ~= nil and seed ~= 0, frame = f })
  local rec = { seed = seed, frame = f, variant = variant, cancel = verdict == "cancel", owner = owner, cause = info.cause }
  Rolls.current = rec
  if not rec.cancel then return nil end
  if cfg.CANCEL_IN_PRE_SPAWN then
    Rolls.current = nil                            -- nothing will initialise
    return { EntityType.ENTITY_EFFECT, (EffectVariant and EffectVariant.POOF01) or POOF, 0, seed }
  end
  pending[#pending + 1] = rec
  return nil
end

local function cancelled(pickup, f)
  for i = #pending, 1, -1 do
    local p = pending[i]
    if f - p.frame > 2 then
      table.remove(pending, i)                     -- its pickup never came
    elseif p.seed == pickup.InitSeed then
      return table.remove(pending, i)
    end
  end
  -- The seed didn't match: the latest one this frame of that variant (0 = "any pickup", resolved at init).
  for i = #pending, 1, -1 do
    local p = pending[i]
    if p.frame == f and (p.variant == pickup.Variant or p.variant == 0) then return table.remove(pending, i) end
  end
  return nil
end

--- MC_POST_PICKUP_INIT. True if the pickup was a cancelled spawn (now removed).
local function pickupInit(pickup)
  local f = game:GetFrameCount()
  local cur = Rolls.current
  if cur and (cur.seed == pickup.InitSeed or cur.frame ~= f) then Rolls.current = nil end
  if #pending > 0 then
    local rec = cancelled(pickup, f)
    if rec then
      pickup:Remove()
      return true
    end
  end
  for i, c in ipairs(nexts) do
    if c.frame ~= f then
      nexts[i] = false
    elseif c.variant == pickup.Variant then
      table.remove(nexts, i)
      c.fn(pickup)
      break
    end
  end
  for i = #nexts, 1, -1 do if not nexts[i] then table.remove(nexts, i) end end
  return false
end

local function route(hook, ...)
  local h = handlers[hook]
  local cause = Rolls.cause()
  if not h then
    Rolls.count(hook, cause, nil)
    return nil
  end
  local ret, status = h.fn(...)
  if status then Rolls.count(hook, cause, status == "dm" and h.owner or nil) end
  return ret
end

--- Every roll callback, registered once. `safe` wraps each in the mod's error guard.
function Rolls.register(mod, safe)
  local MC = ModCallbacks
  local function add(id, name, fn, arg)
    if not id then return end                      -- not on this version of the API: those rolls stay vanilla
    if arg then mod:AddCallback(id, safe(name, fn), arg) else mod:AddCallback(id, safe(name, fn)) end
  end
  add(MC.MC_PRE_ENTITY_SPAWN, "pre_entity_spawn", function(_, ...) return preSpawn(...) end)
  add(MC.MC_POST_PICKUP_INIT, "pickup_init", function(_, pickup) pickupInit(pickup) end)
  add(MC.MC_POST_PICKUP_SELECTION, "pickup_selection", function(_, pickup, variant, subtype)
    local cur = Rolls.current
    if Apply.spawning or (cur and cur.cancel and cur.frame == game:GetFrameCount()) then return nil end
    -- What a random pickup resolves to: owned by whoever owns its spawn, else left to the game.
    local owner = cur and cur.frame == game:GetFrameCount() and cur.owner or nil
    Rolls.count("pickup_selection", (PickupVariant and Causes.variantName(variant)) or variant, owner)
    return nil
  end)
  add(MC.MC_PRE_ROOM_ENTITY_SPAWN, "room_entity_spawn", function(_, etype, variant, subtype, grid, seed)
    if etype == EntityType.ENTITY_PICKUP then
      local owner = cfg.CONTROL_ROOM_LAYOUT and "layout" or nil
      Rolls.count("room_entity", Causes.variantName(variant), owner)
      local f = game:GetFrameCount()
      for i = #roomEnts, 1, -1 do if f - roomEnts[i].frame > 1 then table.remove(roomEnts, i) end end
      roomEnts[#roomEnts + 1] = { variant = variant, frame = f, owner = owner }
      sample({ hook = "room_entity", type = etype, variant = variant, subtype = subtype, grid = grid, frame = f })
    elseif etype and etype >= 1000 and not gridSeen[etype] then
      gridSeen[etype] = true
      sample({ hook = "room_entity", type = etype, variant = variant, subtype = subtype, grid = grid })
    end
    return nil
  end)
  add(MC.MC_POST_NPC_DEATH, "npc_death", function(_, npc) route("npc_death", npc) end)
  add(MC.MC_PRE_SPAWN_CLEAN_AWARD, "clean_award", function(_, rng, pos) return route("clean_award", rng, pos) end)
  add(MC.MC_PRE_GET_COLLECTIBLE, "get_collectible", function(_, pool, decrease, seed)
    return route("get_collectible", pool, decrease, seed)
  end)
  add(MC.MC_GET_CARD, "get_card", function(_, rng, card, playing, runes, onlyRunes)
    return route("get_card", rng, card, playing, runes, onlyRunes)
  end)
  add(MC.MC_GET_TRINKET, "get_trinket", function(_, trinket, rng) return route("get_trinket", trinket, rng) end)
  add(MC.MC_GET_PILL_EFFECT, "get_pill_effect", function(_, effect, color) return route("get_pill_effect", effect, color) end)
  add(MC.MC_POST_CURSE_EVAL, "curse_eval", function(_, curses) return route("curse_eval", curses) end)
end

--- Every frame: send the roll counts (and spawn samples) every ROLL_REPORT_MS.
function Rolls.update()
  local t = Isaac.GetTime()
  if t - lastReport < (cfg.ROLL_REPORT_MS or 3000) or not next(tally) or not Bridge.ready() then return end
  lastReport = t
  local counts = {}
  for k, n in pairs(tally) do
    local hook, cause, owner = k:match("^(.-)|(.-)|(.*)$")
    counts[#counts + 1] = { hook = hook, cause = cause, owner = owner, n = n }
  end
  Bridge.event("rolls", { counts = counts, samples = #samples > 0 and samples or nil })
  tally, samples = {}, {}
end

-- Test hooks: the fake game drives these directly.
Rolls._preSpawn, Rolls._pickupInit = preSpawn, pickupInit

return Rolls
