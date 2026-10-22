-- curses:<f>  The next floor's curses and treasure-room distance (MC_POST_CURSE_EVAL).
--
-- A deck of one, asked on entering this floor's boss room and rewritten once the boss dies. The roll is
-- the next floor's curse evaluation. If the plan isn't in by then, the floor is generated with the game's
-- curses, held, and changed once the plan lands. Optionally (ALLOW_LAYOUT_RESEED) the floor is rerolled
-- until the treasure-room distance matches the DM's wish.
local Curses = {}

local game = Game()
local D, cfg, Bridge, State, Apply, Taunt, Decks, Rolls

local asked = false       -- a plan was asked from this floor's boss room (the next floor's curses wait for it)
local askedFor            -- {stage, stage_type} it was asked from
local used, usedKey       -- the last plan applied (re-used if we reseed), and its key
local reseeds = 0
local bossReplanned = false
local late = false        -- the new floor was generated before its plan landed: it is held for it

local function floorEffect(plan, curses)
  if plan.treasure_distance and plan.treasure_distance ~= "any" and not cfg.ALLOW_LAYOUT_RESEED then
    curses[#curses + 1] = "treasure room " .. plan.treasure_distance .. ": ignored (layout rerolls are off)"
  end
  return curses
end

-- Optional: reroll the layout until the DM's treasure-distance wish is met. True if it rerolled.
local function maybeReseed()
  if cfg.ALLOW_LAYOUT_RESEED and used and used.treasure_distance ~= "any" then
    local dist = State.level().treasure_distance_from_start
    local want = used.treasure_distance
    local ok = dist == nil or (want == "far" and dist >= 4) or (want == "near" and dist <= 2)
    if not ok and reseeds < cfg.MAX_RESEEDS then
      reseeds = reseeds + 1
      Bridge.effect(usedKey, { "floor rerolled: treasure room " .. tostring(dist) .. " rooms away, wanted " .. want })
      D.Sched.forgetLevel()                    -- the reseeded floor counts as a new floor
      Isaac.ExecuteCommand("reseed")
      return true
    end
  end
  reseeds = 0
  return false
end

local function request(reason)
  local level = game:GetLevel()
  asked = true
  askedFor = { level:GetStage(), level:GetStageType() }
  Decks.request("curses", D.Sched.floor(), reason, true)
end

function Curses.init(deps)
  D, cfg, Bridge, State, Apply, Taunt = deps, deps.cfg, deps.Bridge, deps.State, deps.Apply, deps.Taunt
  Decks, Rolls = deps.Decks, deps.Rolls
  Decks.define("curses", {
    kind = "floor_plan",
    key = function(f) return "curses:" .. f end,
    entries = function(d) return { d } end,
    deck = false,
    context = function()
      return { current_stage = askedFor and askedFor[1], current_stage_type = askedFor and askedFor[2],
               allowed_layout_rerolls = cfg.ALLOW_LAYOUT_RESEED and cfg.MAX_RESEEDS or 0 }
    end,
    deadline = 12000,
    crossFloor = true,                             -- the next floor still waits on a late one
    holdLabel = "this floor's curses",
    outcome = function() return { applied = true } end,
    enabled = function() return cfg.CONTROL_FLOOR end,
  })

  Rolls.on("curse_eval", "curses", function(curses)
    if not cfg.CONTROL_FLOOR or game:IsGreedMode() then return nil, "vanilla" end
    local f = D.Sched.floor()                    -- still the floor that asked: the new one isn't counted yet
    local key = Decks.get("curses", f).key
    local fresh = Decks.draw("curses", f, { cause = "floor_start" })
    local plan = fresh or (reseeds > 0 and used) or nil
    if not plan then
      local wasAsked = asked
      asked = false
      if not (wasAsked and Bridge.ready()) then return nil, "vanilla" end
      -- Asked but not in: the floor is generated now, and its curses change once the plan lands.
      late = true
      Decks.owe("curses", f, { cause = "floor_start" }, function(plan2)
        late = false
        if not plan2 then return end
        used, usedKey = plan2, key
        Bridge.effect(key, floorEffect(plan2, Apply.cursesNow(plan2)))
        maybeReseed()
      end)
      return nil, "dm"
    end
    asked = false
    used = plan
    local mask = Apply.curses(plan, curses)
    if fresh then
      usedKey = key
      Bridge.effect(key, floorEffect(plan, Apply.curseEffect(curses, mask)))
    end
    return mask, "dm"
  end)
end

function Curses.onNewRoom(room)
  bossReplanned = false
  if cfg.CONTROL_FLOOR and room.type == RoomType.ROOM_BOSS then request("player entered the boss room") end
end

function Curses.update(room)
  -- Boss dead: re-plan the next floor with the post-fight state.
  if room.type == RoomType.ROOM_BOSS and game:GetRoom():IsClear() and not bossReplanned and cfg.CONTROL_FLOOR then
    bossReplanned = true
    request("boss defeated")
  end
end

--- A new floor was generated (POST_NEW_LEVEL): a reroll waits for a late plan, else it may happen now.
function Curses.onNewLevel()
  if late then return end                        -- any reroll waits for the late plan
  maybeReseed()
end

function Curses.resetRun()
  asked, askedFor, used, usedKey, reseeds, bossReplanned, late = false, nil, nil, nil, 0, false, false
end

return Curses
