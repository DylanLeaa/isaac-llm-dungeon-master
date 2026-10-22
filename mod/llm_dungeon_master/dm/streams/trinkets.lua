-- trinkets:<f>  Which trinket the game hands out next, from any source (MC_GET_TRINKET).
--
-- A short deck (TRINKETS_DEPTH) of real, unlocked candidates, kept stocked from floor start. A golden
-- draw gets the DM's trinket golden. Empty: the game makes the trinket now and it is swapped for the
-- DM's once the answer lands (the game is held meanwhile).
local Trinkets = {}

local game = Game()
local D, cfg, Bridge, State, Decks, Rolls

local GOLDEN = (TrinketType and TrinketType.TRINKET_GOLDEN_FLAG) or 32768

local function curIdx() return game:GetLevel():GetCurrentRoomIndex() end

local function available(cfgEntry)
  if not cfgEntry then return false end
  local ok, avail = pcall(function() return cfgEntry:IsAvailable() end)
  return not ok or avail ~= false            -- no IsAvailable on this version: assume unlocked
end

local function candidates(sends)
  local ic, player = Isaac.GetItemConfig(), Isaac.GetPlayer(0)
  local size = ic:GetTrinkets().Size
  local rng = RNG()
  rng:SetSeed(math.max(1, game:GetSeeds():GetStartSeed() + D.Sched.floor() * 7919 + 17 + sends * 101), 35)
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

local function handOut(f, id, golden)
  pcall(function() game:GetItemPool():RemoveTrinket(id) end)
  local info = State.trinketInfo(id)
  Bridge.effect(Decks.get("trinkets", f).key, { "trinket handed out: " .. info.name .. (golden and " (golden)" or "") })
  return golden and (id | GOLDEN) or id
end

function Trinkets.init(deps)
  D, cfg, Bridge, State, Decks, Rolls = deps, deps.cfg, deps.Bridge, deps.State, deps.Decks, deps.Rolls
  Decks.define("trinkets", {
    kind = "trinkets",
    key = function(f) return "trinkets:" .. f end,
    entries = function(d)
      local out = {}
      for _, id in ipairs(d.trinkets or {}) do out[#out + 1] = math.tointeger(id) end
      return out
    end,
    usable = function(id)
      if id and State.trinketInfo(id) and not Isaac.GetPlayer(0):HasTrinket(id) then return true end
      return "drop"
    end,
    context = function(st) return { trinket_candidates = candidates(st.sends) } end,
    wanted = function() return cfg.TRINKETS_DEPTH end,
    wants = function() return { { arg = D.Sched.floor(), n = 1, reason = "restock: your trinkets deck is running out" } } end,
    maxSends = cfg.MAX_DECK_SENDS,
    askGapMs = cfg.REFRESH_MIN_MS,
    holdLabel = "what that trinket is",
    label = function(id) return tostring(id) end,
    outcome = function(id)
      local info = State.trinketInfo(id) or {}
      return { trinket = id, name = info.name }
    end,
    enabled = function() return cfg.CONTROL_CONSUMABLES end,
  })

  Rolls.on("get_trinket", "trinkets", function(trinket)
    if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil, "vanilla" end
    if Rolls.suppressed() then return nil, nil end
    local f = D.Sched.floor()
    local golden = (math.tointeger(trinket) or 0) & GOLDEN ~= 0
    local id = Decks.draw("trinkets", f, { golden = golden, cause = Rolls.cause() })
    if id then return handOut(f, id, golden), "dm" end
    if not Bridge.ready() then return nil, "vanilla" end
    local slot, here = {}, curIdx()
    Rolls.claimNext(PickupVariant.PICKUP_TRINKET, function(p) slot.pickup = p end)
    Decks.owe("trinkets", f, { golden = golden }, function(pick)
      local p = slot.pickup
      if pick and p and p:Exists() and p.SubType ~= 0 and curIdx() == here then
        p:Morph(EntityType.ENTITY_PICKUP, p.Variant, handOut(f, pick, p.SubType & GOLDEN ~= 0), true, true, true)
      end
    end)
    return nil, "dm"
  end)
end

return Trinkets
