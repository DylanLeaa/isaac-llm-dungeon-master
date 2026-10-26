-- What just happened: the one tracker every stream asks "why did the game roll this?"
--
-- It sees the room's kills (MC_POST_NPC_DEATH), its rocks, poops and urns breaking (a grid scan), its
-- machines and chests (a slot used: the player's coins/keys/bombs drop beside it; a chest opening: it
-- changes), the clean award and the player's own card/pill/item uses. Streams subscribe to the events
-- they own (Causes.on: "slot_use", "chest_open", "rock_break", "breakable", "machine_seen"), and every
-- spawn roll asks Causes.of for its cause: the real spawner first, and only without one, what just
-- happened beside it.
local Causes = {}

local game = Game()

local deaths = {}       -- recent kills: {pos, frame, ptr, seed, boss, breakable}; streams add their own fields
local breaks = {}       -- recent breaks: {pos, frame, grid, kind}
local watched = {}      -- grid index -> {type, kind, pos, broken}: what can break in this room
local machines = {}     -- InitSeed -> {seed, class, variant, label, pool, hearts, bag, pos, room, uses, ...}
local listeners = {}
local last              -- the player's coins/keys/bombs/hearts last frame
local lastScan, lastWatch = -1e9, -1e9
local awardFrame, useFrame

Causes.SLOTS, Causes.CHESTS = {}, {}   -- variant -> {label, pool, hearts | bag}
local WATCH = {}        -- grid type -> "rock_break" | "breakable"
local VARIANT_NAMES = {}

local DEATH_FRAMES, DEATH_RADIUS = 2, 140
local BREAK_FRAMES, BREAK_RADIUS = 6, 90
local WINDOW = 240      -- frames after a use / an opening in which a spawn beside the machine is its payout
local NEAR_SLOT, NEAR_CHEST, NEAR_PLAYER = 130, 90, 90
Causes.WINDOW = WINDOW

local function frame() return game:GetFrameCount() end
local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end
local function hashOf(e) return GetPtrHash and GetPtrHash(e) or e end
local function copy(v) return Vector(v.X, v.Y) end

function Causes.init()
  local function slot(variant, label, poolName, hearts)
    Causes.SLOTS[variant] = { label = label, pool = poolName and ItemPoolType[poolName] or nil, hearts = hearts }
  end
  -- Slot variants have no enum in the vanilla API; these are the game's own numbers.
  slot(1, "slot_machine")
  slot(4, "beggar", "POOL_BEGGAR")
  slot(5, "demon_beggar", "POOL_DEMON_BEGGAR", true)
  slot(6, "shell_game", "POOL_SHELL_GAME")
  slot(7, "key_master", "POOL_KEY_MASTER")
  slot(9, "bomb_bum", "POOL_BOMB_BUM")
  slot(13, "battery_bum", "POOL_BATTERY_BUM")
  slot(15, "hell_game", nil, true)              -- a shell game paid for with hearts
  slot(16, "crane_game", "POOL_CRANE_GAME")
  slot(18, "rotten_beggar", "POOL_ROTTEN_BEGGAR")
  local function chest(name, label, poolName, bag)
    local v = PickupVariant[name]
    if v then Causes.CHESTS[v] = { label = label, pool = poolName and ItemPoolType[poolName] or nil, bag = bag } end
  end
  chest("PICKUP_LOCKEDCHEST", "golden_chest", "POOL_GOLDEN_CHEST")
  chest("PICKUP_REDCHEST", "red_chest", "POOL_RED_CHEST")
  chest("PICKUP_WOODENCHEST", "wooden_chest", "POOL_WOODEN_CHEST")
  chest("PICKUP_OLDCHEST", "old_chest", "POOL_OLD_CHEST")
  chest("PICKUP_MOMSCHEST", "moms_chest", "POOL_MOMS_CHEST")
  -- No item pool. Eternal and mega chests can close and be reopened: only the first opening counts.
  for name, label in pairs({ PICKUP_CHEST = "chest", PICKUP_BOMBCHEST = "bomb_chest", PICKUP_SPIKEDCHEST = "spiked_chest",
                             PICKUP_ETERNALCHEST = "eternal_chest", PICKUP_MIMICCHEST = "mimic_chest",
                             PICKUP_MEGACHEST = "mega_chest", PICKUP_HAUNTEDCHEST = "haunted_chest" }) do
    chest(name, label)
  end
  chest("PICKUP_GRAB_BAG", "grab_bag", nil, true)
  local G = GridEntityType or {}
  for name, kind in pairs({ GRID_ROCKT = "rock_break", GRID_ROCK_SS = "rock_break", GRID_POOP = "breakable",
                            GRID_ROCK_ALT = "breakable" }) do
    if G[name] then WATCH[G[name]] = kind end
  end
  for k, v in pairs(PickupVariant or {}) do VARIANT_NAMES[v] = k:gsub("^PICKUP_", ""):lower() end
