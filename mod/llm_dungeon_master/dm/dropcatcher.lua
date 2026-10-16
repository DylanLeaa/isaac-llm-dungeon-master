-- Enemy-death drops and natural spawns, taken out of the game's hands.
--
-- In a room whose clear reward the DM decides, pickups the game would scatter while the fight is on
-- (kills, loot from things that die, anything nobody owns) are swallowed instead: removed the frame
-- they appear and tallied by name. The tally rides along with the room_clear request, so the DM sees
-- what the game would have dropped and decides what to put back; whatever it leaves out is lost, and
-- the clear's effect says so ("-1 key (would have dropped, not replaced)").
--
-- Nothing is ever just lost: if the DM never answers (or the room has no clear award to hang it on),
-- the swallowed pickups are spawned again where they fell.
--
-- A room's own pickups (its layout) spawn while the room loads, a frame before the scheduler arms
-- the catcher for it. They're remembered here and taken in by Catcher.adopt once the room's
-- layout decision has run, so a pickup the DM's layout already removed or morphed is left alone.
--
-- What the player breaks open with a bomb is theirs: loot from a tinted rock is never swallowed.
--
-- The last kill's own fallout (an explosion breaking a rock, say) lands on or just after the clear
-- frame, so a closed room keeps swallowing for a few frames more: into the same tally while the
-- reward is still being decided, and reported as lost once it has been.
local Catcher = {}

local game = Game()
local cfg, Apply

local deaths = {}       -- recent enemy deaths: {pos, frame}
local rooms = {}        -- room index -> {open, held = {{variant, subtype, pos, name}}, counts, total}
local cur = nil         -- the current room's entry, nil when this room isn't being caught
local trail = nil       -- {entry, untilFrame}: the room just closed, still swallowing past its clear
local pending = {}    -- pickups that appeared with nothing catching: {pickup, variant, subtype, frame}
local logged = 0
local exempt = function() return false end   -- pos -> true when a machine/chest/bag stands there

local rocks = {}        -- grid index -> {pos, brokenAt}: tinted rocks seen in this room

local DEATH_FRAMES, DEATH_RADIUS = 2, 140
local ROCK_FRAMES, ROCK_RADIUS = 6, 90
local LOG_CAP = 300
local PENDING_CAP = 24

-- Pickups an enemy or the room can drop. Chests, collectibles, beds and trophies are never touched.
local ALLOWED = {}

function Catcher.init(config, apply)
  cfg, Apply = config, apply
  for _, name in ipairs({ "PICKUP_HEART", "PICKUP_COIN", "PICKUP_KEY", "PICKUP_BOMB", "PICKUP_LIL_BATTERY",
                          "PICKUP_GRAB_BAG", "PICKUP_TAROTCARD", "PICKUP_PILL", "PICKUP_TRINKET" }) do
    if PickupVariant[name] then ALLOWED[PickupVariant[name]] = true end
  end
end

--- `fn(pos)` says whether a payout source (slot, beggar, chest, grab bag) stands at pos.
function Catcher.setExempt(fn) exempt = fn end

function Catcher.reset()
  deaths, rooms, cur, trail, pending, logged = {}, {}, nil, nil, {}, 0
  rocks = {}
end

--- Entering a room: catch drops there if the DM will decide its clear reward (`enabled`).
function Catcher.arm(idx, enabled)
  cur, trail = nil, nil
  rocks = {}
  local f = game:GetFrameCount()                -- keep only this room's load (the last couple of frames)
  for i = #pending, 1, -1 do
    if pending[i].frame < f - 2 then table.remove(pending, i) end
  end
  if not enabled then return end
  rooms[idx] = rooms[idx] or { open = true, held = {}, counts = {}, total = 0 }
  cur = rooms[idx]
end

