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
--   item_choice  also asked for chest/beggar pools the moment one of those shows up in a room.
--   consumables  see consumables.lua (floor start, then when a new pill colour is in play).
--   payout_plan  see payouts.lua: asked once per beggar/machine/chest when it shows up.
--   drops        enemy-death and natural pickups are swallowed (dropcatcher.lua) and ride along
--                with room_clear as context.suppressed_drops.
--
-- If an answer isn't there when the game needs it, the game freezes on a hold (loading.lua) until it
-- is, asking right then if it never asked. Rooms are dressed and rewards dropped late; item
-- pedestals, which the game fills on the spot, are swapped for the DM's picks when the ranking
-- lands; a floor's curses are changed once its plan does. Only with no backend at all does the
-- game decide on its own.
local Sched = {}

local game = Game()
local cfg, Bridge, State, Apply, Taunt, Cons, Loading, Catcher, Payouts

local floorNo = 0
local levelSig
local room = nil         -- per-room tracker
local itemQueues = {}    -- pool -> {resultId, ids}
local itemSigs = {}      -- pool -> signature of the last item_choice request (candidates + player state)
local itemSends = {}     -- pool -> item_choice requests sent this floor
local layoutSends = {}   -- layout key -> {hash, n}: what we last asked about that room, and how often
local fallbackSent = {}  -- key -> true: a hold on it gave up and the game decided (reported once)
local floorPlanAsked = false
local floorPlanFor       -- {stage, stage_type}: the floor the last floor_plan was asked from
local latePlan           -- floor_plan key the new floor's curses wait on (held on the next update)
local drawn              -- {pool, frame, stale}: an item drawn with no DM answer; its pedestal comes next
local swaps = {}         -- pool -> pedestals to turn into the DM's picks once its ranking lands
local toHold = {}        -- pool -> stale result id (or false): hold on it next update, not mid-draw
local floorPlanUsed      -- last floor plan applied (re-used if we reseed)
local floorPlanKey       -- its key, for what its rerolls did
local reseeds = 0
Sched.peeking = false

local POOL_FOR_ROOM -- RoomType -> ItemPoolType (filled in init)
local CONTROLLED_POOL = {}
local PICKUP_POOLS = {}  -- chest PickupVariant -> {pool, label}
local SLOT_POOLS = {}    -- beggar/machine variant -> {pool, label}
-- Rooms whose clear award the DM decides. "replace": the DM's drop is the drop.
-- "bonus": the game keeps its own award (the boss item lives there) and the DM adds to it.
local CLEAR_MODE = {}

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

-- Tell the backend a hold gave up: the game needed a decision and the DM never gave one (once per key).
local function reportFallback(kind, key, asked)
  if fallbackSent[key] then return end
  fallbackSent[key] = true
  Bridge.event("fallback", { kind = kind, key = key, asked = asked })
end

-- Worth (re)asking about this room? Not if an answer for the same player state is already
-- waiting or on its way, and never more than MAX_LAYOUT_SENDS times per room.
local function wantLayout(key, hash)
  local s = layoutSends[key]
  if not s then return true end
  if s.n >= cfg.MAX_LAYOUT_SENDS then return false end
  return not (s.hash == hash and (Bridge.isPending(key) or Bridge.peek(key)))
end
local function eligibleClearRoom(t) return CLEAR_MODE[t] ~= nil end

-- ------------------------------------------------------------------ requests
local function requestLayout(desc, reason)
  local key = fkey("layout", desc.SafeGridIndex)
  local level = game:GetLevel()
  local prev = layoutSends[key]
  layoutSends[key] = { hash = State.materialHash(), n = (prev and prev.n or 0) + 1 }
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

local function requestClear(reason, etaMs, deadlineMs, caught)
  local key = fkey("clear", room.idx)
  local hp, n = alive()
  Bridge.request("room_clear", key, State.snapshot(), {
    reason = reason, room_type = State.roomTypeName(room.type), mode = CLEAR_MODE[room.type],
    enemies_left = n, enemy_hp_left = math.floor(hp),
    est_ms_until_clear = math.floor(math.min(etaMs or 0, 60000)),
    seconds_in_room = math.floor((Isaac.GetTime() - room.enteredAt) / 100) / 10,
    suppressed_drops = caught or Catcher.counts(),     -- a late ask after the clear carries what was caught
  }, deadlineMs or math.floor(math.max(1500, math.min(etaMs or 4000, 10000)) + 2000))
  room.clearReqAt = Isaac.GetTime()
  room.clearReqHash = State.materialHash()
  room.clearReqSupp = Catcher.total()
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

