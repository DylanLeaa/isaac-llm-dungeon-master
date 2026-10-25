-- clear:<f>:<room>  The room-clear award (MC_PRE_SPAWN_CLEAN_AWARD).
--
-- A deck of one, keyed by the room: it is about how this fight went (hits taken, time, what the kills
-- already dropped), so it is asked as late as the answer can still land: when the estimated time to clear
-- (remaining enemy HP / recent DPS) drops under LEAD_FACTOR * p90 latency + LEAD_PAD_MS. It is rewritten
-- if the player gets hit or picks something up (or another deck spends its budget) while there's still
-- time. In "replace" rooms the DM's drop is the drop; in "bonus" rooms (bosses) the game keeps its own
-- award and the DM's comes on top. Not in when the room clears: the game's award is skipped and the game
-- waits for the DM's, dropped where the game's would have been. In agent mode every fight room's award is asked
-- at floor start too, before its fight: the late ask then only reaches the agent if the re-ask gate finds the
-- fight changed things, and a room found already clear gives its award back to the floor's budget.
local Clear = {}

local game = Game()
local D, cfg, Bridge, State, Apply, Decks, Rolls, Causes

local MODE = {}         -- RoomType -> "replace" | "bonus"
local probe = nil       -- this room's prefetch tracker
local floorAsked = -1   -- the floor whose every fight room was asked at its start (agent mode)

local function curIdx() return game:GetLevel():GetCurrentRoomDesc().SafeGridIndex end
local function floorNo() return D.Sched.floor() end

local function alive()
  local hp, n = 0, 0
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    if e:IsActiveEnemy(false) and e:IsVulnerableEnemy() and not e:HasEntityFlags(EntityFlag.FLAG_FRIENDLY) then
      hp = hp + math.max(0, e.HitPoints)
      n = n + 1
    end
  end
  return hp, n
end

local function leadMs() return cfg.LEAD_FACTOR * Bridge.latencyP90() + cfg.LEAD_PAD_MS end

function Clear.eligible(t) return MODE[t] ~= nil end

local function request(reason, eta, deadline)
  local room = D.Sched.room()
  probe.eta, probe.deadline = eta, deadline
  if Decks.request("clear", room.idx, reason, deadline ~= nil, reason:find("^stale") ~= nil) then
    probe.sends = probe.sends + 1
    probe.askedHash, probe.askedDrops = State.materialHash(), #D.Drops.paidHere()
  end
end

function Clear.init(deps)
  D, cfg, Bridge, State, Apply = deps, deps.cfg, deps.Bridge, deps.State, deps.Apply
  Decks, Rolls, Causes = deps.Decks, deps.Rolls, deps.Causes
  for name, mode in pairs({ ROOM_DEFAULT = "replace", ROOM_MINIBOSS = "replace", ROOM_CURSE = "replace",
                            ROOM_BOSS = "bonus" }) do
    if RoomType[name] then MODE[RoomType[name]] = mode end
  end

  Decks.define("clear", {
    kind = "room_clear",
    key = function(idx) return "clear:" .. floorNo() .. ":" .. idx end,
    entries = function(d) return { d } end,
    deck = false,
    context = function(st)
      local room = D.Sched.room()
      if not room or st.arg ~= room.idx then       -- asked ahead, at floor start: its fight hasn't happened yet
        local desc = game:GetLevel():GetRoomByIdx(st.arg)
        if not (desc and desc.Data) then return false end
        return { room_type = State.roomTypeName(desc.Data.Type), mode = MODE[desc.Data.Type],
                 room_shape = desc.Data.Shape, room_difficulty = desc.Data.Difficulty, fight = "not started yet" }
      end
      local hp, n = alive()
      return { room_type = State.roomTypeName(room.type), mode = MODE[room.type],
               enemies_left = n, enemy_hp_left = math.floor(hp),
               est_ms_until_clear = math.floor(math.min(probe and probe.eta or 0, 60000)),
               seconds_in_room = math.floor((Isaac.GetTime() - room.enteredAt) / 100) / 10,
               kills_dropped = D.Drops.paidHere() }
    end,
    deadline = function()
      if probe and probe.deadline then return probe.deadline end
      local eta = probe and probe.eta or 4000
      return math.floor(math.max(1500, math.min(eta, 10000)) + 2000)
    end,
    holdLabel = "your reward",
    mercy = function(d)
      local rewards, changed = {}, false
      for i, r in ipairs(d.rewards or {}) do
        local name, swapped = Decks.mercyReward(r.reward)
        rewards[i] = swapped and { reward = name, count = 1 } or r
        changed = changed or swapped
      end
      if not changed then return d end
      local out = {}
      for k, v in pairs(d) do out[k] = v end
      out.rewards = rewards
      return out
    end,
    outcome = function(d) return { rewards = d.rewards or {} } end,
    enabled = function() return cfg.CONTROL_ROOM_CLEAR end,
  })

  Rolls.on("clean_award", "clear", function(rng, pos)
    local room = D.Sched.room()
    if not room or game:IsGreedMode() or not cfg.CONTROL_ROOM_CLEAR then return nil, "vanilla" end
    if not Clear.eligible(game:GetRoom():GetType()) then return nil, "vanilla" end
    Causes.noteAward()
    local secs, hits = State.onRoomCleared()
    Bridge.event("room_cleared", { room = room.idx, seconds = secs, hits = hits })
    local idx, at = room.idx, Vector(pos.X, pos.Y)
    local key = Decks.get("clear", idx).key
    room.cleared = true
    local d = Decks.draw("clear", idx, { cause = "clean_award" })
    if d then
      Bridge.effect(key, Apply.roomClear(d, at))
    elseif Bridge.ready() then
      if probe then probe.eta, probe.deadline = 0, 10000 end   -- asked now (or again): the room is clear
      -- Not in yet: the game's award is skipped (or, for a bonus, spawned without it), the game waits,
      -- and the DM's drops where the game's would have.
      Decks.owe("clear", idx, { cause = "clean_award" }, function(late)
        if not late then return end
        if curIdx() ~= idx then return Bridge.effect(key, { "not applied: the player left the room" }) end
        Bridge.effect(key, Apply.roomClear(late, at))
      end)
      Decks.spec("clear").holdLabel = MODE[room.type] == "bonus" and "what else you get" or "your reward"
      Decks.holdNow("clear", idx)
    else
      return nil, "vanilla"                        -- no backend: the game's own award
    end
    if MODE[room.type] == "bonus" then return nil, "dm" end   -- the game still spawns its own award
    -- Documented API bug: returning true stops the award seed advancing. Not every build exposes the
    -- field, and a throw here would let the game's own award through, so never let it escape.
    local desc = game:GetLevel():GetCurrentRoomDesc()
    pcall(function() desc.AwardSeed = rng:GetSeed() end)
    return true, "dm"
  end)
