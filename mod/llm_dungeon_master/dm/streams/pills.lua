-- pills:<f>  What each pill colour does (MC_GET_PILL_EFFECT), plus the pill-button block.
--
-- A sticky deck: one entry per colour in play, looked up (not used up) every time the game asks what a
-- colour does. Asked at floor start for the colours already in play and again whenever a colour no answer
-- covers turns up (held, or on the floor). The game keeps a colour's effect for the whole run, so the first
-- time a colour is swallowed its effect is locked (the DM's, or vanilla's when there was no DM) and never
-- changes again: that swallow is the draw. Locks go to Pills.onLock so main.lua saves them with the run.
-- A pill no answer covers can't be swallowed: pressing the button holds the game until one does.
local Pills = {}

local game = Game()
local D, cfg, Bridge, State, Decks, Loading

local GIANT_FLAG = 2048          -- horse pills: same colour, doubled effect
local GOLD = 14                  -- PillColor.PILL_GOLD: random every time, left to vanilla

local lastAskAt, lastScan = -1e9, -1e9
local askedColors = {}           -- colours the latest request covered
local seenColors = {}            -- colour -> true: in play this floor and not locked yet
local locked = {}                -- colour -> entry (+ by = "dm" | "vanilla"), for the whole run
local queries = { frame = -1 }   -- this frame's effect lookups: {color, entry, effect}
local gaveUp = false             -- a hold on this floor's pills gave up: colours it doesn't cover aren't blocked
local probing = false            -- reading the real pill button from inside the input hook
Pills.onLock = nil

local function key() return "pills:" .. D.Sched.floor() end
local function look(c) return PillColor and State.enumName(PillColor, c, "^PILL_") or tostring(c) end

local function baseColor(c)
  c = math.tointeger(c) or 0
  return c & (GIANT_FLAG - 1)
end

local function notePill(color)
  local c = baseColor(color)
  if c > 0 and c < GOLD and not locked[c] then seenColors[c] = true end
end

local function scanPills()
  local p = Isaac.GetPlayer(0)
  notePill(p:GetPill(0))
  notePill(p:GetPill(1))
  for _, e in ipairs(Isaac.FindByType(EntityType.ENTITY_PICKUP, PickupVariant.PICKUP_PILL)) do notePill(e.SubType) end
end

local function covered(c)
  return (Decks.peek("pills", D.Sched.floor(), { color = c })) ~= nil
end

local function request(reason, force)
  if Decks.request("pills", D.Sched.floor(), reason, force) then lastAskAt = Isaac.GetTime() end
end

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

local function holdFor(color)
  notePill(color)                                  -- so the ask covers it, scan or not
  Decks.owe("pills", D.Sched.floor(), { color = baseColor(color) }, function(_, gave)
    if gave then gaveUp = true else gaveUp = false end
  end)
  Decks.holdNow("pills", D.Sched.floor())
end

