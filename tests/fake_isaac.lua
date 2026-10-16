-- Minimal fake of the Isaac Lua API: just enough surface for the mod to run headless.
-- Records spawns so tests can assert on what the DM did.
local W = { now = 0, frame = 0, callbacks = {}, spawned = {}, gridSpawned = {}, removedGrid = {}, gridState = {} }
FAKE = W

local function enum(names, start)
  local t, i = {}, start or 0
  for _, n in ipairs(names) do t[n] = i; i = i + 1 end
  return t
end

RoomType = enum({ "ROOM_NULL", "ROOM_DEFAULT", "ROOM_SHOP", "ROOM_ERROR", "ROOM_TREASURE", "ROOM_BOSS",
  "ROOM_MINIBOSS", "ROOM_SECRET", "ROOM_SUPERSECRET", "ROOM_ARCADE", "ROOM_CURSE", "ROOM_CHALLENGE",
  "ROOM_LIBRARY", "ROOM_SACRIFICE", "ROOM_DEVIL", "ROOM_ANGEL" })
RoomType.ROOM_PLANETARIUM = 24
ItemPoolType = enum({ "POOL_TREASURE", "POOL_SHOP", "POOL_BOSS", "POOL_DEVIL", "POOL_ANGEL", "POOL_SECRET",
  "POOL_LIBRARY", "POOL_SHELL_GAME", "POOL_GOLDEN_CHEST", "POOL_RED_CHEST", "POOL_BEGGAR", "POOL_DEMON_BEGGAR",
  "POOL_CURSE" })
ItemPoolType.POOL_PLANETARIUM = 26
ItemPoolType.POOL_ULTRA_SECRET = 24
GridEntityType = { GRID_ROCK = 2, GRID_ROCKT = 4 }
EntityType = { ENTITY_PLAYER = 1, ENTITY_FAMILIAR = 3, ENTITY_SLOT = 6, ENTITY_BOMB = 4, ENTITY_PICKUP = 5, ENTITY_GAPER = 10, ENTITY_BOOMFLY = 25,
  ENTITY_HOST = 27, ENTITY_ATTACKFLY = 18, ENTITY_KNIGHT = 41, ENTITY_DIP = 217 }
setmetatable(EntityType, { __index = function(_, k) if tostring(k):match("^ENTITY_") then return 900 end end })
EntityFlag = { FLAG_FREEZE = 1 << 1, FLAG_FRIENDLY = 1 << 29 }
ActiveSlot = { SLOT_PRIMARY = 0, SLOT_POCKET = 2 }
PlayerType = { PLAYER_ISAAC = 0, PLAYER_MAGDALENE = 1, PLAYER_ISAAC_B = 21 }
ItemType = { ITEM_PASSIVE = 1, ITEM_ACTIVE = 3 }
PickupVariant = { PICKUP_OLDCHEST = 55, PICKUP_WOODENCHEST = 56, PICKUP_MOMSCHEST = 390, PICKUP_HEART = 10, PICKUP_COIN = 20, PICKUP_KEY = 30, PICKUP_BOMB = 40, PICKUP_CHEST = 50,
  PICKUP_SPIKEDCHEST = 52, PICKUP_MIMICCHEST = 54, PICKUP_LOCKEDCHEST = 60, PICKUP_GRAB_BAG = 69,
  PICKUP_PILL = 70, PICKUP_LIL_BATTERY = 90, PICKUP_COLLECTIBLE = 100, PICKUP_TAROTCARD = 300,
  PICKUP_TRINKET = 350, PICKUP_REDCHEST = 360 }
CoinSubType = { COIN_PENNY = 1, COIN_NICKEL = 2, COIN_DIME = 3, COIN_DOUBLEPACK = 4, COIN_LUCKYPENNY = 5, COIN_STICKYNICKEL = 6 }
KeySubType = { KEY_NORMAL = 1, KEY_GOLDEN = 2, KEY_DOUBLEPACK = 3, KEY_CHARGED = 4 }
BombSubType = { BOMB_NORMAL = 1, BOMB_DOUBLEPACK = 2, BOMB_TROLL = 3, BOMB_GOLDEN = 4 }
HeartSubType = { HEART_FULL = 1, HEART_HALF = 2, HEART_SOUL = 3, HEART_ETERNAL = 4, HEART_BLACK = 6 }
ChestSubType = { CHEST_OPENED = 0, CHEST_CLOSED = 1 }
SackSubType = { SACK_NORMAL = 1 }
BatterySubType = { BATTERY_NORMAL = 1 }
BombVariant = { BOMB_NORMAL = 0, BOMB_TROLL = 3 }
LevelCurse = { CURSE_NONE = 0, CURSE_OF_DARKNESS = 1, CURSE_OF_LABYRINTH = 2, CURSE_OF_THE_LOST = 4,
  CURSE_OF_THE_UNKNOWN = 8, CURSE_OF_MAZE = 32, CURSE_OF_BLIND = 64 }
