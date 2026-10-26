-- layout:<f>:<room>  How an unvisited room is dressed (on its first MC_POST_NEW_ROOM): normal and special rooms.
--
-- A deck of one, keyed by the room, written for its type, shape, difficulty and what its layout file places
-- (rocks, free pickups, enemies). Asked the moment the player enters a room next to it (the soonest they could
-- walk in is about a second away), and rewritten if their state changes while they linger (MAX_LAYOUT_SENDS a
-- room). Not in when they walk in: the game is held and the room dressed late. (What its tinted rocks hold is
-- the floor's rocks deck, rocks.lua.) In agent mode every room on the floor is asked at floor start instead, so
-- the agent dresses the floor in a few turns; asking again from next door then only reaches the agent if the
-- re-ask gate finds the player's state has moved. A room off the floor's map (devil, angel, crawlspace) can't be
-- asked ahead: it is only held for when its layout file places free pickups, the rolls it would leave vanilla.
local Layout = {}

local game = Game()
local D, cfg, Bridge, State, Apply, Decks, Rolls

local roomKeys = {}       -- key -> idx: the neighbours asked about from this room (the loading screen waits on these)
local floorAsked = -1     -- the floor whose every room was asked at its start (agent mode)

local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end
local function eligible(t) return t ~= nil and t ~= RoomType.ROOM_NULL end
local function startIdx() return game:GetLevel():GetStartingRoomIndex() end

--- Will the room being entered be dressed? Not the floor's start room, nor an off-map room with no free
--- pickups and no deck already in.
local function dressed(desc)
  if not (cfg.CONTROL_ROOM_LAYOUT and desc and desc.Data and eligible(desc.Data.Type)) then return false end
  if desc.SafeGridIndex == startIdx() then return false end
  if desc.SafeGridIndex >= 0 or Decks.left("layout", desc.SafeGridIndex) > 0 then return true end
  local free = State.freePickups(State.roomContents(desc))
  return free == nil or free > 0
end

local function apply(idx, d, late)
  local key = Decks.get("layout", idx).key
  Bridge.effect(key, Apply.roomLayout(d, late))
end

function Layout.init(deps)
  D, cfg, Bridge, State, Apply, Decks, Rolls = deps, deps.cfg, deps.Bridge, deps.State, deps.Apply, deps.Decks, deps.Rolls
  Decks.define("layout", {
    kind = "room_layout",
    key = function(idx) return "layout:" .. D.Sched.floor() .. ":" .. idx end,
    entries = function(d) return { d } end,
    deck = false,
    context = function(st, reason)
      local level = game:GetLevel()
      local desc = level:GetRoomByIdx(st.arg)
      if not (desc and desc.Data) then return false end
      return { room_type = State.roomTypeName(desc.Data.Type), room_shape = desc.Data.Shape,
               room_difficulty = desc.Data.Difficulty, room_contents = State.roomContents(desc),
               distance_from_start = State.gridDistance(level:GetStartingRoomIndex(), desc.SafeGridIndex),
               first_room_of_floor = desc.SafeGridIndex == level:GetStartingRoomIndex(),
               player_is_next_door = reason ~= "floor start" and curIdx() ~= st.arg }
    end,
    wants = function()
      local out = {}
      for _, idx in pairs(roomKeys) do
        local d = game:GetLevel():GetRoomByIdx(idx)
        if d and d.VisitedCount == 0 then
          out[#out + 1] = { arg = idx, n = 0, reason = "player entered an adjacent room" }
        end
      end
      return out
    end,
    refresh = { max = math.max(0, cfg.MAX_LAYOUT_SENDS - 1), minMs = cfg.REFRESH_MIN_MS * 2,
                reason = "refresh: player state changed while lingering" },
    maxSends = cfg.MAX_LAYOUT_SENDS,
    deadline = 10000,
    holdLabel = "this room",
    outcome = function(d) return { applied = true, layout_pickups = d.layout_pickups } end,
    enabled = function() return cfg.CONTROL_ROOM_LAYOUT end,
  })
  -- The pickups a room's layout file places are this deck's when it dresses the room (keep, remove, morph).
  Rolls.claimRoomEntities("layout", function()
    local desc = game:GetLevel():GetCurrentRoomDesc()
    return dressed(desc) and (Bridge.ready() or Decks.left("layout", desc.SafeGridIndex) > 0)
  end)
end

local function prefetch(reason)
  for _, d in ipairs(D.Items.neighbours()) do
    if d.VisitedCount == 0 and cfg.CONTROL_ROOM_LAYOUT and eligible(d.Data.Type) then
      if not Decks.upToDate("layout", d.SafeGridIndex) then Decks.request("layout", d.SafeGridIndex, reason) end
      roomKeys[Decks.get("layout", d.SafeGridIndex).key] = d.SafeGridIndex
    end
  end
end

function Layout.onNewRoom(room)
  roomKeys = {}
  -- 1. Dress this room: now if the DM already decided how, else once it has (the game waits).
  if room.firstVisit and cfg.CONTROL_ROOM_LAYOUT then
    local idx, entered = room.idx, game:GetFrameCount()
    local d = Decks.draw("layout", idx, { cause = "room_enter" })
    if d then
      apply(idx, d, 0)
      Rolls.count("room_layout", State.roomTypeName(room.type), "layout")
    elseif dressed(game:GetLevel():GetCurrentRoomDesc()) then
      if Bridge.ready() then
        Decks.owe("layout", idx, { cause = "room_enter" }, function(late)
          if not late then return end
          if curIdx() ~= idx then
            return Bridge.effect(Decks.get("layout", idx).key, { "not applied: the player left the room" })
          end
          apply(idx, late, game:GetFrameCount() - entered)
        end)
        Decks.holdNow("layout", idx)
        Rolls.count("room_layout", State.roomTypeName(room.type), "layout")
      else
        Rolls.count("room_layout", State.roomTypeName(room.type), nil)    -- no backend: the game's own
      end
    end
  end
  -- 2. Ask about every unvisited room the player could walk into next.
  if Bridge.ready() then
    room.layoutPrefetched = true
    prefetch("player entered an adjacent room")
  end
end

--- Agent mode: every unvisited room on the floor, once, after the start room's neighbours.
local function askFloor()
  local level = game:GetLevel()
  local rooms, start = level:GetRooms(), level:GetStartingRoomIndex()
  for i = 0, rooms.Size - 1 do
    local d = rooms:Get(i)
    if d and d.Data and d.VisitedCount == 0 and eligible(d.Data.Type) and d.SafeGridIndex ~= start then
      local st = Decks.get("layout", d.SafeGridIndex)
      if st.sends == 0 and not Bridge.isPending(st.key) then Decks.request("layout", d.SafeGridIndex, "floor start") end
    end
  end
end

function Layout.update(room)
  if not room.layoutPrefetched and Bridge.ready() then   -- the backend connected after we walked in
    room.layoutPrefetched = true
    prefetch("backend connected")
  end
  if cfg.CONTROL_ROOM_LAYOUT and Bridge.agentMode() and floorAsked ~= D.Sched.floor() then
    floorAsked = D.Sched.floor()
    askFloor()
  end
end

function Layout.readyKeys(add)
  for key in pairs(roomKeys) do add(key) end
end

function Layout.onNewFloor() roomKeys = {} end
function Layout.resetRun() roomKeys, floorAsked = {}, -1 end

return Layout