local function requestItems(pool, roomTypeName, reason, force)
  local cands = peekCandidates(pool)
  if #cands == 0 then return end
  -- Candidates come from a fixed seed, so walking past the same room again would resend the
  -- exact same question. Only re-ask when the candidates or the player's state changed (a ranking
  -- and its taunt written at floor start go stale), at most MAX_ITEM_SENDS times a floor.
  local key, ids = fkey("items", pool), {}
  for i, c in ipairs(cands) do ids[i] = tostring(c.id) end
  local sig = table.concat(ids, ",") .. "|" .. State.materialHash()
  if not force then
    if itemSigs[pool] == sig and (Bridge.isPending(key) or Bridge.peek(key)) then return end
    if (itemSends[pool] or 0) >= cfg.MAX_ITEM_SENDS then return end
  end
  itemSigs[pool] = sig
  itemSends[pool] = (itemSends[pool] or 0) + 1
  Bridge.request("item_choice", key, State.snapshot(), {
    reason = reason, pool = pool, room_type = roomTypeName, candidates = cands,
  }, 10000)
end

-- Chests and beggars draw from their own item pools. Ask about a pool once per room visit,
-- as soon as its source is in the room (well before the player opens or pays it).
local function requestSourcePools()
  if not cfg.CONTROL_ITEMS then return end
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    local src
    if e.Type == EntityType.ENTITY_PICKUP and e.SubType ~= 0 then src = PICKUP_POOLS[e.Variant]   -- 0 = opened
    elseif e.Type == EntityType.ENTITY_SLOT then src = SLOT_POOLS[e.Variant] end
    if src and not room.poolsAsked[src[1]] then
      room.poolsAsked[src[1]] = true
      requestItems(src[1], src[2], "a " .. src[2]:gsub("_", " ") .. " is in the player's room")
    end
  end
end

local function requestFloorPlan(reason, key)
  local level = game:GetLevel()
  if not key then                       -- a fresh ask from this floor's boss room
    floorPlanAsked = true
    floorPlanFor = { level:GetStage(), level:GetStageType() }
  end
  Bridge.request("floor_plan", key or fkey("floor", "next"), State.snapshot(), {
    reason = reason, current_stage = floorPlanFor[1], current_stage_type = floorPlanFor[2],
    allowed_layout_rerolls = cfg.ALLOW_LAYOUT_RESEED and cfg.MAX_RESEEDS or 0,
  }, 12000)
end

-- ------------------------------------------------------------------- holds
-- The player walked into a room the DM hasn't dressed yet.
local function holdLayout(key, desc)
  local idx, entered = room.idx, game:GetFrameCount()
  Loading.hold(key, {
    label = "this room",
    ask = function() requestLayout(desc, "the player walked in before the DM had dressed this room") end,
    apply = function()
      local res = Bridge.take(key)
      if not res then return end
      if curIdx() ~= idx then return Bridge.effect(key, { "not applied: the player left the room" }) end
      Bridge.effect(key, Apply.roomLayout(res.decision, game:GetFrameCount() - entered))
      if room and room.idx == idx then
        room.layoutApplied = true
        Catcher.adopt(Bridge)
      end
    end,
    giveUp = function()
      reportFallback("room_layout", key, true)
      if room and room.idx == idx then Catcher.adopt(Bridge) end
    end,
  })
end

-- Drops still trailing in after this clear's reward is out: lost, and the clear's effect says so.
local function lostAfterClear(key)
  return function(name)
    Bridge.effect(key, { Apply.count("-", 1, name) .. " (dropped as the room cleared, not replaced)" })
  end
end

