-- The after-death stats screen: the run's numbers from both sides of the table, drawn over the
-- game-over screen, and kept in the save file for the next launch.
--
-- The mod's side (floor reached, time, damage, items...) is ready the moment the run ends, so
-- the screen never waits on the backend. The backend answers the death event with the DM's side
-- (what it spawned, dropped and cursed, the con it was running, its LLM bill) and, a few seconds
-- later, the DM's epitaph.
local Death = {}

local cfg, Bridge, State, Pills, Settings

--- Split text into lines of at most `width` chars, breaking on spaces where possible.
local function wrap(text, width, out)
  out = out or {}
  text = tostring(text):gsub("%s+", " ")
  while #text > width do
    local cut = width
    local space = text:sub(1, width + 1):match(".*() ")
    if space and space > width / 2 then cut = space - 1 end
    out[#out + 1] = text:sub(1, cut)
    text = text:sub(cut + 1):gsub("^ ", "")
  end
  if #text > 0 then out[#out + 1] = text end
  return out
end

-- {run, outcome, character, title, sections = {{title, rows = {{label, value}}}}, scheme, taunt,
--  epitaph, epitaph_pending, dm_pending}
local summary
local runId
local shown = false

local font = Font()
local fontOk = pcall(function() font:Load("font/pftempestasevencondensed.fnt") end) and font:IsLoaded()
local RED, WHITE, GREY, GOLD = KColor(1, 0.35, 0.35, 1), KColor(1, 1, 1, 1), KColor(0.7, 0.7, 0.7, 1),
                               KColor(1, 0.85, 0.4, 1)
local SHADOW = KColor(0, 0, 0, 0.9)
-- Measured on the game-over screen (480x270, scaled with the screen width). The death note is
-- drawn after every mod callback, so nothing can go on top of it: the screen lives in the strips
-- down either side of it, which the seed note (pinned top left) and the EXIT / RESTART notes (bottom
-- corners) cut down to about eight lines a side. So it is two pages that take turns: the stats, then
-- the epitaph and the con. The map button turns the page.
local PAPER_W, EDGE, TOP, MIN_COL, MAX_COL = 200, 8, 10, 96, 150
local SEED_X0, SEED_X1, SEED_Y0, SEED_Y1, BASE_W = 64, 160, 48, 110, 480
local EXIT_Y, RESTART_Y = 200, 180   -- top edge of the corner notes
local PAGE_FRAMES = 60 * 9           -- render frames per page
local page, frames = 1, 0

local function setHud(visible)
  pcall(function() Game():GetHUD():SetVisible(visible) end)
end

local function set(s)
  summary = s
  Settings.save({ last_run = summary })
end

-- --------------------------------------------------------------- run events
function Death.startRun(seed)
  runId, shown, page, frames = seed, false, 1, 0
  setHud(true)
end

--- MC_POST_GAME_END: show the player's side now and ask the backend for the DM's.
function Death.onGameEnd(gameOver)
  local outcome = gameOver and "death" or "victory"
  local character = Isaac.GetPlayer(0):GetName()
  local rows = State.runRows()
  set({ run = runId, outcome = outcome, character = character, sections = { { title = "YOU", rows = rows } },
        dm_pending = Bridge.ready() })
  shown, page, frames = gameOver and cfg.SHOW_DEATH_SCREEN ~= false, 1, 0
  Bridge.event(outcome, { run = runId, character = character, rows = rows, pills = Pills.lockedList() })
  Bridge.update()      -- send it now: the game-over screen may not keep pumping the bridge
end

--- Bridge handler for the backend's "run_summary" (DM side, then again with the epitaph).
function Death.handle(msg)
  if not summary or msg.run ~= summary.run or type(msg.sections) ~= "table" then return end
  msg.t, msg.session = nil, nil
  set(msg)
end

-- ------------------------------------------------------------------ the text
local function quotes()
  local out = {}
  if summary.epitaph then
    out[#out + 1] = { '"' .. summary.epitaph .. '"', RED }
  elseif summary.epitaph_pending then
    out[#out + 1] = { "The DM is writing your epitaph...", GREY }
  end
  if summary.taunt then out[#out + 1] = { 'Last word: "' .. summary.taunt .. '"', GREY } end
  if summary.scheme then out[#out + 1] = { "The con: " .. summary.scheme, WHITE } end
  return out
end

local function heading()
  local who = string.upper(summary.character or "Isaac")
  return summary.outcome == "victory" and (who .. " WON") or ("HERE LIES " .. who)
end

local function dmMissing()
  return summary.dm_pending and "tallying..." or "offline this run"
end

-- ----------------------------------------------------------------- drawing
local SCALE = 0.8       -- a little smaller than the game's own text, so a page fits between the notes
local function textWidth(s) return (fontOk and font:GetStringWidth(s) or #s * 6) * SCALE end
local function lineHeight() return (fontOk and font:GetLineHeight() + 1 or 12) * SCALE end

local function draw(s, x, y, color, boxW, center)
  if fontOk then
    font:DrawStringScaled(s, x + 1, y + 1, SCALE, SCALE, SHADOW, boxW or 0, center or false)
    font:DrawStringScaled(s, x, y, SCALE, SCALE, color, boxW or 0, center or false)
  else
    if center then x = x + (boxW - textWidth(s)) / 2 end
    Isaac.RenderText(s, x, y, color.Red, color.Green, color.Blue, color.Alpha)
  end
end

local function fit(s, maxW)
  s = tostring(s or "")
  if textWidth(s) <= maxW then return s end
  while #s > 1 and textWidth(s .. "..") > maxW do s = s:sub(1, -2) end
  return s .. ".."
end

local function toggled()
  if type(Input) ~= "table" and type(Input) ~= "userdata" or not ButtonAction then return false end
  local p = Isaac.GetPlayer(0)
  return Input.IsActionTriggered(ButtonAction.ACTION_MAP, p and p.ControllerIndex or 0)
end

local function hasWords()
  return summary.epitaph or summary.epitaph_pending or summary.scheme
end

-- One page's two columns, top to bottom: {text, color} lines, {row = {label, value}}, false for a gap.
-- `below` starts a section under the seed note, so its header and rows stay together.
local function columns()
  local left, right = { { heading(), RED } }, {}
  if summary.title then left[#left + 1] = { summary.title, WHITE, wrap = true, maxLines = 2 } end
  left[#left + 1] = false
  if page == 1 then
    for i, col in ipairs({ left, right }) do
      local sec = summary.sections[i]
      col[#col + 1] = { sec and tostring(sec.title) or "THE DM", GOLD, below = i == 1 }
      if not sec then col[#col + 1] = { dmMissing(), GREY } end
      for _, row in ipairs(sec and sec.rows or {}) do col[#col + 1] = { row = row } end
    end
    if summary.taunt then
      right[#right + 1] = false
      right[#right + 1] = { 'Last word: "' .. summary.taunt .. '"', GREY, wrap = true }
    end
  else
    left[#left + 1] = { "EPITAPH", GOLD, below = true }
    if summary.epitaph then
      left[#left + 1] = { '"' .. summary.epitaph .. '"', RED, wrap = true }
    else
      left[#left + 1] = { summary.epitaph_pending and "The DM is still writing it..." or "None. You weren't worth one.",
                          GREY, wrap = true }
    end
    right[#right + 1] = { "THE CON", GOLD }
    right[#right + 1] = { summary.scheme or "It never said.", summary.scheme and WHITE or GREY, wrap = true }
  end
  return left, right
end

local function drawColumn(items, x, w, bottom, avoidSeed)
  local lh, y = lineHeight(), TOP
  local k = Isaac.GetScreenWidth() / BASE_W
  local function clear(h)           -- hop below the seed note if a line of height h would touch it
    if avoidSeed and x < SEED_X1 * k and x + w > SEED_X0 * k and y + h > SEED_Y0 * k and y < SEED_Y1 * k then
      y = SEED_Y1 * k
    end
  end
  local perLine = math.max(12, math.floor(w / (textWidth("the quick brown fox ") / 20)))
  for _, it in ipairs(items) do
    if it and it.below and avoidSeed then y = math.max(y, SEED_Y1 * k) end
    if not it then
      y = y + 4
    elseif it.row then
      clear(lh)
      if y + lh > bottom then return end
      local label = fit(it.row[1], math.max(w * 0.5, w - textWidth(tostring(it.row[2])) - 6))
      local value = fit(it.row[2], w - textWidth(label) - 6)
      draw(label, x, y, GREY)
      draw(value, x + w - textWidth(value), y, WHITE)
      y = y + lh
    else
      local lines = it.wrap and wrap(it[1], perLine) or { fit(it[1], w) }
      if it.maxLines and #lines > it.maxLines then
        for i = #lines, it.maxLines + 1, -1 do lines[i] = nil end
        lines[#lines] = fit(lines[#lines] .. "..", w)
      end
      for _, line in ipairs(lines) do
        clear(lh)
        if y + lh > bottom then return end
        draw(line, x, y, it[2])
        y = y + lh
      end
    end
  end
end

--- Whether the screen is up (the debug HUD stays out of its way).
function Death.active() return shown and summary ~= nil end

--- MC_POST_RENDER: the screen, once a run has ended in death.
function Death.render()
  if not shown or not summary then return end
  setHud(false)
  local pages = hasWords() and 2 or 1
  frames = frames + 1
  if toggled() or frames >= PAGE_FRAMES then page, frames = page % pages + 1, 0 end
  page = math.min(page, pages)
  local sw = Isaac.GetScreenWidth()
  local w = math.max(MIN_COL, math.min(MAX_COL, math.floor((sw - PAPER_W) / 2) - 2 * EDGE))
  local k = sw / BASE_W
  if pages > 1 then
    local hint = string.format("[map] page %d/2", page)
    draw(hint, math.floor((sw - textWidth(hint)) / 2), 2, GREY)
  end
  local left, right = columns()
  drawColumn(left, EDGE, w, EXIT_Y * k, true)
  drawColumn(right, sw - EDGE - w, w, RESTART_Y * k)
end

function Death.init(config, bridge, state, pills, settings)
  cfg, Bridge, State, Pills, Settings = config, bridge, state, pills, settings
  local saved = Settings.load().last_run
  if type(saved) == "table" and type(saved.sections) == "table" then
    saved.epitaph_pending, saved.dm_pending = nil, nil   -- whatever was on its way is not coming
    summary = saved
  end
end

return Death
