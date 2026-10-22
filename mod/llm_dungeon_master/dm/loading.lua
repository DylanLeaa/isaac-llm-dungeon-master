-- The loading screen: every floor (the run's first one included) waits here until the DM is ready
-- for it.
--
-- "Ready" means two things. First, the backend says this floor's plans are in: the run narrative
-- (on the first floor) and the floor brief. Decisions are only made against the brief, so the
-- floor's first asks are held on the backend until it lands. Second, every decision the player
-- could need on leaving the start room has an answer (its neighbours' layouts, and the item ranking
-- for any special room next door). Without this screen those first asks would land after the player
-- walked through the door, and the game would freeze on a hold (below) right away. The rest of the
-- floor (other item pools, the consumables plan) is asked in the background and isn't waited for.
--
-- On a new floor the mod sends "prepare" with a snapshot and the floor number; the backend
-- answers "ready" for that floor. Meanwhile input is blocked, the player can't be hurt, the HUD is
-- hidden and the run clock is stopped. The wait can't be skipped. The screen also ends on its own if the
-- backend can't be reached (CONNECT) or takes too long (TIMEOUT): a DM that never answers must not
-- lock the player out of the game. Nothing here ever blocks the game after it ends.
--
-- Holds: mid-run, the game sometimes needs a decision the DM hasn't answered yet (a room's
-- layout as the player walks in, the reward as the last enemy dies, a pedestal's item, a pill's
-- effect). It never decides without the DM: it freezes on this screen (input blocked, enemies
-- frozen, the player can't be hurt) until the answer lands, then the caller applies it. If the
-- backend says it has no answer, the hold asks again (HOLD_RETRIES). It only gives up if the
-- backend is gone (CONNECT) or the answer never comes (HOLD_TIMEOUT); the caller then leaves the
-- game as it is.
local Loading = {}

local game = Game()
local cfg, Bridge, State, Taunt, Sched

local on = false
local runStart = false   -- this wait also covers the run narrative
local floor = 0          -- the floor being waited for
local loadedFloor = 0    -- the last floor that got a loading screen
local startedAt, startFrame, readyAt
local sent = false
local verdict            -- the backend's "ready" status for this floor, once it arrives
local noticeAt           -- when to tell the player the DM isn't playing this run, if it isn't
local deferUntil         -- no backend seen yet: raise this floor's screen if one answers before then
local keysDone, keysTotal = 0, 0
local clock, lastTime = 0, nil  -- ms spent on screen while the game ran: a pause or tab-out doesn't count

local function tick()
  local now = Isaac.GetTime()
  if lastTime then clock = clock + math.max(0, math.min(now - lastTime, 250)) end
  lastTime = now
  return clock
end

local font = Font()
local fontOk = pcall(function() font:Load("font/pftempestasevencondensed.fnt") end) and font:IsLoaded()
local RED, WHITE, GREY = KColor(1, 0.35, 0.35, 1), KColor(1, 1, 1, 1), KColor(0.7, 0.7, 0.7, 1)
local SHADOW = KColor(0, 0, 0, 0.9)

local RUN_LINES = {
  "The DM is writing your run",
  "The DM is choosing what to take from you",
  "The DM is planning your first betrayal",
  "The DM is rehearsing the jokes",
}
local FLOOR_LINES = {
  "The DM is writing this floor",
  "The DM is reading your pockets",
  "The DM is deciding what you need least",
}
local LINE_MS = 4500

local function hud(visible)
  pcall(function() game:GetHUD():SetVisible(visible) end)
end

local holds = {}         -- key -> hold (see Loading.hold), while the game waits on that decision
local holdFrames = 0     -- frames the current freeze has lasted (for the run clock)
local frozen = {}        -- ptr hash -> {e, ...} for what this freeze stopped (FLAG_FREEZE), freed when it ends

function Loading.active() return on or next(holds) ~= nil end

local function begin(n)
  floor, sent, verdict, keysDone, keysTotal = n, false, nil, 0, 0
  if on then return end                  -- a reseed mid-wait: same screen, new floor number
  if cfg.LOADING_SCREEN == false then return end
  -- No backend has answered since the game started: likely none is running, so don't make them
  -- wait. One started after the game may still answer this run's hello: if it does in time, the
  -- screen goes up then (Loading.update).
  if not Bridge.seen() then
    deferUntil = Isaac.GetTime() + cfg.LOADING_CONNECT_MS
    return
  end
  deferUntil = nil
  on, startedAt, startFrame, readyAt = true, tick(), game:GetFrameCount(), nil
  Bridge.holdPending(true)
  hud(false)
end

--- A new run (or a continue) has started: its first floor waits for the run narrative too.
function Loading.start()
  Loading.dropHolds()
  on, runStart, loadedFloor, readyAt, deferUntil = false, true, 0, nil, nil
  noticeAt = Isaac.GetTime() + cfg.LOADING_CONNECT_MS
  begin(0)
end

local function finish(why)
  if not on then return end
  on, runStart = false, false
  hud(true)
  Bridge.holdPending(false)
  -- the clock kept running while the player stood here; it isn't part of the run
  State.addPausedFrames(game:GetFrameCount() - startFrame)
  Isaac.DebugString("[LLMDM] loading screen for floor " .. floor .. " over: " .. why)
  if why == "timeout" then Taunt.show("Still writing. I'll improvise.")
  elseif verdict == "failed" then Taunt.show("The plan never came. I'll improvise.") end
end

--- Bridge handler for the backend's "ready". A "ready" for an earlier floor is ignored.
function Loading.handle(msg)
  if not msg.status then return end
  if msg.floor ~= nil and tonumber(msg.floor) ~= floor then return end
  verdict = tostring(msg.status)
end

-- How many of the floor's opening decisions have an answer (or a definite "none").
local function countKeys()
  local keys = Sched.floorReadyKeys()
  local done = 0
  for _, k in ipairs(keys) do
    if Bridge.settled(k) then done = done + 1 end
  end
  return done, #keys
end

--- MC_POST_RENDER, after Bridge.update: ask, wait, and end the wait.
function Loading.update()
  if noticeAt and Isaac.GetTime() >= noticeAt then
    noticeAt = nil
    local why = Bridge.problem()
    if why then Taunt.show(why) end
  end
  -- A new floor (or the run's first) starts its own wait.
  local f = Sched.floor()
  if f > 0 and f ~= loadedFloor then
    loadedFloor = f
    begin(f)
  end
  if deferUntil and not on then
    if Bridge.seen() then begin(floor)
    elseif Isaac.GetTime() > deferUntil then deferUntil = nil end
  end
  if not on then return end
  local t = tick()
  if floor == 0 then return end           -- run start, before the first floor is known
  if Bridge.ready() then
    if not sent then                     -- also again after a reconnect: the backend answers once per ask
      sent, readyAt = true, readyAt or t
      Bridge.prepare(State.snapshot(), floor)
    end
  else
    sent = false
  end
  if verdict then
    keysDone, keysTotal = countKeys()
    if keysDone >= keysTotal then return finish(verdict) end
  end
  if not readyAt and t - startedAt > cfg.LOADING_CONNECT_MS then return finish("offline") end
  local limit = runStart and cfg.LOADING_TIMEOUT_MS or (cfg.LOADING_FLOOR_TIMEOUT_MS or cfg.LOADING_TIMEOUT_MS)
  if readyAt and t - readyAt > limit then return finish("timeout") end
  -- Keep the player safe and the room dark while waiting.
  pcall(function() Isaac.GetPlayer(0):SetMinDamageCooldown(30) end)
  pcall(function() game:Darken(1, 2) end)
end

-- ------------------------------------------------------------------ holds
local FRAME_MS = 1000 / 30

--- Freeze the game until the DM answers `key`. spec:
---   label   what the screen says the DM is deciding ("your reward")
---   ask()   (re)sends the request; called now if nothing is on its way, and on each retry
---   apply(res)  the answer is in (res = Bridge.peek(key)); take or mark it applied here
---   giveUp()    optional: no answer is coming; leave the game as it is
---   stale   optional: a result id already seen and not good enough (wait for a newer one)
--- One hold per key: holding a key that is already held keeps the first spec, so callers keep
--- what the answer is for (the pedestals, the pickups) in their own state.
function Loading.hold(key, spec)
  if holds[key] or not key then return end
  local res = Bridge.peek(key)
  if res and res.id ~= spec.stale then return spec.apply(res) end   -- landed after all
  holds[key] = { spec = spec, frames = 0, offline = 0, retries = 0 }
  if not Bridge.isPending(key) then spec.ask() end
  Isaac.DebugString("[LLMDM] holding the game for " .. key)
end

function Loading.holding(key) return holds[key] ~= nil end

local function hashOf(e) return GetPtrHash and GetPtrHash(e) or e end

-- FLAG_FREEZE alone can't be trusted: it is a status effect with no countdown, and the game may
-- clear it (enemies just spawned into a new room walked straight through it), so it is put back
-- every frame. Enemies are really stopped by Loading.holdNpc; projectiles have no pre-update
-- hook in the vanilla API, so they are pinned in place here.
local function freezeWorld()
  pcall(function() Isaac.GetPlayer(0):SetMinDamageCooldown(30) end)
  pcall(function() game:Darken(1, 2) end)
  local flag = EntityFlag and EntityFlag.FLAG_FREEZE
  if not flag then return end
  for _, e in ipairs(Isaac.GetRoomEntities()) do
    local shot = e.Type == EntityType.ENTITY_PROJECTILE and e:ToProjectile()
    if shot or e:IsActiveEnemy(false) then
      local h = hashOf(e)
      local f = frozen[h]
      if not f and not e:HasEntityFlags(flag) then   -- already frozen by the game: not ours to free
        f = { e = e }
        if shot then
          f.pos, f.vel = Vector(e.Position.X, e.Position.Y), Vector(e.Velocity.X, e.Velocity.Y)
          f.height, f.fall = shot.Height, shot.FallingSpeed
        end
        frozen[h] = f
      end
      if f then
        pcall(function() e:AddEntityFlags(flag) end)
        if shot then
          e.Position, e.Velocity = f.pos, Vector(0, 0)
          shot.Height, shot.FallingSpeed = f.height, 0
        end
      end
    end
  end
end

local function thaw()
  local flag = EntityFlag and EntityFlag.FLAG_FREEZE
  for _, f in pairs(frozen) do
    pcall(function()
      local e = f.e
      if not e:Exists() then return end
      e:ClearEntityFlags(flag)
      if f.vel then e.Velocity, e:ToProjectile().FallingSpeed = f.vel, f.fall end
    end)
  end
  frozen = {}
  State.addPausedFrames(holdFrames)     -- the freeze isn't part of the run's time either
  holdFrames = 0
end

-- One hold, one frame. A hold that's over is dropped before its callback runs, so the callback
-- may hold the same key again.
local function step(key, h)
  local res = Bridge.peek(key)
  if res and res.id ~= h.spec.stale then
    Isaac.DebugString("[LLMDM] " .. key .. " answered after a " .. math.floor(h.frames * FRAME_MS) .. "ms hold")
    holds[key] = nil
    return h.spec.apply(res)
  end
  h.frames = h.frames + 1
  local why
  if not Bridge.ready() then
    h.offline = h.offline + 1
    if h.offline * FRAME_MS > cfg.LOADING_CONNECT_MS then why = "backend gone" end
  elseif not Bridge.isPending(key) then           -- answered "no decision", or never answered
    h.offline = 0
    if h.retries < (cfg.HOLD_RETRIES or 2) then
      h.retries = h.retries + 1
      h.spec.ask()
    else
      why = "no answer after " .. h.retries .. " re-asks"
    end
  end
  if not why and h.frames * FRAME_MS > (cfg.HOLD_TIMEOUT_MS or 45000) then why = "timed out" end
  if not why then return end
  Isaac.DebugString("[LLMDM] gave up holding for " .. key .. ": " .. why)
  holds[key] = nil
  Taunt.show("I lost my train of thought. Carry on.")
  if h.spec.giveUp then h.spec.giveUp(why) end
end

--- MC_POST_UPDATE: resolve holds whose answers are in, keep the world frozen for the rest.
function Loading.updateHolds()
  if not next(holds) then return end
  local keys = {}                     -- an answer's apply may start another hold: don't walk a moving table
  for key in pairs(holds) do keys[#keys + 1] = key end
  for _, key in ipairs(keys) do
    if holds[key] then step(key, holds[key]) end
  end
  if next(holds) then
    if not on then holdFrames = holdFrames + 1 end   -- the floor screen counts its own frames
    freezeWorld()
  else
    thaw()
  end
end

--- MC_PRE_NPC_UPDATE: while the game is held, no enemy thinks or moves. Returning true skips its
--- AI, but not its physics, so its velocity is zeroed too. This also covers enemies that spawn
--- mid-hold, and a room's first frames, before freezeWorld has run once.
function Loading.holdNpc(_, npc)
  if not next(holds) then return nil end
  npc.Velocity = Vector(0, 0)
  return true
end

--- A new run: whatever the last one was waiting on is gone.
function Loading.dropHolds()
  holds = {}
  frozen, holdFrames = {}, 0
end

--- MC_INPUT_ACTION: nothing the player presses reaches the game while the screen is up.
function Loading.blockInput(_, _, hook, action)
  if not on and not next(holds) then return nil end
  if action == ButtonAction.ACTION_PAUSE then return nil end   -- they can always leave
  if hook == InputHook.GET_ACTION_VALUE then return 0 end
  return false
end

local function draw(s, x, y, color, boxW)
  if fontOk then
    font:DrawStringScaled(s, x + 1, y + 1, 1, 1, SHADOW, boxW, true)
    font:DrawStringScaled(s, x, y, 1, 1, color, boxW, true)
  else
    Isaac.RenderText(s, math.max(4, x + boxW / 2 - #s * 3), y, color.Red, color.Green, color.Blue, color.Alpha)
  end
end

--- MC_POST_RENDER.
local function renderHold()
  local key, h = next(holds)
  if not key then return end
  local most = h
  for _, o in pairs(holds) do if o.frames > most.frames then most = o end end
  local w, ht = Isaac.GetScreenWidth(), Isaac.GetScreenHeight()
  local t = Isaac.GetTime()
  draw("The DM is deciding " .. (most.spec.label or "something") .. string.rep(".", 1 + math.floor(t / 400) % 3),
       0, ht / 2 - 14, RED, w)
  draw(string.format("%ds", math.floor(most.frames * FRAME_MS / 1000)), 0, ht / 2 + 4, WHITE, w)
end

function Loading.render()
  if not on then return renderHold() end
  local t = clock
  local w, h = Isaac.GetScreenWidth(), Isaac.GetScreenHeight()
  local secs = math.floor((t - startedAt) / 1000)
  local line
  if not readyAt then
    line = "Finding the DM"
  elseif verdict then
    line = string.format("The DM is dressing the rooms (%d/%d)", keysDone, keysTotal)
  else
    local lines = runStart and RUN_LINES or FLOOR_LINES
    line = lines[(math.floor((t - readyAt) / LINE_MS) % #lines) + 1]
  end
  draw(line .. string.rep(".", 1 + math.floor(t / 400) % 3), 0, h / 2 - 14, RED, w)
  draw(string.format("%ds", secs), 0, h / 2 + 4, WHITE, w)
end

function Loading.init(config, bridge, state, taunt, sched)
  cfg, Bridge, State, Taunt, Sched = config, bridge, state, taunt, sched
  if Bridge.keepPending then Bridge.keepPending(Loading.holding) end
end

return Loading