-- The room is clear and the DM's reward isn't in: the game's award was skipped (or, for a bonus,
-- spawned without it), so drop the DM's where the game would have once it lands.
local function holdClear(key, pos, caught)
  local idx, at = room.idx, Vector(pos.X, pos.Y)
  Loading.hold(key, {
    label = CLEAR_MODE[room.type] == "bonus" and "what else you get" or "your reward",
    ask = function()
      if room and room.idx == idx then
        requestClear("the room was cleared before the DM answered", 0, 10000, next(caught.counts) and caught.counts or nil)
      end
    end,
    apply = function()
      local res = Bridge.take(key)
      if not res then return end
      if curIdx() ~= idx then return Bridge.effect(key, { "not applied: the player left the room" }) end
      Bridge.effect(key, Apply.roomClear(res.decision, at, caught.counts))
      Catcher.settle(caught, lostAfterClear(key))
    end,
    giveUp = function()
      Catcher.settle(caught)                         -- stop swallowing: nobody will account for them
      local n = Catcher.release(caught)              -- the DM never answered: the game's drops come back
      if n > 0 then Bridge.effect(key, { "the DM never answered: " .. n .. " swallowed drop(s) returned" }) end
      reportFallback("room_clear", key, true)
    end,
  })
end

-- This pool's ranking as a queue of picks, rebuilt when a newer ranking lands.
local function queueFor(pool)
  local res = Bridge.peek(fkey("items", pool))
  local q = itemQueues[pool]
  if res and (not q or q.resultId ~= res.id) then
    local ids = {}
    for i, id in ipairs(res.decision.order or {}) do ids[i] = id end
    q = { resultId = res.id, ids = ids, used = false, taunt = res.decision.taunt }
    itemQueues[pool] = q
  end
  return q
end

-- The DM's next pick from this pool that the player can still get, or nil (and the queue, if any).
local function nextItem(pool, decrease)
  local q = queueFor(pool)
  if not q then return nil, nil end
  local player = Isaac.GetPlayer(0)
  while #q.ids > 0 do
    local id = table.remove(q.ids, 1)
    if id and id > 0 and State.itemInfo(id) and not player:HasCollectible(id, true) then
      if decrease then game:GetItemPool():RemoveCollectible(id) end
      if not q.used then
        q.used = true
        Bridge.markApplied(fkey("items", pool))
        Taunt.show(q.taunt)
      end
      local info = State.itemInfo(id)
      Bridge.effect(fkey("items", pool), { "item offered: " .. info.name .. " (q" .. info.quality .. ")" })
      return id, q
    end
  end
  return nil, q
end

local function swapPedestals(pool, idx)
  local list = swaps[pool] or {}
  swaps[pool] = nil
  if curIdx() ~= idx then return end
  for _, p in ipairs(list) do
    if p:Exists() and p.SubType ~= 0 then          -- 0: already taken
      local id = nextItem(pool, true)
      if not id then break end
      p:Morph(EntityType.ENTITY_PICKUP, PickupVariant.PICKUP_COLLECTIBLE, id, true, true, true)
    end
  end
end

-- Pedestals came out of a pool the DM had no (usable) ranking for: hold until it has one.
local function holdItems(pool, stale)
  local key, idx = fkey("items", pool), curIdx()
  Loading.hold(key, {
    label = "what's on the pedestal", stale = stale,
    ask = function()
      requestItems(pool, State.roomTypeName(room.type), "the game needs an item from this pool right now", true)
    end,
    apply = function() swapPedestals(pool, idx) end,
    giveUp = function()
      swaps[pool] = nil
      reportFallback("item_choice", key, true)
    end,
  })
end

