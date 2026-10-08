-- Bridge to the Dungeon Master backend.
--
-- Non-blocking, newline-delimited JSON over TCP (LuaSocket) or two append-only files.
-- Every decision is addressed by a `key` ("clear:3:84"). Requests for the same key
-- supersede each other: the backend cancels the stale one, and here a result is only
-- replaced by a result for a newer request. That is what lets the scheduler ask early,
-- then re-ask with a fresher snapshot, without ever blocking the game.
local json = require("json")

local Bridge = {}

local cfg
local transport
local session
local welcomed = false
local nextId = 1
local pending = {}     -- key -> {id, sentAt, kind, sends}
local results = {}     -- key -> {id, kind, decision, at, ms}
local latencies = {}   -- ring buffer of recent round-trips (ms)
local LAT_N = 24
local lastHello = -1e9
local onResult         -- optional callback(key, result)
local onReceipt        -- optional callback(msg) for the backend's receipt / receipt_sync messages
local onSummary        -- optional callback(msg) for the backend's run_summary (after-death stats)
local cruelty          -- nil = backend's --cruelty; set from config / the DM Settings menu

local function now() return Isaac.GetTime() end
Bridge.now = now

-- --------------------------------------------------------------- transports
local function tcpTransport(host, port)
  local ok, socket = pcall(require, "socket")
  if not ok or type(socket) ~= "table" then return nil, "socket library unavailable (is --luadebug set?)" end
  local T = { name = "tcp", out = {}, partial = "", nextTry = 0 }

  function T:drop(why)
    if self.sock then pcall(function() self.sock:close() end) end
    self.sock, self.out, self.partial = nil, {}, ""
    self.nextTry = now() + 3000
    welcomed = false
    if why then Isaac.DebugString("[LLMDM] tcp dropped: " .. tostring(why)) end
  end

  function T:tick()
    if self.sock or now() < self.nextTry then return end
    local s = socket.tcp()
    s:settimeout(0.02)                 -- localhost: refuse/accept is near-instant
    local okc, err = s:connect(host, port)
    if not okc then
      s:close()
      self.nextTry = now() + 3000
      return
    end
    s:settimeout(0)
    pcall(function() s:setoption("tcp-nodelay", true) end)
    self.sock = s
    lastHello = -1e9                   -- say hello immediately
  end

  function T:ready() return self.sock ~= nil end

  function T:send(line)
    if not self.sock then return false end
    self.out[#self.out + 1] = line .. "\n"
    return true
  end

  function T:flush()
    while self.sock and #self.out > 0 do
      local data = self.out[1]
      local i, err, last = self.sock:send(data)
      if i then
        table.remove(self.out, 1)
      elseif err == "timeout" then
        if last and last > 0 then self.out[1] = data:sub(last + 1) end
        return
      else
        return self:drop(err)
      end
    end
  end

  function T:poll()
    local lines = {}
    while self.sock do
      local line, err, partial = self.sock:receive("*l", self.partial)
      if line then
        self.partial = ""
        lines[#lines + 1] = line
      elseif err == "timeout" then
        self.partial = partial or ""
        break
      else
        self:drop(err)
        break
      end
    end
    return lines
  end

  return T
end

local function fileTransport(dir)
  if type(io) ~= "table" or not io.open then return nil, "io unavailable (is --luadebug set?)" end
  local toDm, fromDm = dir .. "/to_dm.jsonl", dir .. "/from_dm.jsonl"
  local f = io.open(toDm, "w")         -- truncate: tells the backend a new session started
  if not f then return nil, "cannot write " .. toDm .. " (create the folder / start the backend first)" end
  f:close()
  local T = { name = "file", offset = 0, partial = "" }
  function T:tick() end
  function T:ready() return true end
  function T:flush() end
  function T:send(line)
    local fh = io.open(toDm, "a")
    if not fh then return false end
    fh:write(line, "\n")
    fh:close()
    return true
  end
  function T:poll()
    local lines = {}
    local fh = io.open(fromDm, "rb")
    if not fh then return lines end
    local size = fh:seek("end")
    if size < self.offset then self.offset, self.partial = 0, "" end   -- backend truncated
    if size > self.offset then
      fh:seek("set", self.offset)
      local chunk = fh:read("*a") or ""
      self.offset = self.offset + #chunk
      local buf = self.partial .. chunk
      local start = 1
      while true do
        local nl = buf:find("\n", start, true)
        if not nl then break end
        lines[#lines + 1] = buf:sub(start, nl - 1)
        start = nl + 1
      end
      self.partial = buf:sub(start)
    end
    fh:close()
    return lines
  end
  return T
end

-- --------------------------------------------------------------- public API
function Bridge.init(config, runId, callback)
  cfg = config
  onResult = callback
  session = tostring(runId) .. "-" .. tostring(now())
  welcomed, pending, results, latencies = false, {}, {}, {}
  lastHello = -1e9
  if transport then return Bridge.status() end   -- keep the socket across runs
  local err1, err2
  if cfg.TRANSPORT == "tcp" or cfg.TRANSPORT == "auto" then
    transport, err1 = tcpTransport(cfg.HOST, cfg.PORT)
  end
  if not transport and (cfg.TRANSPORT == "file" or cfg.TRANSPORT == "auto") then
    transport, err2 = fileTransport(cfg.BRIDGE_DIR)
  end
  if not transport then
    Isaac.DebugString("[LLMDM] no transport: " .. tostring(err1) .. " / " .. tostring(err2))
  end
  return Bridge.status()
end

function Bridge.status()
  if not transport then return "offline (needs --luadebug)" end
  if welcomed then return transport.name .. " connected" end
  return transport.name .. " waiting for backend"
end

function Bridge.ready() return transport ~= nil and welcomed end

local function send(tbl)
  if not transport then return false end
  local ok, line = pcall(json.encode, tbl)
  if not ok then
    Isaac.DebugString("[LLMDM] json encode failed: " .. tostring(line))
    return false
  end
  return transport:send(line)
end

local runIdForHello
function Bridge.setRun(runId, isNewRun)
  runIdForHello = { run = runId, new_run = isNewRun }
  lastHello = -1e9
end

-- p90 of recent round-trip times; pessimistic default until we have data.
function Bridge.latencyP90()
  if #latencies < 3 then return 1800 end
  local s = {}
  for i = 1, #latencies do s[i] = latencies[i] end
  table.sort(s)
  return s[math.max(1, math.ceil(#s * 0.9))]
end

function Bridge.latencyAvg()
  if #latencies == 0 then return 0 end
  local t = 0
  for i = 1, #latencies do t = t + latencies[i] end
  return t / #latencies
end

--- Ask for a decision. Returns the request id, or nil if the DM is unreachable.
function Bridge.request(kind, key, state, ctx, deadlineMs)
  if not Bridge.ready() then return nil end
  local id = nextId
  nextId = nextId + 1
  local prev = pending[key]
  pending[key] = { id = id, sentAt = now(), kind = kind, sends = (prev and prev.sends or 0) + 1 }
  send({ t = "req", id = id, key = key, kind = kind, deadline_ms = deadlineMs or 4000,
         state = state, ctx = ctx or {} })
  return id
end

--- Change cruelty (0..10, nil = backend default). Sent now if connected, else with the next hello.
function Bridge.setCruelty(v)
  cruelty = v
  if Bridge.ready() then send({ t = "settings", cruelty = v }) end
end

function Bridge.getCruelty() return cruelty end

--- Where the backend's receipt entries for this session go (the DM Settings > Receipt menu).
function Bridge.setReceiptHandler(fn) onReceipt = fn end

--- Where the backend's after-death stats go. Not filtered by session: they can arrive after the
--- next run has started, so the handler matches them to the run they describe.
function Bridge.setSummaryHandler(fn) onSummary = fn end

function Bridge.event(name, data)
  if Bridge.ready() then send({ t = "event", name = name, data = data or {} }) end
end

function Bridge.isPending(key) return pending[key] ~= nil end
function Bridge.pendingInfo(key) return pending[key] end
function Bridge.peek(key) return results[key] end

--- Remove and return a result, telling the backend it was actually used.
function Bridge.take(key)
  local r = results[key]
  if r then
    results[key] = nil
    pending[key] = nil
    send({ t = "applied", key = key })
  end
  return r
end

--- Mark a result as used without removing it (e.g. an item list that is consumed gradually).
function Bridge.markApplied(key) send({ t = "applied", key = key }) end

--- Drop every pending/result whose key starts with prefix.
function Bridge.forget(prefix)
  for k in pairs(pending) do if k:sub(1, #prefix) == prefix then pending[k] = nil end end
  for k in pairs(results) do if k:sub(1, #prefix) == prefix then results[k] = nil end end
end

local function handle(msg)
  if msg.t == "welcome" then
    if msg.session == session then welcomed = true end
    return
  end
  if msg.t == "run_summary" then
    if onSummary then onSummary(msg) end
    return
  end
  if msg.t == "receipt" or msg.t == "receipt_sync" then
    if msg.session == session and onReceipt then onReceipt(msg) end
    return
  end
  if msg.t ~= "res" or msg.session ~= session then return end
  local key, id = msg.key, msg.id
  local p = pending[key]
  if p and p.id == id then
    latencies[#latencies + 1] = now() - p.sentAt
    if #latencies > LAT_N then table.remove(latencies, 1) end
  end
  if msg.ok and type(msg.decision) == "table" then
    local cur = results[key]
    if not cur or id >= cur.id then
      results[key] = { id = id, kind = msg.kind, decision = msg.decision, at = now(), ms = msg.ms }
      if onResult then onResult(key, results[key]) end
    end
  end
  if p and id >= p.id then pending[key] = nil end
end

--- Call every frame (MC_POST_UPDATE). Cheap when idle.
function Bridge.update()
  if not transport then return end
  transport:tick()
  if transport:ready() and not welcomed and now() - lastHello > 2000 then
    lastHello = now()
    local h = { t = "hello", session = session, cruelty = cruelty }
    if runIdForHello then h.run, h.new_run = runIdForHello.run, runIdForHello.new_run end
    send(h)
  end
  transport:flush()
  for _, line in ipairs(transport:poll()) do
    local ok, msg = pcall(json.decode, line)
    if ok and type(msg) == "table" then handle(msg) end
  end
  -- Forget requests the backend never answered (it gives up after its own deadline).
  local t = now()
  for k, p in pairs(pending) do
    if t - p.sentAt > 15000 then pending[k] = nil end
  end
end

return Bridge
