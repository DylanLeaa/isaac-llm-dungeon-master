-- Cards, trinkets and pills: which ones the game hands out, and what each pill colour does.
--
-- One `consumables` decision per floor holds the DM's plan: a queue of cards, a queue of
-- trinkets (picked from real, unlocked candidates) and an effect for every pill colour in
-- play. It is asked at floor start and re-asked when a pill colour the plan doesn't cover
-- turns up, or the card/trinket queues run dry. Draws take from the newest plan; with no
-- plan, vanilla decides.
--
-- Pill colours: the game keeps a colour's effect for the whole run, so the first time a
-- colour is swallowed its effect is locked (the DM's or vanilla's) and never changes again.
-- Locks are handed to `Cons.onLock` so main.lua can save them with the run.
local Cons = {}

local game = Game()
local cfg, Bridge, State, Taunt

local GIANT_FLAG = 2048          -- horse pills: same colour, doubled effect
local GOLD = 14                  -- PillColor.PILL_GOLD: random every time, left to vanilla

local floorNo = 0
local plan            -- {resultId, cards, trinkets, pills = colour -> entry, taunt, used, handedOut}
local sends, lastAskAt, lastScan = 0, -1e9, -1e9
local askedColors = {}   -- colours the latest request covered
local seenColors = {}    -- colour -> true: in play this floor and not locked yet
local locked = {}        -- colour -> entry (+ by = "dm" | "vanilla"), for the whole run
local queries = { frame = -1 }  -- this frame's effect lookups: {color, entry, fromPlan, effect}
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

local function request(reason)
  if sends >= cfg.MAX_CONSUMABLE_SENDS then return end
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
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() or not Bridge.ready() then return end
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

--- MC_GET_CARD. Returns a Card or nil.
function Cons.onGetCard(rng, card, playing, runes, onlyRunes)
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil end
  local p = currentPlan()
  if not p then return nil end
  local i = 1
  while i <= #p.cards do
    local c = p.cards[i]
    local id = Card and Card[c.enum]
    if not id or not cardAvailable(id) then
      table.remove(p.cards, i)                     -- missing on this version / still locked
    elseif cardFits(c.cat, playing, runes, onlyRunes) then
      table.remove(p.cards, i)
      handOut(p)
      Bridge.event("card_drawn", { name = c.key })
      return id
    else
      i = i + 1
    end
  end
  return nil
end

--- MC_GET_TRINKET. Returns a TrinketType or nil.
function Cons.onGetTrinket(trinket, rng)
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil end
  local p = currentPlan()
  if not p then return nil end
  local player = Isaac.GetPlayer(0)
  local golden = (TrinketType and TrinketType.TRINKET_GOLDEN_FLAG) or 32768
  while #p.trinkets > 0 do
    local id = math.tointeger(table.remove(p.trinkets, 1))
    local info = id and State.trinketInfo(id)
    if info and not player:HasTrinket(id) then
      pcall(function() game:GetItemPool():RemoveTrinket(id) end)
      handOut(p)
      Bridge.event("trinket_drawn", { name = info.name })
      if (math.tointeger(trinket) or 0) & golden ~= 0 then id = id | golden end
      return id
    end
  end
  return nil
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
  else
    entry = { by = "vanilla", effect = State.enumName(PillEffect, effect, "^PILLEFFECT_") }
  end
  locked[q.color] = entry
  seenColors[q.color] = nil
  Bridge.event("pill_locked", { color = q.color, look = look(q.color), effect = entry.effect, by = entry.by })
  if Cons.onLock then Cons.onLock(locked) end
end

--- Every pill colour swallowed this run: {effect, by = "dm" | "vanilla"} (for the stats screen).
function Cons.lockedList()
  local out = {}
  for _, e in pairs(locked) do out[#out + 1] = { effect = e.effect, by = e.by } end
  return out
end

-- --------------------------------------------------------------- lifecycle
--- This floor's consumables key, once it has been asked about.
function Cons.askedKey() return sends > 0 and key() or nil end

function Cons.onNewFloor(n)
  Bridge.forget("cons:" .. floorNo)
  floorNo, plan, sends, lastAskAt, askedColors, seenColors = n, nil, 0, -1e9, {}, {}
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
end

function Cons.init(config, bridge, state, taunt)
  cfg, Bridge, State, Taunt = config, bridge, state, taunt
end

return Cons
