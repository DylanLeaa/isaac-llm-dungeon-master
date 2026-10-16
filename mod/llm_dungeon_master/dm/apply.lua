-- Turns (already clamped) DM decisions into game changes.
-- Every enum is looked up by name at runtime so a missing enum just skips that piece.
local Apply = {}

local game = Game()
local Taunt -- set by Apply.init
local ZERO = Vector(0, 0)

local function E(tbl, name)
  local t = _G[tbl]
  return t and t[name]
end

-- reward name -> {kind, variant, subtype}; kind "pickup" or "trollbomb"
local REWARDS = {
  penny         = { "pickup", "PICKUP_COIN", "CoinSubType", "COIN_PENNY" },
  double_penny  = { "pickup", "PICKUP_COIN", "CoinSubType", "COIN_DOUBLEPACK" },
  nickel        = { "pickup", "PICKUP_COIN", "CoinSubType", "COIN_NICKEL" },
  dime          = { "pickup", "PICKUP_COIN", "CoinSubType", "COIN_DIME" },
  lucky_penny   = { "pickup", "PICKUP_COIN", "CoinSubType", "COIN_LUCKYPENNY" },
  sticky_nickel = { "pickup", "PICKUP_COIN", "CoinSubType", "COIN_STICKYNICKEL" },
  bomb          = { "pickup", "PICKUP_BOMB", "BombSubType", "BOMB_NORMAL" },
  double_bomb   = { "pickup", "PICKUP_BOMB", "BombSubType", "BOMB_DOUBLEPACK" },
  golden_bomb   = { "pickup", "PICKUP_BOMB", "BombSubType", "BOMB_GOLDEN" },
  troll_bomb    = { "trollbomb" },
  key           = { "pickup", "PICKUP_KEY", "KeySubType", "KEY_NORMAL" },
  double_key    = { "pickup", "PICKUP_KEY", "KeySubType", "KEY_DOUBLEPACK" },
  golden_key    = { "pickup", "PICKUP_KEY", "KeySubType", "KEY_GOLDEN" },
  charged_key   = { "pickup", "PICKUP_KEY", "KeySubType", "KEY_CHARGED" },
  red_heart     = { "pickup", "PICKUP_HEART", "HeartSubType", "HEART_FULL" },
  half_heart    = { "pickup", "PICKUP_HEART", "HeartSubType", "HEART_HALF" },
  soul_heart    = { "pickup", "PICKUP_HEART", "HeartSubType", "HEART_SOUL" },
  black_heart   = { "pickup", "PICKUP_HEART", "HeartSubType", "HEART_BLACK" },
  eternal_heart = { "pickup", "PICKUP_HEART", "HeartSubType", "HEART_ETERNAL" },
  chest         = { "pickup", "PICKUP_CHEST", "ChestSubType", "CHEST_CLOSED" },
  locked_chest  = { "pickup", "PICKUP_LOCKEDCHEST", "ChestSubType", "CHEST_CLOSED" },
  red_chest     = { "pickup", "PICKUP_REDCHEST", "ChestSubType", "CHEST_CLOSED" },
  spiked_chest  = { "pickup", "PICKUP_SPIKEDCHEST", "ChestSubType", "CHEST_CLOSED" },
  mimic_chest   = { "pickup", "PICKUP_MIMICCHEST", "ChestSubType", "CHEST_CLOSED" },
  grab_bag      = { "pickup", "PICKUP_GRAB_BAG", "SackSubType", "SACK_NORMAL" },
  pill          = { "pickup", "PICKUP_PILL" },        -- subtype 0 = random
  card          = { "pickup", "PICKUP_TAROTCARD" },
  trinket       = { "pickup", "PICKUP_TRINKET" },
  battery       = { "pickup", "PICKUP_LIL_BATTERY", "BatterySubType", "BATTERY_NORMAL" },
}

-- layout_pickups mode -> what every layout pickup becomes
local MORPH = { to_pennies = "penny", to_bombs = "bomb", to_keys = "key", to_hearts = "half_heart" }

