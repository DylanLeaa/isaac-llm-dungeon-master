-- Turns (already clamped) DM decisions into game changes.
-- Every enum is looked up by name at runtime so a missing enum just skips that piece.
-- Each apply returns its effect: short lines saying what it actually did ("+1 bomb dropped",
-- "+2 champions (red)"), for the receipt.
local Apply = {}

local game = Game()
local Taunt -- set by Apply.init
local ZERO = Vector(0, 0)

-- True while the mod itself is spawning: the drop catcher and payout hooks must leave those alone.
Apply.spawning = false
local function spawn(...)
  Apply.spawning = true
  local ok, e = pcall(Isaac.Spawn, ...)
  Apply.spawning = false
  if not ok then error(e, 0) end
  return e
end

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

-- ------------------------------------------------------------------ effects
local function plural(name, n)
  if n == 1 then return name end
  if name:match("[^aeiou]y$") then return name:sub(1, -2) .. "ies" end
  if name:match("[sx]$") or name:match("ch$") then return name .. "es" end
  return name .. "s"
end
Apply.plural = plural

--- "+2 keys", "-1 heart": `n` of `name` (a catalog key like "soul_heart" is fine).
local function count(sign, n, name)
  return sign .. n .. " " .. plural((name:gsub("_", " ")), n)
end
Apply.count = count

