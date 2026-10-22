-- payout:<f>:<seed>  What each beggar, machine or bum use pays, and what each chest or sack holds.
--
-- One deck per instance, asked when it is first in the player's room: one entry per paid use (about 6),
-- or a chest's contents. The roll is the payment, not the payout: causes.lua sees the player's coins,
-- keys, bombs or hearts drop beside the machine (a dud pays nothing, so the payment is the only moment
-- always seen), and that use draws the next entry. A chest's roll is its opening, and draws everything.
-- The game's own payout (its spawns from that machine, or beside it within the payout window) is
-- cancelled, and the entry is paid at the machine instead, even "nothing", even when the game wouldn't pay.
-- An "item" comes from that machine's own pool deck; a card or trinket from theirs.
--
-- Refilled when one entry is left and the machine is still in use (the request carries uses_so_far):
-- a schedule is a story, so it is only extended, never rewritten. Empty: a slot pays what the game
-- rolled (reported uncontrolled); a chest holds its contents (the game waits) until its plan lands.
-- Left to the game, and reported: a machine blown up, a beggar leaving, the fortune teller, blood
-- donation and the confessional.
local Payouts = {}

local game = Game()
local D, cfg, Bridge, State, Apply, Decks, Rolls, Causes

local DELIVER_AFTER = 90   -- frames to wait for the game to pay before paying the scheduled outcome
local ASK_GAP_MS = 3000

local function active() return cfg.CONTROL_PAYOUTS and Bridge.ready() and not game:IsGreedMode() end
local function keyOf(m) return Decks.get("payout", m.seed).key end

--- Is this machine's payout window the schedule's? (Its pedestal is then the schedule's, not the pool's.)
function Payouts.owns(m) return m ~= nil and m.window ~= nil end