end

function Clear.onNewRoom(room, prev)
  -- The last room's reward was asked for but never used (left mid-fight): it promises nothing now.
  if prev and probe and probe.sends > 0 and not prev.cleared then Decks.abandon("clear", prev.idx) end
  -- Asked at floor start for a room with no fight after all: nothing will draw it.
  local ahead = room.wasClear and Decks.find(Decks.spec("clear").key(room.idx))
  if ahead and #ahead.drawn == 0 then Decks.abandon("clear", room.idx) end
  probe = { hpSamples = {}, startHp = nil, sends = 0, lastProbe = 0 }
end

--- Agent mode: every unvisited fight room on the floor, once.
local function askFloor()
  local level = game:GetLevel()
  local rooms, start = level:GetRooms(), level:GetStartingRoomIndex()
  for i = 0, rooms.Size - 1 do
    local d = rooms:Get(i)
    if d and d.Data and d.VisitedCount == 0 and MODE[d.Data.Type] and d.SafeGridIndex ~= start then
      local st = Decks.get("clear", d.SafeGridIndex)
      if st.sends == 0 and not Bridge.isPending(st.key) then Decks.request("clear", d.SafeGridIndex, "floor start") end
    end
  end
end

--- Every frame: ask when the fight is nearly over, rewrite if the player's state moved while there's time.
function Clear.update(room)
  if cfg.CONTROL_ROOM_CLEAR and Bridge.agentMode() and floorAsked ~= floorNo() then
    floorAsked = floorNo()
    askFloor()
  end
  local r = game:GetRoom()
  if not (cfg.CONTROL_ROOM_CLEAR and Clear.eligible(room.type) and not room.wasClear and not r:IsClear()) then return end
  local t = Isaac.GetTime()
  if t - probe.lastProbe < 100 then return end
  probe.lastProbe = t
  local hp, n = alive()
  if n == 0 then return end
  probe.startHp = math.max(probe.startHp or 0, hp)
  local s = probe.hpSamples
  s[#s + 1] = { t = t, hp = hp }
  while #s > 1 and t - s[1].t > 3000 do table.remove(s, 1) end
  local dps = 0
  if #s >= 2 and s[#s].t > s[1].t then
    dps = math.max(0, (s[1].hp - s[#s].hp) / (s[#s].t - s[1].t))    -- hp per ms
  end
  local eta = dps > 0 and hp / dps or math.huge
  local frac = hp / math.max(1, probe.startHp)
  local st = Decks.get("clear", room.idx)
  if probe.sends == 0 then
    if eta <= leadMs() or frac <= 0.15 or (n == 1 and frac <= 0.35) then
      request(eta <= leadMs() and "eta under lead time" or "almost clear", eta)
    end
  elseif probe.sends <= cfg.MAX_REFRESHES and t - st.lastAsk >= cfg.REFRESH_MIN_MS
      and eta > Bridge.latencyP90() then           -- only if the fresh answer can still land
    if st.staleFlag then
      request("stale: another deck spent this budget", eta)
    elseif State.materialHash() ~= probe.askedHash or #D.Drops.paidHere() ~= probe.askedDrops then
      request("refresh: player state or the kills' drops changed", eta)
    end
  end
end

function Clear.resetRun() probe, floorAsked = nil, -1 end

return Clear
