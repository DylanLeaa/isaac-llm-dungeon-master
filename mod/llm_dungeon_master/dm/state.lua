-- Game-state snapshots for the Dungeon Master, plus the bookkeeping that feeds them
-- (locked doors seen, hits taken, clear times). Everything here is read-only w.r.t. the game.
local State = {}

local game = Game()

-- ------------------------------------------------------------------ helpers
local function enumName(enumTable, value, strip)
  if type(enumTable) ~= "table" then return tostring(value) end
  for k, v in pairs(enumTable) do
    if v == value then
      local s = k
      if strip then s = s:gsub(strip, "") end
      return s:lower()
    end
  end
  return tostring(value)
end

local roomTypeNames = {}
for k, v in pairs(RoomType or {}) do roomTypeNames[v] = k:gsub("^ROOM_", ""):lower() end
function State.roomTypeName(t) return roomTypeNames[t] or tostring(t) end

local function round(x, n)
  local m = 10 ^ (n or 2)
  return math.floor(x * m + 0.5) / m
end

local function prettyItemName(raw)
  if type(raw) ~= "string" or raw == "" then return "?" end
  if raw:sub(1, 1) ~= "#" then return raw end
  local s = raw:sub(2):gsub("_NAME$", ""):gsub("_", " "):lower()
  s = s:gsub("(%a)([%w']*)", function(a, b) return a:upper() .. b end)
  return s
end

local itemConfig = Isaac.GetItemConfig()
function State.itemInfo(id)
  local c = itemConfig:GetCollectible(id)
  if not c then return nil end
  return { id = id, name = prettyItemName(c.Name), quality = c.Quality or 0,
           active = c.Type == ItemType.ITEM_ACTIVE }
end

function State.trinketInfo(id)
  local c = itemConfig:GetTrinket(id)
  if not c then return nil end
  return { id = id, name = prettyItemName(c.Name) }
end

local function gridXY(idx) return idx % 13, math.floor(idx / 13) end
function State.gridDistance(a, b)
  local ax, ay = gridXY(a)
  local bx, by = gridXY(b)
  return math.abs(ax - bx) + math.abs(ay - by)
end

-- -------------------------------------------------------------- run tracker
local T = {}
function State.resetRun()
  T = { floorHits = 0, runHits = 0, itemsTaken = {}, floorClears = 0, clearSecs = {},
        lockedDoors = {}, tintedSeen = {}, roomHits = 0, roomEnteredAt = 0,
        itemCount = -1, owned = {} }
end
State.resetRun()

function State.onNewFloor()
  T.floorHits, T.floorClears, T.clearSecs, T.lockedDoors, T.tintedSeen = 0, 0, {}, {}, {}
end

function State.onPlayerHit(amount)
  T.floorHits = T.floorHits + amount
  T.runHits = T.runHits + amount
  T.roomHits = T.roomHits + amount
end

function State.onRoomEnter()
  T.roomHits = 0
  T.roomEnteredAt = Isaac.GetTime()
  local room = game:GetRoom()
  local level = game:GetLevel()
  local here = level:GetCurrentRoomDesc().SafeGridIndex
  -- Remember locked doors (and forget ones that are now open).
  for slot = 0, 7 do
    local door = room:GetDoor(slot)
    if door then
      local k = here .. ">" .. door.TargetRoomIndex
      if door:IsLocked() then T.lockedDoors[k] = true else T.lockedDoors[k] = nil end
    end
  end
  -- Tinted rocks visible in this room.
  local n = 0
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g and g:GetType() == GridEntityType.GRID_ROCKT and g.State ~= 2 then n = n + 1 end
  end
  T.tintedSeen[here] = n > 0 and n or nil
end