local function deliver(m, w, at)
  w.paid = true
  local bits = {}
  for _, e in ipairs(w.entries or {}) do
    local n = math.max(1, tonumber(e.count) or 1)
    if e.reward == "nothing" then
      bits[#bits + 1] = "nothing"
    elseif e.reward == "item" then
      local id = m.pool and D.Items.next(m.pool)
      if id and pcall(Apply.spawnPickup, PickupVariant.PICKUP_COLLECTIBLE, id, at) then bits[#bits + 1] = "an item" end
    else
      local made = 0
      for _ = 1, n do
        if Apply.spawnReward(e.reward, at) then made = made + 1 end
      end
      if made > 0 then bits[#bits + 1] = Apply.count("+", made, e.reward) end
    end
  end
  local what = #bits > 0 and table.concat(bits, ", ") or "nothing"
  local use = m.class == "chest" and (m.label .. " opened") or (m.label .. " use " .. w.use)
  Bridge.effect(keyOf(m), { use .. ": " .. what .. " (scheduled)" })
end

local function drawRest(m, list)
  while true do
    local e = Decks.draw("payout", m.seed, { cause = "chest_open" })
    if not e then return list end
    list[#list + 1] = e
  end
end

function Payouts.init(deps)
  D, cfg, Bridge, State, Apply = deps, deps.cfg, deps.Bridge, deps.State, deps.Apply
  Decks, Rolls, Causes = deps.Decks, deps.Rolls, deps.Causes
  Decks.define("payout", {
    kind = "payout_plan",
    key = function(seed) return "payout:" .. D.Sched.floor() .. ":" .. seed end,
    entries = function(d)
      local out = {}
      for i, e in ipairs(d.schedule or {}) do out[i] = { reward = e.reward, count = e.count or 1 } end
      return out
    end,
    context = function(st)
      local m = Causes.machine(st.arg)
      if not m then return false end
      return { source = m.label, source_class = m.class, variant = m.variant, has_item_pool = m.pool ~= nil,
               uses_so_far = m.uses, room_type = State.roomTypeName(game:GetRoom():GetType()) }
    end,
    wanted = function(st)
      local m = Causes.machine(st.arg)
      return m and m.class == "chest" and 4 or 6
    end,
    maxSends = cfg.MAX_PAYOUT_SENDS,
    holdLabel = "what's in the chest",
    mercy = function(e)
      local name = Decks.mercyReward(e.reward)
      if name ~= e.reward then return { reward = name, count = 1 } end
      return e
    end,
    outcome = function(e) return { reward = e.reward, count = e.count } end,
    enabled = function() return cfg.CONTROL_PAYOUTS end,
  })

  -- The roll of a slot: a payment beside it.
  Causes.on("slot_use", function(m)
    if not active() then return end
    local e = Decks.draw("payout", m.seed, { cause = "slot_use" })
    Rolls.count("slot_use", m.label, e and "payouts" or nil)
    m.window = e and { frame = game:GetFrameCount(), entries = { e }, use = m.uses } or nil
  end)

  -- The roll of a chest: its opening. Everything inside is drawn at once.
  Causes.on("chest_open", function(m)
    if not active() then return end
    local f = game:GetFrameCount()
    local list = drawRest(m, {})
    if #list > 0 then
      m.window = { frame = f, entries = list, use = 1 }
      Rolls.count("chest_open", m.label, "payouts")
      return
    end
    -- Opened before its plan landed: its contents are cancelled and held, and the game waits for the plan.
    local w = { frame = f, waiting = true, held = {}, use = 1 }
    m.window = w
    Rolls.count("chest_open", m.label, "payouts")
    Decks.owe("payout", m.seed, { cause = "chest_open" }, function(first, gaveUp)
      w.waiting = false
      if first then
        w.entries = drawRest(m, { first })
        return deliver(m, w, w.at or m.pos)
      end
      w.paid = true                                -- none came: the game's own contents come back
      for _, h in ipairs(w.held) do pcall(Apply.spawnPickup, h.variant, h.subtype, h.pos) end
      Bridge.effect(keyOf(m), { m.label .. " opened: no plan arrived, the game's own contents put back" })
    end)
  end)

  Rolls.claimSpawns("payouts", function(info)
    if info.cause ~= "chest_open" and info.cause ~= "slot_payout" then return nil end
    local m, w = info.source, info.source and info.source.window
    if not w or info.frame - w.frame > Causes.WINDOW then return nil end   -- no schedule: the game pays
    local at = Vector(info.pos.X, info.pos.Y)
    if w.waiting then
      w.held[#w.held + 1] = { variant = info.variant, subtype = info.subtype, pos = at }
      w.at = w.at or at
    elseif not w.paid then
      w.deliverAt = w.deliverAt or at              -- paid where the game's payout would have landed
    end
    return "cancel"
  end)
end

--- Every frame: ask for schedules (and refills) of machines in the room, pay what the game didn't.
function Payouts.update()
  if not active() then return end
  local f, t = game:GetFrameCount(), Isaac.GetTime()
  for _, m in ipairs(Causes.machinesHere()) do
    local st = Decks.sync("payout", m.seed)
    if not Bridge.isPending(st.key) and st.sends < cfg.MAX_PAYOUT_SENDS and t - st.lastAsk > ASK_GAP_MS then
      if st.sends == 0 and not m.opened then
        Decks.request("payout", m.seed, m.class .. " appeared in the room")
      elseif m.class == "slot" and m.uses > 0 and #st.entries <= 1 then
        Decks.request("payout", m.seed, "its schedule is running low")
      end
    end
    local w = m.window
    if w then
      if not w.paid and not w.waiting then
        if w.deliverAt then deliver(m, w, w.deliverAt)
        elseif f - w.frame >= DELIVER_AFTER then deliver(m, w, m.pos) end      -- the game paid nothing
      end
      if f - w.frame > Causes.WINDOW and not w.waiting then m.window = nil end
    end
  end
end

return Payouts
