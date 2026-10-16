-- Cards, trinkets and pills: which ones the game hands out, and what each pill colour does.
--
-- One `consumables` decision per floor holds the DM's plan: a queue of cards, a queue of
-- trinkets (picked from real, unlocked candidates) and an effect for every pill colour in
-- play. It is asked at floor start and re-asked when a pill colour the plan doesn't cover
-- turns up, or the card/trinket queues run dry. Draws take from the newest plan. With no plan
-- (or an empty queue) the game makes the card or trinket on the spot, and it is swapped for the
-- DM's once the plan lands, the game held meanwhile (loading.lua). A pill the plan doesn't cover
-- can't be swallowed: trying to holds the game until it does.
--
-- Pill colours: the game keeps a colour's effect for the whole run, so the first time a
-- colour is swallowed its effect is locked (the DM's, or vanilla's when there was no DM) and
-- never changes again. Locks are handed to `Cons.onLock` so main.lua can save them with the run.
local Cons = {}

local game = Game()
local cfg, Bridge, State, Taunt, Loading

local GIANT_FLAG = 2048          -- horse pills: same colour, doubled effect
local GOLD = 14                  -- PillColor.PILL_GOLD: random every time, left to vanilla

local floorNo = 0
local plan            -- {resultId, cards, trinkets, pills = colour -> entry, taunt, used, handedOut}
local sends, lastAskAt, lastScan = 0, -1e9, -1e9
local askedColors = {}   -- colours the latest request covered
local seenColors = {}    -- colour -> true: in play this floor and not locked yet
local locked = {}        -- colour -> entry (+ by = "dm" | "vanilla"), for the whole run
local queries = { frame = -1 }  -- this frame's effect lookups: {color, entry, fromPlan, effect}
local drawn              -- {kind, frame, stale, ...}: a card/trinket drawn with no plan; its pickup comes next
local swaps = {}         -- pickups to turn into the DM's cards/trinkets once the plan lands
local toHold             -- {label, stale}: hold on the plan next update, not mid-draw
local gaveUp = false     -- a hold on this floor's plan gave up: pills it doesn't cover aren't blocked
local probing = false    -- reading the real pill button from inside the input hook
Cons.onLock = nil

local function key() return "cons:" .. floorNo end
local function look(c) return PillColor and State.enumName(PillColor, c, "^PILL_") or tostring(c) end

local function baseColor(c)
  c = math.tointeger(c) or 0
  return c & (GIANT_FLAG - 1)
end

local function available(cfgEntry)
  if not cfgEntry then return false end
  local ok, avail = pcall(function() return cfgEntry:IsAvailable() end)
  return not ok or avail ~= false            -- no IsAvailable on this version: assume unlocked
end

local function cardAvailable(id)
  local ok, c = pcall(function() return Isaac.GetItemConfig():GetCard(id) end)
  return not ok or available(c)              -- no GetCard on this version: assume unlocked
end

