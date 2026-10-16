-- The loading screen: every floor (the run's first one included) waits here until the DM is ready
-- for it.
--
-- "Ready" means two things. First, the backend says this floor's plans are in: the run narrative
-- (on the first floor) and the floor brief. Decisions are only made against the brief, so the
-- floor's first asks are held on the backend until it lands. Second, every decision the floor
-- opened with has an answer (the start room's neighbour layouts, the floor's item pools, the
-- consumables plan). Without this screen those first asks would land after the player walked
-- through the door, and vanilla RNG would decide.
--
-- On a new floor the mod sends "prepare" with a snapshot and the floor number; the backend
-- answers "ready" for that floor. Meanwhile input is blocked, the player can't be hurt, the HUD is
-- hidden and the run clock is stopped. The wait can't be skipped. The screen also ends on its own if the
-- backend can't be reached (CONNECT) or takes too long (TIMEOUT): a DM that never answers must not
-- lock the player out of the game. Nothing here ever blocks the game after it ends.
local Loading = {}

local game = Game()
local cfg, Bridge, State, Taunt, Sched

local on = false
local runStart = false   -- this wait also covers the run narrative
local floor = 0          -- the floor being waited for
local loadedFloor = 0    -- the last floor that got a loading screen
local startedAt, startFrame, readyAt
local sent = false
local verdict            -- the backend's "ready" status for this floor, once it arrives
local noticeAt           -- when to tell the player the DM isn't playing this run, if it isn't
local keysDone, keysTotal = 0, 0
local clock, lastTime = 0, nil  -- ms spent on screen while the game ran: a pause or tab-out doesn't count

local function tick()
  local now = Isaac.GetTime()
  if lastTime then clock = clock + math.max(0, math.min(now - lastTime, 250)) end
  lastTime = now
  return clock
end

local font = Font()
local fontOk = pcall(function() font:Load("font/pftempestasevencondensed.fnt") end) and font:IsLoaded()
local RED, WHITE, GREY = KColor(1, 0.35, 0.35, 1), KColor(1, 1, 1, 1), KColor(0.7, 0.7, 0.7, 1)
local SHADOW = KColor(0, 0, 0, 0.9)

local RUN_LINES = {
  "The DM is writing your run",
  "The DM is choosing what to take from you",
  "The DM is planning your first betrayal",
  "The DM is rehearsing the jokes",
}
local FLOOR_LINES = {
  "The DM is writing this floor",
  "The DM is reading your pockets",
  "The DM is deciding what you need least",
}
local LINE_MS = 4500

local function hud(visible)
  pcall(function() game:GetHUD():SetVisible(visible) end)
end

function Loading.active() return on end

local function begin(n)
  floor, sent, verdict, keysDone, keysTotal = n, false, nil, 0, 0
  if on then return end                  -- a reseed mid-wait: same screen, new floor number
  -- No backend has answered since the game started: likely none is running, so don't make them wait.
  if cfg.LOADING_SCREEN == false or not Bridge.seen() then return end
  on, startedAt, startFrame, readyAt = true, tick(), game:GetFrameCount(), nil
  Bridge.holdPending(true)
  hud(false)
end

--- A new run (or a continue) has started: its first floor waits for the run narrative too.
function Loading.start()
  on, runStart, loadedFloor, readyAt = false, true, 0, nil
  noticeAt = Isaac.GetTime() + cfg.LOADING_CONNECT_MS
  begin(0)
end

local function finish(why)
  if not on then return end
  on, runStart = false, false
  hud(true)
  Bridge.holdPending(false)
  -- the clock kept running while the player stood here; it isn't part of the run
  State.addPausedFrames(game:GetFrameCount() - startFrame)
  Isaac.DebugString("[LLMDM] loading screen for floor " .. floor .. " over: " .. why)
  if why == "timeout" then Taunt.show("Still writing. I'll improvise.")
  elseif verdict == "failed" then Taunt.show("The plan never came. I'll improvise.") end
end

--- Bridge handler for the backend's "ready". A "ready" for an earlier floor is ignored.
function Loading.handle(msg)
  if not msg.status then return end
  if msg.floor ~= nil and tonumber(msg.floor) ~= floor then return end
  verdict = tostring(msg.status)
end

-- How many of the floor's opening decisions have an answer (or a definite "none").
local function countKeys()
  local keys = Sched.floorReadyKeys()
  local done = 0
  for _, k in ipairs(keys) do
    if Bridge.settled(k) then done = done + 1 end
  end
  return done, #keys
end

--- MC_POST_RENDER, after Bridge.update: ask, wait, and end the wait.
function Loading.update()
  if noticeAt and Isaac.GetTime() >= noticeAt then
    noticeAt = nil
    local why = Bridge.problem()
    if why then Taunt.show(why) end
  end
  -- A new floor (or the run's first) starts its own wait.
  local f = Sched.floor()
  if f > 0 and f ~= loadedFloor then
    loadedFloor = f
    begin(f)
  end
  if not on then return end
  local t = tick()
  if floor == 0 then return end           -- run start, before the first floor is known
  if Bridge.ready() then
    if not sent then                     -- also again after a reconnect: the backend answers once per ask
      sent, readyAt = true, readyAt or t
      Bridge.prepare(State.snapshot(), floor)
    end
  else
    sent = false
  end
  if verdict then
    keysDone, keysTotal = countKeys()
    if keysDone >= keysTotal then return finish(verdict) end
  end
  if not readyAt and t - startedAt > cfg.LOADING_CONNECT_MS then return finish("offline") end
  local limit = runStart and cfg.LOADING_TIMEOUT_MS or (cfg.LOADING_FLOOR_TIMEOUT_MS or cfg.LOADING_TIMEOUT_MS)
  if readyAt and t - readyAt > limit then return finish("timeout") end
  -- Keep the player safe and the room dark while waiting.
  pcall(function() Isaac.GetPlayer(0):SetMinDamageCooldown(30) end)
  pcall(function() game:Darken(1, 2) end)
end

--- MC_INPUT_ACTION: nothing the player presses reaches the game while the screen is up.
function Loading.blockInput(_, _, hook, action)
  if not on then return nil end
  if action == ButtonAction.ACTION_PAUSE then return nil end   -- they can always leave
  if hook == InputHook.GET_ACTION_VALUE then return 0 end
  return false
end

local function draw(s, x, y, color, boxW)
  if fontOk then
    font:DrawStringScaled(s, x + 1, y + 1, 1, 1, SHADOW, boxW, true)
    font:DrawStringScaled(s, x, y, 1, 1, color, boxW, true)
  else
    Isaac.RenderText(s, math.max(4, x + boxW / 2 - #s * 3), y, color.Red, color.Green, color.Blue, color.Alpha)
  end
end

--- MC_POST_RENDER.
function Loading.render()
  if not on then return end
  local t = clock
  local w, h = Isaac.GetScreenWidth(), Isaac.GetScreenHeight()
  local secs = math.floor((t - startedAt) / 1000)
  local line
  if not readyAt then
    line = "Finding the DM"
  elseif verdict then
    line = string.format("The DM is dressing the rooms (%d/%d)", keysDone, keysTotal)
  else
    local lines = runStart and RUN_LINES or FLOOR_LINES
    line = lines[(math.floor((t - readyAt) / LINE_MS) % #lines) + 1]
  end
  draw(line .. string.rep(".", 1 + math.floor(t / 400) % 3), 0, h / 2 - 14, RED, w)
  draw(string.format("%ds", secs), 0, h / 2 + 4, WHITE, w)
end

function Loading.init(config, bridge, state, taunt, sched)
  cfg, Bridge, State, Taunt, Sched = config, bridge, state, taunt, sched
end

return Loading