local function resolve(name)
  local r = REWARDS[name]
  if not r then return nil end
  if r[1] == "trollbomb" then return "trollbomb" end
  local variant = E("PickupVariant", r[2])
  if not variant then return nil end
  local subtype = r[3] and E(r[3], r[4]) or 0
  return "pickup", variant, subtype or 0
end

local function farFromPlayer(pos, minDist)
  local p = Isaac.GetPlayer(0)
  return p.Position:Distance(pos) >= minDist
end

local function randomFreePos(minDistFromPlayer)
  local room = game:GetRoom()
  for _ = 1, 12 do
    local pos = room:FindFreeTilePosition(room:GetRandomPosition(40), 0)
    if farFromPlayer(pos, minDistFromPlayer or 120) then return pos end
  end
  return room:FindFreeTilePosition(room:GetCenterPos(), 0)
end

local function spawnTrollBomb(pos)
  local v = E("BombVariant", "BOMB_TROLL")
  if not v then return end
  if not farFromPlayer(pos, 90) then pos = randomFreePos(140) end
  Isaac.Spawn(EntityType.ENTITY_BOMB, v, 0, pos, ZERO, nil)
end

function Apply.spawnReward(name, pos)
  local kind, variant, subtype = resolve(name)
  if kind == "trollbomb" then return spawnTrollBomb(pos) end
  if kind ~= "pickup" then return end
  local room = game:GetRoom()
  local at = room:FindFreePickupSpawnPosition(pos, 0, true)
  Isaac.Spawn(EntityType.ENTITY_PICKUP, variant, subtype, at, ZERO, nil)
end

-- --------------------------------------------------------------- room clear
function Apply.roomClear(decision, pos)
  for _, r in ipairs(decision.rewards or {}) do
    for _ = 1, math.max(1, math.min(4, tonumber(r.count) or 1)) do
      Apply.spawnReward(r.reward, pos)
    end
  end
  Taunt.show(decision.taunt)
end

-- -------------------------------------------------------------- room layout
-- Grid entities can't be respawned on a tile in the same frame they were removed,
-- so conversions are queued and finished on the next update.
local gridQueue = {}

