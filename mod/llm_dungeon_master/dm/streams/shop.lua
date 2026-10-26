-- shop:<f>  What the shop sells in its pickup slots (MC_POST_PICKUP_SELECTION of a shop slot).
--
-- A deck of wares, in the order the game stocks the slots: each slot the game fills with a pickup takes the
-- next entry (a slot the game makes a pedestal stays one: its item is the shop pool's ranking, items.lua).
-- Asked at floor start on a floor with a shop the player hasn't walked into. Empty when a slot is stocked:
-- the game's pick stands for now and the DM's replaces it, at the same price, once the deck lands (the game
-- is held meanwhile). A card or pill on sale is the cards or pills deck's, like any card or pill.
local Shop = {}

local game = Game()
local D, cfg, Bridge, State, Apply, Decks, Rolls

local function floorNo() return D.Sched.floor() end
local function curDesc() return game:GetLevel():GetCurrentRoomDesc() end

--- This floor's shop, if the player hasn't been in it yet (its stock is still to be decided).
local function shopAhead()
  local rooms = game:GetLevel():GetRooms()
  for i = 0, rooms.Size - 1 do
    local d = rooms:Get(i)
    if d and d.Data and d.Data.Type == RoomType.ROOM_SHOP and d.VisitedCount == 0 then return d end
  end
  return nil
end

local function forSale(key, name)
  Bridge.effect(key, { "for sale: " .. name:gsub("_", " ") })
end

function Shop.init(deps)
  D, cfg, Bridge, State, Apply, Decks, Rolls = deps, deps.cfg, deps.Bridge, deps.State, deps.Apply, deps.Decks, deps.Rolls
  Decks.define("shop", {
    kind = "shop",
    key = function(f) return "shop:" .. f end,
    entries = function(d)
      local out = {}
      for i, name in ipairs(d.goods or {}) do out[i] = name end
      return out
    end,
    usable = function(name) return Apply.pickupFor(name) and true or "drop" end,
    context = function()
      local shop = shopAhead()
      return { shop_contents = shop and State.roomContents(shop) or nil }
    end,
    wanted = function()
      local shop = shopAhead()
      local c = shop and State.roomContents(shop)
      return c and math.max(1, c.shop_slots) or cfg.SHOP_DEPTH
    end,
    wants = function()
      if not shopAhead() then return {} end
      return { { arg = floorNo(), n = 1, reason = "floor start" } }
    end,
    maxSends = cfg.MAX_DECK_SENDS,
    askGapMs = cfg.REFRESH_MIN_MS,
    holdLabel = "what the shop sells",
    outcome = function(name) return { good = name } end,
    enabled = function() return cfg.CONTROL_SHOP end,
  })

  -- The shop's slots, as its layout file places them: what each one turns out to be is this deck's.
  Rolls.claimRoomEntities("shop", function(variant)
    local desc = curDesc()
    return cfg.CONTROL_SHOP and variant == PickupVariant.PICKUP_SHOPITEM and desc.Data.Type == RoomType.ROOM_SHOP
           and (Bridge.ready() or Decks.left("shop", floorNo()) > 0)
  end)

  Rolls.onSelection("shop", function(pickup, variant)
    if variant == PickupVariant.PICKUP_COLLECTIBLE then          -- a pedestal: its item is the shop pool's
      return nil, cfg.CONTROL_ITEMS and "items" or nil
    end
    local f = floorNo()
    local key = Decks.get("shop", f).key
    local name = Decks.draw("shop", f, { cause = "shop_slot" })
    if name then
      forSale(key, name)
      return { Apply.pickupFor(name) }, "shop"
    end
    if not Bridge.ready() then return nil, nil end
    -- Nothing in the deck: the game stocks this slot now; the DM's ware replaces it once the deck lands.
    local here = curDesc().SafeGridIndex
    Decks.owe("shop", f, { cause = "shop_slot" }, function(late)
      if late and pickup:Exists() and curDesc().SafeGridIndex == here and Apply.morphPickup(pickup, late) then
        forSale(key, late)
      end
    end)
    return nil, "shop"
  end)
end

return Shop
