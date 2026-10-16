-- What a tinted rock holds, decided by the DM.
--
-- A room's layout decision can name the loot of every tinted rock in it (tinted_rock_loot): the
-- room's own rocks and the ones the DM added. The plan is kept for the rest of the floor, so a rock
-- bombed on a later visit still pays it. When a planned rock breaks, the game's loot from it is
-- cancelled and the DM's dropped in its place ("nothing" drops nothing). Rooms without a plan keep
-- the game's loot.
local Rocks = {}

local game = Game()
local Apply, Bridge

local plans = {}        -- room index -> {key, loot}: the layout decision it came from, and the loot
local rocks = {}        -- grid index -> {pos, brokenAt, paid, cancelled}: tinted rocks in this room
local roomIdx

local WINDOW, RADIUS = 6, 90   -- the game's loot lands within this many frames and units of the rock
local PAY_AFTER = 2            -- frames to let the game's loot show (so the effect can name it)

local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end

function Rocks.init(apply, bridge) Apply, Bridge = apply, bridge end

function Rocks.reset()
  plans, rocks, roomIdx = {}, {}, nil
end

--- The layout decision for room `idx` (applied under `key`) landed: remember what its rocks hold.
function Rocks.plan(idx, key, loot)
  if type(loot) == "table" and #loot > 0 then plans[idx] = { key = key, loot = loot } end
end

-- This room's plan, with its tinted rocks' breaks noted (a rock first seen broken is the past's).
local function scan()
  local idx = curIdx()
  if idx ~= roomIdx then rocks, roomIdx = {}, idx end
  local plan = plans[idx]
  if not plan then return nil end
  local f = game:GetFrameCount()
  local room = game:GetRoom()
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g and g:GetType() == GridEntityType.GRID_ROCKT then
      local r = rocks[i]
      if g.State == 2 then
        if r and not r.brokenAt then r.brokenAt = f end
      elseif not r or r.brokenAt then
        rocks[i] = { pos = Vector(g.Position.X, g.Position.Y), cancelled = {} }
      end
    end
  end
  return plan
end

local function pay(r, plan)
  r.paid = true
  local given = {}
  for _, name in ipairs(plan.loot) do
    if name ~= "nothing" and Apply.spawnReward(name, r.pos) then given[#given + 1] = Apply.count("+", 1, name) end
  end
  local line = "tinted rock bombed: " .. (#given > 0 and table.concat(given, ", ") or "nothing")
  if #r.cancelled > 0 then line = line .. " (instead of the game's " .. table.concat(r.cancelled, ", ") .. ")" end
  Bridge.effect(plan.key, { line })
end

--- MC_POST_PICKUP_INIT. True if this is a planned rock's loot from the game (cancelled).
function Rocks.onPickupInit(pickup)
  if Apply.spawning or pickup.Price ~= 0 or pickup.Variant == PickupVariant.PICKUP_COLLECTIBLE then return false end
  local sp = pickup.SpawnerEntity
  if sp and (sp.Type == EntityType.ENTITY_PLAYER or sp.Type == EntityType.ENTITY_FAMILIAR or sp:ToNPC()) then
    return false
  end
  local plan = scan()
  if not plan then return false end
  local f = game:GetFrameCount()
  for _, r in pairs(rocks) do
    if r.brokenAt and f - r.brokenAt <= WINDOW and r.pos:Distance(pickup.Position) <= RADIUS then
      local name = Apply.rewardNameFor(pickup.Variant, pickup.SubType)
      pickup:Remove()
      if r.paid then
        Bridge.effect(plan.key, { Apply.count("-", 1, name) .. " (the game's tinted rock loot, cancelled)" })
      else
        r.cancelled[#r.cancelled + 1] = Apply.count("", 1, name)
      end
      return true
    end
  end
  return false
end

--- Every frame: pay the planned loot of rocks that just broke.
function Rocks.update()
  local plan = scan()
  if not plan then return end
  local f = game:GetFrameCount()
  for _, r in pairs(rocks) do
    if r.brokenAt and not r.paid and f - r.brokenAt >= PAY_AFTER then pay(r, plan) end
  end
end

return Rocks
