-- The loading screen: a new run waits here until the backend says the run narrative is written.
--
-- The narrative is the one plan that takes a while (Opus, 20-60s), and the first rooms are where
-- it matters most, so the run does not start playable without it. On game start the mod sends
-- "prepare" with a snapshot; the backend starts the planners and answers "ready" (status ready,
-- failed or none) once the narrative is in or has given up. Meanwhile input is blocked, the
-- player can't be hurt and the HUD is hidden. Enter skips the wait, and it ends on its own if the
-- backend can't be reached (CONNECT) or takes too long (TIMEOUT): a DM that never answers must
-- not lock the player out of the game. Nothing here ever blocks the game after it ends.
local Loading = {}

local game = Game()
local cfg, Bridge, State, Taunt

local on = false
local startedAt, startFrame, readyAt
local sent = false
local verdict            -- the backend's "ready" status, once it arrives
local checking = false   -- reading input ourselves: the input block must let that through

local font = Font()
local fontOk = pcall(function() font:Load("font/pftempestasevencondensed.fnt") end) and font:IsLoaded()
local RED, WHITE, GREY = KColor(1, 0.35, 0.35, 1), KColor(1, 1, 1, 1), KColor(0.7, 0.7, 0.7, 1)
local SHADOW = KColor(0, 0, 0, 0.9)

local LINES = {
  "The DM is writing your run",
  "The DM is choosing what to take from you",
  "The DM is planning your first betrayal",
  "The DM is rehearsing the jokes",
}
local LINE_MS = 4500

local function hud(visible)
  pcall(function() game:GetHUD():SetVisible(visible) end)
end

function Loading.active() return on end

--- A new run (or a continue) has started: wait for the narrative, if there's a backend to wait for.
function Loading.start()
  on, sent, verdict, readyAt = false, false, nil, nil
  if cfg.LOADING_SCREEN == false or not Bridge.hasTransport() then return end
  on, startedAt, startFrame = true, Isaac.GetTime(), game:GetFrameCount()
  hud(false)
end

local function finish(why)
  if not on then return end
  on = false
  hud(true)
  -- the clock kept running while the player stood here; it isn't part of the run
  State.addPausedFrames(game:GetFrameCount() - startFrame)
  Isaac.DebugString("[LLMDM] loading screen over: " .. why)
  if why == "failed" or why == "timeout" then Taunt.show("The narrative never came. I'll improvise.") end
end

--- Bridge handler for the backend's "ready".
function Loading.handle(msg)
  if msg.status then verdict = tostring(msg.status) end
end

local function skipped()
  checking = true
  local ok, pressed = pcall(function()
    return Input.IsButtonTriggered(Keyboard.KEY_ENTER, 0)
        or Input.IsActionTriggered(ButtonAction.ACTION_MENUCONFIRM, Isaac.GetPlayer(0).ControllerIndex)
  end)
  checking = false
  return ok and pressed
end

--- MC_POST_RENDER, after Bridge.update: ask, wait, and end the wait.
function Loading.update()
  if not on then return end
  local t = Isaac.GetTime()
  if Bridge.ready() then
    if not sent then                     -- also again after a reconnect: the backend answers once per ask
      sent, readyAt = true, readyAt or t
      Bridge.prepare(State.snapshot())
    end
  else
    sent = false
  end
  if verdict then return finish(verdict) end
  if skipped() then return finish("skipped") end
  if not readyAt and t - startedAt > cfg.LOADING_CONNECT_MS then return finish("offline") end
  if readyAt and t - readyAt > cfg.LOADING_TIMEOUT_MS then return finish("timeout") end
  -- Keep the player safe and the room dark while waiting.
  pcall(function() Isaac.GetPlayer(0):SetMinDamageCooldown(30) end)
  pcall(function() game:Darken(1, 2) end)
end

--- MC_INPUT_ACTION: nothing the player presses reaches the game while the screen is up.
function Loading.blockInput(_, _, hook, action)
  if not on or checking then return nil end
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
  local t = Isaac.GetTime()
  local w, h = Isaac.GetScreenWidth(), Isaac.GetScreenHeight()
  local secs = math.floor((t - startedAt) / 1000)
  local line = readyAt and LINES[(math.floor((t - readyAt) / LINE_MS) % #LINES) + 1] or "Finding the DM"
  draw(line .. string.rep(".", 1 + math.floor(t / 400) % 3), 0, h / 2 - 14, RED, w)
  draw(string.format("%ds", secs), 0, h / 2 + 4, WHITE, w)
  draw("Enter: skip and play without the narrative", 0, h - 22, GREY, w)
end

function Loading.init(config, bridge, state, taunt)
  cfg, Bridge, State, Taunt = config, bridge, state, taunt
end

return Loading
