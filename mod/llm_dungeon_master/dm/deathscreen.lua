-- The after-death stats screen: the run's numbers from both sides of the table, drawn over the
-- game-over screen and kept under Mod Config Menu > DM Settings > Last run.
--
-- The mod's side (floor reached, time, damage, items...) is ready the moment the run ends, so
-- the screen never waits on the backend. The backend answers the death event with the DM's side
-- (what it spawned, dropped and cursed, the con it was running, its LLM bill) and, a few seconds
-- later, the DM's epitaph. The map button hides and shows the screen.
local Death = {}

local cfg, Bridge, State, Cons, Settings, wrap

-- {run, outcome, character, title, sections = {{title, rows = {{label, value}}}}, scheme, taunt,
--  epitaph, epitaph_pending, dm_pending}
local summary
local runId
local shown, hidden = false, false
local version, menuCache = 0, {}

local font = Font()
local fontOk = pcall(function() font:Load("font/pftempestasevencondensed.fnt") end) and font:IsLoaded()
local RED, WHITE, GREY, GOLD = KColor(1, 0.35, 0.35, 1), KColor(1, 1, 1, 1), KColor(0.7, 0.7, 0.7, 1),
                               KColor(1, 0.85, 0.4, 1)
local SHADOW = KColor(0, 0, 0, 0.9)
-- The game-over note is drawn after every mod callback, so nothing can go on top of it: the screen
-- is two columns hugging the screen edges, either side of the note. PAPER_W is the note's width
-- (in screen pixels, centred) that the columns keep clear of.
local PAPER_W, EDGE, TOP, MIN_COL, MAX_COL = 230, 8, 10, 96, 150

local function set(s)
  summary, version = s, version + 1
  Settings.save({ last_run = summary })
end

-- --------------------------------------------------------------- run events
function Death.startRun(seed)
  runId, shown, hidden = seed, false, false
end

--- MC_POST_GAME_END: show the player's side now and ask the backend for the DM's.
function Death.onGameEnd(gameOver)
  local outcome = gameOver and "death" or "victory"
  local character = Isaac.GetPlayer(0):GetName()
  local rows = State.runRows()
  set({ run = runId, outcome = outcome, character = character, sections = { { title = "YOU", rows = rows } },
        dm_pending = Bridge.ready() })
  shown, hidden = gameOver and cfg.SHOW_DEATH_SCREEN ~= false, false
  Bridge.event(outcome, { run = runId, character = character, rows = rows, pills = Cons.lockedList() })
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

--- The last run as plain lines for the menu, wrapped to `width` chars.
function Death.menuLines(width)
  if not summary then return { "No finished run yet." } end
  if menuCache.version == version and menuCache.width == width then return menuCache.lines end
  local out = {}
  local function add(text) wrap(text, width, out) end
  add(heading())
  if summary.title then add(summary.title) end
  for _, sec in ipairs(summary.sections) do
    out[#out + 1] = ""
    add("- " .. tostring(sec.title) .. " -")
    for _, row in ipairs(sec.rows or {}) do add(tostring(row[1]) .. ": " .. tostring(row[2])) end
  end
  if #summary.sections < 2 then add("- THE DM - " .. dmMissing()) end
  for _, q in ipairs(quotes()) do
    out[#out + 1] = ""
    add(q[1])
  end
  menuCache = { version = version, width = width, lines = out }
  return out
end

-- ----------------------------------------------------------------- drawing
local function textWidth(s) return fontOk and font:GetStringWidth(s) or #s * 6 end
local function lineHeight() return fontOk and font:GetLineHeight() + 1 or 12 end

local function draw(s, x, y, color, boxW, center)
  if fontOk then
    font:DrawStringScaled(s, x + 1, y + 1, 1, 1, SHADOW, boxW or 0, center or false)
    font:DrawStringScaled(s, x, y, 1, 1, color, boxW or 0, center or false)
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

-- One column's contents, top to bottom: {text, color} lines, {label, value} rows, false for a gap.
local function columns()
  local left, right = { { heading(), RED } }, {}
  if summary.title then left[#left + 1] = { summary.title, WHITE, wrap = true } end
  left[#left + 1] = false
  for i, col in ipairs({ left, right }) do
    local sec = summary.sections[i]
    col[#col + 1] = { sec and tostring(sec.title) or "THE DM", GOLD }
    if not sec then col[#col + 1] = { dmMissing(), GREY } end
    for _, row in ipairs(sec and sec.rows or {}) do col[#col + 1] = { row = row } end
  end
  -- the epitaph under the player's stats, the DM's own words under its column
  for i, q in ipairs(quotes()) do
    local col = (i == 1 and (summary.epitaph or summary.epitaph_pending)) and left or right
    col[#col + 1] = false
    col[#col + 1] = { q[1], q[2], wrap = true }
  end
  return left, right
end

local function drawColumn(items, x, w, bottom)
  local lh, y = lineHeight(), TOP
  local perLine = math.max(12, math.floor(w / (textWidth("the quick brown fox ") / 20)))
  for _, it in ipairs(items) do
    if not it then
      y = y + 4
    elseif it.row then
      if y > bottom then return end
      local label = fit(it.row[1], math.max(w * 0.5, w - textWidth(tostring(it.row[2])) - 6))
      local value = fit(it.row[2], w - textWidth(label) - 6)
      draw(label, x, y, GREY)
      draw(value, x + w - textWidth(value), y, WHITE)
      y = y + lh
    else
      for _, line in ipairs(it.wrap and wrap(it[1], perLine) or { fit(it[1], w) }) do
        if y > bottom then return end
        draw(line, x, y, it[2])
        y = y + lh
      end
    end
  end
end

--- MC_POST_RENDER: the screen, once a run has ended in death.
function Death.render()
  if not shown or not summary then return end
  if toggled() then hidden = not hidden end
  local sw, lh = Isaac.GetScreenWidth(), lineHeight()
  local w = math.max(MIN_COL, math.min(MAX_COL, math.floor((sw - PAPER_W) / 2) - 2 * EDGE))
  local footY = Isaac.GetScreenHeight() - lh - 4
  draw(hidden and "[map] show the DM's stats" or "[map] hide", EDGE, footY, GREY)
  if hidden then return end
  local left, right = columns()
  drawColumn(left, EDGE, w, footY - lh)
  drawColumn(right, sw - EDGE - w, w, footY - lh)
end

function Death.init(config, bridge, state, cons, settings, wrapFn)
  cfg, Bridge, State, Cons, Settings, wrap = config, bridge, state, cons, settings, wrapFn
  local saved = Settings.load().last_run
  if type(saved) == "table" and type(saved.sections) == "table" then
    saved.epitaph_pending, saved.dm_pending = nil, nil   -- whatever was on its way is not coming
    summary = saved
  end
end

return Death