ModCallbacks = { MC_POST_UPDATE = 1, MC_POST_RENDER = 2, MC_INPUT_ACTION = 13, MC_USE_PILL = 10, MC_ENTITY_TAKE_DMG = 11,
  MC_POST_CURSE_EVAL = 12, MC_POST_GAME_STARTED = 15, MC_POST_GAME_END = 16, MC_PRE_GAME_EXIT = 17, MC_POST_NEW_LEVEL = 18,
  MC_POST_NEW_ROOM = 19, MC_GET_CARD = 20, MC_GET_TRINKET = 47, MC_PRE_GET_COLLECTIBLE = 62,
  MC_GET_PILL_EFFECT = 65, MC_PRE_NPC_UPDATE = 69, MC_PRE_SPAWN_CLEAN_AWARD = 70, MC_POST_PICKUP_INIT = 34, MC_POST_PICKUP_UPDATE = 35, MC_POST_NPC_DEATH = 29 }
PillColor = enum({ "PILL_NULL", "PILL_BLUE_BLUE", "PILL_WHITE_BLUE", "PILL_ORANGE_ORANGE", "PILL_WHITE_WHITE",
  "PILL_REDDOTS_RED", "PILL_PINK_RED", "PILL_BLUE_CADETBLUE", "PILL_YELLOW_ORANGE", "PILL_ORANGEDOTS_WHITE",
  "PILL_WHITE_AZURE", "PILL_BLACK_YELLOW", "PILL_WHITE_BLACK", "PILL_WHITE_YELLOW", "PILL_GOLD" })
PillEffect = enum({ "PILLEFFECT_BAD_GAS", "PILLEFFECT_BAD_TRIP", "PILLEFFECT_BALLS_OF_STEEL",
  "PILLEFFECT_BOMBS_ARE_KEYS", "PILLEFFECT_EXPLOSIVE_DIARRHEA", "PILLEFFECT_FULL_HEALTH", "PILLEFFECT_HEALTH_DOWN",
  "PILLEFFECT_HEALTH_UP", "PILLEFFECT_I_FOUND_PILLS", "PILLEFFECT_PUBERTY", "PILLEFFECT_PRETTY_FLY",
  "PILLEFFECT_RANGE_DOWN", "PILLEFFECT_RANGE_UP", "PILLEFFECT_SPEED_DOWN", "PILLEFFECT_SPEED_UP",
  "PILLEFFECT_TEARS_DOWN", "PILLEFFECT_TEARS_UP", "PILLEFFECT_LUCK_DOWN", "PILLEFFECT_LUCK_UP",
  "PILLEFFECT_TELEPILLS", "PILLEFFECT_48HOUR_ENERGY", "PILLEFFECT_HEMATEMESIS", "PILLEFFECT_PARALYSIS",
  "PILLEFFECT_SEE_FOREVER", "PILLEFFECT_PHEROMONES", "PILLEFFECT_AMNESIA", "PILLEFFECT_LEMON_PARTY",
  "PILLEFFECT_WIZARD", "PILLEFFECT_PERCS", "PILLEFFECT_ADDICTED", "PILLEFFECT_RELAX", "PILLEFFECT_QUESTIONMARK",
  "PILLEFFECT_LARGER", "PILLEFFECT_SMALLER", "PILLEFFECT_INFESTED_EXCLAMATION", "PILLEFFECT_INFESTED_QUESTION",
  "PILLEFFECT_POWER", "PILLEFFECT_RETRO_VISION", "PILLEFFECT_FRIENDS_TILL_THE_END", "PILLEFFECT_X_LAX",
  "PILLEFFECT_SOMETHINGS_WRONG", "PILLEFFECT_IM_DROWSY", "PILLEFFECT_IM_EXCITED", "PILLEFFECT_GULP",
  "PILLEFFECT_HORF", "PILLEFFECT_SUNSHINE", "PILLEFFECT_VURP", "PILLEFFECT_SHOT_SPEED_DOWN",
  "PILLEFFECT_SHOT_SPEED_UP", "PILLEFFECT_EXPERIMENTAL" })