function Catcher.onNpcDeath(npc)
  deaths[#deaths + 1] = { pos = Vector(npc.Position.X, npc.Position.Y), frame = game:GetFrameCount() }
  if #deaths > 24 then table.remove(deaths, 1) end
end

--- Note which tinted rocks stand in the room and when each one broke.
local function scanRocks()
  local f = game:GetFrameCount()
  local room = game:GetRoom()
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g and g:GetType() == GridEntityType.GRID_ROCKT then
      local r = rocks[i]
      if g.State == 2 then
        if r and not r.brokenAt then r.brokenAt = f end
      elseif not r or r.brokenAt then
        rocks[i] = { pos = Vector(g.Position.X, g.Position.Y) }
      end
    end
  end
end

--- Catcher.update: once a frame, while drops are being caught, so a rock's break is seen when it happens.
function Catcher.update()
  if cur or trail then scanRocks() end
end

local function nearBrokenRock(pos)
  scanRocks()
  local f = game:GetFrameCount()
  for _, r in pairs(rocks) do
    if r.brokenAt and f - r.brokenAt <= ROCK_FRAMES and r.pos:Distance(pos) <= ROCK_RADIUS then return true end
  end
  return false
end

local function nearDeath(pos)
  local f = game:GetFrameCount()
  for _, d in ipairs(deaths) do
    if math.abs(f - d.frame) <= DEATH_FRAMES and d.pos:Distance(pos) <= DEATH_RADIUS then return true end
  end
  return false
end

--- How a pickup that just appeared should be classed: "enemy_death", "natural", "rock" (a tinted rock
--- the player broke: logged, never swallowed), or nil to leave it
--- alone (items, shop goods, the player's own, the mod's own).
local function classify(pickup)
  if Apply.spawning or pickup.Price ~= 0 or not ALLOWED[pickup.Variant] then return nil end
  local ok, shop = pcall(pickup.IsShopItem, pickup)
  if ok and shop then return nil end
  local sp = pickup.SpawnerEntity
  if sp and (sp.Type == EntityType.ENTITY_PLAYER or sp.Type == EntityType.ENTITY_FAMILIAR
      or sp.Type == EntityType.ENTITY_PICKUP or sp.Type == EntityType.ENTITY_SLOT) then return nil end
  if not (sp and sp:ToNPC()) and exempt(pickup.Position) then return nil end   -- chest, bag, beggar payouts
  if (sp and sp:ToNPC()) or nearDeath(pickup.Position) then return "enemy_death" end
  if nearBrokenRock(pickup.Position) then return "rock" end
  return "natural"
end

-- The entry a drop appearing now goes to: the open room, or the one that closed a few frames ago.
local function catching()
  if cur and cur.open then return cur end
  if trail and game:GetFrameCount() <= trail.untilFrame then return trail.entry end
  trail = nil
  return nil
end

--- Tell the backend how this pickup was classed (so the classifier can be checked against the game).
function Catcher.log(pickup, class, bridge)
  if not cfg.LOG_SPAWNS or logged >= LOG_CAP then return end
  logged = logged + 1
  local sp = pickup.SpawnerEntity
  bridge.event("spawn_log", {
    variant = pickup.Variant, subtype = pickup.SubType, class = class or "ignored",
    spawner = sp and (sp.Type .. "." .. sp.Variant) or "none", frame = game:GetFrameCount(),
    swallowed = catching() ~= nil,
  })
end

--- Swallow `pickup` into `entry`: removed now, held to be put back where it fell.
local function hold(entry, pickup, variant, subtype)
  local pos = Vector(pickup.Position.X, pickup.Position.Y)
  local name = Apply.rewardNameFor(variant, subtype)
  pickup:Remove()
  if entry.onLate then          -- the reward is already out: this one is lost, and the clear says so
    entry.onLate(name)
    return
  end
  entry.held[#entry.held + 1] = { variant = variant, subtype = subtype, pos = pos, name = name }
  entry.counts[name] = (entry.counts[name] or 0) + 1
  entry.total = entry.total + 1
end

--- MC_POST_PICKUP_INIT. True if the pickup was swallowed.
function Catcher.onPickupInit(pickup, bridge)
  local class = classify(pickup)
  Catcher.log(pickup, class, bridge)
  if not (class and class ~= "rock" and cfg.CONTROL_DROPS) then return false end
  if class == "natural" and not cfg.DROPS_NATURAL then return false end
  local entry = catching()
  if not entry then
    -- Maybe a room's own pickup, spawned as it loaded: Catcher.adopt decides once the room is armed.
    pending[#pending + 1] = { pickup = pickup, variant = pickup.Variant, subtype = pickup.SubType,
                              frame = game:GetFrameCount() }
    if #pending > PENDING_CAP then table.remove(pending, 1) end
    return false
  end
  hold(entry, pickup, pickup.Variant, pickup.SubType)
  return true
end

--- The room just entered is armed and its layout decision has run (or won't): swallow the pickups
--- the room loaded with that are still as they spawned. Ones the layout removed or morphed are the
--- DM's own work and stay out of the tally.
function Catcher.adopt(bridge)
  local list = pending
  pending = {}
  local entry = cur
  if not (entry and entry.open) then return 0 end
  local n = 0
  for _, p in ipairs(list) do
    local ok, same = pcall(function()
      local e = p.pickup
      return e:Exists() and e.Variant == p.variant and e.SubType == p.subtype and e.Price == 0
    end)
    if ok and same then
      if cfg.LOG_SPAWNS then bridge.event("spawn_log", { variant = p.variant, subtype = p.subtype,
        class = "layout", spawner = "none", frame = p.frame, swallowed = true }) end
      hold(entry, p.pickup, p.variant, p.subtype)
      n = n + 1
    end
  end
  return n
end

--- {name = n} of what has been swallowed in the current room so far, or nil if nothing.
function Catcher.counts()
  if not (cur and cur.open and cur.total > 0) then return nil end
  local out = {}
  for k, v in pairs(cur.counts) do out[k] = v end
  return out
end

function Catcher.total() return cur and cur.total or 0 end

--- Is the current room still swallowing? (Its clear reward hasn't been decided yet.)
function Catcher.isOpen() return cur ~= nil and cur.open end

--- The reward is being decided: stop swallowing and hand back what was held. With `trailFrames`,
--- drops in the next few frames still join it (the tables returned fill up as they come).
--- Returns {held, counts} (empty tables when the room wasn't being caught).
function Catcher.close(trailFrames)
  if not cur then return { held = {}, counts = {} } end
  local entry = cur
  entry.open = false
  for idx, r in pairs(rooms) do
    if r == entry then rooms[idx] = nil end
  end
  cur = nil
  if trailFrames and trailFrames > 0 then
    trail = { entry = entry, untilFrame = game:GetFrameCount() + trailFrames }
  end
  return entry
end

--- The closed room's reward is out: drops still trailing in are lost and passed to `onLate(name)`,
--- or with no `onLate` (the game went ahead with its own) left alone.
function Catcher.settle(entry, onLate)
  if not (trail and trail.entry == entry) then return end
  if onLate then entry.onLate = onLate else trail = nil end
end

--- Spawn the held pickups again, where they fell.
function Catcher.release(entry)
  local n = 0
  for _, h in ipairs(entry and entry.held or {}) do
    if pcall(Apply.spawnPickup, h.variant, h.subtype, h.pos) then n = n + 1 end
  end
  return n
end

return Catcher
