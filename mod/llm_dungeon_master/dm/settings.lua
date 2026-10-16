-- DM Settings: the in-game menu (Mod Config Menu) and what the mod saves.
--   General : the cruelty slider
--   Last run: the after-death stats screen of the last finished run
-- Without Mod Config Menu installed this just applies config.lua and stays out of the way.
local Settings = {}

local store, cfg, Bridge, Death
local registered = false
local DEFAULT = -1     -- menu/save value meaning "use the backend's --cruelty"
local CATEGORY = "DM Settings"
local LAST_RUN_LINES = 36

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

local function width() return cfg.MENU_WIDTH or 32 end

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

function Settings.init(theStore, config, theBridge, theDeath)
  store, cfg, Bridge, Death = theStore, config, theBridge, theDeath
  applySavedCruelty()
  Settings.registerMenu()
end

--- A game is starting: the save slot is chosen now, so read it again.
function Settings.reload()
  store.reload()
  applySavedCruelty()
end

return Settings