-- Every card/rune name resolves; a few have their real ids so tests can tell them apart.
Card = setmetatable({ CARD_TOWER = 17, RUNE_HAGALAZ = 32 }, { __index = function(_, k)
  if tostring(k):match("^CARD_") or tostring(k):match("^RUNE_") then return 50 end end })
InputHook = { IS_ACTION_PRESSED = 0, IS_ACTION_TRIGGERED = 1, GET_ACTION_VALUE = 2 }
ButtonAction = { ACTION_PAUSE = 1, ACTION_MENUCONFIRM = 2, ACTION_MAP = 3, ACTION_PILLCARD = 10 }
Keyboard = { KEY_ENTER = 257 }
Input = { skip = false }
function Input.IsButtonTriggered() return Input.skip end
function Input.IsActionTriggered(action) return action == ButtonAction.ACTION_PILLCARD and W.pillPressed or false end
ChampionColor = { CHAMPION_RED = 0, CHAMPION_DARK_RED = 12, CHAMPION_TINY = 18 }

-- ---------------------------------------------------------------- vectors
local VMT = {}
VMT.__index = VMT
Vector = setmetatable({}, { __call = function(_, x, y) return setmetatable({ X = x, Y = y }, VMT) end })
function VMT:Distance(o) return math.sqrt((self.X - o.X) ^ 2 + (self.Y - o.Y) ^ 2) end
Vector.Zero = Vector(0, 0)

