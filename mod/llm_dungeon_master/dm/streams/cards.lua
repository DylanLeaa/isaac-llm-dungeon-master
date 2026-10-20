-- cards:<f>  Which card or rune the game hands out next, from any source (MC_GET_CARD).
--
-- A short deck (CARDS_DEPTH), kept stocked from floor start. A draw takes the first card that fits it (a
-- rune-only draw takes a rune); cards left that just don't fit this draw aren't worth a wait, so it stays
-- vanilla. An empty deck lets the game make the card now and swaps it for the DM's once the answer lands
-- (the game is held meanwhile; the request says which draws are owed).
local Cards = {}

local game = Game()
local D, cfg, Bridge, Decks, Rolls

local function curIdx() return game:GetLevel():GetCurrentRoomIndex() end

local function available(id)
  local ok, c = pcall(function() return Isaac.GetItemConfig():GetCard(id) end)
  if not ok or not c then return true end         -- no GetCard on this version: assume unlocked
  local ok2, avail = pcall(function() return c:IsAvailable() end)
  return not ok2 or avail ~= false
end

local function fits(cat, roll)
  if roll.onlyRunes then return cat == "rune" end
  if cat == "rune" then return roll.runes end
  if cat == "playing" or cat == "special" then return roll.playing end
  return true
end

function Cards.init(deps)
  D, cfg, Bridge, Decks, Rolls = deps, deps.cfg, deps.Bridge, deps.Decks, deps.Rolls
  Decks.define("cards", {
    kind = "cards",
    key = function(f) return "cards:" .. f end,
    entries = function(d)
      local out = {}
      for i, c in ipairs(d.cards or {}) do out[i] = c end
      return out
    end,
    usable = function(c, roll)
      local id = Card and Card[c.enum]
      if not id or not available(id) then return "drop" end    -- missing on this version / still locked
      return fits(c.cat, roll)
    end,
    wanted = function() return cfg.CARDS_DEPTH end,
    wants = function() return { { arg = D.Sched.floor(), n = 1, reason = "restock: your cards deck is running out" } } end,
    maxSends = cfg.MAX_DECK_SENDS,
    askGapMs = cfg.REFRESH_MIN_MS,
    owedCtx = function(owed)
      local out = {}
      for i, o in ipairs(owed) do
        out[i] = { runes = o.roll.runes and true or false, playing = o.roll.playing and true or false,
                   runes_only = o.roll.onlyRunes and true or false }
      end
      return out
    end,
    holdLabel = "what that card is",
    label = function(c) return c.key end,
    outcome = function(c) return { card = c.key } end,
    enabled = function() return cfg.CONTROL_CONSUMABLES end,
  })

  Rolls.on("get_card", "cards", function(_rng, _card, playing, runes, onlyRunes)
    if not cfg.CONTROL_CONSUMABLES or game:IsGreedMode() then return nil, "vanilla" end
    if Rolls.suppressed() then return nil, nil end
    local f = D.Sched.floor()
    local roll = { playing = playing, runes = runes, onlyRunes = onlyRunes, cause = Rolls.cause() }
    local c = Decks.draw("cards", f, roll)
    if c then
      Bridge.effect(Decks.get("cards", f).key, { "card handed out: " .. c.key:gsub("_", " ") })
      return Card[c.enum], "dm"
    end
    if Decks.left("cards", f) > 0 or not Bridge.ready() then return nil, "vanilla" end
    -- Nothing in the deck: the game makes this card; the DM's replaces it once the answer lands.
    local slot, here = {}, curIdx()
    Rolls.claimNext(PickupVariant.PICKUP_TAROTCARD, function(p) slot.pickup = p end)
    Decks.owe("cards", f, roll, function(pick)
      local p = slot.pickup
      if pick and p and p:Exists() and p.SubType ~= 0 and curIdx() == here then
        Bridge.effect(Decks.get("cards", f).key, { "card handed out: " .. pick.key:gsub("_", " ") })
        p:Morph(EntityType.ENTITY_PICKUP, p.Variant, Card[pick.enum], true, true, true)
      end
    end)
    return nil, "dm"
  end)
end

return Cards
