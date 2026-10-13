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
        itemCount = -1, owned = {}, runClears = 0, pills = 0, lastHitBy = nil, pausedFrames = 0 }
end
State.resetRun()

--- Frames spent on the loading screen: the game's clock ran, the run's didn't.
function State.addPausedFrames(n) T.pausedFrames = (T.pausedFrames or 0) + math.max(0, n) end

function State.onNewFloor()
  T.floorHits, T.floorClears, T.clearSecs, T.lockedDoors, T.tintedSeen = 0, 0, {}, {}, {}
end

function State.onPlayerHit(amount, by)
  T.lastHitBy = by
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
  T.runClears = T.runClears + 1
  local secs = (Isaac.GetTime() - T.roomEnteredAt) / 1000
  T.clearSecs[#T.clearSecs + 1] = secs
  return round(secs, 1), T.roomHits
end

function State.onItemTaken(id)
  local info = State.itemInfo(id)
  if info then T.itemsTaken[#T.itemsTaken + 1] = info.name end
  return info
end

function State.onPillUsed() T.pills = T.pills + 1 end

--- Who hit the player, readably: a projectile or creep counts as whoever fired it.
function State.hitSourceName(ent)
  if not ent then return "the room" end
  local t = ent.Type
  if ent.SpawnerType and ent.SpawnerType > 0 and t ~= EntityType.ENTITY_PLAYER
      and (t == EntityType.ENTITY_PROJECTILE or t == EntityType.ENTITY_LASER or t == EntityType.ENTITY_EFFECT) then
    t = ent.SpawnerType
  end
  return (enumName(EntityType, t, "^ENTITY_"):gsub("_", " "))
end

-- The run totals a continued run picks up again (saved on exit, see main.lua).
local CARRIED = { "runHits", "runClears", "pills", "lastHitBy", "pausedFrames" }
function State.saveRun()
  local out = { items = T.itemsTaken }
  for _, k in ipairs(CARRIED) do out[k] = T[k] end
  return out
end

function State.restoreRun(saved)
  if type(saved) ~= "table" then return end
  for _, k in ipairs(CARRIED) do
    if saved[k] ~= nil then T[k] = saved[k] end
  end
  if type(saved.items) == "table" then T.itemsTaken = saved.items end
end

local function clock(frames)
  local s = math.floor(frames / 30)
  if s >= 3600 then return string.format("%d:%02d:%02d", s // 3600, s // 60 % 60, s % 60) end
  return string.format("%d:%02d", s // 60, s % 60)
end

--- The player's column of the after-death stats screen: {label, value} rows.
function State.runRows()
  local frames = math.max(0, (game.TimeCounter or game:GetFrameCount()) - (T.pausedFrames or 0))
  return {
    { "Reached", game:GetLevel():GetName() },
    { "Time", clock(frames) },
    { "Rooms cleared", tostring(T.runClears) },
    { "Damage taken", string.format("%g heart%s", T.runHits / 2, T.runHits == 2 and "" or "s") },
    { "Items taken", tostring(#T.itemsTaken) },
    { "Pills eaten", tostring(T.pills) },
    { "Last hit by", T.lastHitBy or "nothing" },
  }
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

-- Empty slots say "none" outright: a missing key reads as "unknown", and the models then fill it in
-- from what the character usually starts with (an Isaac without the D6 still got D6 plans).
local function maybe(f) local ok, v = pcall(f) if ok then return v end end

local function activeName(p, slot)
  local id = slot and maybe(function() return p:GetActiveItem(slot) end) or 0
  return id and id > 0 and (State.itemInfo(id) or {}).name or "none"
end

local GOLDEN_TRINKET = 32768
local function trinketNames(p)
  local out = {}
  for slot = 0, 1 do
    local id = math.tointeger(p:GetTrinket(slot)) or 0
    if id > 0 then
      local info = State.trinketInfo(id & (GOLDEN_TRINKET - 1))
      out[#out + 1] = (info and info.name or tostring(id)) .. ((id & GOLDEN_TRINKET) ~= 0 and " (golden)" or "")
    end
  end
  return #out > 0 and table.concat(out, ", ") or "none"
end

local function pocketName(p)
  local card = maybe(function() return p:GetCard(0) end) or 0
  if card > 0 then
    local c = maybe(function() return itemConfig:GetCard(card) end)
    return "card: " .. (c and prettyItemName(c.Name) or tostring(card))
  end
  local pill = maybe(function() return p:GetPill(0) end) or 0
  if pill > 0 then return "pill: " .. enumName(PillColor, pill, "^PILL_") end
  return "none"
end

--- How much of the item catalogue this save has unlocked (locked items never show up). Tells a
--- fresh save from a finished one, so the planners don't promise items or chapters it lacks.
local function saveProgress()
  if T.save == nil then
    T.save = false                              -- no IsAvailable on this version: say nothing
    local total, unlocked = 0, 0
    local ok = pcall(function()
      for id = 1, itemConfig:GetCollectibles().Size - 1 do
        local c = itemConfig:GetCollectible(id)
        if c then
          total = total + 1
          if c:IsAvailable() then unlocked = unlocked + 1 end
        end
      end
    end)
    if ok and total > 0 then T.save = { items_unlocked = unlocked, items_total = total } end
  end
  return T.save or nil
end

function State.player()
  local p = Isaac.GetPlayer(0)
  local hp = {
    red = p:GetHearts(), max_red = p:GetMaxHearts(), soul = p:GetSoulHearts(),
    bone = p:GetBoneHearts(), eternal = p:GetEternalHearts(), golden = p:GetGoldenHearts(),
    broken = p:GetBrokenHearts(), rotten = p:GetRottenHearts(),
  }
  local ptype = maybe(function() return p:GetPlayerType() end)
  local tainted                                 -- Tainted characters report the base name
  if ptype and PlayerType and PlayerType.PLAYER_ISAAC_B then tainted = ptype >= PlayerType.PLAYER_ISAAC_B end
  return {
    character = p:GetName(),
    player_type = ptype and PlayerType and enumName(PlayerType, ptype, "^PLAYER_") or nil,
    tainted = tainted,
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
    active_item = activeName(p, ActiveSlot.SLOT_PRIMARY),
    pocket_active = ActiveSlot.SLOT_POCKET and activeName(p, ActiveSlot.SLOT_POCKET) or nil,
    trinkets = trinketNames(p),
    pocket = pocketName(p),
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
    save = saveProgress(),
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