-- ----------------------------------------------------------------- requests
local function trinketCandidates()
  local ic, player = Isaac.GetItemConfig(), Isaac.GetPlayer(0)
  local size = ic:GetTrinkets().Size
  local rng = RNG()
  rng:SetSeed(math.max(1, game:GetSeeds():GetStartSeed() + floorNo * 7919 + 17), 35)
  local out, seen = {}, {}
  for _ = 1, cfg.TRINKET_CANDIDATES * 6 do
    if #out >= cfg.TRINKET_CANDIDATES or size < 2 then break end
    local id = rng:RandomInt(size - 1) + 1
    if not seen[id] then
      seen[id] = true
      local info = State.trinketInfo(id)
      if info and available(ic:GetTrinket(id)) and not player:HasTrinket(id) then out[#out + 1] = info end
    end
  end
  return out
end

--- force: the game is waiting on this plan, so the per-floor cap doesn't apply.
local function request(reason, force)
  if sends >= cfg.MAX_CONSUMABLE_SENDS and not force then return end
  sends, lastAskAt = sends + 1, Isaac.GetTime()
  local colors, lockedList = {}, {}
  askedColors = {}
  for c in pairs(seenColors) do
    colors[#colors + 1] = { color = c, look = look(c) }
    askedColors[c] = true
  end
  table.sort(colors, function(a, b) return a.color < b.color end)
  for c, l in pairs(locked) do lockedList[#lockedList + 1] = { color = c, look = look(c), effect = l.effect } end
  Bridge.request("consumables", key(), State.snapshot(), {
    reason = reason, pill_colors_to_assign = colors, pills_locked = lockedList,
    trinket_candidates = trinketCandidates(),
  }, 10000)
end

local function notePill(color)
  local c = baseColor(color)
  if c > 0 and c < GOLD and not locked[c] then seenColors[c] = true end
end

local function scanPills()
  local p = Isaac.GetPlayer(0)
  notePill(p:GetPill(0))
  notePill(p:GetPill(1))
  for _, e in ipairs(Isaac.FindByType(EntityType.ENTITY_PICKUP, PickupVariant.PICKUP_PILL)) do
    notePill(e.SubType)
  end
end

--- Call every frame. Asks at floor start, then whenever the plan stops covering what's in play.
function Cons.update()
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return end
  Cons.flushHolds()
  if not Bridge.ready() then return end
  local t = Isaac.GetTime()
  if t - lastScan < 500 then return end
  lastScan = t
  scanPills()
  if sends == 0 then return request("floor start") end
  if Bridge.isPending(key()) or t - lastAskAt < cfg.REFRESH_MIN_MS then return end
  for c in pairs(seenColors) do
    if not askedColors[c] then return request("a pill colour your plan doesn't cover is in play") end
  end
  if plan and plan.handedOut > 0 and #plan.cards == 0 and #plan.trinkets == 0 then
    request("your card and trinket queues ran out")
  end
end

-- ------------------------------------------------------------------- draws
local function currentPlan()
  local res = Bridge.peek(key())
  if res and (not plan or plan.resultId ~= res.id) then
    local d, pills = res.decision, {}
    for _, e in ipairs(d.pills or {}) do pills[math.tointeger(e.color) or e.color] = e end
    plan = { resultId = res.id, cards = { table.unpack(d.cards or {}) },
             trinkets = { table.unpack(d.trinkets or {}) }, pills = pills,
             taunt = d.taunt, used = false, handedOut = 0 }
  end
  return plan
end

local function handOut(p)
  p.handedOut = p.handedOut + 1
  if not p.used then
    p.used = true
    Bridge.markApplied(key())
    Taunt.show(p.taunt)
  end
end

local function cardFits(cat, playing, runes, onlyRunes)
  if onlyRunes then return cat == "rune" end
  if cat == "rune" then return runes end
  if cat == "playing" or cat == "special" then return playing end
  return true
end

-- The plan's next card that fits this draw, or nil.
local function nextCard(p, playing, runes, onlyRunes)
  local i = 1
  while i <= #p.cards do
    local c = p.cards[i]
    local id = Card and Card[c.enum]
    if not id or not cardAvailable(id) then
      table.remove(p.cards, i)                     -- missing on this version / still locked
    elseif cardFits(c.cat, playing, runes, onlyRunes) then
      table.remove(p.cards, i)
      handOut(p)
      Bridge.effect(key(), { "card handed out: " .. c.key:gsub("_", " ") })
      Bridge.event("card_drawn", { name = c.key })
      return id
    else
      i = i + 1
    end
  end
  return nil
end

local GOLDEN = (TrinketType and TrinketType.TRINKET_GOLDEN_FLAG) or 32768

-- The plan's next trinket the player doesn't have, golden if the draw was, or nil.
local function nextTrinket(p, golden)
  local player = Isaac.GetPlayer(0)
  while #p.trinkets > 0 do
    local id = math.tointeger(table.remove(p.trinkets, 1))
    local info = id and State.trinketInfo(id)
    if info and not player:HasTrinket(id) then
      pcall(function() game:GetItemPool():RemoveTrinket(id) end)
      handOut(p)
      Bridge.effect(key(), { "trinket handed out: " .. info.name .. (golden and " (golden)" or "") })
      Bridge.event("trinket_drawn", { name = info.name })
      if golden then id = id | GOLDEN end
      return id
    end
  end
  return nil
end

-- No plan, or its queue is empty: the game makes this one, and Cons.onPickupInit marks the pickup
-- to be swapped for the DM's once a plan lands.
local function owe(kind, p, extra)
  if not Bridge.ready() then return end
  drawn = extra
  drawn.kind, drawn.frame, drawn.stale = kind, game:GetFrameCount(), p and p.resultId
end

--- MC_GET_CARD. Returns a Card or nil.
function Cons.onGetCard(rng, card, playing, runes, onlyRunes)
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil end
  local p = currentPlan()
  local id = p and nextCard(p, playing, runes, onlyRunes)
  if id then return id end
  -- Cards left that just don't fit this draw (a rune-only draw, say) aren't worth a wait.
  if not p or #p.cards == 0 then owe("card", p, { playing = playing, runes = runes, onlyRunes = onlyRunes }) end
  return nil
end

--- MC_GET_TRINKET. Returns a TrinketType or nil.
function Cons.onGetTrinket(trinket, rng)
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil end
  local p = currentPlan()
  local id = p and nextTrinket(p, (math.tointeger(trinket) or 0) & GOLDEN ~= 0)
  if id then return id end
  owe("trinket", p, {})
  return nil
end

local VARIANT = { card = "PICKUP_TAROTCARD", trinket = "PICKUP_TRINKET" }

--- MC_POST_PICKUP_INIT: the card or trinket an unplanned draw just made.
function Cons.onPickupInit(pickup)
  if not drawn then return end
  local d = drawn
  if pickup.Variant ~= PickupVariant[VARIANT[d.kind]] then return end
  drawn = nil
  if d.frame ~= game:GetFrameCount() then return end
  swaps[#swaps + 1] = { pickup = pickup, d = d, room = game:GetLevel():GetCurrentRoomIndex() }
  toHold = toHold or { label = d.kind == "card" and "what that card is" or "what that trinket is", stale = d.stale }
end

-- The plan is in: turn every pickup still waiting into the DM's card or trinket.
local function swapPickups()
  local list = swaps
  swaps = {}
  local p = currentPlan()
  local here = game:GetLevel():GetCurrentRoomIndex()
  if not p then return end
  for _, s in ipairs(list) do
    local e, d = s.pickup, s.d
    if s.room == here and e:Exists() and e.SubType ~= 0 then
      local id
      if d.kind == "card" then id = nextCard(p, d.playing, d.runes, d.onlyRunes)
      else id = nextTrinket(p, e.SubType & GOLDEN ~= 0) end
      if id then e:Morph(EntityType.ENTITY_PICKUP, e.Variant, id, true, true, true) end
    end
  end
end

-- Hold the game on this floor's plan: asks for one now if none is on its way.
local function holdPlan(label, stale)
  local k = key()
  Loading.hold(k, {
    label = label, stale = stale,
    ask = function() request("the game is waiting on your plan right now", true) end,
    apply = function()
      gaveUp = false
      swapPickups()
    end,
    giveUp = function()
      swaps, gaveUp = {}, true
      Bridge.event("fallback", { kind = "consumables", key = k, asked = true })
    end,
  })
end

--- Called from update: hold for the pickups the game made without a plan (outside its draw callbacks).
function Cons.flushHolds()
  if not toHold then return end
  local h = toHold
  toHold = nil
  holdPlan(h.label, h.stale)
end

-- ------------------------------------------------------------------- pills
local function has(player, name)
  local id = CollectibleType and CollectibleType[name]
  return id ~= nil and player:HasCollectible(id)
end

-- The DM's effect after what the game would have done to it on the way: PHD-likes flip bad
-- pills, False PHD flips good ones, and Bad Trip never kills.
local function resolveEffect(entry)
  local p, name = Isaac.GetPlayer(0), entry.enum
  if entry.phd and (has(p, "COLLECTIBLE_PHD") or has(p, "COLLECTIBLE_VIRGO") or has(p, "COLLECTIBLE_LUCKY_FOOT")) then
    name = entry.phd
  elseif entry.false_phd and has(p, "COLLECTIBLE_FALSE_PHD") then
    name = entry.false_phd
  end
  if name == "PILLEFFECT_BAD_TRIP" and p:GetHearts() + p:GetSoulHearts() <= 2 then
    name = "PILLEFFECT_FULL_HEALTH"
  end
  return PillEffect and PillEffect[name]
end

--- MC_GET_PILL_EFFECT. Returns a PillEffect or nil.
function Cons.onGetPillEffect(effect, color)
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil end
  local c = baseColor(color)
  if c <= 0 or c >= GOLD then return nil end
  local entry, fromPlan = locked[c], false
  if not entry then
    local p = currentPlan()
    entry = p and p.pills[c]
    fromPlan = entry ~= nil
  end
  local id
  if entry and entry.by ~= "vanilla" then id = resolveEffect(entry) end
  local frame = game:GetFrameCount()
  if queries.frame ~= frame then queries = { frame = frame } end
  queries[#queries + 1] = { color = c, entry = entry, fromPlan = fromPlan, effect = id or effect }
  return id
end

--- MC_USE_PILL: the colour looked up just before is now swallowed, so its effect is final.
--- Other lookups can happen in the same frame, so match on the effect that was actually used.
function Cons.onUsePill(effect)
  if game:GetFrameCount() - queries.frame > 1 then return end
  local q
  for i = #queries, 1, -1 do
    if queries[i].effect == effect then q = queries[i]; break end
  end
  if not q or locked[q.color] then return end
  local entry
  if q.fromPlan then
    entry = { by = "dm", effect = q.entry.effect, enum = q.entry.enum, phd = q.entry.phd,
              false_phd = q.entry.false_phd }
    if plan then handOut(plan) end
    local did = State.enumName(PillEffect, effect, "^PILLEFFECT_")
    Bridge.effect(key(), { "pill " .. look(q.color) .. " = " .. did:gsub("_", " ")
                           .. (did ~= q.entry.effect and " (planned " .. q.entry.effect:gsub("_", " ") .. ")" or "") })
  else
    entry = { by = "vanilla", effect = State.enumName(PillEffect, effect, "^PILLEFFECT_") }
  end
  locked[q.color] = entry
  seenColors[q.color] = nil
  Bridge.event("pill_locked", { color = q.color, look = look(q.color), effect = entry.effect, by = entry.by })
  if Cons.onLock then Cons.onLock(locked) end
end

-- The pocket pill can't be swallowed yet: its colour isn't locked and the DM's plan doesn't cover it.
local function pillWaits(player)
  if gaveUp or not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() or not Bridge.ready() then return false end
  local c = baseColor(player:GetPill(0))
  if c <= 0 or c >= GOLD or locked[c] then return false end
  local p = currentPlan()
  return not (p and p.pills[c])
end

--- MC_INPUT_ACTION: the pill button does nothing until the plan covers the pill; pressing it holds
--- the game for the plan. Runs on every input poll, so it bails out early.
function Cons.blockPill(ent, hook, action)
  if probing or action ~= ButtonAction.ACTION_PILLCARD or hook == InputHook.GET_ACTION_VALUE then return nil end
  local player = ent and ent:ToPlayer()
  if not player or not pillWaits(player) then return nil end
  if hook == InputHook.IS_ACTION_TRIGGERED and not Loading.holding(key()) then
    probing = true                             -- the real button, past this hook
    local ok, pressed = pcall(Input.IsActionTriggered, action, player.ControllerIndex)
    probing = false
    if ok and pressed then
      notePill(player:GetPill(0))              -- so the ask covers it, scan or not
      local p = currentPlan()
      holdPlan("what that pill does", p and p.resultId)
    end
  end
  return false
end

--- Every pill colour swallowed this run: {effect, by = "dm" | "vanilla"} (for the stats screen).
function Cons.lockedList()
  local out = {}
  for _, e in pairs(locked) do out[#out + 1] = { effect = e.effect, by = e.by } end
  return out
end

-- --------------------------------------------------------------- lifecycle
--- This floor's consumables key, once it has been asked about.
function Cons.onNewFloor(n)
  Bridge.forget("cons:" .. floorNo)
  floorNo, plan, sends, lastAskAt, askedColors, seenColors = n, nil, 0, -1e9, {}, {}
  drawn, swaps, toHold, gaveUp = nil, {}, nil, false
end

--- Pill locks from a saved run (colour keys may come back as strings from JSON).
function Cons.restore(saved)
  locked = {}
  for c, e in pairs(saved or {}) do
    local n = math.tointeger(tonumber(c))
    if n and type(e) == "table" then locked[n] = e end
  end
end

function Cons.resetRun()
  floorNo, plan, sends, lastAskAt, lastScan = 0, nil, 0, -1e9, -1e9
  askedColors, seenColors, locked, queries = {}, {}, {}, { frame = -1 }
  drawn, swaps, toHold, gaveUp = nil, {}, nil, false
end

function Cons.init(config, bridge, state, taunt, loading)
  cfg, Bridge, State, Taunt, Loading = config, bridge, state, taunt, loading
end

return Cons