-- names (in order, repeats allowed) -> "-1 key, -2 hearts"
local function tally(sign, names)
  local n, order, out = {}, {}, {}
  for _, s in ipairs(names) do
    if not n[s] then order[#order + 1], n[s] = s, 0 end
    n[s] = n[s] + 1
  end
  for _, s in ipairs(order) do out[#out + 1] = count(sign, n[s], s) end
  return table.concat(out, ", ")
end

local PICKUP_NAMES = { TAROTCARD = "card", LIL_BATTERY = "battery", GRAB_BAG = "sack", LOCKEDCHEST = "golden chest",
                       REDCHEST = "red chest", SPIKEDCHEST = "spiked chest", MIMICCHEST = "mimic chest" }
local function pickupName(p)
  for k, v in pairs(PickupVariant or {}) do
    if v == p.Variant then
      local short = k:gsub("^PICKUP_", "")
      return PICKUP_NAMES[short] or (short:gsub("_", " "):lower())
    end
  end
  return "pickup"
end

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
  spawn(EntityType.ENTITY_BOMB, v, 0, pos, ZERO, nil)
  return true
end

--- True if it spawned (false when this game version lacks the pickup).
function Apply.spawnReward(name, pos)
  local kind, variant, subtype = resolve(name)
  if kind == "trollbomb" then return spawnTrollBomb(pos) or false end
  if kind ~= "pickup" then return false end
  local room = game:GetRoom()
  local at = room:FindFreePickupSpawnPosition(pos, 0, true)
  spawn(EntityType.ENTITY_PICKUP, variant, subtype, at, ZERO, nil)
  return true
end

--- Put back a pickup the drop catcher swallowed (or hand out a pool item): exact variant and subtype.
function Apply.spawnPickup(variant, subtype, pos)
  local at = game:GetRoom():FindFreePickupSpawnPosition(pos, 0, true)
  spawn(EntityType.ENTITY_PICKUP, variant, subtype, at, ZERO, nil)
end

-- What the game would have dropped, named like the DM's reward menu. Exact match with a REWARDS
-- entry first; anything else gets its pickup family ("heart", "coin", ...).
local byKind
local function rewardIndex()
  if byKind then return byKind end
  byKind = {}
  for name in pairs(REWARDS) do
    local kind, variant, subtype = resolve(name)
    if kind == "pickup" and subtype ~= 0 then byKind[variant .. ":" .. subtype] = name end
  end
  return byKind
end

local FAMILY_OF = {}
for _, n in ipairs({ "penny", "double_penny", "nickel", "dime", "lucky_penny", "sticky_nickel" }) do FAMILY_OF[n] = "coin" end
for _, n in ipairs({ "bomb", "double_bomb", "golden_bomb" }) do FAMILY_OF[n] = "bomb" end
for _, n in ipairs({ "key", "double_key", "golden_key", "charged_key" }) do FAMILY_OF[n] = "key" end
for _, n in ipairs({ "red_heart", "half_heart", "soul_heart", "black_heart", "eternal_heart" }) do FAMILY_OF[n] = "heart" end
local function family(name) return FAMILY_OF[name] or name end

local VARIANT_LABEL = { PICKUP_HEART = "heart", PICKUP_COIN = "coin", PICKUP_KEY = "key", PICKUP_BOMB = "bomb",
                        PICKUP_TAROTCARD = "card", PICKUP_PILL = "pill", PICKUP_TRINKET = "trinket",
                        PICKUP_LIL_BATTERY = "battery", PICKUP_GRAB_BAG = "grab_bag" }
function Apply.rewardNameFor(variant, subtype)
  local exact = rewardIndex()[variant .. ":" .. subtype]
  if exact then return exact end
  for k, label in pairs(VARIANT_LABEL) do
    if PickupVariant[k] == variant then return label end
  end
  return "pickup"
end

-- What the DM's rewards leave uncovered of the drops the game would have made: a heart of any kind
-- replaces a heart, a coin a coin. {name = n}, a list of lines like "-1 key (...)".
local function shortfall(suppressed, rewards)
  local have, names, out = {}, {}, {}
  for _, r in ipairs(rewards or {}) do
    local f = family(r.reward)
    have[f] = (have[f] or 0) + math.max(1, math.min(4, tonumber(r.count) or 1))
  end
  for name in pairs(suppressed) do names[#names + 1] = name end
  table.sort(names)
  for _, name in ipairs(names) do
    local f, lost = family(name), suppressed[name]
    local covered = math.min(lost, have[f] or 0)
    have[f] = (have[f] or 0) - covered
    lost = lost - covered
    if lost > 0 then out[#out + 1] = count("-", lost, name) .. " (would have dropped, not replaced)" end
  end
  return out
end

-- --------------------------------------------------------------- room clear
function Apply.roomClear(decision, pos, suppressed)
  local effect = {}
  for _, r in ipairs(decision.rewards or {}) do
    local n = 0
    for _ = 1, math.max(1, math.min(4, tonumber(r.count) or 1)) do
      if Apply.spawnReward(r.reward, pos) then n = n + 1 end
    end
    if n > 0 then effect[#effect + 1] = count("+", n, r.reward) .. " dropped" end
  end
  Taunt.show(decision.taunt)
  if #effect == 0 then effect[1] = "nothing dropped" end
  for _, line in ipairs(shortfall(suppressed or {}, decision.rewards)) do effect[#effect + 1] = line end
  return effect
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
  if #list == 0 then return "no layout pickups to change (" .. mode .. ")" end
  local names = {}
  for i, p in ipairs(list) do names[i] = pickupName(p) end
  if mode == "remove" then
    for _, p in ipairs(list) do p:Remove() end
    return tally("-", names) .. " (layout pickups removed)"
  elseif MORPH[mode] then
    local _, variant, subtype = resolve(MORPH[mode])
    if variant then
      for _, p in ipairs(list) do p:Morph(EntityType.ENTITY_PICKUP, variant, subtype, true) end
      return tally("-", names) .. " -> " .. count("+", #list, MORPH[mode]) .. " (layout pickups)"
    end
  elseif mode == "troll_bombs" then
    -- Swap the pickup furthest from the door the player came in through.
    table.sort(list, function(a, b)
      local pp = Isaac.GetPlayer(0).Position
      return a.Position:Distance(pp) > b.Position:Distance(pp)
    end)
    local victim = list[1]
    local pos, name = victim.Position, pickupName(victim)
    victim:Remove()
    spawnTrollBomb(pos)
    return count("-", 1, name) .. " -> +1 troll bomb (layout pickup)"
  end
end

local function rocks(decision, rng)
  local room = game:GetRoom()
  local ROCK, TINTED = GridEntityType.GRID_ROCK, GridEntityType.GRID_ROCKT
  local plain, hidden, out = {}, 0, {}
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g then
      local t = g:GetType()
      if t == TINTED and decision.hide_tinted_rocks then
        queueGridSwap(i, ROCK)
        hidden = hidden + 1
      elseif t == ROCK then
        plain[#plain + 1] = i
      end
    end
  end
  local want = tonumber(decision.tinted_rocks) or 0
  local n = math.min(want, #plain)
  for _ = 1, n do
    local pick = table.remove(plain, rng:RandomInt(#plain) + 1)
    queueGridSwap(pick, TINTED)
  end
  if want > 0 then out[#out + 1] = count("+", n, "tinted_rock") .. (n < want and " (not enough rocks)" or "") end
  if hidden > 0 then out[#out + 1] = count("", hidden, "tinted_rock") .. " hidden" end
  return out
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
  local chance = tonumber(decision.champion_chance) or 0
  local extras = decision.extra_enemies or {}
  if alive == 0 or room:IsClear() then                -- never close doors on an empty room
    if chance > 0 or #extras > 0 then return { "no enemies: champions and extra enemies skipped" } end
    return {}
  end

  local out = {}
  local color = decision.champion_enum and E("ChampionColor", decision.champion_enum) or -1
  if chance > 0 then
    local made = 0
    for _, npc in ipairs(npcs) do
      if not npc:IsBoss() and not npc:IsChampion() and rng:RandomFloat() < chance then
        if pcall(function() npc:MakeChampion(rng:Next(), color, false) end) then made = made + 1 end
      end
    end
    out[#out + 1] = count("+", made, "champion") .. " (" .. tostring(decision.champion_color or "random") .. ", "
                    .. math.floor(chance * 100 + 0.5) .. "% of " .. alive .. ")"
  end

  for _, spec in ipairs(extras) do
    local etype = EntityType[spec.etype or ""]
    if etype then
      local n = math.max(1, math.min(3, tonumber(spec.count) or 1))
      for _ = 1, n do
        spawn(etype, tonumber(spec.variant) or 0, 0, randomFreePos(160), ZERO, nil)
      end
      out[#out + 1] = count("+", n, spec.key or spec.etype) .. " spawned"
    end
  end
  return out
end

--- late: frames since the player walked in, when the layout lands after (the game held for it).
function Apply.roomLayout(decision, late)
  local room = game:GetRoom()
  local rng = RNG()
  local seed = room:GetSpawnSeed()
  rng:SetSeed(seed ~= 0 and seed or 1, 35)
  local effect = {}
  local function add(ok, lines)
    if not ok then return end
    if type(lines) == "string" then lines = { lines } end
    for _, line in ipairs(lines or {}) do effect[#effect + 1] = line end
  end
  add(pcall(layoutPickups, decision.layout_pickups, rng, late or 0))
  add(pcall(rocks, decision, rng))
  add(pcall(enemies, decision, rng))
  for _, name in ipairs(decision.bonus_pickups or {}) do
    if Apply.spawnReward(name, randomFreePos(100)) then effect[#effect + 1] = count("+", 1, name) .. " placed" end
  end
  Taunt.show(decision.taunt)
  if #effect == 0 then effect[1] = "no change" end
  return effect
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

local function curseName(bit)
  for k, v in pairs(LevelCurse or {}) do
    if v == bit then return (k:gsub("^CURSE_OF_THE_", ""):gsub("^CURSE_OF_", ""):gsub("_", " "):lower()) end
  end
  for name, b in pairs(CURSE_FALLBACK) do if b == bit then return name end end
  return "curse " .. bit
end

--- What changing the curse mask from `before` (the game's roll) to `after` did.
function Apply.curseEffect(before, after)
  local out = {}
  before, after = math.floor(before or 0), math.floor(after or 0)
  for i = 0, 30 do
    local bit = 1 << i
    if after & bit ~= 0 and before & bit == 0 then
      out[#out + 1] = "+curse of " .. curseName(bit)
    elseif before & bit ~= 0 and after & bit == 0 then
      out[#out + 1] = "-curse of " .. curseName(bit) .. " (the game's, stripped)"
    end
  end
  if #out == 0 then out[1] = before == 0 and "no curses" or "curses unchanged" end
  return out
end

--- The same plan, on a floor that already exists (its plan landed after it was generated). Returns its effect.
function Apply.cursesNow(decision)
  local level = game:GetLevel()
  local cur = math.floor(level:GetCurses())
  local want = Apply.curses(decision, cur)
  local off, on = cur & ~want, want & ~cur
  if off ~= 0 then pcall(function() level:RemoveCurses(off) end) end
  if on ~= 0 then pcall(function() level:AddCurse(on, false) end) end
  return Apply.curseEffect(cur, want)
end

function Apply.init(taunt) Taunt = taunt end

return Apply
