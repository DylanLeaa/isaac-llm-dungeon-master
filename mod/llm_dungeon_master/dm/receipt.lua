-- The engine's receipt as the game sees it: the backend's latest entries (decisions, planner
-- calls, fallbacks, cruelty changes) and this run's totals, browsed in Mod Config Menu under
-- DM Settings > Receipt. The full receipt, with every prompt and raw LLM response, is the
-- JSON-lines file the backend writes to backend/receipts/.
local Receipt = {}

local KEEP = 40        -- matches the backend's GAME_KEEP
Receipt.KEEP = KEEP
local entries = {}     -- compact entries, oldest first
local bySeq = {}
local totals = {}
local file

local function bySeqAsc(a, b) return a.seq < b.seq end

local function upsert(e)
  if type(e) ~= "table" or type(e.seq) ~= "number" then return end
  local old = bySeq[e.seq]
  if old then
    for i = 1, #entries do
      if entries[i] == old then entries[i] = e break end
    end
  else
    entries[#entries + 1] = e
    table.sort(entries, bySeqAsc)          -- pushes can arrive slightly out of order
    while #entries > KEEP do bySeq[table.remove(entries, 1).seq] = nil end
  end
  bySeq[e.seq] = e
end

--- Bridge handler for the backend's "receipt" (one entry) and "receipt_sync" (whole view) messages.
function Receipt.handle(msg)
  if msg.t == "receipt_sync" then
    entries, bySeq, file = {}, {}, msg.file
    for _, e in ipairs(msg.entries or {}) do upsert(e) end
  else
    upsert(msg.entry)
  end
  if type(msg.totals) == "table" then totals = msg.totals end
end

function Receipt.count() return #entries end

--- The n-th newest entry (1 = newest).
function Receipt.get(n) return entries[#entries + 1 - n] end

-- ------------------------------------------------------------------ formatting
local function int(v) return math.floor(tonumber(v) or 0) end

local function k(n)
  n = int(n)
  if n >= 10000 then return string.format("%.1fk", n / 1000) end
  return tostring(n)
end

local function money(c) return string.format("$%.4f", tonumber(c) or 0) end

--- Split text into lines of at most `width` chars, breaking on spaces where possible.
function Receipt.wrap(text, width, out)
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

function Receipt.totalLines()
  local t = totals
  local inTok, cached = int(t["in"]), int(t.cached)
  local pct = inTok > 0 and math.floor(100 * cached / inTok + 0.5) or 0
  return {
    int(t.entries) .. " actions, " .. int(t.calls) .. " LLM calls",
    "cost " .. money(t.cost) .. ", " .. pct .. "% input cached",
    "tokens in " .. k(inTok) .. ", out " .. k(t.out),
    "applied " .. int(t.applied) .. "  replaced " .. int(t.replaced),
    "superseded " .. int(t.superseded) .. "  gave up " .. int(t.fallback),
    "timeout " .. int(t.timeout) .. "  error " .. int(t.error),
  }
end

--- One line per entry for the "Latest" list.
function Receipt.oneLine(e)
  if not e then return "" end
  local s = "#" .. int(e.seq) .. " " .. tostring(e.what) .. " " .. tostring(e.status)
  if e.ms then s = s .. " " .. int(e.ms) .. "ms" end
  return s
end

--- Everything the compact entry knows, wrapped to `width`.
function Receipt.detailLines(e, width)
  if not e then return { "Nothing yet: start a run with the", "backend connected." } end
  local out = {}
  local function add(text) Receipt.wrap(text, width, out) end
  add("#" .. int(e.seq) .. " " .. tostring(e.time or "") .. "  " .. tostring(e.what))
  add((e.key and (tostring(e.key) .. "  ") or "") .. "[" .. tostring(e.status) .. "]")
  if e.model or e.ms then
    add(tostring(e.model or "no LLM call") .. (e.ms and ("  " .. int(e.ms) .. "ms") or ""))
  end
  if type(e.tok) == "table" then
    add("tok " .. int(e.tok[1]) .. " in (" .. int(e.tok[2]) .. " cached) " .. int(e.tok[3]) .. " out")
  end
  local spend = {}
  if e.cost then spend[#spend + 1] = money(e.cost) end
  if e.finish then spend[#spend + 1] = "finish " .. tostring(e.finish) end
  if #spend > 0 then add(table.concat(spend, "  ")) end
  if e.intent then add("intent: " .. tostring(e.intent)) end
  if e.result then add("result: " .. tostring(e.result)) end
  if e.taunt then add('taunt: "' .. tostring(e.taunt) .. '"') end
  if e.scheme then add("scheme: " .. tostring(e.scheme)) end
  if type(e.guardrails) == "table" and #e.guardrails > 0 then
    add("guardrails: " .. table.concat(e.guardrails, "; "))
  end
  if e.error then add("error: " .. tostring(e.error)) end
  return out
end

function Receipt.file() return file end

return Receipt