function KColor(r, g, b, a) return { Red = r, Green = g, Blue = b, Alpha = a } end
function Font() return { Load = function() end, IsLoaded = function() return false end, DrawStringScaled = function() end } end
function Color(r, g, b, a) return { R = r, G = g, B = b, A = a } end
Options = { HUDOffset = 0 }
-- Sprites record what they drew (W.drawn) so tests can see where; W.missingSprites[file] won't load.
W.drawn, W.missingSprites = {}, {}
function Sprite()
  local s = { Scale = nil, Color = nil }
  function s:Load(file) self.file = file end
  function s:IsLoaded() return self.file ~= nil and not W.missingSprites[self.file] end
  function s:Play(anim) self.anim = anim end
  function s:IsPlaying(anim) return self.anim == anim end
  function s:GetDefaultAnimation() return "Default" end
  function s:Render(pos) W.drawn[#W.drawn + 1] = { file = self.file, x = pos.X, y = pos.Y, a = self.Color and self.Color.A } end
  return s
end

function RNG()
  local r = { s = 1 }
  function r:SetSeed(s) self.s = s end
  function r:Next() self.s = (self.s * 1103515245 + 12345) % 2147483648; return self.s end
  function r:RandomFloat() return self:Next() / 2147483648 end
  function r:RandomInt(n) return self:Next() % n end
  function r:GetSeed() return self.s end
  return r
end

-- ------------------------------------------------------------------ world
-- 13-wide grid: start 84, default room 85 east, treasure 71 north, boss 86 east of 85.
local function desc(idx, rtype)
  return { SafeGridIndex = idx, GridIndex = idx, VisitedCount = 0, AwardSeed = 1,
           Data = { Type = rtype, Shape = 1, Difficulty = 1 } }
end
W.rooms = { [84] = desc(84, RoomType.ROOM_DEFAULT), [85] = desc(85, RoomType.ROOM_DEFAULT),
            [71] = desc(71, RoomType.ROOM_TREASURE), [86] = desc(86, RoomType.ROOM_BOSS) }
W.adj = { [84] = { 85, 71 }, [85] = { 84, 86 }, [71] = { 84 }, [86] = { 85 } }
W.cur = 84
W.enemies = {}
W.grid = { [20] = GridEntityType.GRID_ROCK, [21] = GridEntityType.GRID_ROCK, [22] = GridEntityType.GRID_ROCKT }

local player = {
  Position = Vector(300, 300), Damage = 3.5, MaxFireDelay = 10, MoveSpeed = 1, TearRange = 260,
  ShotSpeed = 1, Luck = 0, CanFly = false, QueuedItem = { Item = nil },
}
function player:GetHearts() return 6 end
function player:GetMaxHearts() return 6 end
function player:GetSoulHearts() return 0 end
function player:GetBoneHearts() return 0 end
function player:GetEternalHearts() return 0 end
function player:GetGoldenHearts() return 0 end
function player:GetBrokenHearts() return 0 end
function player:GetRottenHearts() return 0 end
function player:GetNumCoins() return W.coins or 3 end
function player:GetNumBombs() return 1 end
function player:GetNumKeys() return W.keys or 0 end
function player:HasGoldenKey() return false end
function player:HasGoldenBomb() return false end
function player:GetActiveItem() return 0 end
function player:GetTrinket() return 0 end
function player:HasTrinket() return false end
function player:GetPill(slot) return slot == 0 and (W.heldPill or 0) or 0 end
function player:GetName() return "Isaac" end
function player:GetPlayerType() return PlayerType.PLAYER_ISAAC end
function player:GetCard() return 0 end
function player:GetCollectibleCount() return 0 end
function player:HasCollectible() return false end
function player:ToPlayer() return self end
player.ControllerIndex = 0

local function makeEnemy(hp)
  local e = { HitPoints = hp, Type = 10, Variant = 0, champion = false, flags = 0, Position = Vector(300, 300) }
  function e:IsActiveEnemy() return self.HitPoints > 0 end
  function e:IsVulnerableEnemy() return true end
  function e:HasEntityFlags(f) return self.flags & f ~= 0 end
  function e:AddEntityFlags(f) self.flags = self.flags | f end
  function e:ClearEntityFlags(f) self.flags = self.flags & ~f end
  function e:Exists() return true end
  function e:ToNPC() return self end
  function e:IsBoss() return false end
  function e:IsChampion() return self.champion end
  function e:MakeChampion() self.champion = true end
  return e
end
W.makeEnemy = makeEnemy

-- Pickups (and slots) sit in W.pickups and show up in GetRoomEntities / FindByType.
local nextSeed = 1000
function W.makePickup(t, v, s, x, y)
  nextSeed = nextSeed + 1
  local e = { Type = t, Variant = v, SubType = s, Position = Vector(x or 300, y or 300), Price = 0,
              InitSeed = nextSeed, FrameCount = 0 }
  function e:IsShopItem() return false end
  function e:ToPickup() return self end
  function e:Remove() self.removed = true end
  function e:GetSprite() return { IsPlaying = function() return false end } end
  function e:IsActiveEnemy() return false end
  function e:ToNPC() return nil end
  function e:Exists() return not self.removed end
  function e:Morph(t2, v2, s2) self.Type, self.Variant, self.SubType = t2, v2, s2; self.morphed = true end
  return e
end

local room = {}
function room:GetDoor(slot)
  local t = W.adj[W.cur][slot + 1]
  if not t then return nil end
  return { TargetRoomIndex = t, IsLocked = function() return W.rooms[t].Data.Type == RoomType.ROOM_TREASURE end }
end
function room:GetGridSize() return 135 end
function room:GetGridEntity(i)
  local t = W.grid[i]
  if not t or W.cur ~= 85 then return nil end
  return { State = W.gridState[i] or 0, Position = room:GetGridPosition(i), GetType = function() return t end }
end
function room:RemoveGridEntity(i) W.removedGrid[#W.removedGrid + 1] = i; W.grid[i] = nil end
function room:GetGridPosition(i) return Vector(i * 40 % 520, math.floor(i / 13) * 40) end
function room:GetType() return W.rooms[W.cur].Data.Type end
function room:IsFirstVisit() return W.rooms[W.cur].VisitedCount == 1 end
function room:IsClear()
  for _, e in ipairs(W.enemies) do if e.HitPoints > 0 then return false end end
  return true
end
function room:GetSpawnSeed() return 12345 end
function room:FindFreePickupSpawnPosition(p) return p end
function room:FindFreeTilePosition(p) return p end
function room:GetRandomPosition() return Vector(math.random(60, 560), math.random(60, 360)) end
function room:GetCenterPos() return Vector(320, 280) end

local level = {}
function level:GetCurrentRoomDesc() return W.rooms[W.cur] end
function level:GetCurrentRoomIndex() return W.cur end
function level:GetRoomByIdx(i) return W.rooms[i] end
function level:GetStartingRoomIndex() return 84 end
W.stage = 1
local STAGE_NAMES = { "Basement I", "Basement II", "Caves I" }
function level:GetStage() return W.stage end
function level:GetStageType() return 0 end
function level:GetName() return STAGE_NAMES[W.stage] or ("stage " .. W.stage) end
function level:GetCurseName() return "" end
W.curses = 0
function level:GetCurses() return W.curses end
function level:AddCurse(c) W.curses = W.curses | c end
function level:RemoveCurses(c) W.curses = W.curses & ~c end
function level:GetRooms()
  local list = {}
  for _, d in pairs(W.rooms) do list[#list + 1] = d end
  return { Size = #list, Get = function(_, i) return list[i + 1] end }
end

local itemPool = {}
local POOL_ITEMS = { 1, 2, 3, 4, 5, 6, 7, 8 }
function itemPool:GetCollectible(pool, decrease, seed)
  if W.onPreGetCollectible then
    local r = W.onPreGetCollectible(pool, decrease, seed)
    if r then return r end
  end
  return POOL_ITEMS[(seed % #POOL_ITEMS) + 1]
end
function itemPool:RemoveCollectible(id) W.removedItem = id end
function itemPool:RemoveTrinket(id) W.removedTrinket = id end

local seeds = { GetStageSeed = function(_, stage) return 777 + (stage or 0) end, GetStartSeed = function() return 4242 end,
                GetStartSeedString = function() return "TEST 1234" end }
local game = { Difficulty = 0 }
function game:GetLevel() return level end
function game:GetRoom() return room end
function game:GetFrameCount() return W.frame end
function game:GetNumPlayers() return 1 end
function game:IsPaused() return W.paused or false end
function game:IsGreedMode() return false end
function game:GetSeeds() return seeds end
function game:GetItemPool() return itemPool end
function Game() return game end

local ITEMS = {}
for id = 1, 8 do ITEMS[id] = { Name = "#ITEM_" .. id .. "_NAME", Quality = id % 5, Type = ItemType.ITEM_PASSIVE,
                                IsAvailable = function() return id ~= 3 end } end
local TRINKETS = {}
for id = 1, 20 do TRINKETS[id] = { Name = "#TRINKET_" .. id .. "_NAME", IsAvailable = function() return id ~= 7 end } end
local itemConfig = { GetCollectible = function(_, id) return ITEMS[id] end,
                     GetCollectibles = function() return { Size = 9 } end,
                     GetTrinket = function(_, id) return TRINKETS[id] end,
                     GetTrinkets = function() return { Size = 21 } end }

Isaac = {
  GetTime = function() return W.now end,
  -- Like the game: a line in log.txt (W.logPath), where the backend reads the mod's messages.
  DebugString = function(s)
    if W.logPath then
      local f = assert(io.open(W.logPath, "a"))
      f:write("[INFO] - Lua Debug: ", s, "\n")
      f:close()
    end
    if not s:find("^%[LLMDM:") then print("   [isaac] " .. s) end
  end,
  GetPlayer = function() return player end,
  GetRoomEntities = function()
    local out = {}
    for _, e in ipairs(W.enemies) do out[#out + 1] = e end
    for _, e in ipairs(W.pickups or {}) do out[#out + 1] = e end
    return out
  end,
  GetItemConfig = function() return itemConfig end,
  FindByType = function(t, v)
    local out = {}
    for _, e in ipairs(W.pickups or {}) do
      if e.Type == t and (v == nil or v < 0 or e.Variant == v) then out[#out + 1] = e end
    end
    return out
  end,
  Spawn = function(t, v, s, pos) W.spawned[#W.spawned + 1] = { t = t, v = v, s = s }; return {} end,
  GridSpawn = function(t, v, pos) W.gridSpawned[#W.gridSpawned + 1] = t end,
  ExecuteCommand = function(c) W.lastCommand = c end,
  GetScreenWidth = function() return 480 end,
  GetScreenHeight = function() return 270 end,
  RenderText = function() end,
  RenderScaledText = function() end,
}

function RegisterMod(name)
  local m = { Name = name }
  -- The save file is real when W.savePath is set: the backend writes its messages into it.
  local function readSave()
    local f = io.open(W.savePath, "rb")
    if not f then return nil end
    local data = f:read("a")
    f:close()
    return data
  end
  function m:SaveData(s)
    W.saved = s
    if W.savePath then
      local f = assert(io.open(W.savePath, "wb"))
      f:write(s)
      f:close()
    end
  end
  function m:HasData()
    if W.savePath then return readSave() ~= nil end
    return W.saved ~= nil
  end
  function m:LoadData()
    if W.savePath then return readSave() or "" end
    return W.saved
  end
  function m:AddCallback(id, fn) W.callbacks[id] = W.callbacks[id] or {}; table.insert(W.callbacks[id], fn) end
  return m
end

function W.fire(id, ...)
  local ret
  for _, fn in ipairs(W.callbacks[id] or {}) do
    local r = fn(nil, ...)
    if r ~= nil then ret = r end
  end
  return ret
end

function W.enter(idx)
  W.cur = idx
  W.rooms[idx].VisitedCount = W.rooms[idx].VisitedCount + 1
  W.fire(ModCallbacks.MC_POST_NEW_ROOM)
end

return W