function Pills.init(deps)
  D, cfg, Bridge, State, Decks, Loading = deps, deps.cfg, deps.Bridge, deps.State, deps.Decks, deps.Loading
  Decks.define("pills", {
    kind = "pills",
    key = function(f) return "pills:" .. f end,
    entries = function(d)
      local out = {}
      for _, e in ipairs(d.pills or {}) do
        local c = math.tointeger(e.color) or e.color
        out[#out + 1] = { color = c, effect = e.effect, enum = e.enum, phd = e.phd, false_phd = e.false_phd }
      end
      return out
    end,
    sticky = true,
    peekOwed = true,                               -- an answer doesn't swallow the pill: its swallow does
    usable = function(e, roll) return e.color == roll.color end,
    deck = false,
    context = function()
      local colors, lockedList = {}, {}
      askedColors = {}
      for c in pairs(seenColors) do
        colors[#colors + 1] = { color = c, look = look(c) }
        askedColors[c] = true
      end
      table.sort(colors, function(a, b) return a.color < b.color end)
      for c, l in pairs(locked) do lockedList[#lockedList + 1] = { color = c, look = look(c), effect = l.effect } end
      return { pill_colors_to_assign = colors, pills_locked = lockedList }
    end,
    owedCtx = function(owed)
      local out = {}
      for i, o in ipairs(owed) do out[i] = { color = o.roll.color, look = look(o.roll.color) } end
      return out
    end,
    maxSends = cfg.MAX_DECK_SENDS,
    holdLabel = "what that pill does",
    label = function(e) return e.color .. "=" .. e.effect end,
    outcome = function(e, roll) return { pill = e.color, effect = roll.did or e.effect } end,
    enabled = function() return cfg.CONTROL_CONSUMABLES end,
  })

  D.Rolls.on("get_pill_effect", "pills", function(effect, color)
    if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil, nil end
    local c = baseColor(color)
    if c <= 0 or c >= GOLD then return nil, nil end
    -- A lookup, not a roll: the roll is the first swallow (MC_USE_PILL locks it).
    local entry, st = locked[c], nil
    local fromDeck = false
    if not entry then
      entry, st = Decks.peek("pills", D.Sched.floor(), { color = c })
      fromDeck = entry ~= nil
    end
    local id
    if entry and entry.by ~= "vanilla" then id = resolveEffect(entry) end
    local frame = game:GetFrameCount()
    if queries.frame ~= frame then queries = { frame = frame } end
    queries[#queries + 1] = { color = c, entry = entry, fromDeck = fromDeck, effect = id or effect }
    return id, nil
  end)
end

--- Every frame: ask at floor start, then whenever a colour no answer covers is in play.
function Pills.update()
  if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() or not Bridge.ready() then return end
  local t = Isaac.GetTime()
  if t - lastScan < 500 then return end
  lastScan = t
  scanPills()
  if Bridge.isPending(key()) or t - lastAskAt < cfg.REFRESH_MIN_MS then return end
  local first = Decks.get("pills", D.Sched.floor()).sends == 0
  for c in pairs(seenColors) do
    if not askedColors[c] then
      return request(first and "floor start: pill colours in play" or "a pill colour no answer covers is in play")
    end
  end
end

--- MC_USE_PILL: the colour looked up just before is now swallowed, so its effect is final (the draw).
--- Other lookups can happen in the same frame, so match on the effect that was actually used.
function Pills.onUsePill(effect)
  if game:GetFrameCount() - queries.frame > 1 then return end
  local q
  for i = #queries, 1, -1 do
    if queries[i].effect == effect then q = queries[i]; break end
  end
  if not q or locked[q.color] then return end
  local entry
  local did = State.enumName(PillEffect, effect, "^PILLEFFECT_")
  if q.fromDeck then
    entry = { by = "dm", effect = q.entry.effect, enum = q.entry.enum, phd = q.entry.phd, false_phd = q.entry.false_phd }
    local st = Decks.get("pills", D.Sched.floor())
    Decks.consume(st, q.entry, { color = q.color, did = did, cause = "swallowed" })
    D.Rolls.count("use_pill", "swallowed", "pills")
    Bridge.effect(key(), { "pill " .. look(q.color) .. " = " .. did:gsub("_", " ")
                           .. (did ~= q.entry.effect and " (planned " .. q.entry.effect:gsub("_", " ") .. ")" or "") })
  else
    entry = { by = "vanilla", effect = did }
    D.Rolls.count("use_pill", "swallowed", nil)
  end
  locked[q.color] = entry
  seenColors[q.color] = nil
  Bridge.event("pill_locked", { color = q.color, look = look(q.color), effect = entry.effect, by = entry.by })
  if Pills.onLock then Pills.onLock(locked) end
end

-- The pocket pill can't be swallowed yet: its colour isn't locked and no answer covers it.
local function waits(player)
  if gaveUp or not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() or not Bridge.ready() then return false end
  local c = baseColor(player:GetPill(0))
  if c <= 0 or c >= GOLD or locked[c] then return false end
  return not covered(c)
end

--- MC_INPUT_ACTION: the pill button does nothing until an answer covers the pill; pressing it holds the
--- game for one. Runs on every input poll, so it bails out early.
function Pills.blockPill(ent, hook, action)
  if probing or action ~= ButtonAction.ACTION_PILLCARD or hook == InputHook.GET_ACTION_VALUE then return nil end
  local player = ent and ent:ToPlayer()
  if not player or not waits(player) then return nil end
  if hook == InputHook.IS_ACTION_TRIGGERED and not Loading.holding(key()) then
    probing = true                             -- the real button, past this hook
    local ok, pressed = pcall(Input.IsActionTriggered, action, player.ControllerIndex)
    probing = false
    if ok and pressed then holdFor(player:GetPill(0)) end
  end
  return false
end

--- Every pill colour swallowed this run: {effect, by = "dm" | "vanilla"} (for the stats screen).
function Pills.lockedList()
  local out = {}
  for _, e in pairs(locked) do out[#out + 1] = { effect = e.effect, by = e.by } end
  return out
end

--- Pill locks from a saved run (colour keys may come back as strings from JSON).
function Pills.restore(saved)
  locked = {}
  for c, e in pairs(saved or {}) do
    local n = math.tointeger(tonumber(c))
    if n and type(e) == "table" then locked[n] = e end
  end
end

function Pills.onNewFloor()
  lastAskAt, askedColors, seenColors, gaveUp = -1e9, {}, {}, false
end

function Pills.resetRun()
  lastAskAt, lastScan, askedColors, seenColors, locked, queries = -1e9, -1e9, {}, {}, {}, { frame = -1 }
  gaveUp = false
end

return Pills
