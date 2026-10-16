-- Beggars, machines, bums and chests pay out on a schedule the DM wrote when they appeared.
--
-- Each one is asked about once (payout_plan, keyed by its seed). For a slot the answer is one outcome
-- per paid use, in order ("penny", "nothing", "key"); for a chest it is everything inside. The mod
-- spots a use (the player's coins/keys/bombs drop beside a slot) or an opening (the chest changes),
-- cancels what the game pays for it, and hands out the scheduled outcome instead - even when the
-- game would have paid nothing. With no schedule yet, or one used up, the game pays as usual.
local Payouts = {}

local game = Game()
local cfg, Bridge, State, Apply, Taunt
local itemFor              -- pool -> collectible id the DM's ranking hands out next (or nil)

local insts = {}           -- seed -> instance
local SLOT, CHEST = {}, {} -- variant -> {label, pool, hearts}
local floor = 0
local lastScan, last = -1000, nil

local WINDOW = 240         -- frames after a use/opening in which the game's own payout is cancelled
local DELIVER_AFTER = 90   -- frames to wait for the game to pay before paying the scheduled outcome
local GIVE_UP = 1800       -- frames a chest opened before its plan landed waits for it (then the game's contents return)
local NEAR_SLOT, NEAR_CHEST, NEAR_PLAYER = 130, 90, 90
local ASK_GAP_MS = 3000

local function now() return Isaac.GetTime() end
local function active() return cfg.CONTROL_PAYOUTS and Bridge.ready() and not game:IsGreedMode() end
local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end
local function keyOf(i) return "payout:" .. i.floor .. ":" .. i.seed end

function Payouts.init(config, bridge, state, apply, taunt, itemForFn, slotPools, pickupPools)
  cfg, Bridge, State, Apply, Taunt, itemFor = config, bridge, state, apply, taunt, itemForFn
  SLOT, CHEST = {}, {}
  for variant, src in pairs(slotPools) do SLOT[variant] = { label = src[2], pool = src[1], hearts = variant == 5 } end
  SLOT[1] = { label = "slot_machine" }                      -- the plain slot machine has no item pool
  for variant, src in pairs(pickupPools) do CHEST[variant] = { label = src[2], pool = src[1] } end
  if PickupVariant.PICKUP_CHEST then CHEST[PickupVariant.PICKUP_CHEST] = { label = "chest" } end
  if PickupVariant.PICKUP_GRAB_BAG then CHEST[PickupVariant.PICKUP_GRAB_BAG] = { label = "grab_bag", bag = true } end
end

function Payouts.reset()
  insts, last, lastScan = {}, nil, -1000
end

function Payouts.onNewLevel(floorNo)
  floor = floorNo
  Bridge.forget("payout:" .. (floorNo - 1))
  insts, last = {}, nil
end

-- ------------------------------------------------------------------ requests
local function ask(i, reason)
  i.lastAsk, i.sends = now(), i.sends + 1
  Bridge.request("payout_plan", keyOf(i), State.snapshot(), {
    reason = reason, source = i.label, source_class = i.class, variant = i.variant,
    has_item_pool = i.pool ~= nil, uses_so_far = i.uses,
    room_type = State.roomTypeName(game:GetRoom():GetType()),
  }, 10000)
end

-- The schedule as a queue, rebuilt when a newer answer lands.
local function refreshQueue(i)
  local res = Bridge.peek(keyOf(i))
  if res and i.resultId ~= res.id then
    i.resultId, i.queue, i.taunt = res.id, {}, res.decision.taunt
    for k, e in ipairs(res.decision.schedule or {}) do i.queue[k] = e end
  end
  return i.queue
end

-- ------------------------------------------------------------------ delivery
local function deliver(i, p, at)
  p.resolved = true
  local bits = {}
  for _, e in ipairs(p.entries) do
    local n = math.max(1, tonumber(e.count) or 1)
    if e.reward == "nothing" then
      bits[#bits + 1] = "nothing"
    elseif e.reward == "item" then
      if p.itemSeen then
        bits[#bits + 1] = "an item"
      else
        local id = i.pool and itemFor(i.pool)
        if id then
          pcall(Apply.spawnPickup, PickupVariant.PICKUP_COLLECTIBLE, id, at)
          bits[#bits + 1] = "an item"
        end
      end
    else
      local made = 0
      for _ = 1, n do
        if Apply.spawnReward(e.reward, at) then made = made + 1 end
      end
      if made > 0 then bits[#bits + 1] = Apply.count("+", made, e.reward) end
    end
  end
  local what = #bits > 0 and table.concat(bits, ", ") or "nothing"
  local use = i.class == "chest" and (i.label .. " opened") or (i.label .. " use " .. p.use)
  Bridge.effect(keyOf(i), { use .. ": " .. what .. " (scheduled)" })
end

local function markApplied(i)
  if not i.applied then
    i.applied = true
    Bridge.markApplied(keyOf(i))
    if i.taunt then Taunt.show(i.taunt) end
  end
end

-- `entries` nil: the chest opened before its plan landed. Its contents are cancelled and held
-- (p.held) until the plan arrives, then paid out in their place.
local function startPending(i, entries)
  i.pending[#i.pending + 1] = { frame = game:GetFrameCount(), entries = entries or {}, resolved = false,
                                use = i.uses, waiting = entries == nil, held = {} }
  if entries then markApplied(i) end
end

-- A waiting opening: pay the plan once it lands, or give the game's contents back if it never does.
local function settleWaiting(i, p, f)
  local q = refreshQueue(i)
  local at = p.at or i.pos
  if q and #q > 0 then
    p.entries, p.waiting, i.queue = q, false, {}
    markApplied(i)
    deliver(i, p, at)
  elseif f - p.frame >= GIVE_UP or Bridge.peek(keyOf(i)) or not Bridge.isPending(keyOf(i)) then
    -- (an answer with nothing in it, or none coming)
    p.waiting, p.resolved = false, true
    for _, h in ipairs(p.held) do pcall(Apply.spawnPickup, h.variant, h.subtype, h.pos) end
    Bridge.effect(keyOf(i), { i.label .. " opened: no plan arrived, the game's own contents put back" })
  end
end

-- ------------------------------------------------------------------ scanning
local function scan()
  local idx = curIdx()
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    local info, class
    if e.Type == EntityType.ENTITY_SLOT then info, class = SLOT[e.Variant], "slot"
    elseif e.Type == EntityType.ENTITY_PICKUP and e.SubType ~= 0 then info, class = CHEST[e.Variant], "chest" end
    if info then
      local seed = e.InitSeed
      local i = insts[seed]
      if not i then
        i = { seed = seed, floor = floor, class = class, variant = e.Variant, label = info.label, pool = info.pool,
              hearts = info.hearts, bag = info.bag, uses = 0, sends = 0, lastAsk = -1e9, pending = {}, lastUse = -100 }
        insts[seed] = i
      end
      i.ent, i.pos, i.room, i.seen = e, Vector(e.Position.X, e.Position.Y), idx, game:GetFrameCount()
    end
  end
end

local function here(i)
  return i.room == curIdx() and i.seen and game:GetFrameCount() - i.seen <= 40
end

local function chestOpening(i)
  if i.class ~= "chest" or i.opened or not i.ent then return false end
  if i.bag then   -- a sack is taken, not opened: it plays its pickup animation or is gone, with the player beside it
    if Isaac.GetPlayer(0).Position:Distance(i.pos) > NEAR_PLAYER then return false end
    if not i.ent:Exists() then return true end
    local ok, playing = pcall(function() return i.ent:GetSprite():IsPlaying("Collect") end)
    return ok and playing or false
  end
  if not i.ent:Exists() then return false end
  if i.ent.SubType == 0 then return true end
  local ok, playing = pcall(function() return i.ent:GetSprite():IsPlaying("Open") end)
  return ok and playing or false
end

local function openChest(i)
  i.opened, i.uses = true, 1
  local q = refreshQueue(i)
  if q and #q > 0 then
    startPending(i, q)
    i.queue = {}
  elseif Bridge.isPending(keyOf(i)) then
    startPending(i, nil)   -- asked, not answered yet: hold the game's contents until it is
  end
end

-- ------------------------------------------------------------------ hooks
--- MC_POST_PICKUP_INIT. True if this pickup is a payout the schedule cancelled or let through.
function Payouts.onPickupInit(pickup)
  if Apply.spawning or not active() then return false end
  local f, pos = game:GetFrameCount(), pickup.Position
  for _, i in pairs(insts) do
    if here(i) and i.pos then
      if i.class == "chest" and not i.opened and i.pos:Distance(pos) <= NEAR_CHEST and chestOpening(i) then
        openChest(i)
      end
      local radius = i.class == "chest" and NEAR_CHEST or NEAR_SLOT
      if i.pos:Distance(pos) <= radius then
        for _, p in ipairs(i.pending) do
          if p.waiting then
            if f - p.frame <= WINDOW then   -- the game's contents spawn right after the opening
              p.held[#p.held + 1] = { variant = pickup.Variant, subtype = pickup.SubType,
                                      pos = Vector(pos.X, pos.Y) }
              p.at = p.at or Vector(pos.X, pos.Y)
              pickup:Remove()
              return true
            end
          elseif f - p.frame <= WINDOW then
            local wantsItem = false
            for _, e in ipairs(p.entries) do if e.reward == "item" then wantsItem = true end end
            if pickup.Variant == PickupVariant.PICKUP_COLLECTIBLE and wantsItem then
              p.itemSeen = true
              if not p.resolved then deliver(i, p, pos) end
              return true
            end
            pickup:Remove()
            if not p.resolved then deliver(i, p, pos) end
            return true
          end
        end
      end
    end
  end
  return false
end

--- Is a scheduled machine, chest or sack standing at pos? (Its spawns are not natural drops.)
function Payouts.near(pos)
  if not active() then return false end
  for _, i in pairs(insts) do
    if here(i) and i.pos and i.pos:Distance(pos) <= (i.class == "chest" and NEAR_CHEST or NEAR_SLOT) then return true end
  end
  return false
end

--- MC_POST_PICKUP_UPDATE for a chest: its opening starts the scheduled payout.
function Payouts.onPickupUpdate(pickup)
  if not active() then return end
  local i = insts[pickup.InitSeed]
  if i and i.class == "chest" and not i.opened and chestOpening(i) then openChest(i) end
end

local function snapshot()
  local p = Isaac.GetPlayer(0)
  return { c = p:GetNumCoins(), k = p:GetNumKeys(), b = p:GetNumBombs(), h = p:GetHearts() }
end

local function spent(a, b)
  return b.c < a.c or b.k < a.k or b.b < a.b, b.h < a.h
end

--- Every frame: find machines/chests, spot uses, ask for missing schedules, pay what the game didn't.
function Payouts.update()
  if not active() then return end
  local f = game:GetFrameCount()
  if f - lastScan >= 15 then
    lastScan = f
    scan()
  end
  local player = Isaac.GetPlayer(0)
  local snap = snapshot()
  local items, hearts = false, false
  if last then items, hearts = spent(last, snap) end
  last = snap
  for _, i in pairs(insts) do
    if here(i) then
      -- Ask once it exists; again when a slot's schedule has run out.
      local q = refreshQueue(i)
      local waiting = Bridge.isPending(keyOf(i))
      if not waiting and i.sends < cfg.MAX_PAYOUT_SENDS and now() - i.lastAsk > ASK_GAP_MS then
        if i.sends == 0 then
          ask(i, i.class .. " appeared in the room")
        elseif i.class == "slot" and i.uses > 0 and (not q or #q == 0) and #i.pending == 0 then
          ask(i, "its schedule is used up")
        end
      end
      -- A use: the player paid beside this machine.
      if i.class == "slot" and (items or (hearts and i.hearts)) and f - i.lastUse > 8
          and player.Position:Distance(i.pos) <= NEAR_PLAYER then
        i.lastUse, i.uses = f, i.uses + 1
        local head = q and table.remove(q, 1)
        if head then startPending(i, { head }) end
      end
      if i.class == "chest" and chestOpening(i) then openChest(i) end
      -- The game never paid what the schedule says: pay it.
      for _, p in ipairs(i.pending) do
        if p.waiting then settleWaiting(i, p, f)
        elseif not p.resolved and f - p.frame >= DELIVER_AFTER then deliver(i, p, i.pos) end
      end
      while i.pending[1] and not i.pending[1].waiting and f - i.pending[1].frame > WINDOW do
        table.remove(i.pending, 1)
      end
    end
  end
end

return Payouts
