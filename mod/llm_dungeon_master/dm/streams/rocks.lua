-- rocks:<f>  What the floor's tinted rocks hold, decided by the DM.
--
-- One deck for the whole floor: the DM writes what the next tinted rocks hold, in the order they are
-- broken, in whichever room that happens (ROCKS_DECK entries). The roll is the rock breaking (causes.lua):
-- it draws the deck's next entry, the game's own loot from it is cancelled at its spawn and the entry is
-- paid where the rock stood ("nothing" pays nothing; "small_rock" is the Small Rock item on a pedestal,
-- if the player has unlocked it). Asked at floor start and kept stocked while the player stands in a
-- room with a tinted rock. A rock broken with the deck empty is owed: the game's loot is cancelled and
-- the game freezes (decks.lua's IOU) until the DM answers; only if that freeze times out is the game's
-- loot put back. (GRID_ROCK_SS is left to the drops deck, as a breakable.)
local Rocks = {}

local game = Game()
local D, cfg, Bridge, Apply, Decks, Rolls, Causes

local toPay = {}          -- breaks drawn (or answered late): paid once the game's loot has shown (and been cancelled)
local PAY_AFTER = 2       -- frames
local floorAsked = -1     -- the floor whose deck was asked for at its start
local tintedHere = 0      -- tinted rocks standing in this room, rescanned about once a second
local lastScan = -1e9

local function floorNo() return D.Sched.floor() end
local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end
local function on() return cfg.CONTROL_DROPS and Bridge.ready() and not game:IsGreedMode() end

local function scanTinted()
  local room, n = game:GetRoom(), 0
  local ROCKT = GridEntityType.GRID_ROCKT
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g and g:GetType() == ROCKT and g.State ~= 2 then n = n + 1 end
  end
  return n
end

--- The Small Rock's id, if this game has it and the player can be given it (unlocked, not held).
local function smallRock()
  local id = CollectibleType and CollectibleType.COLLECTIBLE_SMALL_ROCK
  if not id then return nil end
  local item = Isaac.GetItemConfig():GetCollectible(id)
  if not item then return nil end
  local ok, avail = pcall(function() return item:IsAvailable() end)
  if ok and avail == false then return nil end
  if Isaac.GetPlayer(0):HasCollectible(id, true) then return nil end
  return id
end

function Rocks.init(deps)
  D, cfg, Bridge, Apply = deps, deps.cfg, deps.Bridge, deps.Apply
  Decks, Rolls, Causes = deps.Decks, deps.Rolls, deps.Causes
  Decks.define("rocks", {
    kind = "rocks",
    key = function(f) return "rocks:" .. f end,
    entries = function(d)
      local out = {}
      for i, o in ipairs(d.outcomes or {}) do out[i] = { reward = o.reward, count = o.count or 1 } end
      return out
    end,
    context = function()
      return { tinted_rocks_here = tintedHere, room_type = D.Sched.room() and D.State.roomTypeName(D.Sched.room().type) or nil }
    end,
    wanted = function() return cfg.ROCKS_DECK end,
    wants = function()
      if not on() or tintedHere == 0 then return {} end
      return { { arg = floorNo(), n = 2, reason = "restock: a tinted rock is in the room and the deck is running out" } }
    end,
    refresh = { max = 1, minMs = cfg.REFRESH_MIN_MS },
    maxSends = cfg.MAX_ROCK_SENDS,
    askGapMs = cfg.REFRESH_MIN_MS,
    holdLabel = "what's in the rock",
    mercy = function(e)
      local name = Decks.mercyReward(e.reward)
      if name ~= e.reward then return { reward = name, count = 1 } end
      return e
    end,
    outcome = function(e) return { reward = e.reward, count = e.count } end,
    enabled = function() return cfg.CONTROL_DROPS end,
  })

  -- The roll: a tinted rock breaking. Listens before the drops deck, which takes the other breakables.
  Causes.on("rock_break", function(b)
    if b.owner or b.gridType ~= GridEntityType.GRID_ROCKT or not on() then return end
    b.owner, b.cancelled, b.room = "rocks", {}, curIdx()
    local e = Decks.draw("rocks", floorNo(), { cause = "rock_break" })
    if e then
      b.entry = e
      toPay[#toPay + 1] = b
      Rolls.count("break", "rock_break", "rocks")
      return
    end
    local owed = Decks.owe("rocks", floorNo(), { cause = "rock_break" }, function(entry, gaveUp)
      if entry then
        b.entry = entry
        toPay[#toPay + 1] = b
      elseif gaveUp then
        for _, name in ipairs(b.cancelled) do Apply.spawnReward(name, b.pos) end   -- the game's own, after all
      end
    end)
    if owed then Rolls.count("break", "rock_break", "rocks") else b.owner = nil end
  end)

  Rolls.claimSpawns("rocks", function(info)
    local b = info.source
    if info.cause ~= "rock_break" or not b or b.owner ~= "rocks" then return nil end
    if info.variant == PickupVariant.PICKUP_COLLECTIBLE then return nil end
    b.cancelled[#b.cancelled + 1] = Apply.rewardNameFor(info.variant, info.subtype)
    return "cancel"
  end)
end

local function pay(b)
  local e, what = b.entry, "nothing"
  if e.reward == "small_rock" then
    local id = smallRock()
    if id and pcall(Apply.spawnPickup, PickupVariant.PICKUP_COLLECTIBLE, id, b.pos) then
      what = "the Small Rock"
    else
      what = "nothing (the Small Rock is unavailable)"
    end
  elseif e.reward ~= "nothing" then
    local made = 0
    for _ = 1, math.max(1, math.min(2, tonumber(e.count) or 1)) do
      if Apply.spawnReward(e.reward, b.pos) then made = made + 1 end
    end
    what = made > 0 and Apply.count("+", made, e.reward) or ("nothing (" .. e.reward .. " unavailable)")
  end
  local line = "tinted rock bombed: " .. what
  if #b.cancelled > 0 then line = line .. " (instead of the game's " .. table.concat(b.cancelled, ", ") .. ")" end
  Bridge.effect(Decks.get("rocks", floorNo()).key, { line })
end

--- Every frame: pay the entries of rocks that just broke, and keep the deck asked for.
function Rocks.update()
  local t = Isaac.GetTime()
  if t - lastScan >= 1000 then
    lastScan = t
    tintedHere = scanTinted()
  end
  if on() and floorAsked ~= floorNo() then
    floorAsked = floorNo()
    Decks.request("rocks", floorNo(), "floor start")
  end
  if #toPay == 0 then return end
  local f, rest = game:GetFrameCount(), {}
  for _, b in ipairs(toPay) do
    if f - b.frame < PAY_AFTER then
      rest[#rest + 1] = b
    elseif curIdx() == b.room then                 -- (left the room that instant: nowhere to pay it)
      pay(b)
    end
  end
  toPay = rest
end

function Rocks.onNewRoom() toPay, lastScan = {}, -1e9 end
function Rocks.onNewFloor() toPay, tintedHere, lastScan = {}, 0, -1e9 end
function Rocks.resetRun() toPay, floorAsked, tintedHere, lastScan = {}, -1, 0, -1e9 end

return Rocks
