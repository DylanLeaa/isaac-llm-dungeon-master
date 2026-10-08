-- DM Settings: the in-game menu (Mod Config Menu) and the mod's save file.
-- Without Mod Config Menu installed this just applies config.lua and stays out of the way.
local json = require("json")

local Settings = {}

local mod, Bridge
local registered = false
local DEFAULT = -1     -- menu/save value meaning "use the backend's --cruelty"

local function band(c)
  if c <= 2 then return "generous" end
  if c <= 5 then return "mischievous" end
  if c <= 8 then return "cruel" end
  return "merciless"
end

--- The whole save file as a table ({} if missing or unreadable).
function Settings.load()
  if not (mod.HasData and mod:HasData()) then return {} end
  local ok, data = pcall(json.decode, mod:LoadData())
  return (ok and type(data) == "table") and data or {}
end

--- Merge fields into the save file. Isaac gives a mod one file, so everything saved goes through here.
function Settings.save(fields)
  if not mod.SaveData then return end
  local data = Settings.load()
  for k, v in pairs(fields) do data[k] = v end
  local ok, line = pcall(json.encode, data)
  if ok then mod:SaveData(line) end
end

local function saveCruelty()
  local c = Bridge.getCruelty()
  Settings.save({ cruelty = c == nil and DEFAULT or c })
end

function Settings.registerMenu()
  if registered or type(ModConfigMenu) ~= "table" then return end
  registered = true
  ModConfigMenu.AddSetting("DM Settings", nil, {
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
end

function Settings.init(theMod, cfg, theBridge)
  mod, Bridge = theMod, theBridge
  local c = cfg.CRUELTY
  local saved = Settings.load().cruelty
  if type(saved) == "number" then c = saved ~= DEFAULT and saved or nil end
  if c ~= nil then c = math.max(0, math.min(10, math.floor(c))) end
  Bridge.setCruelty(c)
  Settings.registerMenu()
end

return Settings
