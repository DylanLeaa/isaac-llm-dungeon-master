-- The prefetch scheduler: decides WHEN to ask the Dungeon Master each question.
--
-- Principle: ask as late as possible (freshest context) but early enough that the
-- answer lands before the game needs it. Concretely:
--
--   room_layout  asked the moment the player enters a room that borders an unvisited
--                room (the soonest they could walk in is ~1s away). Re-asked with a fresh
--                snapshot if their state changes materially while they linger.
--   room_clear   asked when the estimated time-to-clear (remaining enemy HP / recent DPS)
--                drops under LEAD_FACTOR * p90 latency + LEAD_PAD_MS. Re-asked if they get
--                hit or pick something up afterwards and there's still time.
--   item_choice  asked per item pool on floor entry, and again when the player stands next
--                to a treasure/shop/boss/... room, so the ranking reflects their newest build.
--   floor_plan   asked on entering the boss room; re-asked once the boss dies.
--
-- If an answer isn't there when the game needs it, vanilla RNG decides (and nobody waits).
local Sched = {}

local game = Game()
local cfg, Bridge, State, Apply, Taunt

local floorNo = 0
local levelSig
local room = nil         -- per-room tracker
local itemQueues = {}    -- pool -> {resultId, ids}
local floorPlanUsed      -- last floor plan applied (re-used if we reseed)
local reseeds = 0
Sched.peeking = false

local POOL_FOR_ROOM -- RoomType -> ItemPoolType (filled in init)
local KEY_ROOMS = {}

-- ----------------------------------------------------------------- utilities
local function fkey(prefix, rest) return prefix .. ":" .. floorNo .. ":" .. tostring(rest) end
local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end

local function alive()
  local hp, n = 0, 0
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    if e:IsActiveEnemy(false) and e:IsVulnerableEnemy() and not e:HasEntityFlags(EntityFlag.FLAG_FRIENDLY) then
      hp = hp + math.max(0, e.HitPoints)
      n = n + 1
    end
  end
  return hp, n
end

local function leadMs() return cfg.LEAD_FACTOR * Bridge.latencyP90() + cfg.LEAD_PAD_MS end

local function eligibleLayoutRoom(t) return t == RoomType.ROOM_DEFAULT end
local function eligibleClearRoom(t) return t == RoomType.ROOM_DEFAULT end

-- ------------------------------------------------------------------ requests
local function requestLayout(desc, reason)
  local key = fkey("layout", desc.SafeGridIndex)
  local level = game:GetLevel()
  Bridge.request("room_layout", key, State.snapshot(), {
    reason = reason,
    room_type = State.roomTypeName(desc.Data.Type),
    room_shape = desc.Data.Shape,
    room_difficulty = desc.Data.Difficulty,
    distance_from_start = State.gridDistance(level:GetStartingRoomIndex(), desc.SafeGridIndex),
    first_room_of_floor = desc.SafeGridIndex == level:GetStartingRoomIndex(),
    player_is_next_door = true,
  }, 10000)
  return key
end

local function requestClear(reason, etaMs)
  local key = fkey("clear", room.idx)
  local hp, n = alive()
  Bridge.request("room_clear", key, State.snapshot(), {
    reason = reason, room_type = State.roomTypeName(room.type),
    enemies_left = n, enemy_hp_left = math.floor(hp),
    est_ms_until_clear = math.floor(math.min(etaMs or 0, 60000)),
    seconds_in_room = math.floor((Isaac.GetTime() - room.enteredAt) / 100) / 10,
  }, math.floor(math.max(1500, math.min(etaMs or 4000, 10000)) + 2000))
  room.clearReqAt = Isaac.GetTime()
  room.clearReqHash = State.materialHash()
  room.clearSends = room.clearSends + 1
end