-- A floor plan's effect: its curse changes, and whether its treasure-distance wish can act at all.
local function floorEffect(plan, curses)
  if plan.treasure_distance and plan.treasure_distance ~= "any" and not cfg.ALLOW_LAYOUT_RESEED then
    curses[#curses + 1] = "treasure room " .. plan.treasure_distance .. ": ignored (layout rerolls are off)"
  end
  return curses
end

-- Optional: reroll the layout until the DM's treasure-distance wish is met. True if it rerolled.
local function maybeReseed()
  if cfg.ALLOW_LAYOUT_RESEED and floorPlanUsed and floorPlanUsed.treasure_distance ~= "any" then
    local dist = State.level().treasure_distance_from_start
    local want = floorPlanUsed.treasure_distance
    local ok = dist == nil or (want == "far" and dist >= 4) or (want == "near" and dist <= 2)
    if not ok and reseeds < cfg.MAX_RESEEDS then
      reseeds = reseeds + 1
      Bridge.effect(floorPlanKey, { "floor rerolled: treasure room " .. tostring(dist) .. " rooms away, wanted " .. want })
      levelSig = nil                 -- the reseeded floor counts as a new floor
      Isaac.ExecuteCommand("reseed")
      return true
    end
  end
  reseeds = 0
  return false
end

-- The floor was generated before its plan landed: change its curses once it does.
local function holdFloorPlan(key)
  Loading.hold(key, {
    label = "this floor's curses",
    ask = function() requestFloorPlan("the player is already on this floor; its curses wait on you", key) end,
    apply = function()
      local res = Bridge.take(key)
      if not res then return end
      floorPlanUsed, floorPlanKey = res.decision, key
      Taunt.show(res.decision.taunt)
      Bridge.effect(key, floorEffect(res.decision, Apply.cursesNow(res.decision)))
      maybeReseed()
    end,
    giveUp = function() reportFallback("floor_plan", key, true) end,
  })
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
        local key, h = fkey("layout", d.SafeGridIndex), State.materialHash()
        if wantLayout(key, h) then requestLayout(d, reason) end
        room.layoutKeys[key] = h
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
  itemQueues, itemSigs, itemSends, layoutSends, fallbackSent = {}, {}, {}, {}, {}
  drawn, swaps, toHold = nil, {}, {}
  Bridge.forget("layout:" .. (floorNo - 1))
  Bridge.forget("clear:" .. (floorNo - 1))
  Bridge.forget("items:" .. (floorNo - 1))
  Catcher.reset()
  Payouts.onNewLevel(floorNo)
  Cons.onNewFloor(floorNo)
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
    lastProbe = 0, lastNeighbourRefresh = Isaac.GetTime(), poolsAsked = {}, lastSourceScan = 0,
    clearReqSupp = 0, clearSeenFrame = nil,
  }
  -- Drops the game scatters while this room's fight runs are swallowed and handed to its clear reward.
  Catcher.arm(room.idx, cfg.CONTROL_DROPS and cfg.CONTROL_ROOM_CLEAR and eligibleClearRoom(room.type)
                        and not r:IsClear() and Bridge.ready())

  -- 1. Dress this room: now if the DM already decided how, else once it has (the game waits).
  local layoutHeld = false
  if room.firstVisit and cfg.CONTROL_ROOM_LAYOUT then
    local key = fkey("layout", room.idx)
    local res = Bridge.take(key)
    if res then
      Bridge.effect(key, Apply.roomLayout(res.decision))
      room.layoutApplied = true
    elseif Bridge.ready() and eligibleLayoutRoom(room.type)
        and room.idx ~= game:GetLevel():GetStartingRoomIndex() then   -- the start room is never dressed
      holdLayout(key, desc)
      layoutHeld = true                -- adopts the room's own pickups once the layout lands
    end
  end
  if not layoutHeld then Catcher.adopt(Bridge) end

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
  if latePlan then return end        -- any reroll waits for the plan (holdFloorPlan)
  maybeReseed()
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

  -- Decisions the game had to go ahead without, inside its own callbacks: wait on them now.
  if latePlan then
    holdFloorPlan(latePlan)
    latePlan = nil
  end
  for pool, stale in pairs(toHold) do holdItems(pool, stale or nil) end
  toHold = {}

  Cons.update()

  -- Chests dropped mid-room and beggars spawned by cards count too, so keep looking.
  if Bridge.ready() and t - room.lastSourceScan >= 1000 then
    room.lastSourceScan = t
    requestSourcePools()
  end

  -- Lingering: refresh neighbour layouts if the player's situation changed.
  if t - room.lastNeighbourRefresh > cfg.REFRESH_MIN_MS * 2 then
    room.lastNeighbourRefresh = t
    local h = State.materialHash()
    for key, sentHash in pairs(room.layoutKeys) do
      if sentHash ~= h and wantLayout(key, h) then
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
          and (State.materialHash() ~= room.clearReqHash or Catcher.total() ~= room.clearReqSupp)
          and eta > Bridge.latencyP90() then      -- only if the fresh answer can still land
        requestClear("refresh: player state or the swallowed drops changed", eta)
      end
    end
  end

  -- A room that cleared without a clear award (nothing fired): give back what was swallowed.
  if Catcher.isOpen() and r:IsClear() then
    room.clearSeenFrame = room.clearSeenFrame or game:GetFrameCount()
    if game:GetFrameCount() - room.clearSeenFrame > 90 then Catcher.release(Catcher.close()) end
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
  local key = fkey("clear", room.idx)
  local res = Bridge.take(key)
  -- The reward is decided now, but the last kill's fallout can land a frame or two late: keep
  -- swallowing a little longer, unless the game's own award (a bonus room, no backend) is coming.
  local trail = (res or Bridge.ready()) and CLEAR_MODE[room.type] ~= "bonus" and cfg.CLEAR_TRAIL_FRAMES or 0
  local caught = Catcher.close(trail)
  if res then
    Bridge.effect(key, Apply.roomClear(res.decision, pos, caught.counts))
    Catcher.settle(caught, lostAfterClear(key))
  elseif Bridge.ready() then
    holdClear(key, pos, caught)                       -- not in yet: the game waits, the DM's drop comes after
  else
    Catcher.release(caught)                           -- no backend: the game's own award, and its drops
    return nil
  end
  if CLEAR_MODE[room.type] == "bonus" then return nil end   -- the game still spawns its own award
  -- Documented API bug: returning true stops the award seed advancing. Not every build exposes the
  -- field, and a throw here would let the game's own award through, so never let it escape.
  local desc = game:GetLevel():GetCurrentRoomDesc()
  pcall(function() desc.AwardSeed = rng:GetSeed() end)
  return true
