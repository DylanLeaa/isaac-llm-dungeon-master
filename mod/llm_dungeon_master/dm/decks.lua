-- The deck store: every stream's answers live here.
--
-- A deck is a short list of outcomes the DM wrote ahead of time, one per roll: the next cards, what each
-- kill in this room drops, what a beggar pays use by use, a room's dressing (a deck of one). Streams
-- (dm/streams/*.lua) define a deck type and draw from it when the game rolls:
--
--   Decks.define(type, spec)        spec fields:
--     kind            the backend decision kind (nil: the deck is filled by another, e.g. rock loot)
--     key(arg)        the key it is asked under ("drops:3", "payout:3:1234")
--     entries(d, st)  the answer's outcomes, in draw order
--     usable(e, roll) can this entry serve this roll? true, false (skip it) or "drop" (never: discard it)
--     sticky          drawing doesn't use an entry up (pill colours, a room's rock loot)
--     context(st, reason)   the request's ctx (false: nothing to ask about)
--     wanted(st)      how many entries to ask for (ctx.wanted); deck = false sends no deck fields
--     wants()         {arg, n, reason} the stream needs stocked right now: asked when fewer than n are left
--     refresh         {max, minMs}: stale tails (the snapshot or the economy moved) are rewritten this often
--     maxSends        requests per key (per floor)
--     owedCtx(owed)   ctx.owed for the draws waiting on this deck (default: how many)
--     peekOwed        an IOU is settled by looking, not drawing (a pill isn't swallowed by the answer)
--     mercy(e)        the entry as it may be handed out now (at <= 1 heart a trap becomes a half heart)
--     outcome(e, roll)  what the backend is told was handed out (`drawn`)
--     label(e)        the entry as ctx.deck_left lists it; holdLabel: what the frozen screen says
--
--   Decks.draw(type, arg, roll)    the next usable entry, used up and reported (Bridge.drawn), or nil
--   Decks.owe(type, arg, roll, fixup)   the only late-answer path: nothing usable now, the game goes ahead
--                                   and is held (loading.lua) until an answer lands; fixup(entry) then puts
--                                   the DM's outcome in (entry nil: none came, the game's stands)
--   Decks.update()                  restocks: a request when a wanted deck runs low, a rewrite of the
--                                   unused tail when the state it was written against moved
--
-- An answer replaces the deck's unused tail (latest wins per key). Entries drawn after a request went out
-- were already handed out, so the answer's first entries in their places are skipped.
local Decks = {}

local cfg, Bridge, State, Loading, Taunt

local specs = {}
local streams = {}      -- key -> stream
local toHold = {}       -- key -> stream: IOUs raised inside the game's callbacks, held on the next update
local fallbackSent = {}
local lastRestock = -1e9

local TRAPS = { troll_bomb = true, mimic_chest = true, spiked_chest = true }

function Decks.init(config, bridge, state, loading, taunt)
  cfg, Bridge, State, Loading, Taunt = config, bridge, state, loading, taunt
end

function Decks.define(dt, spec)
  spec.type = dt
  specs[dt] = spec
end

function Decks.spec(dt) return specs[dt] end

function Decks.get(dt, arg)
  local key = specs[dt].key(arg)
  local st = streams[key]
  if not st then
    st = { type = dt, key = key, arg = arg, entries = {}, drawn = {}, sends = 0, stales = 0, lastAsk = -1e9,
           owed = {}, asks = {} }
    streams[key] = st
  end
  return st
end

function Decks.find(key) return streams[key] end

--- Take in the newest answer, if there is one we haven't seen.
local function sync(st)
  local spec = specs[st.type]
  local res
  if spec.result then res = spec.result(st) else res = Bridge.peek(st.key) end
  if res and res.id ~= st.resultId then
    local list = spec.entries(res.decision, st) or {}
    local ask = st.asks[res.id]
    -- Draws since the answer's base: a fresh answer was written against the deck as it was asked; a reused
    -- one (the backend's gate kept it) is already what was left when it was sent.
    local base = res.base or (ask and ask.drawn)
    local skip = (base and not spec.sticky) and math.max(0, #st.drawn - base) or 0
    st.entries = {}
    for i = skip + 1, #list do st.entries[#st.entries + 1] = list[i] end
    st.resultId, st.decision = res.id, res.decision
    st.taunt, st.tauntShown = res.decision.taunt, false
    st.answerHash = ask and ask.hash or nil
    st.answerAsk = ask
  end
  return st
end

function Decks.sync(dt, arg) return sync(Decks.get(dt, arg)) end

--- How many unused entries the deck holds.
function Decks.left(dt, arg) return #sync(Decks.get(dt, arg)).entries end

local function labelOf(spec, e)
  if spec.label then return spec.label(e) end
  if type(e) == "table" and e.reward then
    return e.reward == "nothing" and "nothing" or ((e.count or 1) .. "x " .. e.reward)
  end
  return tostring(e)
end

--- Ask the DM for this deck. force: the game is waiting on it, so maxSends doesn't apply.
function Decks.request(dt, arg, reason, force, stale)
  local spec = specs[dt]
  local st = sync(Decks.get(dt, arg))
  if not spec.kind or not Bridge.ready() then return nil end
  if not force and spec.maxSends and st.sends >= spec.maxSends then return nil end
  local ctx = spec.context and spec.context(st, reason) or {}
  if ctx == false then return nil end
  ctx.reason = reason
  if spec.deck ~= false then
    ctx.wanted = spec.wanted and spec.wanted(st) or 1
    local left, drawn = {}, {}
    for i, e in ipairs(st.entries) do left[i] = labelOf(spec, e) end
    for i = math.max(1, #st.drawn - 5), #st.drawn do drawn[#drawn + 1] = labelOf(spec, st.drawn[i]) end
    ctx.deck_left, ctx.drawn_so_far = left, drawn
  end
  if #st.owed > 0 then ctx.owed = spec.owedCtx and spec.owedCtx(st.owed) or #st.owed end
  local deadline = spec.deadline
  if type(deadline) == "function" then deadline = deadline(st) end
  local id = Bridge.request(spec.kind, st.key, State.snapshot(), ctx, deadline or 10000)
  if id then
    st.sends, st.lastAsk = st.sends + 1, Isaac.GetTime()
    st.asks[id] = { drawn = #st.drawn, hash = State.materialHash() }
    st.lastAskHash, st.askedOwed = st.asks[id].hash, #st.owed > 0
    st.staleFlag = false
    if not stale then st.stales = 0 end
  end
  return id
end

--- Is there an answer (or one on its way) written against the player's state as it is now?
function Decks.upToDate(dt, arg)
  local st = Decks.get(dt, arg)
  return (Bridge.isPending(st.key) or Bridge.peek(st.key) ~= nil) and st.lastAskHash == State.materialHash()
end

-- ----------------------------------------------------------------- drawing
--- The player is one or two hits from death.
function Decks.mercy()
  local p = Isaac.GetPlayer(0)
  return p:GetHearts() + p:GetSoulHearts() <= 2
end

--- A reward as it may be handed out right now: at <= 1 heart, a trap becomes a half heart. (clamp() made
--- it legal when written; this is for the state having changed since.)
function Decks.mercyReward(name)
  if TRAPS[name] and Decks.mercy() then return "half_heart", true end
  return name, false
end

function Decks.consume(st, e, roll)
  local spec = specs[st.type]
  st.drawn[#st.drawn + 1] = e
  local outcome = spec.outcome and spec.outcome(e, roll or {}) or e
  Bridge.drawn(st.key, spec.kind and st.resultId or nil, outcome, roll and roll.cause or nil)
  if not st.tauntShown then
    st.tauntShown = true
    if st.taunt then Taunt.show(st.taunt) end
  end
end

--- The next entry usable for this roll, without using it.
function Decks.peek(dt, arg, roll)
  local spec, st = specs[dt], sync(Decks.get(dt, arg))
  for _, e in ipairs(st.entries) do
    local ok = not spec.usable or spec.usable(e, roll or {}, st)
    if ok and ok ~= "drop" then return e, st end
  end
  return nil, st
end

--- The next entry usable for this roll, used up (unless sticky) and reported. nil if none.
function Decks.draw(dt, arg, roll)
  local spec, st = specs[dt], sync(Decks.get(dt, arg))
  roll = roll or {}
  local i = 1
  while i <= #st.entries do
    local e = st.entries[i]
    local ok = not spec.usable or spec.usable(e, roll, st)
    if ok == "drop" then
      table.remove(st.entries, i)
    elseif ok then
      if not spec.sticky then table.remove(st.entries, i) end
      if spec.mercy then e = spec.mercy(e) end
      Decks.consume(st, e, roll)
      return e, st
    else
      i = i + 1
    end
  end
  return nil, st
end

-- ----------------------------------------------------------------- owing
function Decks.reportFallback(kind, key)
  if fallbackSent[key] then return end
  fallbackSent[key] = true
  Bridge.event("fallback", { kind = kind, key = key, asked = true })
end

local function settle(st)
  local spec = specs[st.type]
  local owed = st.owed
  st.owed = {}
  for _, o in ipairs(owed) do
    local e
    if spec.peekOwed then e = Decks.peek(st.type, st.arg, o.roll) else e = Decks.draw(st.type, st.arg, o.roll) end
    o.fixup(e, false)
  end
end

local function hold(st)
  local spec = specs[st.type]
  sync(st)
  -- An answer already in had nothing for these draws: wait for a newer one, asked with what is owed. One
  -- on its way is kept (a re-ask would restart it), unless what is owed changes the answer (a rune-only
  -- card draw, a pill colour it may not cover).
  if not Bridge.isPending(st.key) or (spec.owedCtx and not st.askedOwed) then
    Decks.request(st.type, st.arg, "the game is waiting on this right now", true)
  end
  Loading.hold(st.key, {
    label = spec.holdLabel or "something",
    stale = st.resultId,
    ask = function() Decks.request(st.type, st.arg, "the game is waiting on this right now", true) end,
    apply = function() settle(st) end,
    giveUp = function()
      local owed = st.owed
      st.owed = {}
      for _, o in ipairs(owed) do o.fixup(nil, true) end
      Decks.reportFallback(spec.kind, st.key)
    end,
  })
end

--- Nothing usable for this roll: the game goes ahead now, and the deck owes it. The game is held until the
--- answer lands, from the next update (never from inside the game's own callback), or now with holdNow.
--- False if there is no backend to wait for.
function Decks.owe(dt, arg, roll, fixup)
  if not Bridge.ready() then return false end
  local st = Decks.get(dt, arg)
  st.owed[#st.owed + 1] = { roll = roll or {}, fixup = fixup }
  toHold[st.key] = st
  return true
end

--- Hold for this deck's IOUs right away (safe outside the game's spawn/draw callbacks).
function Decks.holdNow(dt, arg)
  local st = Decks.get(dt, arg)
  toHold[st.key] = nil
  if #st.owed > 0 then hold(st) end
end

--- The backend says these decks' unused entries are out of date (another deck spent their budget).
function Decks.markStale(keys)
  for _, k in ipairs(keys or {}) do
    local st = streams[k]
    if st then st.staleFlag = true end
  end
end

-- ----------------------------------------------------------------- restocking
local function restock(spec, w, t)
  local st = sync(Decks.get(spec.type, w.arg))
  if Bridge.isPending(st.key) then return end
  if #st.entries < (w.n or 1) then
    if spec.askGapMs and t - st.lastAsk < spec.askGapMs then return end
    Decks.request(spec.type, w.arg, w.reason or "restock")
    return
  end
  -- The unused tail was written against a state that has moved: rewrite it (the drawn entries stand).
  local r = spec.refresh
  if r and #st.entries > 0 and (st.staleFlag or (st.answerHash and st.answerHash ~= State.materialHash()))
      and st.stales < r.max and t - st.lastAsk >= (r.minMs or 0) and (not r.ok or r.ok(st)) then
    if spec.maxSends and st.sends >= spec.maxSends then return end
    st.stales = st.stales + 1
    Decks.request(spec.type, w.arg, st.staleFlag and "stale: another deck spent this budget"
                                     or (r.reason or "refresh: player state changed"), false, true)
  end
end

--- Every frame (after the streams' own updates): hold for IOUs, then keep wanted decks stocked.
function Decks.update()
  for key, st in pairs(toHold) do
    toHold[key] = nil
    if #st.owed > 0 then hold(st) end
  end
  if not Bridge.ready() then return end
  local t = Isaac.GetTime()
  if t - lastRestock < 100 then return end
  lastRestock = t
  for _, spec in pairs(specs) do
    if spec.wants and spec.kind and (not spec.enabled or spec.enabled()) then
      for _, w in ipairs(spec.wants() or {}) do restock(spec, w, t) end
    end
  end
end

--- The player left this deck's room before it was used: the backend stops counting it as promised.
function Decks.abandon(dt, arg)
  local key = specs[dt].key(arg)
  local st = streams[key]
  if st and #st.drawn == 0 and (Bridge.isPending(key) or Bridge.peek(key)) then Bridge.abandon(key) end
  streams[key] = nil
  Bridge.forget(key)
end

-- ----------------------------------------------------------------- lifecycle
--- A new floor: every deck starts over, except a late answer the new floor still waits on (its curses).
function Decks.onNewFloor()
  local keep = {}
  for key, st in pairs(toHold) do
    if specs[st.type].crossFloor then keep[key] = st end
  end
  streams, toHold, fallbackSent = {}, keep, {}
  for key, st in pairs(keep) do streams[key] = st end
end

function Decks.reset()
  streams, toHold, fallbackSent, lastRestock = {}, {}, {}, -1e9
end

return Decks
