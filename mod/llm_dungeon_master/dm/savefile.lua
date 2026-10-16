-- The mod's save file. Isaac gives a mod one (per save slot), and two parties write it: this mod
-- (settings, pill locks, run stats) and the backend, which leaves its messages to us under "dm"
-- (see bridge.lua). Each writes the other's part back as it last read it, so one write can undo
-- the other's latest. The backend notices ours and writes again; we number our saves ("rev") and
-- save again when the file shows an older one.
local json = require("json")

local SaveFile = {}

local mod
local data = {}       -- our part: everything but "dm"
local lastRaw         -- the file as poll() last saw it

local function read()
  local ok, raw = pcall(function() return mod:HasData() and mod:LoadData() or "" end)
  return ok and raw or ""
end

local function decode(raw)
  local ok, t = pcall(json.decode, raw)
  if ok and type(t) == "table" then return t end
end

local function write()
  local ok, line = pcall(json.encode, data)
  if ok then mod:SaveData(line) end
end

--- Read our part from disk. At load, and again at game start (the save slot is chosen by then).
function SaveFile.reload()
  data = decode(read()) or {}
  data.dm = nil
end

--- Our part, as a table. Read-only: change it through save().
function SaveFile.get() return data end

--- Merge fields into our part and write it.
function SaveFile.save(fields)
  for k, v in pairs(fields) do data[k] = v end
  data.rev = (tonumber(data.rev) or 0) + 1
  write()
end

--- The backend's section if the file changed since the last call, else nil. Cheap when it hasn't.
function SaveFile.poll()
  local raw = read()
  if raw == lastRaw then return nil end
  lastRaw = raw
  local t = decode(raw)
  if not t then return nil end
  if t.rev ~= data.rev then write() end     -- a write carried an older copy of our part: put ours back
  return t.dm
end

function SaveFile.init(theMod)
  mod = theMod
  SaveFile.reload()
end

return SaveFile
