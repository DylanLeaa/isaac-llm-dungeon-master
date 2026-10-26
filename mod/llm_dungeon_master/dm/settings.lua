-- DM Settings: what the mod saves. Isaac gives a mod one save file, so everything saved goes through here.
-- (Cruelty and the other DM options live in the desktop app.)
local Settings = {}

local store

--- Everything the mod has saved, as a table.
function Settings.load() return store.get() end

--- Merge fields into the save file.
function Settings.save(fields) store.save(fields) end

function Settings.init(theStore) store = theStore end

--- A game is starting: the save slot is chosen now, so read it again.
function Settings.reload() store.reload() end

return Settings
