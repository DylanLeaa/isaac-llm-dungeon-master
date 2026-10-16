-- DM Settings: the in-game menu (Mod Config Menu) and what the mod saves.
--   General : the cruelty slider
--   Receipt : what the engine did this run (decisions, LLM calls, tokens, cost), newest first
--   Last run: the after-death stats screen of the last finished run
-- Without Mod Config Menu installed this just applies config.lua and stays out of the way.
local Settings = {}

local store, cfg, Bridge, Receipt, Death
local registered = false
local DEFAULT = -1     -- menu/save value meaning "use the backend's --cruelty"
local CATEGORY = "DM Settings"
local TOTAL_LINES, DETAIL_LINES, LATEST_LINES, LAST_RUN_LINES = 6, 18, 10, 36
local selected = 1     -- receipt entry shown in detail, 1 = newest

local function band(c)
  if c <= 2 then return "generous" end
  if c <= 5 then return "mischievous" end
  if c <= 8 then return "cruel" end
  return "merciless"
end

--- Everything the mod has saved, as a table.
function Settings.load() return store.get() end

--- Merge fields into the save file. Isaac gives a mod one file, so everything saved goes through here.
function Settings.save(fields) store.save(fields) end

local function saveCruelty()
  local c = Bridge.getCruelty()
  Settings.save({ cruelty = c == nil and DEFAULT or c })
end

-- ------------------------------------------------------------------ receipt tab
local function width() return cfg.RECEIPT_WIDTH or 32 end

local function shownEntry()
  local n = Receipt.count()
  if n == 0 then return nil, 0, 0 end
  local i = math.max(1, math.min(selected, n))
  return Receipt.get(i), i, n
end

-- `count` text rows; row i shows line i of whatever lines() returns when the menu draws it.
local function addLines(sub, count, lines)
  for i = 1, count do
    ModConfigMenu.AddSetting(CATEGORY, sub, {
      Type = ModConfigMenu.OptionType.TEXT,
      Display = function()
        local all = lines()
        if i == count and #all > count then return "..." end
        return all[i] or ""
      end,
      NoCursorHelp = true,
    })
  end
end

local function registerReceipt()
  local T, sub = ModConfigMenu.OptionType, "Receipt"
  local title = T.TITLE or T.TEXT
  ModConfigMenu.AddSetting(CATEGORY, sub, { Type = title, Display = "This run" })
  addLines(sub, TOTAL_LINES, function()
    local out = {}
    for _, line in ipairs(Receipt.totalLines()) do Receipt.wrap(line, width(), out) end
    return out
  end)
  ModConfigMenu.AddSetting(CATEGORY, sub, { Type = T.SPACE })
  ModConfigMenu.AddSetting(CATEGORY, sub, {
    Type = T.NUMBER,
    Minimum = 1,
    Maximum = Receipt.KEEP,
    CurrentSetting = function() return math.max(1, (select(2, shownEntry()))) end,
    Display = function()
      local e, i, n = shownEntry()
      if not e then return "Entry: none yet" end
      return "Entry " .. i .. "/" .. n .. (i == 1 and " (newest)" or "")
    end,
    OnChange = function(v) selected = math.max(1, math.min(v, math.max(1, Receipt.count()))) end,
    Info = { "Left/right steps through the DM's actions, newest first.",
             "Prompts and raw LLM replies: backend/receipts/ on the PC." },
  })
  addLines(sub, DETAIL_LINES, function() return Receipt.detailLines((shownEntry()), width()) end)
  ModConfigMenu.AddSetting(CATEGORY, sub, { Type = T.SPACE })
  ModConfigMenu.AddSetting(CATEGORY, sub, { Type = title, Display = "Latest" })
  addLines(sub, LATEST_LINES, function()
    local out = {}
    for i = 1, math.min(LATEST_LINES, Receipt.count()) do
      out[i] = Receipt.oneLine(Receipt.get(i)):sub(1, width())
    end
    return out
  end)
  addLines(sub, 2, function()
    return Receipt.file() and Receipt.wrap("saved as " .. Receipt.file(), width()) or {}
  end)
end

function Settings.registerMenu()
  if registered or type(ModConfigMenu) ~= "table" then return end
  registered = true
  ModConfigMenu.AddSetting(CATEGORY, "General", {
    Type = ModConfigMenu.OptionType.NUMBER,
    Minimum = DEFAULT,
    Maximum = 10,
    CurrentSetting = function()
      local c = Bridge.getCruelty()
      return c == nil and DEFAULT or c
    end,
    Display = function()
      local c = Bridge.getCruelty()
      if c == nil then return "Cruelty: backend default" end
      return "Cruelty: " .. c .. "/10 (" .. band(c) .. ")"
    end,
    OnChange = function(v)
      Bridge.setCruelty(v ~= DEFAULT and v or nil)
      saveCruelty()
    end,
    Info = { "How hard the Dungeon Master trolls you. Applies immediately, even mid-run." },
  })
  registerReceipt()
  addLines("Last run", LAST_RUN_LINES, function() return Death.menuLines(width()) end)
end

-- Cruelty from config.lua, unless the menu saved one.
local function applySavedCruelty()
  local c = cfg.CRUELTY
  local saved = Settings.load().cruelty
  if type(saved) == "number" then c = saved ~= DEFAULT and saved or nil end
  if c ~= nil then c = math.max(0, math.min(10, math.floor(c))) end
  Bridge.setCruelty(c)
end

function Settings.init(theStore, config, theBridge, theReceipt, theDeath)
  store, cfg, Bridge, Receipt, Death = theStore, config, theBridge, theReceipt, theDeath
  applySavedCruelty()
  Settings.registerMenu()
end

--- A game is starting: the save slot is chosen now, so read it again.
function Settings.reload()
  store.reload()
  applySavedCruelty()
end

return Settings