local function peekCandidates(pool)
  local itemPool = game:GetItemPool()
  local player = Isaac.GetPlayer(0)
  local seen, out = {}, {}
  local seed = game:GetSeeds():GetStartSeed() + floorNo * 7919 + pool * 104729
  Sched.peeking = true
  for i = 1, cfg.ITEM_CANDIDATES * 3 do
    if #out >= cfg.ITEM_CANDIDATES then break end
    local ok, id = pcall(itemPool.GetCollectible, itemPool, pool, false, seed + i * 31337)
    if ok and id and id > 0 and not seen[id] and not player:HasCollectible(id, true) then
      seen[id] = true
      local info = State.itemInfo(id)
      if info then out[#out + 1] = info end
    end
  end
  Sched.peeking = false
  return out
end

local function requestItems(pool, roomTypeName, reason)
  local cands = peekCandidates(pool)
  if #cands == 0 then return end
  Bridge.request("item_choice", fkey("items", pool), State.snapshot(), {
    reason = reason, pool = pool, room_type = roomTypeName, candidates = cands,
  }, 10000)
end

local function requestFloorPlan(reason)
  local level = game:GetLevel()
  Bridge.request("floor_plan", fkey("floor", "next"), State.snapshot(), {
    reason = reason, current_stage = level:GetStage(), current_stage_type = level:GetStageType(),
    allowed_layout_rerolls = cfg.ALLOW_LAYOUT_RESEED and cfg.MAX_RESEEDS or 0,
  }, 12000)
end

-- --------------------------------------------------------- room transitions
local function neighbours()
  local level, r = game:GetLevel(), game:GetRoom()
  local out, seen = {}, {}
  for slot = 0, 7 do
    local door = r:GetDoor(slot)
    if door and door.TargetRoomIndex >= 0 then
      local d = level:GetRoomByIdx(door.TargetRoomIndex)
      if d and d.Data and not seen[d.SafeGridIndex] then
        seen[d.SafeGridIndex] = true
        out[#out + 1] = d
      end
    end
  end
  return out
end

local function prefetchNeighbours(reason)
  for _, d in ipairs(neighbours()) do
    if d.VisitedCount == 0 then
      if cfg.CONTROL_ROOM_LAYOUT and eligibleLayoutRoom(d.Data.Type) then
        local key = requestLayout(d, reason)
        room.layoutKeys[key] = State.materialHash()
      end
      local pool = POOL_FOR_ROOM[d.Data.Type]
      if cfg.CONTROL_ITEMS and pool then
        requestItems(pool, State.roomTypeName(d.Data.Type), "player is next to this room")
      end
    end
  end
end

local function checkLevelChange()
  local level = game:GetLevel()
  local sig = level:GetStage() .. "/" .. level:GetStageType() .. "/" ..
              game:GetSeeds():GetStageSeed(level:GetStage())
  if sig == levelSig then return false end
  levelSig = sig
  floorNo = floorNo + 1
  itemQueues = {}
  Bridge.forget("layout:" .. (floorNo - 1))
  Bridge.forget("clear:" .. (floorNo - 1))
  Bridge.forget("items:" .. (floorNo - 1))
  State.onNewFloor()
  return true
end

function Sched.onNewRoom()
  if game:IsGreedMode() then return end
  checkLevelChange()
  local r, desc = game:GetRoom(), game:GetLevel():GetCurrentRoomDesc()
  State.onRoomEnter()
  room = {
    idx = desc.SafeGridIndex, type = r:GetType(), enteredAt = Isaac.GetTime(),
    enteredFrame = game:GetFrameCount(), firstVisit = r:IsFirstVisit(),
    layoutApplied = false, layoutKeys = {}, wasClear = r:IsClear(),
    hpSamples = {}, dps = 0, startHp = nil, clearSends = 0, clearReqAt = nil,
    lastProbe = 0, lastNeighbourRefresh = Isaac.GetTime(),
  }

  -- 1. Dress this room if the DM already decided how.
  if room.firstVisit and cfg.CONTROL_ROOM_LAYOUT then
    local res = Bridge.take(fkey("layout", room.idx))
    if res then
      Apply.roomLayout(res.decision)
      room.layoutApplied = true
    end
  end

  -- 2. Ask about every unvisited room the player could walk into next.
  if Bridge.ready() then
    room.prefetched = true
    prefetchNeighbours("player entered an adjacent room")
  end

  -- 3. Boss room: plan the next floor while they fight.
  if cfg.CONTROL_FLOOR and room.type == RoomType.ROOM_BOSS then
    requestFloorPlan("player entered the boss room")
    local pool = POOL_FOR_ROOM[RoomType.ROOM_BOSS]
    if cfg.CONTROL_ITEMS and pool then requestItems(pool, "boss", "boss item, fight starting") end
  end
end

local floorItemsRequestedFor = -1
local function requestFloorItems()
  if not cfg.CONTROL_ITEMS or floorItemsRequestedFor == floorNo or not Bridge.ready() then return end
  floorItemsRequestedFor = floorNo
  local level, done = game:GetLevel(), {}
  local rooms = level:GetRooms()
  for i = 0, rooms.Size - 1 do
    local d = rooms:Get(i)
    local pool = d and d.Data and POOL_FOR_ROOM[d.Data.Type]
    if pool and not done[pool] then
      done[pool] = true
      requestItems(pool, State.roomTypeName(d.Data.Type), "floor start")
    end
  end
end

function Sched.onNewLevel()
  if game:IsGreedMode() then return end
  checkLevelChange()
  Bridge.event("new_level", { stage = game:GetLevel():GetStage(), name = game:GetLevel():GetName() })
  -- Base item rankings for the pools this floor actually has.
  requestFloorItems()
  -- Optional: reroll layout until the DM's treasure-distance wish is met.
  if cfg.ALLOW_LAYOUT_RESEED and floorPlanUsed and floorPlanUsed.treasure_distance ~= "any" then
    local dist = State.level().treasure_distance_from_start
    local want = floorPlanUsed.treasure_distance
    local ok = dist == nil or (want == "far" and dist >= 4) or (want == "near" and dist <= 2)
    if not ok and reseeds < cfg.MAX_RESEEDS then
      reseeds = reseeds + 1
      levelSig = nil                 -- the reseeded floor counts as a new floor
      Isaac.ExecuteCommand("reseed")
      return
    end
  end
  reseeds = 0
end

-- ------------------------------------------------------------- per-frame work
function Sched.update()
  if not room or game:IsGreedMode() then return end
  local t = Isaac.GetTime()
  local r = game:GetRoom()

  -- The backend connected after we entered this room: catch up on prefetches.
  if not room.prefetched and Bridge.ready() then
    room.prefetched = true
    prefetchNeighbours("backend connected")
    requestFloorItems()
  end

  -- Late layout: the answer arrived just after the player walked in.
  if room.firstVisit and not room.layoutApplied and cfg.CONTROL_ROOM_LAYOUT
      and game:GetFrameCount() - room.enteredFrame <= cfg.LATE_LAYOUT_GRACE_FRAMES then
    local res = Bridge.take(fkey("layout", room.idx))
    if res then
      Apply.roomLayout(res.decision)
      room.layoutApplied = true
    end
  end

  -- Lingering: refresh neighbour layouts if the player's situation changed.
  if t - room.lastNeighbourRefresh > cfg.REFRESH_MIN_MS * 2 then
    room.lastNeighbourRefresh = t
    local h = State.materialHash()
    for key, sentHash in pairs(room.layoutKeys) do
      local p = Bridge.pendingInfo(key)
      if sentHash ~= h and (not p or p.sends <= cfg.MAX_REFRESHES) then
        local idx = tonumber(key:match(":(%-?%d+)$"))
        local d = idx and game:GetLevel():GetRoomByIdx(idx)
        if d and d.Data and d.VisitedCount == 0 then
          requestLayout(d, "refresh: player state changed while lingering")
          room.layoutKeys[key] = h
        end
      end
    end
  end

  -- Room-clear prefetch, timed off the enemy-HP burn rate.
  if cfg.CONTROL_ROOM_CLEAR and eligibleClearRoom(room.type) and not room.wasClear and not r:IsClear()
      and t - room.lastProbe >= 100 then
    room.lastProbe = t
    local hp, n = alive()
    if n > 0 then
      room.startHp = math.max(room.startHp or 0, hp)
      local s = room.hpSamples
      s[#s + 1] = { t = t, hp = hp }
      while #s > 1 and t - s[1].t > 3000 do table.remove(s, 1) end
      local dps = 0
      if #s >= 2 and s[#s].t > s[1].t then
        dps = math.max(0, (s[1].hp - s[#s].hp) / (s[#s].t - s[1].t))    -- hp per ms
      end
      local eta = dps > 0 and hp / dps or math.huge
      local lead = leadMs()
      local frac = hp / math.max(1, room.startHp)
      if room.clearSends == 0 then
        if eta <= lead or frac <= 0.15 or (n == 1 and frac <= 0.35) then
          requestClear(eta <= lead and "eta under lead time" or "almost clear", eta)
        end
      elseif room.clearSends <= cfg.MAX_REFRESHES
          and t - room.clearReqAt >= cfg.REFRESH_MIN_MS
          and State.materialHash() ~= room.clearReqHash
          and eta > Bridge.latencyP90() then      -- only if the fresh answer can still land
        requestClear("refresh: player state changed", eta)
      end
    end
  end

  -- Boss dead: re-plan the next floor with the post-fight state.
  if room.type == RoomType.ROOM_BOSS and r:IsClear() and not room.bossReplanned and cfg.CONTROL_FLOOR then
    room.bossReplanned = true
    requestFloorPlan("boss defeated")
  end
end

-- ------------------------------------------------------------ decision hooks
--- MC_PRE_SPAWN_CLEAN_AWARD. Returns true if the DM handled the reward.
function Sched.onCleanAward(rng, pos)
  if not room or game:IsGreedMode() or not cfg.CONTROL_ROOM_CLEAR then return nil end
  if not eligibleClearRoom(game:GetRoom():GetType()) then return nil end
  local secs, hits = State.onRoomCleared()
  Bridge.event("room_cleared", { room = room.idx, seconds = secs, hits = hits })
  local res = Bridge.take(fkey("clear", room.idx))
  if not res then return nil end                      -- DM too slow: vanilla decides
  Apply.roomClear(res.decision, pos)
  -- Documented API bug: returning true stops the award seed advancing.
  local level = game:GetLevel()
  local desc = level:GetRoomDesc(level:GetCurrentRoomIndex())
  desc.AwardSeed = rng:GetSeed()
  return true
end

--- MC_PRE_GET_COLLECTIBLE. Returns a CollectibleType or nil.
function Sched.onGetCollectible(pool, decrease, seed)
  if Sched.peeking or not cfg.CONTROL_ITEMS or game:IsGreedMode() then return nil end
  local key = fkey("items", pool)
  local res = Bridge.peek(key)
  local q = itemQueues[pool]
  if res and (not q or q.resultId ~= res.id) then
    local ids = {}
    for i, id in ipairs(res.decision.order or {}) do ids[i] = id end
    q = { resultId = res.id, ids = ids, used = false, taunt = res.decision.taunt }
    itemQueues[pool] = q
  end
  if not q then return nil end
  local player = Isaac.GetPlayer(0)
  while #q.ids > 0 do
    local id = table.remove(q.ids, 1)
    if id and id > 0 and State.itemInfo(id) and not player:HasCollectible(id, true) then
      if decrease then game:GetItemPool():RemoveCollectible(id) end
      if not q.used then
        q.used = true
        Bridge.markApplied(key)
        Taunt.show(q.taunt)
      end
      return id
    end
  end
  return nil
end

--- MC_POST_CURSE_EVAL. Returns a new curse mask or nil.
function Sched.onCurseEval(curses)
  if not cfg.CONTROL_FLOOR or game:IsGreedMode() then return nil end
  local res = Bridge.take(fkey("floor", "next"))
  local plan = res and res.decision or (reseeds > 0 and floorPlanUsed) or nil
  if not plan then return nil end
  floorPlanUsed = plan
  if res then Taunt.show(plan.taunt) end
  return Apply.curses(plan, curses)
end

function Sched.resetRun()
  floorNo, levelSig, room, itemQueues, floorPlanUsed, reseeds = 0, nil, nil, {}, nil, 0
  floorItemsRequestedFor = -1
end

function Sched.init(config, bridge, state, apply, taunt)
  cfg, Bridge, State, Apply, Taunt = config, bridge, state, apply, taunt
  POOL_FOR_ROOM = {}
  local function map(rt, pool) if rt and pool then POOL_FOR_ROOM[rt] = pool end end
  map(RoomType.ROOM_TREASURE, ItemPoolType.POOL_TREASURE)
  map(RoomType.ROOM_BOSS, ItemPoolType.POOL_BOSS)
  map(RoomType.ROOM_SHOP, ItemPoolType.POOL_SHOP)
  map(RoomType.ROOM_DEVIL, ItemPoolType.POOL_DEVIL)
  map(RoomType.ROOM_ANGEL, ItemPoolType.POOL_ANGEL)
  map(RoomType.ROOM_SECRET, ItemPoolType.POOL_SECRET)
  map(RoomType.ROOM_LIBRARY, ItemPoolType.POOL_LIBRARY)
  map(RoomType.ROOM_CURSE, ItemPoolType.POOL_CURSE)
  map(RoomType.ROOM_PLANETARIUM, ItemPoolType.POOL_PLANETARIUM)
end

function Sched.debugCounts()
  return string.format("floor %d", floorNo)
end

return Sched
