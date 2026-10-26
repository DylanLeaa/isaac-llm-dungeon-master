-- The floor and room lifecycle: what floor and room the player is in, and telling the streams.
--
-- WHEN each decision is asked lives with its stream (dm/streams/*.lua), as that deck's forecast: a
-- room's layout when the player stands next door, its clear reward when the fight's ETA drops under the
-- answer's latency, its drops on the way in, the next floor's curses in the boss room, the card, trinket
-- and pill decks kept stocked from floor start, a machine's schedule when it shows up. decks.lua does
-- the restocking and the stale-tail rewrites; rolls.lua routes each of the game's rolls to its stream.
-- This file only counts floors, tracks the room, and calls the streams' hooks in order:
--   onNewFloor(n), onNewRoom(room, prev), onNewLevel(), update(room), readyKeys(add), resetRun()
local Sched = {}

local game = Game()
local D, streams

local floorNo = 0
local levelSig
local room = nil         -- the current room: {idx, type, enteredAt, enteredFrame, firstVisit, wasClear, ...}

-- Key prefixes that belong to one floor, forgotten when it ends (curses: the next floor may still wait on one).
local FLOOR_PREFIXES = { "layout", "clear", "items", "cards", "trinkets", "pills", "drops", "payout" }

function Sched.init(deps, list) D, streams = deps, list end

function Sched.floor() return floorNo end
function Sched.room() return room end

local function each(method, ...)
  for _, s in ipairs(streams) do
    if s[method] then s[method](...) end
  end
end

--- A reseeded floor counts as a new one.
function Sched.forgetLevel() levelSig = nil end

local function checkLevelChange()
  local level = game:GetLevel()
  local sig = level:GetStage() .. "/" .. level:GetStageType() .. "/" ..
              game:GetSeeds():GetStageSeed(level:GetStage())
  if sig == levelSig then return false end
  levelSig = sig
  floorNo = floorNo + 1
  for _, prefix in ipairs(FLOOR_PREFIXES) do D.Bridge.forget(prefix .. ":" .. (floorNo - 1) .. ":") end
  for _, prefix in ipairs({ "cards", "trinkets", "pills", "drops", "rocks" }) do D.Bridge.forget(prefix .. ":" .. (floorNo - 1)) end
  D.Decks.onNewFloor()
  D.Causes.reset()
  each("onNewFloor", floorNo)
  D.State.onNewFloor()
  return true
end

function Sched.onNewRoom()
  if game:IsGreedMode() then return end
  checkLevelChange()
  local r, desc = game:GetRoom(), game:GetLevel():GetCurrentRoomDesc()
  D.State.onRoomEnter()
  local prev = room
  room = {
    idx = desc.SafeGridIndex, type = r:GetType(), enteredAt = Isaac.GetTime(),
    enteredFrame = game:GetFrameCount(), firstVisit = r:IsFirstVisit(), wasClear = r:IsClear(),
  }
  D.Causes.onNewRoom()
  each("onNewRoom", room, prev)
end

function Sched.onNewLevel()
  if game:IsGreedMode() then return end
  checkLevelChange()
  D.Bridge.event("new_level", { stage = game:GetLevel():GetStage(), name = game:GetLevel():GetName() })
  each("onNewLevel")
end

--- Every frame: what just happened first (causes.lua), then the streams, then the decks' restocking.
function Sched.update()
  if not room or game:IsGreedMode() then return end
  D.Causes.update()
  each("update", room)
  D.Decks.update()
  D.Rolls.update()
end

function Sched.resetRun()
  floorNo, levelSig, room = 0, nil, nil
  D.Decks.reset()
  D.Causes.reset()
  D.Rolls.reset()
  each("resetRun")
end

--- The decisions the player could need the moment they step out of the start room: its neighbours'
--- layouts and the item rankings for any special room next door. The loading screen waits for these
--- only; the floor's other pools and decks are asked in the background, and if the player gets to one
--- first, its hold covers it. Only keys that were actually sent count (a request made while disconnected
--- would never settle).
function Sched.floorReadyKeys()
  local keys = {}
  each("readyKeys", function(key)
    if D.Bridge.isPending(key) or D.Bridge.settled(key) then keys[#keys + 1] = key end
  end)
  return keys
end

function Sched.debugCounts()
  return string.format("floor %d", floorNo)
end

return Sched
