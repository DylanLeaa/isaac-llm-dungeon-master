-- drops:<f>  What each kill drops, and each poop, fire, urn or skull broken (and a tinted rock no
-- layout planned).
--
-- The roll is the kill (MC_POST_NPC_DEATH) or the break (causes.lua): each one draws the deck's next
-- entry, pays it where it happened, and the game's own drop from it is cancelled in MC_PRE_ENTITY_SPAWN.
-- The deck is asked on entering a room with enemies, sized to them (+ DROPS_SPARE), and its unused tail
-- is rewritten when the player's state moves or another deck spends the budget. A kill with the deck
-- empty keeps the game's own drop, and the roll audit reports it uncontrolled. Bosses keep theirs.
local Drops = {}

local game = Game()
local D, cfg, Bridge, State, Apply, Decks, Rolls, Causes

local paidHere = {}       -- what this room's kills and breaks dropped (the clear's context)
local toPay = {}          -- drawn kills / breaks: paid on the next update, then their effect line
local PAY_AFTER = 1       -- frames: let the game's own drop show (and be cancelled), then pay

local function floorNo() return D.Sched.floor() end
local function on() return cfg.CONTROL_DROPS and Bridge.ready() and not game:IsGreedMode() end

local function aliveEnemies()
  local names, n = {}, 0
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    if e:IsActiveEnemy(false) and e:IsVulnerableEnemy() and not e:HasEntityFlags(EntityFlag.FLAG_FRIENDLY) then
      n = n + 1
      if #names < 12 then names[#names + 1] = State.enumName(EntityType, e.Type, "^ENTITY_") end
    end
  end
  return n, names
end

local function breakablesHere()
  local room, n = game:GetRoom(), 0
  local G = GridEntityType or {}
  for i = 0, room:GetGridSize() - 1 do
    local g = room:GetGridEntity(i)
    if g then
      local t = g:GetType()
      if (t == G.GRID_POOP or t == G.GRID_ROCK_ALT) and g.State ~= 2 and (g.State or 0) < 1000 then n = n + 1 end
    end
  end
  return n
end