local function queueGridSwap(idx, newType)
  local room = game:GetRoom()
  room:RemoveGridEntity(idx, 0, false)
  gridQueue[#gridQueue + 1] = { idx = idx, type = newType, room = game:GetLevel():GetCurrentRoomIndex() }
end

function Apply.update()
  if #gridQueue == 0 then return end
  local room = game:GetRoom()
  local here = game:GetLevel():GetCurrentRoomIndex()
  for _, q in ipairs(gridQueue) do
    if q.room == here and not room:GetGridEntity(q.idx) then
      Isaac.GridSpawn(q.type, 0, room:GetGridPosition(q.idx), true)
    end
  end
  gridQueue = {}
end

-- The room's own pickups: spawned with it, so younger than 5 frames plus however late the layout is.
local function layoutPickups(mode, rng, late)
  if mode == nil or mode == "keep" then return end
  local list = {}
  for _, e in ipairs(Isaac.FindByType(EntityType.ENTITY_PICKUP)) do
    local p = e:ToPickup()
    if p and p.Variant ~= PickupVariant.PICKUP_COLLECTIBLE and p.Price == 0
        and not p:IsShopItem() and p.FrameCount < 5 + late then
      list[#list + 1] = p
    end
  end
  if mode == "remove" then
    for _, p in ipairs(list) do p:Remove() end
  elseif MORPH[mode] then
    local _, variant, subtype = resolve(MORPH[mode])
    if variant then
      for _, p in ipairs(list) do p:Morph(EntityType.ENTITY_PICKUP, variant, subtype, true) end
    end
  elseif mode == "troll_bombs" and #list > 0 then
    -- Swap the pickup furthest from the door the player came in through.
    table.sort(list, function(a, b)
      local pp = Isaac.GetPlayer(0).Position
      return a.Position:Distance(pp) > b.Position:Distance(pp)
    end)
    local victim = list[1]
    local pos = victim.Position
    victim:Remove()
    spawnTrollBomb(pos)
  end
end

local function rocks(decision, rng)
  local room = game:GetRoom()
  local ROCK, TINTED = GridEntityType.GRID_ROCK, GridEntityType.GRID_ROCKT
  local plain = {}
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g then
      local t = g:GetType()
      if t == TINTED and decision.hide_tinted_rocks then
        queueGridSwap(i, ROCK)
      elseif t == ROCK then
        plain[#plain + 1] = i
      end
    end
  end
  local n = math.min(tonumber(decision.tinted_rocks) or 0, #plain)
  for _ = 1, n do
    local pick = table.remove(plain, rng:RandomInt(#plain) + 1)
    queueGridSwap(pick, TINTED)
  end
end

local function enemies(decision, rng)
  local room = game:GetRoom()
  local alive = 0
  local npcs = {}
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    local npc = e:ToNPC()
    if npc and npc:IsActiveEnemy(false) and npc:IsVulnerableEnemy() then
      alive = alive + 1
      npcs[#npcs + 1] = npc
    end
  end
  if alive == 0 or room:IsClear() then return end   -- never close doors on an empty room

  local chance = tonumber(decision.champion_chance) or 0
  local color = decision.champion_enum and E("ChampionColor", decision.champion_enum) or -1
  if chance > 0 then
    for _, npc in ipairs(npcs) do
      if not npc:IsBoss() and not npc:IsChampion() and rng:RandomFloat() < chance then
        pcall(function() npc:MakeChampion(rng:Next(), color, false) end)
      end
    end
  end

  for _, spec in ipairs(decision.extra_enemies or {}) do
    local etype = EntityType[spec.etype or ""]
    if etype then
      for _ = 1, math.max(1, math.min(3, tonumber(spec.count) or 1)) do
        Isaac.Spawn(etype, tonumber(spec.variant) or 0, 0, randomFreePos(160), ZERO, nil)
      end
    end
  end
end

--- late: frames since the player walked in, when the layout lands after (the game held for it).
function Apply.roomLayout(decision, late)
  local room = game:GetRoom()
  local rng = RNG()
  local seed = room:GetSpawnSeed()
  rng:SetSeed(seed ~= 0 and seed or 1, 35)
  pcall(layoutPickups, decision.layout_pickups, rng, late or 0)
  pcall(rocks, decision, rng)
  pcall(enemies, decision, rng)
  for _, name in ipairs(decision.bonus_pickups or {}) do
    Apply.spawnReward(name, randomFreePos(100))
  end
  Taunt.show(decision.taunt)
end

-- -------------------------------------------------------------- floor curses
local CURSE_BITS = {
  darkness = "CURSE_OF_DARKNESS", lost = "CURSE_OF_THE_LOST", unknown = "CURSE_OF_THE_UNKNOWN",
  maze = "CURSE_OF_MAZE", blind = "CURSE_OF_BLIND",
}
local CURSE_FALLBACK = { darkness = 1, lost = 4, unknown = 8, maze = 32, blind = 64 }

function Apply.curses(decision, curses)
  local mask = decision.strip_vanilla_curses and 0 or curses
  for _, c in ipairs(decision.add_curses or {}) do
    local bit = E("LevelCurse", CURSE_BITS[c] or "") or CURSE_FALLBACK[c]
    if bit then mask = mask | bit end
  end
  return math.floor(mask)
end

--- The same plan, on a floor that already exists (its plan landed after it was generated).
function Apply.cursesNow(decision)
  local level = game:GetLevel()
  local cur = math.floor(level:GetCurses())
  local want = Apply.curses(decision, cur)
  local off, on = cur & ~want, want & ~cur
  if off ~= 0 then pcall(function() level:RemoveCurses(off) end) end
  if on ~= 0 then pcall(function() level:AddCurse(on, false) end) end
end

function Apply.init(taunt) Taunt = taunt end

return Apply
