-- On-screen DM voice: one line, fades in and out at the top of the screen.
local Taunt = { enabled = true }

local current       -- {text, born}
local queue = {}
local HOLD_MS, FADE_MS = 2600, 400
local font = Font()
local fontOk = pcall(function() font:Load("font/pftempestasevencondensed.fnt") end) and font:IsLoaded()
local COLOR = KColor(1, 0.35, 0.35, 1)

function Taunt.show(text)
  if not Taunt.enabled or type(text) ~= "string" or text == "" then return end
  if #queue < 3 then queue[#queue + 1] = text end
end

local function alphaFor(age)
  if age < FADE_MS then return age / FADE_MS end
  if age > HOLD_MS + FADE_MS then return math.max(0, 1 - (age - HOLD_MS - FADE_MS) / FADE_MS) end
  return 1
end

function Taunt.render()
  local t = Isaac.GetTime()
  if not current and #queue > 0 then current = { text = table.remove(queue, 1), born = t } end
  if not current then return end
  local age = t - current.born
  if age > HOLD_MS + 2 * FADE_MS then current = nil return end
  local a = alphaFor(age)
  local w = Isaac.GetScreenWidth()
  local text = "DM: " .. current.text
  if fontOk then
    font:DrawStringScaled(text, 0, 38, 1, 1, KColor(COLOR.Red, COLOR.Green, COLOR.Blue, a), w, true)
  else
    Isaac.RenderText(text, math.max(4, w / 2 - #text * 3), 38, 1, 0.35, 0.35, a)
  end
end

--- Tiny status readout (connection + latency), bottom-left.
function Taunt.renderDebug(status, avgMs, p90Ms, pending)
  local h = Isaac.GetScreenHeight()
  Isaac.RenderScaledText(string.format("DM %s  avg %dms p90 %dms  %s", status, avgMs, p90Ms, pending),
    4, h - 12, 0.5, 0.5, 1, 1, 1, 0.6)
end

return Taunt
