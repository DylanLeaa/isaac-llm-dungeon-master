-- items:<f>:<pool>  What each pedestal holds (MC_PRE_GET_COLLECTIBLE, rerolls included).
--
-- The deck is the DM's ranking of real candidates from the pool (item_choice): each pedestal takes the
-- next one the player can still get. Asked for every pool on the floor at floor start (in the background),
-- again for a pool next door (the loading screen waits on those), for a boss pool as the fight starts, and
-- for a chest's or beggar's pool as soon as one is in the room. Re-asked when the player's state moved
-- since (MAX_ITEM_SENDS a floor). Empty: the game fills the pedestal now, and the DM's pick replaces it
-- once the ranking lands (the game is held meanwhile).
local Items = {}

local game = Game()
local D, cfg, Bridge, State, Decks, Rolls, Causes

local POOL_FOR_ROOM = {}  -- RoomType -> ItemPoolType
local CONTROLLED = {}     -- pools the DM ranks
local floorAsked = -1
local roomKeys = {}       -- keys asked as next door from this room (the loading screen waits on these)
local sourcesAsked = {}   -- pools whose source (a chest, a beggar) was seen in this room
local lastSourceScan = 0
local wantedNow = {}      -- pool -> reason: in demand while the player is in this room
Items.peeking = false

local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end
local function floorNo() return D.Sched.floor() end

local function peekCandidates(pool)
  local itemPool = game:GetItemPool()
  local player = Isaac.GetPlayer(0)
  local seen, out = {}, {}
  local seed = game:GetSeeds():GetStartSeed() + floorNo() * 7919 + pool * 104729
  Items.peeking = true
  for i = 1, cfg.ITEM_CANDIDATES * 3 do
    if #out >= cfg.ITEM_CANDIDATES then break end
    local ok, id = pcall(itemPool.GetCollectible, itemPool, pool, false, seed + i * 31337)
    if ok and id and id > 0 and not seen[id] and not player:HasCollectible(id, true) then
      seen[id] = true
      local info = State.itemInfo(id)
      if info then out[#out + 1] = info end
    end
  end
  Items.peeking = false
  return out
end

--- Ask about `pool` unless an answer for the player's state as it is now is already in or on its way.
local function ask(pool, roomTypeName, reason)
  if not cfg.CONTROL_ITEMS or Decks.upToDate("items", pool) then return end
  local st = Decks.get("items", pool)
  st.roomType = roomTypeName or st.roomType
  Decks.request("items", pool, reason)
end

local function offered(pool, id, decrease)
  if decrease then game:GetItemPool():RemoveCollectible(id) end
  local info = State.itemInfo(id)
  Bridge.effect(Decks.get("items", pool).key, { "item offered: " .. info.name .. " (q" .. info.quality .. ")" })
end

--- The DM's next pick from `pool`, used up (a payout's "item"), or nil.
function Items.next(pool)
  local id = Decks.draw("items", pool, { pool = pool, cause = "payout" })
  if id then offered(pool, id, true) end
  return id
end

function Items.poolForRoom(rt) return POOL_FOR_ROOM[rt] end

