-- Bridge to the Dungeon Master backend, from inside Isaac's mod sandbox (no sockets, no io).
--
-- Out: each message is one JSON line written to the game's log.txt with Isaac.DebugString, in
-- chunks of LOG_CHUNK chars tagged [LLMDM:<n>:<i>/<k>]. The backend tails the log.
-- In: the backend writes its messages into this mod's save file (dm.savefile, under "dm"),
-- numbered. We read the file every POLL_MS, handle the ones we haven't seen and ack the highest on
-- our next message, so the backend can drop them. See backend/bridge.py for its side.
--
-- Every decision is addressed by a `key` ("clear:3:84"). Requests for the same key
-- supersede each other: the backend cancels the stale one, and here a result is only
-- replaced by a result for a newer request. That is what lets the decks (decks.lua) ask early,
-- then re-ask with a fresher snapshot, without ever blocking the game.
--
-- A result is a deck: each outcome the game draws from it is reported (`drawn`), so the backend
-- counts what was actually handed out. The backend can push `stale` for keys whose unused entries
-- another deck's spending made out of date (the floor's economy); decks.lua re-asks those.
local json = require("json")

local Bridge = {}

-- Must match PROTO in backend/bridge.py. Bump both when a message changes shape.
-- 2: decks (`drawn` per draw, `abandon`, the backend's `stale` push), cards/trinkets/pills/drops kinds.
local PROTO = 2
local HELLO_MS = 2000
local PING_MS = 2000      -- how often a connected bridge checks the backend is still there
local LINK_TIMEOUT_MS = 6000
local ACK_MS = 250        -- how long an ack waits to ride on a message before going alone

local cfg, store
local session
local welcomed = false
local seen = false     -- the backend has welcomed us since the game started
local outdated         -- the backend's protocol, when it isn't ours
local nextId = 1
local pending = {}     -- key -> {id, sentAt, kind, sends}
local results = {}     -- key -> {id, kind, decision, at, ms}
local failed = {}      -- key -> true: the backend answered "no decision" (timeout / error)
local holding = false  -- a loading screen is up: requests wait on the floor brief, don't expire them
local latencies = {}   -- ring buffer of recent round-trips (ms)
local LAT_N = 24
local lastHello = -1e9
local onSummary        -- optional callback(msg) for the backend's run_summary (after-death stats)
local onReady          -- optional callback(msg) for the backend's "ready" (the run narrative is in)
local onEffect         -- optional callback(lines) for every effect sent (the on-screen effect list)
local onStale          -- optional callback(keys) for the backend's "stale" (decks to rewrite)
local cruelty          -- nil = backend's --cruelty; set from config / the DM Settings menu

local lineNo = 0       -- numbers our messages in the log
local boot             -- the backend run whose messages we're reading
local lastSeq = 0      -- its last message we handled
local ackedSeq = 0     -- the last of those we've told it about
local lastSent, lastPoll = -1e9, -1e9
local pingId, pingAt, nextPing = 0, nil, 0
local linkMs           -- the last ping's round trip: the link alone, no LLM

local function now() return Isaac.GetTime() end
Bridge.now = now

-- --------------------------------------------------------------- public API
function Bridge.init(config, theStore, runId)
  cfg, store = config, theStore
  session = tostring(runId) .. "-" .. tostring(now())
  welcomed, pending, results, failed, latencies = false, {}, {}, {}, {}
  outdated, pingAt, linkMs = nil, nil, nil
  lastHello = -1e9
  -- The backend finds our save file by this: it looks for the session our hello names.
  store.save({ link = session })
  return Bridge.status()
end

function Bridge.status()
  if welcomed then return linkMs and string.format("connected, link %dms", linkMs) or "connected" end
  if outdated then return "backend speaks protocol " .. tostring(outdated) .. ", mod " .. PROTO end
  return "waiting for backend"
end

function Bridge.ready() return welcomed end

--- The backend has answered since the game started (it may have gone since).
function Bridge.seen() return seen end

--- Why the DM isn't playing, in its voice; nil when it is.
function Bridge.problem()
  if welcomed then return nil end
  if outdated then
    if tonumber(outdated) and outdated > PROTO then return "This mod is older than my backend. Update it. Vanilla RNG for now." end
    return "My backend is out of date. Update it. Vanilla RNG for now."
  end
  return "I'm not running. Vanilla RNG decides this run."
end

local function send(tbl)
  if lastSeq > ackedSeq then tbl.ack, tbl.boot, ackedSeq = lastSeq, boot, lastSeq end
  local ok, line = pcall(json.encode, tbl)
  if not ok then
    Isaac.DebugString("[LLMDM] json encode failed: " .. tostring(line))
    return false
  end
  -- JSON has "%" only inside strings, where the escape % reads the same: keeps format specifiers
  -- out of the log call.
  line = line:gsub("%%", "\\u0025")
  local size = cfg.LOG_CHUNK or 900
  local k = math.max(1, math.ceil(#line / size))
  lineNo = lineNo + 1
  for i = 1, k do
    Isaac.DebugString(string.format("[LLMDM:%d:%d/%d]", lineNo, i, k) .. line:sub((i - 1) * size + 1, i * size))
  end
  lastSent = now()
  return true
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
  failed[key] = nil
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

--- Where the backend's after-death stats go. Not filtered by session: they can arrive after the
--- next run has started, so the handler matches them to the run they describe.
function Bridge.setSummaryHandler(fn) onSummary = fn end

--- Where the backend's "ready" goes (the loading screen).
function Bridge.setReadyHandler(fn) onReady = fn end

--- Start the planners now and be told when this floor's plans (narrative + brief) are in. Safe to repeat.
function Bridge.prepare(state, floor)
  if Bridge.ready() then send({ t = "prepare", state = state, floor = floor }) end
end

--- While a loading screen is up, requests sit on the backend waiting for the floor brief: keep them
--- pending however long that takes. When it ends, their 15s expiry starts over.
function Bridge.holdPending(on)
  if holding and not on then
    local t = now()
    for _, p in pairs(pending) do p.sentAt = t end
  end
  holding = on and true or false
end

--- A key the game is frozen on (Loading.hold) never expires: its answer may come late, and a re-ask
--- would restart the call that is about to land. The hold has its own timeout.
local keepPending = function() return false end
function Bridge.keepPending(fn) keepPending = fn end

function Bridge.event(name, data)
  if Bridge.ready() then send({ t = "event", name = name, data = data or {} }) end
end

function Bridge.isPending(key) return pending[key] ~= nil end
function Bridge.pendingInfo(key) return pending[key] end
function Bridge.peek(key) return results[key] end
--- The backend has answered this key, with a decision or with "none in time".
function Bridge.settled(key) return results[key] ~= nil or failed[key] == true end

--- Remove and return a result (what it hands out is reported with Bridge.drawn).
function Bridge.take(key)
  local r = results[key]
  if r then
    results[key] = nil
    pending[key] = nil
  end
  return r
end

--- One outcome the game drew from the deck answered as request `id` under `key`, and why the game
--- rolled ("enemy_death", "chest_open"...). The first draw from an answer is what makes it "applied".
function Bridge.drawn(key, id, outcome, cause)
  send({ t = "drawn", key = key, id = id, outcome = outcome, cause = cause })
end

--- The game will never use this key's answer (the player left the room): it promises nothing now.
function Bridge.abandon(key) send({ t = "abandon", key = key }) end

--- Where the backend's "stale" goes: keys whose unused entries should be rewritten.
function Bridge.setStaleHandler(fn) onStale = fn end

--- What a used result actually did, as short lines ("+1 bomb dropped"), for the receipt. A result
--- used gradually reports each use as it happens.
function Bridge.effect(key, lines)
  if type(lines) == "table" and #lines > 0 then
    send({ t = "effect", key = key, effect = lines })
    if onEffect then pcall(onEffect, lines) end    -- a display must never stop the game's side
  end
end

--- Who else hears about each effect (the on-screen effect list).
function Bridge.setEffectHandler(fn) onEffect = fn end

--- Drop every pending/result whose key starts with prefix.
function Bridge.forget(prefix)
  for k in pairs(pending) do if k:sub(1, #prefix) == prefix then pending[k] = nil end end
  for k in pairs(results) do if k:sub(1, #prefix) == prefix then results[k] = nil end end
  for k in pairs(failed) do if k:sub(1, #prefix) == prefix then failed[k] = nil end end
end

local function handle(msg)
  if msg.t == "welcome" then
    if msg.session ~= session then return end
    if msg.proto ~= PROTO then
      outdated = msg.proto or 0
      return
    end
    outdated, welcomed, seen = nil, true, true
    -- The backend now knows this run; a later hello (the app restarted mid-run) is a continue.
    if runIdForHello then runIdForHello.new_run = false end
    pingAt, nextPing = nil, now() + PING_MS
    return
  end
  if msg.t == "pong" then
    if pingAt and msg.id == pingId then
      linkMs, pingAt, nextPing = now() - pingAt, nil, now() + PING_MS
    end
    return
  end
  if msg.t == "ready" then
    if msg.session == session and onReady then onReady(msg) end
    return
  end
  if msg.t == "run_summary" then
    if onSummary then onSummary(msg) end
    return
  end
  if msg.t == "stale" then
    if msg.session == session and onStale and type(msg.keys) == "table" then onStale(msg.keys) end
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
      -- base: a reused answer already accounts for this many draws of the key (decks.lua skips the rest)
      results[key] = { id = id, kind = msg.kind, decision = msg.decision, at = now(), ms = msg.ms,
                       base = msg.drawn_base }
    end
  elseif not msg.ok and (not p or id >= p.id) then
    failed[key] = true
  end
  if p and id >= p.id then pending[key] = nil end
end

-- The backend's section of our save file: its numbered messages, oldest first.
local function receive(dm)
  if type(dm) ~= "table" or type(dm.msgs) ~= "table" then return end
  if dm.boot ~= boot then boot, lastSeq, ackedSeq = dm.boot, 0, 0 end   -- a new backend counts from 1
  for _, m in ipairs(dm.msgs) do
    local seq = tonumber(m[1]) or 0
    if seq > lastSeq then
      lastSeq = seq
      if type(m[2]) == "table" then
        local ok, err = pcall(handle, m[2])
        if not ok then Isaac.DebugString("[LLMDM] error handling " .. tostring(m[2].t) .. ": " .. tostring(err)) end
      end
    end
  end
end

--- Call every frame (MC_POST_RENDER). Cheap when idle.
function Bridge.update()
  local t = now()
  -- Until a backend has answered, there may be none: look less often.
  if t - lastPoll >= (seen and (cfg.POLL_MS or 30) or 250) then
    lastPoll = t
    receive(store.poll())
  end
  if welcomed then
    if pingAt and t - pingAt > LINK_TIMEOUT_MS then
      welcomed, pingAt, linkMs = false, nil, nil       -- closed or restarted: say hello again
      lastHello = -1e9
      Isaac.DebugString("[LLMDM] backend stopped answering")
    elseif not pingAt and t >= nextPing then
      pingId, pingAt = pingId + 1, t
      send({ t = "ping", id = pingId })
    end
  end
  if not welcomed and t - lastHello > (outdated and 5 * HELLO_MS or HELLO_MS) then
    lastHello = t
    local h = { t = "hello", session = session, proto = PROTO, cruelty = cruelty }
    if runIdForHello then h.run, h.new_run = runIdForHello.run, runIdForHello.new_run end
    send(h)
  end
  if lastSeq > ackedSeq and t - lastSent > ACK_MS then send({ t = "ack" }) end
  -- Forget requests the backend never answered (it gives up after its own deadline).
  if holding then return end
  for k, p in pairs(pending) do
    if t - p.sentAt > 15000 and not keepPending(k) then pending[k] = nil end
  end
end

return Bridge
