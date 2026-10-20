-- rock:<f>:<room>  What a tinted rock holds, decided by the DM.
--
-- A room's layout decision can name the loot of every tinted rock in it (tinted_rock_loot): the room's
-- own rocks and the ones the DM added. That is this room's sticky deck, kept for the rest of the floor, so a
-- rock bombed on a later visit still pays it. The roll is the rock breaking (causes.lua): the game's loot
-- from it is cancelled at its spawn and the DM's dropped in its place ("nothing" drops nothing). A room
-- with no plan leaves its rocks to the drops deck, as breakables.
local Rocks = {}

local game = Game()
local D, Bridge, Apply, Decks, Rolls, Causes

local plans = {}        -- room index -> {key (the layout's), loot, id}
local toPay = {}        -- breaks of planned rocks, paid once the game's loot has shown (and been cancelled)
local nextId = 0
local PAY_AFTER = 2

local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end

function Rocks.init(deps)
  D, Bridge, Apply, Decks, Rolls, Causes = deps, deps.Bridge, deps.Apply, deps.Decks, deps.Rolls, deps.Causes
  Decks.define("rock", {
    key = function(idx) return "rock:" .. D.Sched.floor() .. ":" .. idx end,
    result = function(st)                          -- filled by the room's layout, not asked for
      local p = plans[st.arg]
      return p and { id = p.id, decision = { loot = p.loot } } or nil
    end,
    entries = function(d) return { d.loot } end,
    sticky = true,
    mercy = function(loot)
      local out = {}
      for i, name in ipairs(loot) do out[i] = (Decks.mercyReward(name)) end
      return out
    end,
    outcome = function(loot)
      local rewards = {}
      for _, name in ipairs(loot) do
        if name ~= "nothing" then rewards[#rewards + 1] = { reward = name, count = 1 } end
      end
      return { rewards = rewards }
    end,
  })

  -- The roll: a planned tinted rock breaking. Listens before the drops deck, which takes unplanned ones.
  Causes.on("rock_break", function(b)
    local plan = plans[curIdx()]
    if not plan or b.owner then return end
    local loot = Decks.draw("rock", curIdx(), { cause = "rock_break" })
    if not loot then return end
    b.owner, b.loot, b.plan, b.cancelled, b.room = "rocks", loot, plan, {}, curIdx()
    toPay[#toPay + 1] = b
    Rolls.count("break", "rock_break", "rocks")
  end)

  Rolls.claimSpawns("rocks", function(info)
    local b = info.source
    if info.cause ~= "rock_break" or not b or b.owner ~= "rocks" then return nil end
    if info.variant == PickupVariant.PICKUP_COLLECTIBLE then return nil end
    local name = Apply.rewardNameFor(info.variant, info.subtype)
    if b.paid then
      Bridge.effect(b.plan.key, { Apply.count("-", 1, name) .. " (the game's tinted rock loot, cancelled)" })
    else
      b.cancelled[#b.cancelled + 1] = Apply.count("", 1, name)
    end
    return "cancel"
  end)
end

--- The layout decision for room `idx` (applied under `key`) landed: remember what its rocks hold.
function Rocks.plan(idx, key, loot)
  if type(loot) == "table" and #loot > 0 then
    nextId = nextId + 1
    local list = {}
    for i, name in ipairs(loot) do list[i] = name end
    plans[idx] = { key = key, loot = list, id = nextId }
  end
end

local function pay(b)
  b.paid = true
  local given = {}
  for _, name in ipairs(b.loot) do
    if name ~= "nothing" and Apply.spawnReward(name, b.pos) then given[#given + 1] = Apply.count("+", 1, name) end
  end
  local line = "tinted rock bombed: " .. (#given > 0 and table.concat(given, ", ") or "nothing")
  if #b.cancelled > 0 then line = line .. " (instead of the game's " .. table.concat(b.cancelled, ", ") .. ")" end
  Bridge.effect(b.plan.key, { line })
end

--- Every frame: pay the planned loot of rocks that just broke.
function Rocks.update()
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

function Rocks.onNewFloor() plans, toPay = {}, {} end
function Rocks.resetRun() plans, toPay = {}, {} end

return Rocks