function Items.init(deps)
  D, cfg, Bridge, State, Decks, Rolls, Causes = deps, deps.cfg, deps.Bridge, deps.State, deps.Decks, deps.Rolls, deps.Causes
  local function map(rt, pool)
    if rt and pool then POOL_FOR_ROOM[rt], CONTROLLED[pool] = pool, true end
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
  for _, src in pairs(Causes.SLOTS) do if src.pool then CONTROLLED[src.pool] = true end end
  for _, src in pairs(Causes.CHESTS) do if src.pool then CONTROLLED[src.pool] = true end end

  Decks.define("items", {
    kind = "item_choice",
    key = function(pool) return "items:" .. floorNo() .. ":" .. pool end,
    entries = function(d)
      local ids = {}
      for i, id in ipairs(d.order or {}) do ids[i] = id end
      return ids
    end,
    usable = function(id)
      if id and id > 0 and State.itemInfo(id) and not Isaac.GetPlayer(0):HasCollectible(id, true) then return true end
      return "drop"
    end,
    deck = false,                                  -- a ranking: the order is the deck
    context = function(st)
      local cands = peekCandidates(st.arg)
      if #cands == 0 then return false end
      return { pool = st.arg, room_type = st.roomType, candidates = cands }
    end,
    wants = function()
      local out = {}
      for pool, reason in pairs(wantedNow) do out[#out + 1] = { arg = pool, n = 1, reason = reason } end
      return out
    end,
    refresh = { max = math.max(0, cfg.MAX_ITEM_SENDS - 1), minMs = cfg.REFRESH_MIN_MS,
                reason = "refresh: player state changed while next to this room" },
    maxSends = cfg.MAX_ITEM_SENDS,
    holdLabel = "what's on the pedestal",
    outcome = function(id)
      local info = State.itemInfo(id) or {}
      return { item = id, quality = info.quality, name = info.name }
    end,
    label = function(id) return tostring(id) end,
    enabled = function() return cfg.CONTROL_ITEMS end,
  })

  Rolls.on("get_collectible", "items", function(pool, decrease)
    if Items.peeking then return nil, nil end      -- our own candidate peek: not a roll
    if not cfg.CONTROL_ITEMS or game:IsGreedMode() then return nil, "vanilla" end
    if Rolls.suppressed() then return nil, nil end  -- a spawn being cancelled: its item isn't drawn
    if Causes.payingOut(pool) and D.Payouts.owns(Causes.payingOut(pool)) then
      return nil, nil                              -- a chest / beggar paying its schedule: its item is the schedule's
    end
    local id = Decks.draw("items", pool, { pool = pool, cause = Rolls.cause() or "pedestal" })
    if id then
      offered(pool, id, decrease)
      return id, "dm"
    end
    if not (CONTROLLED[pool] and Bridge.ready()) then return nil, "vanilla" end
    -- No ranking, or it ran out: the game fills this pedestal now; the DM's pick replaces it.
    local slot, idx = {}, curIdx()
    Rolls.claimNext(PickupVariant.PICKUP_COLLECTIBLE, function(p) slot.pickup = p end)
    Decks.owe("items", pool, { pool = pool }, function(pick)
      local p = slot.pickup
      if pick and p and p:Exists() and p.SubType ~= 0 and curIdx() == idx then
        offered(pool, pick, true)
        p:Morph(EntityType.ENTITY_PICKUP, PickupVariant.PICKUP_COLLECTIBLE, pick, true, true, true)
      end
    end)
    return nil, "dm"
  end)
end

-- ----------------------------------------------------------------- lifecycle
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
Items.neighbours = neighbours

local function askNeighbours()
  for _, d in ipairs(neighbours()) do
    local pool = POOL_FOR_ROOM[d.Data.Type]
    if cfg.CONTROL_ITEMS and pool and d.VisitedCount == 0 then
      local reason = "player is next to this room"
      ask(pool, State.roomTypeName(d.Data.Type), reason)
      wantedNow[pool] = reason
      roomKeys[Decks.get("items", pool).key] = true
    end
  end
end

function Items.onNewRoom(room)
  roomKeys, sourcesAsked, wantedNow, lastSourceScan = {}, {}, {}, 0
  if Bridge.ready() then
    room.itemsPrefetched = true
    askNeighbours()
  end
  if cfg.CONTROL_ITEMS and room.type == RoomType.ROOM_BOSS and POOL_FOR_ROOM[room.type] then
    ask(POOL_FOR_ROOM[room.type], "boss", "boss item, fight starting")
  end
end

function Items.update(room)
  if not cfg.CONTROL_ITEMS or not Bridge.ready() then return end
  if not room.itemsPrefetched then             -- the backend connected after we walked in
    room.itemsPrefetched = true
    askNeighbours()
  end
  -- Every pool this floor has, once per floor, after the start room's neighbours (they gate the
  -- loading screen; these fill in while the player plays).
  if floorAsked ~= floorNo() then
    floorAsked = floorNo()
    local rooms = game:GetLevel():GetRooms()
    for i = 0, rooms.Size - 1 do
      local d = rooms:Get(i)
      local pool = d and d.Data and POOL_FOR_ROOM[d.Data.Type]
      local st = pool and Decks.get("items", pool)
      if st and st.sends == 0 and not Bridge.isPending(st.key) then
        ask(pool, State.roomTypeName(d.Data.Type), "floor start")
      end
    end
  end
  -- Chests dropped mid-room and beggars spawned by cards count too, so keep looking.
  local t = Isaac.GetTime()
  if t - lastSourceScan >= 1000 then
    lastSourceScan = t
    for _, m in ipairs(Causes.machinesHere()) do
      if m.pool and not m.opened and not sourcesAsked[m.pool] then
        sourcesAsked[m.pool] = true
        ask(m.pool, m.label, "a " .. m.label:gsub("_", " ") .. " is in the player's room")
      end
    end
  end
end

function Items.readyKeys(add)
  for key in pairs(roomKeys) do add(key) end
end

function Items.onNewFloor()
  roomKeys, sourcesAsked, wantedNow = {}, {}, {}
end

function Items.resetRun()
  floorAsked, roomKeys, sourcesAsked, wantedNow = -1, {}, {}, {}
end

return Items