-- A kill or break the deck takes: draws its entry (once), to pay on the next update.
local function take(rec, cause)
  if rec.drawn then return rec.entry ~= nil end
  rec.drawn = true
  if not (on() and (cause ~= "breakable" or cfg.DROPS_NATURAL)) then return false end
  local e = Decks.draw("drops", floorNo(), { cause = cause })
  if not e then return false end                  -- empty: the game's own drop stands
  rec.entry, rec.owner, rec.cancelled, rec.cause = e, "drops", {}, cause
  toPay[#toPay + 1] = rec
  return true
end

local function qualifies(npc)
  if npc:HasEntityFlags(EntityFlag.FLAG_FRIENDLY) then return false end
  if npc.CanShutDoors == false then return false end
  return true
end

function Drops.init(deps)
  D, cfg, Bridge, State, Apply = deps, deps.cfg, deps.Bridge, deps.State, deps.Apply
  Decks, Rolls, Causes = deps.Decks, deps.Rolls, deps.Causes
  Decks.define("drops", {
    kind = "drops",
    key = function(f) return "drops:" .. f end,
    entries = function(d)
      local out = {}
      for i, o in ipairs(d.outcomes or {}) do out[i] = { reward = o.reward, count = o.count or 1 } end
      return out
    end,
    context = function()
      local n, names = aliveEnemies()
      local room = D.Sched.room()
      return { room_type = room and State.roomTypeName(room.type) or nil, enemies = names, enemies_left = n,
               breakables = breakablesHere() }
    end,
    wanted = function()
      return math.max(1, math.min(8, (aliveEnemies()) + math.min(3, breakablesHere()) + cfg.DROPS_SPARE))
    end,
    wants = function()
      local room = D.Sched.room()
      if not room or room.wasClear then return {} end
      local n = aliveEnemies()
      if n == 0 then return {} end
      return { { arg = floorNo(), n = math.min(n, 8), reason = "restock: more kills than entries left" } }
    end,
    refresh = { max = 1, minMs = cfg.REFRESH_MIN_MS },
    maxSends = cfg.MAX_DROPS_SENDS,
    askGapMs = cfg.REFRESH_MIN_MS,
    mercy = function(e)
      local name = Decks.mercyReward(e.reward)
      if name ~= e.reward then return { reward = name, count = 1 } end
      return e
    end,
    outcome = function(e) return { reward = e.reward, count = e.count } end,
    enabled = function() return cfg.CONTROL_DROPS end,
  })

  -- The roll: a kill.
  Rolls.on("npc_death", "drops", function(npc)
    local d = Causes.noteDeath(npc)
    if d.breakable then
      return nil, take(d, "breakable") and "dm" or "vanilla"
    end
    if d.boss or not qualifies(npc) then return nil, d.boss and "vanilla" or nil end
    return nil, take(d, "enemy_death") and "dm" or "vanilla"
  end)
  -- ...or a break (a tinted rock only when no layout planned its loot: rocks.lua listens first).
  local function onBreak(b)
    if b.owner then return end
    take(b, "breakable")
    Rolls.count("break", b.kind, b.entry and "drops" or nil)
  end
  Causes.on("rock_break", onBreak)
  Causes.on("breakable", onBreak)

  -- The game's own drop from a kill or break the deck took: cancelled (its entry is paid instead).
  Rolls.claimSpawns("drops", function(info)
    local src = info.source
    if not src or (info.cause ~= "enemy_death" and info.cause ~= "breakable" and info.cause ~= "rock_break") then
      return nil
    end
    if info.variant == PickupVariant.PICKUP_COLLECTIBLE then return nil end   -- an enemy's item (Krampus): never
    if not src.drawn and info.cause == "enemy_death" then take(src, "enemy_death") end   -- spawned before its death callback
    if src.owner ~= "drops" then return nil end
    src.cancelled[#src.cancelled + 1] = Apply.rewardNameFor(info.variant, info.subtype)
    return "cancel"
  end)
end

local function line(rec)
  local e = rec.entry
  local what = e.reward == "nothing" and "nothing" or Apply.count("+", rec.paid or 0, e.reward)
  if e.reward ~= "nothing" and (rec.paid or 0) == 0 then what = "nothing (" .. e.reward .. " unavailable)" end
  local game_s = #rec.cancelled > 0 and (" (instead of the game's " .. table.concat(rec.cancelled, ", ") .. ")") or ""
  return (rec.cause == "enemy_death" and "kill" or "break") .. ": " .. what .. game_s
end

--- Every frame: pay the entries of kills and breaks drawn a frame ago.
function Drops.update()
  if #toPay == 0 then return end
  local f = game:GetFrameCount()
  local rest = {}
  for _, rec in ipairs(toPay) do
    if f - rec.frame < PAY_AFTER then
      rest[#rest + 1] = rec
    else
      local e = rec.entry
      rec.paid = 0
      if e.reward ~= "nothing" then
        for _ = 1, math.max(1, math.min(2, tonumber(e.count) or 1)) do
          if Apply.spawnReward(e.reward, rec.pos) then rec.paid = rec.paid + 1 end
        end
        if rec.paid > 0 then paidHere[#paidHere + 1] = e.reward end
      end
      if e.reward ~= "nothing" or #rec.cancelled > 0 then
        Bridge.effect(Decks.get("drops", floorNo()).key, { line(rec) })
      end
    end
  end
  toPay = rest
end

--- What this room's kills and breaks dropped so far (names, in order).
function Drops.paidHere()
  local out = {}
  for i, n in ipairs(paidHere) do out[i] = n end
  return out
end

function Drops.onNewRoom(room)
  paidHere, toPay = {}, {}
  -- Asked on the way in, sized to this room's fight: the answer replaces the unused tail.
  if on() and not room.wasClear and (aliveEnemies()) > 0 then
    Decks.request("drops", floorNo(), "player entered the room")
  end
end

function Drops.resetRun() paidHere, toPay = {}, {} end

return Drops