end

--- MC_PRE_GET_COLLECTIBLE. Returns a CollectibleType or nil.
function Sched.onGetCollectible(pool, decrease, seed)
  if Sched.peeking or not cfg.CONTROL_ITEMS or game:IsGreedMode() then return nil end
  local id, q = nextItem(pool, decrease)
  if id then return id end
  -- No ranking, or it ran out: the game fills the pedestal now and the DM's pick replaces it once
  -- it lands (Sched.onPickupInit finds the pedestal, the next update holds the game).
  if CONTROLLED_POOL[pool] and Bridge.ready() then
    drawn = { pool = pool, frame = game:GetFrameCount(), stale = q and q.resultId }
  end
  return nil
end

--- MC_POST_PICKUP_INIT: the pedestal an unanswered draw just filled.
function Sched.onPickupInit(pickup)
  if not drawn or pickup.Variant ~= PickupVariant.PICKUP_COLLECTIBLE then return end
  if drawn.frame == game:GetFrameCount() then
    local list = swaps[drawn.pool] or {}
    list[#list + 1] = pickup
    swaps[drawn.pool] = list
    toHold[drawn.pool] = drawn.stale or false
  end
  drawn = nil
end

--- MC_POST_CURSE_EVAL. Returns a new curse mask or nil.
function Sched.onCurseEval(curses)
  if not cfg.CONTROL_FLOOR or game:IsGreedMode() then return nil end
  local key = fkey("floor", "next")
  local res = Bridge.take(key)
  local plan = res and res.decision or (reseeds > 0 and floorPlanUsed) or nil
  -- Asked but not in: the floor is generated now, and its curses change once the plan lands.
  if not plan and floorPlanAsked and Bridge.ready() then latePlan = key end
  floorPlanAsked = false
  if not plan then return nil end
  floorPlanUsed = plan
  local mask = Apply.curses(plan, curses)
  if res then
    floorPlanKey = key
    Taunt.show(plan.taunt)
    Bridge.effect(key, floorEffect(plan, Apply.curseEffect(curses, mask)))
  end
  return mask
end

function Sched.resetRun()
  floorNo, levelSig, room, itemQueues, floorPlanUsed, floorPlanKey, reseeds = 0, nil, nil, {}, nil, nil, 0
  itemSigs, itemSends, layoutSends, fallbackSent, floorPlanAsked = {}, {}, {}, {}, false
  floorPlanFor, latePlan, drawn, swaps, toHold = nil, nil, nil, {}, {}
  floorItemsRequestedFor = -1
  Catcher.reset()
  Payouts.reset()
  Cons.resetRun()
end

function Sched.init(config, bridge, state, apply, taunt, cons, loading, catcher, payouts)
  cfg, Bridge, State, Apply, Taunt, Cons, Loading = config, bridge, state, apply, taunt, cons, loading
  Catcher, Payouts = catcher, payouts
  POOL_FOR_ROOM = {}
  CONTROLLED_POOL = {}
  local function map(rt, pool)
    if rt and pool then POOL_FOR_ROOM[rt], CONTROLLED_POOL[pool] = pool, true end
  end
  map(RoomType.ROOM_TREASURE, ItemPoolType.POOL_TREASURE)
  map(RoomType.ROOM_BOSS, ItemPoolType.POOL_BOSS)
  map(RoomType.ROOM_SHOP, ItemPoolType.POOL_SHOP)
  map(RoomType.ROOM_DEVIL, ItemPoolType.POOL_DEVIL)
  map(RoomType.ROOM_ANGEL, ItemPoolType.POOL_ANGEL)
  map(RoomType.ROOM_SECRET, ItemPoolType.POOL_SECRET)
  map(RoomType.ROOM_LIBRARY, ItemPoolType.POOL_LIBRARY)
  map(RoomType.ROOM_CURSE, ItemPoolType.POOL_CURSE)
  map(RoomType.ROOM_PLANETARIUM, ItemPoolType.POOL_PLANETARIUM)
  map(RoomType.ROOM_ULTRASECRET, ItemPoolType.POOL_ULTRA_SECRET)

  local function source(tbl, variant, poolName, label)
    local pool = ItemPoolType[poolName]
    if variant and pool then tbl[variant], CONTROLLED_POOL[pool] = { pool, label }, true end
  end
  source(PICKUP_POOLS, PickupVariant.PICKUP_LOCKEDCHEST, "POOL_GOLDEN_CHEST", "golden_chest")
  source(PICKUP_POOLS, PickupVariant.PICKUP_REDCHEST, "POOL_RED_CHEST", "red_chest")
  source(PICKUP_POOLS, PickupVariant.PICKUP_WOODENCHEST, "POOL_WOODEN_CHEST", "wooden_chest")
  source(PICKUP_POOLS, PickupVariant.PICKUP_OLDCHEST, "POOL_OLD_CHEST", "old_chest")
  source(PICKUP_POOLS, PickupVariant.PICKUP_MOMSCHEST, "POOL_MOMS_CHEST", "moms_chest")
  -- Slot variants have no enum in the vanilla API; these are the game's own numbers.
  source(SLOT_POOLS, 4, "POOL_BEGGAR", "beggar")
  source(SLOT_POOLS, 5, "POOL_DEMON_BEGGAR", "demon_beggar")
  source(SLOT_POOLS, 6, "POOL_SHELL_GAME", "shell_game")
  source(SLOT_POOLS, 7, "POOL_KEY_MASTER", "key_master")
  source(SLOT_POOLS, 9, "POOL_BOMB_BUM", "bomb_bum")
  source(SLOT_POOLS, 13, "POOL_BATTERY_BUM", "battery_bum")
  source(SLOT_POOLS, 16, "POOL_CRANE_GAME", "crane_game")
  source(SLOT_POOLS, 18, "POOL_ROTTEN_BEGGAR", "rotten_beggar")

  Payouts.init(cfg, Bridge, State, Apply, Taunt, function(pool) return (nextItem(pool, true)) end,
               SLOT_POOLS, PICKUP_POOLS)

  CLEAR_MODE = {}
  for name, mode in pairs({ ROOM_DEFAULT = "replace", ROOM_MINIBOSS = "replace", ROOM_CURSE = "replace",
                            ROOM_BOSS = "bonus" }) do
    if RoomType[name] then CLEAR_MODE[RoomType[name]] = mode end
  end
end

function Sched.floor() return floorNo end

--- The decisions this floor opened with: the start room's neighbour layouts, the item pools asked
--- so far and the consumables plan. The loading screen waits for these. Only keys that were
--- actually sent count (a request made while disconnected would never settle).
function Sched.floorReadyKeys()
  local keys = {}
  local function add(key)
    if key and (Bridge.isPending(key) or Bridge.settled(key)) then keys[#keys + 1] = key end
  end
  if room then
    for key in pairs(room.layoutKeys) do add(key) end
  end
  for pool in pairs(itemSigs) do add(fkey("items", pool)) end
  add(Cons.askedKey())
  return keys
end

function Sched.debugCounts()
  return string.format("floor %d", floorNo)
end

return Sched
