-- What the DM just did, in the bottom-right corner: one line per effect ("+1 <bomb>", "-1 <penny>",
-- "+2 <attack fly>"), fading out after a few seconds. Fed the same effect lines the receipt gets
-- (Bridge.effect), so it shows what the DM actually did, as it happens. Lines with nothing to count
-- ("nothing dropped", "item offered: ...") stay in the receipt.
local Hud = { enabled = true }

local game = Game()
local plural      -- set by Hud.init (Apply's, so names match its effect lines)

local HOLD_MS, FADE_IN_MS, FADE_MS = 4000, 150, 700
local MAX_ROWS, MAX_QUEUED = 6, 24
local ROW_H, ICON_W, GAP = 18, 18, 10
-- Above the pocket item (card / pill) and its name in the corner, and above Esau's HUD when he's there.
local RIGHT_PAD, BOTTOM_PAD, ESAU_PAD = 14, 48, 40
local GAIN, LOSS = { 0.62, 0.89, 0.62 }, { 1, 0.54, 0.48 }
local ZERO = Vector(0, 0)

local font = Font()
local fontOk = pcall(function() font:Load("font/pftempestasevencondensed.fnt") end) and font:IsLoaded()

-- ------------------------------------------------------------------ icons
-- An effect line's name -> its icon: the entity's own animation file, shrunk to fit a line.
-- A file that doesn't load just leaves the name as text.
local function pickup(file) return { file = file, scale = 0.7, anim = "Idle" } end
local function enemy(file) return { file = file, scale = 0.45 } end

local ICONS = {
  penny = pickup("gfx/005.021_penny.anm2"), nickel = pickup("gfx/005.022_nickel.anm2"),
  dime = pickup("gfx/005.023_dime.anm2"), ["double penny"] = pickup("gfx/005.024_double penny.anm2"),
  ["sticky nickel"] = pickup("gfx/005.025_sticky nickel.anm2"), ["lucky penny"] = pickup("gfx/005.026_lucky penny.anm2"),
  bomb = pickup("gfx/005.041_bomb.anm2"), ["double bomb"] = pickup("gfx/005.042_double bomb.anm2"),
  ["golden bomb"] = pickup("gfx/005.043_golden bomb.anm2"), ["troll bomb"] = pickup("gfx/004.003_troll bomb.anm2"),
  key = pickup("gfx/005.031_key.anm2"), ["golden key"] = pickup("gfx/005.032_golden key.anm2"),
  ["double key"] = pickup("gfx/005.033_keyring.anm2"), ["charged key"] = pickup("gfx/005.034_chargedkey.anm2"),
  ["red heart"] = pickup("gfx/005.011_heart.anm2"), ["half heart"] = pickup("gfx/005.012_heart (half).anm2"),
  ["soul heart"] = pickup("gfx/005.013_heart (soul).anm2"), ["eternal heart"] = pickup("gfx/005.014_heart (eternal).anm2"),
  ["black heart"] = pickup("gfx/005.016_black heart.anm2"),
  chest = pickup("gfx/005.050_chest.anm2"), ["locked chest"] = pickup("gfx/005.060_locked chest.anm2"),
  ["spiked chest"] = pickup("gfx/005.052_spikedchest.anm2"), ["mimic chest"] = pickup("gfx/005.054_mimic chest.anm2"),
  ["red chest"] = pickup("gfx/005.360_red chest.anm2"), ["grab bag"] = pickup("gfx/005.069_grabbag.anm2"),
  pill = pickup("gfx/005.071_pill blue-blue.anm2"), card = pickup("gfx/005.301_tarot card.anm2"),
  battery = pickup("gfx/005.090_littlebattery.anm2"),
  -- the DM's extra enemies (catalog.py ENEMIES), plus the plain fly
  fly = enemy("gfx/013.000_fly.anm2"), ["attack fly"] = enemy("gfx/018.000_attack fly.anm2"),
  pooter = enemy("gfx/014.000_pooter.anm2"), dip = enemy("gfx/217.000_dip.anm2"),
  gaper = enemy("gfx/010.001_gaper.anm2"), maggot = enemy("gfx/021.000_maggot.anm2"),
  spider = enemy("gfx/085.000_spider.anm2"), clotty = enemy("gfx/015.000_clotty.anm2"),
  hopper = enemy("gfx/029.000_hopper.anm2"), charger = enemy("gfx/023.000_charger.anm2"),
  mulligan = enemy("gfx/016.000_mulligan.anm2"), maw = enemy("gfx/026.000_maw.anm2"),
  spitty = enemy("gfx/031.000_spity.anm2"), ["big spider"] = enemy("gfx/094.000_big spider.anm2"),
  ["boom fly"] = enemy("gfx/025.000_boom fly.anm2"), host = enemy("gfx/027.000_host.anm2"),
  knight = enemy("gfx/041.000_knight.anm2"), globin = enemy("gfx/024.000_globin.anm2"),
  sucker = enemy("gfx/061.000_sucker.anm2"),
}
-- Other names the effect lines use for the same things (pickup families, the game's own names).
local ALIASES = { coin = "penny", heart = "red heart", ["golden chest"] = "locked chest", sack = "grab bag" }

local names, sorted   -- "pennies" -> "penny" etc; every known name, longest first
local function index()
  if names then return end
  names, sorted = {}, {}
  local function add(name, icon)
    for _, form in ipairs({ name, plural(name, 2) }) do
      if not names[form] then names[form] = icon; sorted[#sorted + 1] = form end
    end
  end
  for name in pairs(ICONS) do add(name, name) end
  for name, icon in pairs(ALIASES) do add(name, icon) end
  table.sort(sorted, function(a, b) return #a > #b end)
end

local sprites = {}    -- icon -> loaded Sprite, or false if it wouldn't load
local function sprite(icon)
  if sprites[icon] ~= nil then return sprites[icon] end
  local spec, s = ICONS[icon], false
  local ok = spec and pcall(function()
    local sp = Sprite()
    sp:Load(spec.file, true)
    if not sp:IsLoaded() then return end
    if spec.anim then sp:Play(spec.anim, true) end
    if not (spec.anim and sp:IsPlaying(spec.anim)) then sp:Play(sp:GetDefaultAnimation(), true) end
    sp.Scale = Vector(spec.scale, spec.scale)
    s = sp
  end)
  sprites[icon] = ok and s or false
  return sprites[icon]
end

-- ------------------------------------------------------------------ parsing
local VERBS = { "dropped", "placed", "spawned", "hidden" }

--- The thing counted at the start of `rest`: a known name (with its icon), else its words.
local function thing(rest)
  rest = rest:lower()
  for _, n in ipairs(sorted) do
    if rest:sub(1, #n) == n and not rest:sub(#n + 1, #n + 1):match("[%a']") then return n, names[n] end
  end
  local words = rest:match("^[%a' ]+") or ""
  for _, v in ipairs(VERBS) do words = words:gsub("%s+" .. v .. "%s*$", "") end
  return (words:gsub("%s+$", ""))
end

--- One effect line -> what it counts, as {sign, text, icon}; empty when there's nothing to show.
function Hud.parse(line)
  index()
  local out = {}
  if type(line) ~= "string" then return out end
  local sign, curse = line:match("^([+-])curse of ([%a ]+)")
  if sign then
    out[1] = { sign = sign, text = sign .. "curse of " .. curse:gsub("%s+$", "") }
    return out
  end
  for at, sgn, n in line:gmatch("()([+-])(%d+) ") do
    local before = at > 1 and line:sub(at - 1, at - 1) or " "
    n = tonumber(n)
    if n > 0 and not before:match("[%w]") then
      local name, icon = thing(line:sub(at + #sgn + #tostring(n) + 1))
      if icon and sprite(icon) then
        out[#out + 1] = { sign = sgn, text = sgn .. n, icon = icon }
      elseif name ~= "" then
        out[#out + 1] = { sign = sgn, text = sgn .. n .. " " .. name }
      end
    end
  end
  return out
end

-- ------------------------------------------------------------------ rows
local rows, queue = {}, {}

--- What the DM just did (an effect's lines): a row for each line that counts something.
function Hud.push(lines)
  if not Hud.enabled or type(lines) ~= "table" then return end
  for _, line in ipairs(lines) do
    local tokens = Hud.parse(line)
    if #tokens > 0 then
      if #queue >= MAX_QUEUED then table.remove(queue, 1) end
      queue[#queue + 1] = { tokens = tokens }
    end
  end
end

local function alphaFor(age)
  if age < FADE_IN_MS then return age / FADE_IN_MS end
  if age > HOLD_MS then return math.max(0, 1 - (age - HOLD_MS) / FADE_MS) end
  return 1
end

local function width(s)
  if fontOk then
    local ok, w = pcall(function() return font:GetStringWidth(s) end)
    if ok and w then return w end
  end
  return #s * 6
end

local function text(s, x, y, rgb, a)
  if fontOk then
    font:DrawStringScaled(s, x + 1, y + 1, 1, 1, KColor(0, 0, 0, 0.6 * a), 0, false)
    font:DrawStringScaled(s, x, y, 1, 1, KColor(rgb[1], rgb[2], rgb[3], a), 0, false)
  else
    Isaac.RenderText(s, x, y, rgb[1], rgb[2], rgb[3], a)
  end
end

local function esauHere()
  local esau = PlayerType and PlayerType.PLAYER_ESAU
  if not esau then return false end
  for i = 0, game:GetNumPlayers() - 1 do
    if Isaac.GetPlayer(i):GetPlayerType() == esau then return true end
  end
  return false
end

-- The screen point the rows stack up from (their bottom-right), clear of the pocket item.
local function anchor()
  local o = tonumber(Options and Options.HUDOffset) or 0
  local okE, esau = pcall(esauHere)
  return Isaac.GetScreenWidth() - RIGHT_PAD - 20 * o,
         Isaac.GetScreenHeight() - BOTTOM_PAD - 12 * o - ((okE and esau) and ESAU_PAD or 0)
end

local function drawRow(row, right, top, a)
  local x = right
  for j = #row.tokens, 1, -1 do
    local tok = row.tokens[j]
    local s = tok.icon and sprite(tok.icon)
    if s then
      s.Color = Color(1, 1, 1, a, 0, 0, 0)
      s:Render(Vector(x - ICON_W / 2, top + ROW_H - 2), ZERO, ZERO)
      x = x - ICON_W - 1
    end
    local w = width(tok.text)
    text(tok.text, x - w, top + (ROW_H - 10) / 2, tok.sign == "-" and LOSS or GAIN, a)
    x = x - w - GAP
  end
end

--- Call every frame (MC_POST_RENDER). hidden: something covers the screen (the loading or death
--- screen). A row's clock only starts once it's on screen, so nothing fades out unseen.
function Hud.render(hidden)
  local t = Isaac.GetTime()
  for i = #rows, 1, -1 do
    if rows[i].born and t - rows[i].born > HOLD_MS + FADE_MS then table.remove(rows, i) end
  end
  while #rows < MAX_ROWS and #queue > 0 do rows[#rows + 1] = table.remove(queue, 1) end
  if #rows == 0 or hidden then return end
  local okH, visible = pcall(function() return game:GetHUD():IsVisible() end)
  if okH and visible == false then return end
  local okP, paused = pcall(function() return game:IsPaused() end)
  if okP and paused then return end

  local right, bottom = anchor()
  for i, row in ipairs(rows) do
    row.born = row.born or t
    drawRow(row, right, bottom - (#rows - i + 1) * ROW_H, alphaFor(t - row.born))
  end
end

--- A new run: last run's rows go.
function Hud.clear() rows, queue = {}, {} end

function Hud.init(cfg, apply)
  plural = apply.plural
  Hud.enabled = cfg.SHOW_EFFECTS ~= false
end

return Hud