function State.onRoomCleared()
  T.floorClears = T.floorClears + 1
  local secs = (Isaac.GetTime() - T.roomEnteredAt) / 1000
  T.clearSecs[#T.clearSecs + 1] = secs
  return round(secs, 1), T.roomHits
end

function State.onItemTaken(id)
  local info = State.itemInfo(id)
  if info then T.itemsTaken[#T.itemsTaken + 1] = info.name end
  return info
end

-- ------------------------------------------------------------------ player
local function ownedItems(player)
  local count = player:GetCollectibleCount()
  if count == T.itemCount then return T.owned end
  T.itemCount = count
  local list = {}
  local max = itemConfig:GetCollectibles().Size - 1
  for id = 1, max do
    if player:HasCollectible(id, true) then
      local info = State.itemInfo(id)
      if info then list[#list + 1] = info.name .. " (q" .. info.quality .. ")" end
    end
  end
  T.owned = list
  return list
end

function State.player()
  local p = Isaac.GetPlayer(0)
  local hp = {
    red = p:GetHearts(), max_red = p:GetMaxHearts(), soul = p:GetSoulHearts(),
    bone = p:GetBoneHearts(), eternal = p:GetEternalHearts(), golden = p:GetGoldenHearts(),
    broken = p:GetBrokenHearts(), rotten = p:GetRottenHearts(),
  }
  local active = p:GetActiveItem(ActiveSlot.SLOT_PRIMARY)
  return {
    character = p:GetName(),
    hp = hp,
    resources = {
      coins = p:GetNumCoins(), bombs = p:GetNumBombs(), keys = p:GetNumKeys(),
      golden_key = p:HasGoldenKey(), golden_bomb = p:HasGoldenBomb(),
    },
    stats = {
      damage = round(p.Damage), tears_delay = round(p.MaxFireDelay), speed = round(p.MoveSpeed),
      range = round(p.TearRange / 40), shot_speed = round(p.ShotSpeed), luck = round(p.Luck),
      flying = p.CanFly,
    },
    active_item = active > 0 and (State.itemInfo(active) or {}).name or nil,
    trinkets = { p:GetTrinket(0), p:GetTrinket(1) },
    items = ownedItems(p),
  }
end

-- ------------------------------------------------------------------- level
local VANILLA_PER_CLEAR = { any = 0.40, coins = 0.16, hearts = 0.09, keys = 0.05, bombs = 0.05,
                            chests = 0.03, cards_pills = 0.04 }

function State.level()
  local level = game:GetLevel()
  local rooms = level:GetRooms()
  local stage = level:GetStage()
  local types, unvisited, unvisitedNormal, visited = {}, {}, 0, 0
  local secretsHidden = 0
  local startIdx = level:GetStartingRoomIndex()
  local treasureDist
  for i = 0, rooms.Size - 1 do
    local d = rooms:Get(i)
    if d and d.Data then
      local tn = State.roomTypeName(d.Data.Type)
      types[tn] = (types[tn] or 0) + 1
      if d.VisitedCount > 0 then
        visited = visited + 1
      else
        if d.Data.Type == RoomType.ROOM_DEFAULT then unvisitedNormal = unvisitedNormal + 1
        else unvisited[#unvisited + 1] = tn end
        if d.Data.Type == RoomType.ROOM_SECRET or d.Data.Type == RoomType.ROOM_SUPERSECRET then
          secretsHidden = secretsHidden + 1
        end
      end
      if d.Data.Type == RoomType.ROOM_TREASURE and not treasureDist then
        treasureDist = State.gridDistance(startIdx, d.SafeGridIndex)
      end
    end
  end
  local locked, tinted = 0, 0
  for _ in pairs(T.lockedDoors) do locked = locked + 1 end
  for _, n in pairs(T.tintedSeen) do tinted = tinted + n end
  local expected = {}
  for k, v in pairs(VANILLA_PER_CLEAR) do expected[k] = round(v * unvisitedNormal, 1) end
  local shopUnvisited = false
  for _, n in ipairs(unvisited) do if n == "shop" then shopUnvisited = true end end
  return {
    stage = stage, stage_type = level:GetStageType(), name = level:GetName(),
    curses = level:GetCurseName(),
    rooms_total = rooms.Size, rooms_visited = visited,
    room_types = types, special_rooms_unvisited = unvisited,
    normal_rooms_unvisited = unvisitedNormal,
    treasure_distance_from_start = treasureDist,
    needs = {
      locked_doors_seen = locked,
      secret_rooms_unfound = secretsHidden,
      tinted_rocks_seen = tinted,
      bomb_targets = secretsHidden + tinted,
      shop_unvisited = shopUnvisited,
    },
    vanilla_expected_drops_rest_of_floor = expected,   -- approximate, for reference
  }
end

-- ---------------------------------------------------------------- snapshot
function State.performance()
  local avg = 0
  for _, s in ipairs(T.clearSecs) do avg = avg + s end
  if #T.clearSecs > 0 then avg = avg / #T.clearSecs end
  return {
    half_hearts_lost_this_floor = T.floorHits, half_hearts_lost_run = T.runHits,
    rooms_cleared_this_floor = T.floorClears, avg_clear_seconds = round(avg, 1),
    hits_this_room = T.roomHits,
    recent_items_taken = { table.unpack(T.itemsTaken, math.max(1, #T.itemsTaken - 4)) },
  }
end

function State.snapshot()
  return {
    frame = game:GetFrameCount(),
    difficulty = game.Difficulty,
    player = State.player(),
    level = State.level(),
    performance = State.performance(),
  }
end

--- Cheap fingerprint of what matters for a pending decision. If this changes after a
--- request was sent, the scheduler may re-ask with a fresher snapshot.
function State.materialHash()
  local p = Isaac.GetPlayer(0)
  return table.concat({ p:GetHearts(), p:GetSoulHearts(), p:GetNumCoins(), p:GetNumBombs(),
                        p:GetNumKeys(), p:GetCollectibleCount() }, ":")
end

State.enumName = enumName
return State