end

function Causes.variantName(v) return VARIANT_NAMES[v] or tostring(v) end

--- Subscribe to an event: fn(record). Listeners run in the order they subscribed; a stream that takes
--- the event marks the record (record.owner) so the ones after it can leave it.
function Causes.on(event, fn)
  listeners[event] = listeners[event] or {}
  table.insert(listeners[event], fn)
end

local function emit(event, rec)
  for _, fn in ipairs(listeners[event] or {}) do fn(rec) end
end

function Causes.reset()
  deaths, breaks, watched, machines = {}, {}, {}, {}
  last, lastScan, lastWatch, awardFrame, useFrame = nil, -1e9, -1e9, nil, nil
end

function Causes.onNewRoom()
  deaths, breaks, watched = {}, {}, {}
  lastWatch, lastScan = -1e9, -1e9
end

-- ----------------------------------------------------------------- kills
local function deathOf(npc)
  local h = hashOf(npc)
  for _, d in ipairs(deaths) do
    if d.ptr == h or (d.seed and d.seed ~= 0 and d.seed == npc.InitSeed and d.type == npc.Type) then return d end
  end
end

--- A kill (or the record of it, if already noted): MC_POST_NPC_DEATH, or a spawn whose spawner is dying.
function Causes.noteDeath(npc)
  local d = deathOf(npc)
  if d then return d end
  local boss = false
  pcall(function() boss = npc:IsBoss() end)
  d = { pos = copy(npc.Position), frame = frame(), ptr = hashOf(npc), seed = npc.InitSeed, type = npc.Type,
        variant = npc.Variant, boss = boss, breakable = npc.Type == EntityType.ENTITY_FIREPLACE }
  deaths[#deaths + 1] = d
  if #deaths > 24 then table.remove(deaths, 1) end
  return d
end

local function deathCause(d)
  if d.breakable then return "breakable" end
  return d.boss and "boss_death" or "enemy_death"
end

local function nearDeath(pos, f)
  for i = #deaths, 1, -1 do
    local d = deaths[i]
    if math.abs(f - d.frame) <= DEATH_FRAMES and d.pos:Distance(pos) <= DEATH_RADIUS then return d end
  end
end

-- ----------------------------------------------------------------- breaks
local function isBroken(g, t)
  if GridEntityType and t == GridEntityType.GRID_POOP then return (g.State or 0) >= 1000 end
  return g.State == 2
end

local function rewatch()
  local room = game:GetRoom()
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g then
      local t = g:GetType()
      local w = watched[i]
      if WATCH[t] and not (w and w.type == t) then
        -- First seen broken: broken before the player got here, not a roll.
        watched[i] = { type = t, kind = WATCH[t], pos = copy(g.Position), broken = isBroken(g, t) }
      end
    end
  end
end

local function checkBreaks()
  local room, f = game:GetRoom(), frame()
  for i, w in pairs(watched) do
    local g = room:GetGridEntity(i)
    if not g or g:GetType() ~= w.type then
      watched[i] = nil                             -- swapped or removed (a layout change), not broken
    else
      local broken = isBroken(g, w.type)
      if broken and not w.broken then
        local b = { pos = w.pos, frame = f, grid = i, gridType = w.type, kind = w.kind }
        breaks[#breaks + 1] = b
        if #breaks > 24 then table.remove(breaks, 1) end
        emit(w.kind, b)
      end
      w.broken = broken
    end
  end
end

local function nearBreak(pos, f)
  for i = #breaks, 1, -1 do
    local b = breaks[i]
    if f - b.frame <= BREAK_FRAMES and b.pos:Distance(pos) <= BREAK_RADIUS then return b end
  end
end

-- ----------------------------------------------------------------- machines and chests
local function scanMachines()
  local idx, f = curIdx(), frame()
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    local info, class
    if e.Type == EntityType.ENTITY_SLOT then info, class = Causes.SLOTS[e.Variant], "slot"
    elseif e.Type == EntityType.ENTITY_PICKUP and e.SubType ~= 0 then info, class = Causes.CHESTS[e.Variant], "chest" end
    if info then
      local m = machines[e.InitSeed]
      local new = not m
      if new then
        m = { seed = e.InitSeed, class = class, variant = e.Variant, label = info.label, pool = info.pool,
              hearts = info.hearts, bag = info.bag, uses = 0, lastUse = -1e9, opened = false }
        machines[e.InitSeed] = m
      end
      m.ent, m.pos, m.room, m.seen = e, copy(e.Position), idx, f
      if new then emit("machine_seen", m) end
    end
  end
end

function Causes.here(m)
  return m.room == curIdx() and m.seen ~= nil and frame() - m.seen <= 40
end

function Causes.machine(seed) return machines[seed] end

function Causes.machinesHere()
  local out = {}
  for _, m in pairs(machines) do if Causes.here(m) then out[#out + 1] = m end end
  return out
end

local function chestOpening(m)
  if m.class ~= "chest" or m.opened or not m.ent then return false end
  if m.bag then   -- a sack is taken, not opened: it plays its pickup animation or is gone, with the player beside it
    if Isaac.GetPlayer(0).Position:Distance(m.pos) > NEAR_PLAYER then return false end
    if not m.ent:Exists() then return true end
    local ok, playing = pcall(function() return m.ent:GetSprite():IsPlaying("Collect") end)
    return ok and playing or false
  end
  if not m.ent:Exists() then return false end
  if m.ent.SubType == 0 then return true end
  local ok, playing = pcall(function() return m.ent:GetSprite():IsPlaying("Open") end)
  return ok and playing or false
end

local function checkOpening(m)
  if chestOpening(m) then
    m.opened, m.openedAt, m.uses = true, frame(), 1
    emit("chest_open", m)
  end
end

--- The machine an entity is (a slot or a chest/sack), if it is one.
function Causes.machineOf(ent)
  if not ent then return nil end
  local m = machines[ent.InitSeed]
  if not m then
    scanMachines()
    m = machines[ent.InitSeed]
  end
  return m
end

-- The machine a spawn at pos is the payout of: the nearest with a window open (the latest on a tie).
-- Two chests side by side, opened one after the other, each keep their own contents.
local function nearMachine(pos, f)
  local best, bestD, bestAt
  for _, m in pairs(machines) do
    if Causes.here(m) and m.pos then
      local d = m.pos:Distance(pos)
      if m.class == "chest" and not m.opened and d <= NEAR_CHEST then checkOpening(m) end
      local at = m.class == "chest" and (m.opened and m.openedAt) or (m.class == "slot" and m.lastUse) or nil
      local radius = m.class == "chest" and NEAR_CHEST or NEAR_SLOT
      if at and f - at <= WINDOW and d <= radius
          and (not best or d < bestD or (d == bestD and at > bestAt)) then
        best, bestD, bestAt = m, d, at
      end
    end
  end
  return best
end

--- The machine whose payout window covers this frame, for an item pool (its pedestal is the schedule's).
function Causes.payingOut(pool)
  local f = frame()
  for _, m in pairs(machines) do
    if m.pool == pool and Causes.here(m) then
      local at = m.class == "chest" and m.openedAt or m.lastUse
      if at and f - at <= WINDOW then return m end
    end
  end
end

-- ----------------------------------------------------------------- the game's own moments
-- Walking back into a room, the game spawns again the pickups left lying in it (with no spawner, as the
-- room loads): what was already rolled the first time, not a roll of its own.
local function restoring()
  local room = game:GetRoom()
  local ok, first, age = pcall(function() return room:IsFirstVisit(), room:GetFrameCount() end)
  return ok and first == false and type(age) == "number" and age <= 1
end

function Causes.noteAward() awardFrame = frame() end
function Causes.notePlayerUse() useFrame = frame() end

--- Why the game is spawning this pickup: (cause, source). info = {pos, spawner, variant, seed}.
---   enemy_death / boss_death / breakable  source = the death record (a kill, a fire put out)
---   rock_break / breakable                 source = the break record (a tinted rock, a poop, an urn)
---   chest_open / slot_payout               source = the machine
---   room_restore                           a pickup left in a room, spawned again as the player walks back in
---   clean_award, player, player_use, unknown
function Causes.of(info)
  local f, pos, sp = frame(), info.pos, info.spawner
  if not sp and restoring() then return "room_restore", nil end
  if sp then
    local t = sp.Type
    if t == EntityType.ENTITY_PLAYER or t == EntityType.ENTITY_FAMILIAR then return "player", nil end
    if t == EntityType.ENTITY_SLOT then return "slot_payout", Causes.machineOf(sp) end
    if t == EntityType.ENTITY_PICKUP then
      local m = Causes.machineOf(sp)
      if m and m.class == "chest" and not m.opened then checkOpening(m) end
      if m then return m.class == "chest" and "chest_open" or "slot_payout", m end
    end
    local npc = sp.ToNPC and sp:ToNPC()
    if npc then
      local dead = (npc.HitPoints or 0) <= 0
      pcall(function() dead = dead or npc:IsDead() end)
      if dead or deathOf(npc) then
        local d = Causes.noteDeath(npc)
        return deathCause(d), d
      end
      return "enemy_action", nil                   -- alive and spawning something (a Greed's coins)
    end
  end
  if pos then
    local m = nearMachine(pos, f)
    if m then return m.class == "chest" and "chest_open" or "slot_payout", m end
    local d = nearDeath(pos, f)
    if d then return deathCause(d), d end
    rewatch()                                      -- a break this very frame: its loot comes before our update
    checkBreaks()
    local b = nearBreak(pos, f)
    if b then return b.kind, b end
  end
  if awardFrame == f then return "clean_award", nil end
  if useFrame and f - useFrame <= 2 then return "player_use", nil end
  return "unknown", nil
end

-- ----------------------------------------------------------------- every frame
local function resources()
  local p = Isaac.GetPlayer(0)
  return { c = p:GetNumCoins(), k = p:GetNumKeys(), b = p:GetNumBombs(), h = p:GetHearts() }
end

function Causes.update()
  local f = frame()
  if f - lastScan >= 15 then
    lastScan = f
    scanMachines()
  end
  if f - lastWatch >= 30 then
    lastWatch = f
    rewatch()
  end
  checkBreaks()
  -- A use: the player paid beside a machine.
  local snap = resources()
  local spent, hurt = false, false
  if last then
    spent = snap.c < last.c or snap.k < last.k or snap.b < last.b
    hurt = snap.h < last.h
  end
  last = snap
  local player = Isaac.GetPlayer(0)
  for _, m in pairs(machines) do
    if Causes.here(m) then
      if m.class == "slot" and (spent or (hurt and m.hearts)) and f - m.lastUse > 8
          and player.Position:Distance(m.pos) <= NEAR_PLAYER then
        m.lastUse, m.uses = f, m.uses + 1
        emit("slot_use", m)
      elseif m.class == "chest" and not m.opened then
        checkOpening(m)
      end
    end
  end
end

return Causes
