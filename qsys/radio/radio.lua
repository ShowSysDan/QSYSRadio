-- QSYSRadio deck controller (Q-SYS Lua 5.3). Docs: README.md. Station settings are
-- edited on the server's web page and pushed here; SETTINGS below are only the
-- fallback until the server has been reached once.
RADIO_VERSION = "0.3.0-dev"

CFG = {
  PLAYER_A = "Audio_Player_01", METER_A = 1,
  PLAYER_B = "Audio_Player_02", METER_B = 2,
  METER = "PlayerOutput01",
  SERVER_URL = "",     -- e.g. "http://10.0.0.20:5410"; "" = run standalone
  SERVER_TOKEN = "",   -- RADIO_API_TOKEN from the server's .env
  STATION_ID = "",     -- "" = the design name
  AUTOSTART = true,
}

SETTINGS = {
  music_dir = "Test/Tuning/", imaging_dir = "Test/Imaging/",
  play_gain_db = 0, imaging_gain_db = 0,
  crossfade_s = 4, fade_in_s = 0, skip_fade_s = 1.5, silence_fade_s = 1, stop_fade_s = 2, bumper_fade_s = 2,
  silence_threshold_dbfs = -50, silence_hold_s = 2, end_guard_s = 20,
  bumper_every_n_tracks = 4, bumper_tail_s = 1, artist_separation = 2, dead_air_s = 1.5,
}
DEFAULTS = {}
for k, v in pairs(SETTINGS) do DEFAULTS[k] = v end

-- internals (not settings)
local K = {
  floor_db = -100, tick_s = 0.25, fast_s = 0.05, load_timeout_s = 1.5, chores_s = 30, log_ring = 200,
  sync_s = 1, sync_batch = 100, state_every_s = 10, http_timeout_s = 5, outbox_max = 5000,
  learned_save_s = 300, outbox_save_s = 30,
}
FILES = { learned = "media/_radio/learned.json", outbox = "media/_radio/outbox.json",
          settings = "media/_radio/settings.json", state = "media/_radio/state.json" }

SUPPORTED_EXT = { flac = true, mp3 = true, wav = true, aac = true, m4a = true, ogg = true, aif = true, aiff = true }

-- ── UTILITIES ─────────────────────────────────────────────────────────────────
-- RapidJSON first: Q-SYS recommends it for large data (no execution-count limit)
local json = (function()
  for _, name in ipairs({ "rapidjson", "json" }) do
    local ok, j = pcall(require, name)
    if ok and type(j) == "table" then return j end
  end
end)()

local function now() return Timer.Now() end
local function utc() return os.date("!%Y-%m-%dT%H:%M:%SZ") end
local function startsWith(s, p) return type(s) == "string" and s:sub(1, #p) == p end
local function round(v, places) local m = 10 ^ (places or 2); return math.floor(v * m + 0.5) / m end
local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end
local function fmtClock(sec)
  if type(sec) ~= "number" then return "-:--" end
  sec = math.max(0, math.floor(sec + 0.5))
  return string.format("%d:%02d", sec // 60, sec % 60)
end
local function normDir(d)
  d = tostring(d or ""):gsub("^/+", "")
  if d ~= "" and d:sub(-1) ~= "/" then d = d .. "/" end
  return d
end
local function label(tr) return tr and ((tr.artist and (tr.artist .. " - ") or "") .. (tr.title or tr.name)) or "-" end
-- append to a bounded list (oldest dropped); returns true when something was dropped
local function push(list, item, max)
  list[#list + 1] = item
  if #list > max then table.remove(list, 1) return true end
  return false
end

LOG = {}
local function log(fmt, ...)
  local line = os.date("!%H:%M:%S") .. " " .. string.format(fmt, ...)
  print(line)
  push(LOG, line, K.log_ring)
end

-- control writes without a closure per call
local function assign(obj, field, value) obj[field] = value end
local function set(ctl, field, value)
  local ok, err = pcall(assign, ctl, field, value)
  if not ok then log("! control write %s failed: %s", field, tostring(err)) end
end
local function trigger(ctl)
  local ok, err = pcall(ctl.Trigger, ctl)
  if not ok then log("! trigger failed: %s", tostring(err)) end
end

-- ── FILES (JSON on the Core's media drive) ────────────────────────────────────
local function readFile(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local s = f:read("*a")
  f:close()
  return s
end

local function writeFile(path, s)
  pcall(dir.create, "media/_radio")
  local ok, err = pcall(function()
    local f = assert(io.open(path, "w"))
    f:write(s)
    f:close()
  end)
  if not ok then log("! save %s failed: %s", path, tostring(err)) end
end

-- A file that exists but does not decode is kept as <path>.bad, never overwritten.
local function readJson(path)
  if not json then return nil end
  local ok, raw = pcall(readFile, path)
  if not ok or not raw or raw == "" then return nil end
  local okDecode, data = pcall(json.decode, raw)
  if okDecode and type(data) == "table" then return data end
  log("! %s is unreadable; kept as %s.bad", path, path)
  writeFile(path .. ".bad", raw)
  return nil
end

local function writeJson(path, data)
  if not json then return end
  local ok, s = pcall(json.encode, data)
  if ok then writeFile(path, s) else log("! encode %s failed: %s", path, tostring(s)) end
end

-- A file written at most every `every` seconds (or on flush(true)).
Store = {}
Store.__index = Store
function Store.new(path, every, get) return setmetatable({ path = path, every = every, get = get, dirty = false, at = -1e9 }, Store) end
function Store:touch() self.dirty = true end
function Store:flush(force)
  if self.dirty and (force or now() - self.at >= self.every) then
    self.dirty, self.at = false, now()
    writeJson(self.path, self.get())
  end
end

-- ── EVENTS ────────────────────────────────────────────────────────────────────
EVENTS = {}          -- last 500 events: { boot, seq, t = utc, at, kind = ..., ... }
BOOT_ID, EVENT_SEQ, STATE_REV = "-", 0, 0
local META = { kind = true, t = true, at = true, boot = true, seq = true }
local function emit(kind, data)
  data = data or {}
  data.kind, data.t, data.at = kind, utc(), round(now(), 2)
  EVENT_SEQ = EVENT_SEQ + 1
  data.boot, data.seq = BOOT_ID, EVENT_SEQ   -- unique per event; the server dedupes on it
  STATE_REV = STATE_REV + 1                  -- anything worth an event changes the reported state
  push(EVENTS, data, 500)
  if Server then Server.queue(data) end
  local parts = {}
  for k, v in pairs(data) do
    if not META[k] then parts[#parts + 1] = k .. "=" .. tostring(v) end
  end
  table.sort(parts)
  log("%-14s %s", kind, table.concat(parts, "  "))
  if UI then UI.lastEvent = kind end
end

-- ── LEARNED PER-FILE DATA (persisted on the Core) ─────────────────────────────
LEARNED = {}
Learned = Store.new(FILES.learned, K.learned_save_s, function() return LEARNED end)

-- ── LIBRARY ───────────────────────────────────────────────────────────────────
Library = { music = {}, imaging = {}, byKey = {}, root = "Audio/" }

local function mediaPath(dirRel)
  return (("media/" .. Library.root .. "/" .. dirRel):gsub("/+", "/"))
end

local function parseName(name)
  local base = tostring(name):gsub("%.[%w]+$", "")
  local artist, title = base:match("^(.-)%s+%-%s+(.+)$")
  if artist and artist ~= "" then return artist, title end
  return nil, base
end

local function listAudio(dirRel, kind)
  local out = {}
  if dirRel == "" then return out end
  local ok, entries = pcall(dir.get, mediaPath(dirRel))
  if not ok or type(entries) ~= "table" then
    log("library: cannot list %s", mediaPath(dirRel))
    return out
  end
  table.sort(entries, function(a, b) return tostring(a.name) < tostring(b.name) end)
  for _, e in ipairs(entries) do
    local name = tostring(e.name)
    local ext = (name:match("%.([%w]+)$") or ""):lower()
    local key = dirRel .. name
    if e.type ~= "directory" and SUPPORTED_EXT[ext] and not (LEARNED[key] and LEARNED[key].unsupported) then
      local artist, title = parseName(name)
      out[#out + 1] = { key = key, dir = dirRel, name = name, size = tonumber(e.size) or 0,
                        kind = kind, artist = artist, title = title }
    end
  end
  return out
end

function Library.scan()
  Library.root = normDir(DeckA.c.root.String ~= "" and DeckA.c.root.String or "Audio/")
  Library.music = listAudio(SETTINGS.music_dir, "music")
  Library.imaging = listAudio(SETTINGS.imaging_dir, "imaging")
  Library.byKey = {}
  for _, list in ipairs({ Library.music, Library.imaging }) do
    for _, tr in ipairs(list) do Library.byKey[tr.key] = tr end
  end
  Station.refillBag()
  Server.libraryDirty = true
  STATE_REV = STATE_REV + 1
  if #Library.music > 0 and Station.emptyNoted then   -- music is back: play now, not after the back-off
    Station.emptyNoted = false
    if Station.automation and not (DeckA:isBusy() or DeckB:isBusy()) then Station.kick() end
  end
  log("library: %d music in %s, %d imaging in %s", #Library.music, mediaPath(SETTINGS.music_dir),
    #Library.imaging, mediaPath(SETTINGS.imaging_dir))
end

local function removeKey(list, key)
  for i = #list, 1, -1 do if list[i].key == key then table.remove(list, i) end end
end

function Library.remove(track)
  removeKey(track.kind == "imaging" and Library.imaging or Library.music, track.key)
  removeKey(Station.queue, track.key)
  removeKey(Station.bag, track.key)
  Library.byKey[track.key] = nil
end

-- ── DECK ──────────────────────────────────────────────────────────────────────
Deck = {}
Deck.__index = Deck
local DECK_CONTROLS = { "gain", "play", "stop", "playing", "filename", "filename.ui", "directory", "root",
                        "status", "remaining", "loop", "mute" }

function Deck.new(id, compName, meterIdx)
  local self = setmetatable({ id = id, state = "idle", gain = K.floor_db, meter = -120 }, Deck)
  local ok, comp = pcall(Component.New, compName)
  if not ok or not comp then error("Deck " .. id .. ": component '" .. compName .. "' not found") end
  self.c = {}
  for _, name in ipairs(DECK_CONTROLS) do
    local okCtl, ctl = pcall(function() return comp[name] end)
    if not okCtl or not ctl then error("Deck " .. id .. ": '" .. compName .. "' has no control '" .. name .. "' (Script Access on?)") end
    self.c[name] = ctl
  end
  self.peak = Meter["peak." .. meterIdx]
  if not self.peak then error("meter '" .. CFG.METER .. "' has no control peak." .. meterIdx) end
  self.c.playing.EventHandler = function(ctl)
    if not ctl.Boolean and (self.state == "playing" or self.state == "fading") and not self.expectStop then
      self:ended()
    end
  end
  return self
end

function Deck:isBusy() return self.state == "loading" or self.state == "playing" or self.state == "fading" end

function Deck:setGain(g)
  g = clamp(g, K.floor_db, 20)
  if math.abs(g - self.gain) >= 0.05 then set(self.c.gain, "Value", g) end
  self.gain = g
end

function Deck:stop()
  self.expectStop = true
  trigger(self.c.stop)
  self.state, self.fade, self.track, self.pending, self.loadCb = "idle", nil, nil, nil, nil
end

function Deck:load(track, cb)
  if self.state == "playing" or self.state == "fading" then
    local f = self.fade
    if f and f.onDone then f.onDone(self) else Station.finishTrack(self, "cut") end
  end
  self:stop()
  self.prevRem, self.prevStatus = self.c.remaining.Value, self.c.status.String
  if self.c.directory.String ~= track.dir then set(self.c.directory, "String", track.dir) end
  set(self.c.filename, "String", track.name)
  self.pending, self.loadCb, self.loadStart, self.state = track, cb, now(), "loading"
  Engine.wake()
end

function Deck:pollLoad(t)
  local pending, cb = self.pending, self.loadCb
  if not pending then self.state = "idle" return end
  local fnui, st, rem = self.c["filename.ui"].String, self.c.status.String, self.c.remaining.Value
  local waited = t - self.loadStart
  local loaded = fnui == pending.name and startsWith(st, "OK") and type(rem) == "number" and rem > 0
  local result
  if loaded and (rem ~= self.prevRem or st ~= self.prevStatus or waited > 0.4) then
    result = true
  elseif fnui == pending.name and st ~= "" and not startsWith(st, "OK") and st ~= self.prevStatus then
    result = st
  elseif waited > K.load_timeout_s then
    result = loaded or ("timeout (" .. st .. ")")
  end
  if result == nil then return end
  self.pending, self.loadCb = nil, nil
  if result == true then
    self.track, self.duration, self.state = pending, rem, "ready"
    if cb then cb(self, true) end
  else
    self.state = "idle"
    if cb then cb(self, false, result) end
  end
end

function Deck:play(gainDb, fadeInS)
  self.fade = nil
  if fadeInS and fadeInS > 0 then
    self:setGain(K.floor_db)
    self.fade = { from = K.floor_db, to = gainDb, t0 = now(), dur = fadeInS }
    Engine.wake()
  else
    self:setGain(gainDb)
  end
  self.expectStop = false
  trigger(self.c.play)
  self.state, self.playStart = "playing", now()
  self.firstAudioAt, self.lastAudioAt, self.silentSince, self.peakMax = nil, nil, nil, nil
end

function Deck:fadeOut(seconds, onDone)
  self.state = "fading"
  self.fade = { from = self.gain, to = K.floor_db, t0 = now(), dur = seconds, onDone = onDone }
  Engine.wake()
end

function Deck:stepFade(t)
  local f = self.fade
  local frac = (f.dur <= 0) and 1 or clamp((t - f.t0) / f.dur, 0, 1)
  self:setGain(f.from + (f.to - f.from) * frac)
  if frac >= 1 then
    self.fade = nil
    if f.onDone then f.onDone(self) end
  end
end

-- the player stopped by itself (end of file)
function Deck:ended()
  local f = self.fade
  self.fade = nil
  if f and f.onDone then f.onDone(self)      -- a fade-out was already handling this deck
  else self.state = "idle"; Station.onDeckEnded(self) end
end

-- sub-second remaining: duration minus wall time, resynced to the control if they drift
function Deck:remainingEst(t)
  if not self.duration or not self.playStart then return nil end
  local est = self.duration - (t - self.playStart)
  local ctl = self.c.remaining.Value
  if type(ctl) == "number" and math.abs(ctl - est) > 2.5 then
    est = ctl
    self.playStart = t - (self.duration - ctl)
  end
  return math.max(0, est)
end

-- one meter read per station tick, only during clean playback (no fade), so
-- learned lead-in / trail / peak are never skewed by the gain moving
function Deck:sample(t)
  if self.state ~= "playing" or self.fade then return end
  local m = self.peak.Value
  if type(m) ~= "number" then return end
  self.meter = m
  local unity = (m > -118) and (m - self.gain) or -200
  if unity > SETTINGS.silence_threshold_dbfs then
    self.lastAudioAt, self.firstAudioAt, self.silentSince = t, self.firstAudioAt or t, nil
    if unity > (self.peakMax or -200) then self.peakMax = unity end
  else
    self.silentSince = self.silentSince or t
  end
end

function Deck:describe(t)
  local tr = self.track or self.pending
  if self.state == "playing" or self.state == "fading" then
    return string.format("%s %s %s/%s", self.state, tr and (tr.title or tr.name) or "-",
      fmtClock(t - (self.playStart or t)), fmtClock(self.duration))
  end
  return self.state .. " " .. (tr and (tr.title or tr.name) or "-")
end

-- ── ENGINE: the 250 ms station timer + a 50 ms timer that runs only while needed ───
Engine = { fastOn = false }

function Engine.wake()
  if Engine.fastOn or not Fast then return end
  Engine.fastOn = true
  Fast:Start(K.fast_s)
end

function Engine.fast()
  local t, busy = now(), false
  for _, d in ipairs({ DeckA, DeckB }) do
    if d.fade then d:stepFade(t) end
    if d.state == "loading" then d:pollLoad(t) end
    busy = busy or d.fade ~= nil or d.state == "loading"
  end
  if not busy then Fast:Stop(); Engine.fastOn = false end
end

-- ── STATION ───────────────────────────────────────────────────────────────────
Station = {
  state = "idle",          -- idle | playing | crossfading | bumper | off
  live = nil, other = nil, bumperDeck = nil, freeDeck = nil,
  tracksSinceBumper = 0, history = {}, bag = {}, lastBumper = nil,
  skipRequested = false, bumperRequested = false, automation = true,
  nowPlaying = nil, idleSince = nil, ticks = 0,
  queue = {},              -- operator "play next" list (music), consulted before the bag
  stopAfter = false,       -- stop when the current track ends (no next track)
  nextBumper = nil,        -- operator-chosen bumper for the next bumper slot
  armed = nil,             -- the crossfade timer's target (deck + play start)
}

function Station.refillBag()
  local bag = {}
  for i, tr in ipairs(Library.music) do
    local j = math.random(i)
    bag[i] = bag[j]
    bag[j] = tr
  end
  Station.bag = bag
end

-- index in the bag of the track the rotation would play next
local function bagChoice()
  if #Station.bag == 0 then Station.refillBag() end
  local recent, h = {}, Station.history
  for i = math.max(1, #h - SETTINGS.artist_separation + 1), #h do
    if h[i].artist then recent[h[i].artist:lower()] = true end
  end
  local lastKey = h[#h] and h[#h].key
  for i = #Station.bag, 1, -1 do
    local tr = Station.bag[i]
    if tr.key ~= lastKey and not (tr.artist and recent[tr.artist:lower()]) then return i end
  end
  return #Station.bag                  -- nothing satisfies separation; take one anyway
end

function Station.peekNext()
  for _, q in ipairs(Station.queue) do if Library.byKey[q.key] then return q, true end end
  if #Library.music == 0 then return nil end
  return Station.bag[bagChoice()], false
end

function Station.pickNext()
  while #Station.queue > 0 do
    local q = table.remove(Station.queue, 1)
    if Library.byKey[q.key] then return q end
  end
  if #Library.music == 0 then return nil end
  return table.remove(Station.bag, bagChoice())
end

function Station.pickBumper()
  local b = Station.nextBumper
  Station.nextBumper = nil
  if not (b and Library.byKey[b.key]) then
    local n = #Library.imaging
    if n == 0 then return nil end
    repeat b = Library.imaging[math.random(n)] until n == 1 or b ~= Station.lastBumper
  end
  Station.lastBumper = b
  return b
end

-- requests only make sense while running: never carry them across a stop
local function clearRequests()
  Station.skipRequested, Station.bumperRequested, Station.nextBumper, Station.stopAfter = false, false, nil, false
end

function Station.finishTrack(d, reason)
  local tr = d.track
  if not tr then return end
  d.track = nil                        -- finish each play exactly once
  local played = d.playStart and round(now() - d.playStart, 1) or nil
  if tr.kind ~= "music" then
    emit("bumper_end", { deck = d.id, file = tr.name, dir = tr.dir, reason = reason, played = played })
    return
  end
  local L = LEARNED[tr.key] or {}
  L.duration = d.duration and round(d.duration, 2) or L.duration
  L.plays = (L.plays or 0) + 1
  if d.firstAudioAt and d.playStart then L.lead_in_s = round(d.firstAudioAt - d.playStart, 2) end
  if (reason == "end" or reason == "silence" or reason == "eof") and d.lastAudioAt and d.playStart and d.duration then
    local trail = round(math.max(0, d.duration - (d.lastAudioAt - d.playStart)), 2)
    L.trail_s = L.trail_s and math.min(L.trail_s, trail) or trail
  end
  if d.peakMax then L.peak_dbfs = round(d.peakMax, 1) end
  L.last_played = utc()
  LEARNED[tr.key] = L
  Learned:touch()
  emit("track_end", { deck = d.id, file = tr.name, dir = tr.dir, reason = reason, played = played,
    lead_in = L.lead_in_s, trail = L.trail_s, peak = L.peak_dbfs })
end

local function fadeAndFinish(d, seconds, reason)
  d:fadeOut(seconds, function(x) Station.finishTrack(x, reason); x:stop() end)
end

local function otherDeck(d) return d == DeckA and DeckB or DeckA end

function Station.startTrack(deck, track, opts, attempt)
  opts, attempt = opts or {}, attempt or 1
  if not track then
    Station.state = "idle"
    return
  end
  deck:load(track, function(d, ok, err)
    if not ok then
      local permanent = startsWith(err, "Unsupported")
      log("! load failed on deck %s: %s (%s)", d.id, track.name, tostring(err))
      if permanent then                -- the player can never play it: remember across boots
        LEARNED[track.key] = LEARNED[track.key] or {}
        LEARNED[track.key].unsupported, LEARNED[track.key].status = true, tostring(err)
        Learned:touch()
      end
      Library.remove(track)            -- otherwise skipped until the next scan
      emit(permanent and "file_unsupported" or "file_failed", { file = track.name, dir = track.dir, status = tostring(err) })
      if attempt < 5 then
        local nxt = (track.kind == "imaging") and Station.pickBumper() or nil
        Station.startTrack(deck, nxt or Station.pickNext(), opts, attempt + 1)
      else
        Station.state = "idle"
      end
      return
    end
    d:play(track.kind == "imaging" and SETTINGS.imaging_gain_db or SETTINGS.play_gain_db, opts.fadeIn or 0)
    if track.kind == "music" then
      Station.nowPlaying, Station.live, Station.other, Station.state = track, d, otherDeck(d), "playing"
      push(Station.history, track, 50)
      emit("track_start", { deck = d.id, file = track.name, dir = track.dir, artist = track.artist,
        title = track.title, duration = round(d.duration or 0, 1) })
    else
      Station.bumperDeck, Station.state = d, "bumper"
      emit("bumper_start", { deck = d.id, file = track.name, dir = track.dir, title = track.title,
        duration = round(d.duration or 0, 1) })
    end
  end)
end

-- start the next track on a free deck (boot, start, dead-air guard)
function Station.kick(why)
  local nxt = Station.pickNext()
  if not nxt then
    if not Station.emptyNoted then emit("library_empty", { music_dir = SETTINGS.music_dir }) end
    Station.emptyNoted, Station.state = true, "idle"
    return false
  end
  Station.emptyNoted = false
  if why then emit("dead_air_guard", { state = Station.state }) end
  Station.state = "crossfading"
  Station.startTrack(DeckA:isBusy() and DeckB or DeckA, nxt, {})
  return true
end

function Station.transition(reason)
  local live, other = Station.live, Station.other
  if not live or not other then return end
  Station.armed = nil
  local fadeS = (reason == "skip" and SETTINGS.skip_fade_s) or (reason == "silence" and SETTINGS.silence_fade_s)
                or SETTINGS.crossfade_s
  local outReason = (reason == "skip" or reason == "bumper") and "cut" or (reason == "silence" and "silence" or "end")
  if Station.stopAfter then            -- end of the line: fade out, start nothing
    emit("transition", { reason = reason, from = live.id, fade_s = fadeS, bumper = "-", stop = true })
    fadeAndFinish(live, fadeS, outReason)
    Station.goOff("stop_after_current")
    return
  end
  local bumperDue = Station.bumperRequested or
    (SETTINGS.bumper_every_n_tracks > 0 and Station.tracksSinceBumper >= SETTINGS.bumper_every_n_tracks)
  local bumper = bumperDue and Station.pickBumper() or nil
  Station.bumperRequested, Station.skipRequested = false, false
  emit("transition", { reason = reason, from = live.id, fade_s = fadeS, bumper = bumper and bumper.name or "-" })
  Station.state = "crossfading"
  if bumper then
    Station.tracksSinceBumper, Station.freeDeck = 0, live
    Station.startTrack(other, bumper, {})
    if reason == "end" then fadeS = SETTINGS.bumper_fade_s end   -- the song dips under the bumper
  else
    Station.tracksSinceBumper = Station.tracksSinceBumper + 1
    Station.startTrack(other, Station.pickNext(), { fadeIn = SETTINGS.fade_in_s })
  end
  fadeAndFinish(live, fadeS, outReason)
end

function Station.onDeckEnded(d)
  if Station.state == "playing" and d == Station.live then
    log("deck %s reached end of file before the crossfade fired", d.id)
    Station.finishTrack(d, "eof")
    if Station.stopAfter then Station.goOff("stop_after_current") return end
    Station.state = "crossfading"
    Station.startTrack(Station.other, Station.pickNext(), {})
  elseif Station.state == "bumper" and d == Station.bumperDeck then
    Station.finishTrack(d, "eof")
    if Station.stopAfter then Station.goOff("stop_after_current") return end
    Station.state = "crossfading"
    Station.startTrack(Station.freeDeck, Station.pickNext(), {})
  else
    Station.finishTrack(d, "end")      -- e.g. a bumper whose tail the next track started under
    emit("deck_idle", { deck = d.id })
  end
end

-- automation off without touching the decks (whatever is playing finishes)
function Station.goOff(why)
  clearRequests()
  Station.automation, Station.state, Station.armed = false, "off", nil
  UI.syncAutomation()
  Learned:flush(true)
  emit("automation", { on = false, why = why })
end

function Station.setAutomation(on, why)
  if on == Station.automation then return end
  clearRequests()
  if not on then
    for _, d in ipairs({ DeckA, DeckB }) do
      if d.state == "playing" or d.state == "fading" then fadeAndFinish(d, SETTINGS.stop_fade_s, "cut")
      elseif d.state == "loading" then d:stop() end
    end
    return Station.goOff(why or "control")
  end
  Station.automation, Station.state = true, "idle"
  UI.syncAutomation()
  emit("automation", { on = true, why = why or "control" })
  if not (DeckA:isBusy() or DeckB:isBusy()) then Station.kick() end
end

-- the crossfade point is checked every tick; inside the last tick it is armed
-- with a one-shot timer so the fade starts on time without a fast loop
local function crossfadeCheck(live, t)
  local L = (live.track and LEARNED[live.track.key]) or {}
  local due = (live:remainingEst(t) or 999) - (SETTINGS.crossfade_s + (L.trail_s or 0) + 0.25)
  if due <= 0 then return Station.transition("end") end
  if due < K.tick_s and Station.armed ~= live.playStart then
    Station.armed = live.playStart
    local start = live.playStart
    Timer.CallAfter(function()
      if Station.state == "playing" and Station.live == live and live.playStart == start and Station.armed == start then
        Station.transition("end")
      end
    end, due)
  end
end

function Station.tick(t)
  DeckA:sample(t)
  DeckB:sample(t)
  Station.ticks = Station.ticks + 1
  if Station.ticks % math.max(1, math.floor(1 / K.tick_s + 0.5)) == 0 then UI.refresh(t) end
  if not Station.automation then return end

  local live = Station.live
  if Station.state == "playing" and live and live.state == "playing" then
    local playedFor = t - live.playStart
    if Station.skipRequested and playedFor > 1 then
      Station.transition("skip")
    elseif Station.bumperRequested and playedFor > 1 then
      Station.transition("bumper")
    elseif playedFor > 5 and live.silentSince and (t - live.silentSince) >= SETTINGS.silence_hold_s
           and (live:remainingEst(t) or 999) <= SETTINGS.end_guard_s then
      Station.transition("silence")
    elseif playedFor > 5 then
      crossfadeCheck(live, t)
    end
  elseif Station.state == "bumper" and Station.bumperDeck and Station.bumperDeck.state == "playing" then
    if (Station.bumperDeck:remainingEst(t) or 0) <= SETTINGS.bumper_tail_s then
      local free = Station.freeDeck
      if free.state == "fading" then
        free.fade = nil
        Station.finishTrack(free, "end")
        free:stop()
      end
      if Station.stopAfter then return Station.goOff("stop_after_current") end   -- bumper plays out
      Station.state = "crossfading"
      Station.startTrack(free, Station.pickNext(), { fadeIn = SETTINGS.fade_in_s })
    end
  end

  -- dead-air guard (backs off while the library is empty)
  if DeckA:isBusy() or DeckB:isBusy() then
    Station.idleSince = nil
  else
    Station.idleSince = Station.idleSince or t
    if t - Station.idleSince >= (Station.emptyNoted and 30 or SETTINGS.dead_air_s) then
      Station.idleSince = nil
      if #Library.music == 0 and #Station.queue == 0 then Library.scan() end
      Station.kick(not Station.emptyNoted)
    end
  end
end

function Station.statusLine(t)
  local live = Station.live
  if Station.state == "off" then return "automation off" end
  if live and live.track and (live.state == "playing" or live.state == "fading") then
    return string.format("%s | %s | since bumper: %d/%d | bag: %d",
      Station.state, live:describe(t), Station.tracksSinceBumper, SETTINGS.bumper_every_n_tracks, #Station.bag)
  end
  return Station.state
end

-- ── COMMANDS (web panel via the server, and the optional panel controls) ──────
Control = {}

local function trackInfo(tr)
  if not tr then return nil end
  return { key = tr.key, file = tr.name, dir = tr.dir, kind = tr.kind, artist = tr.artist,
           title = tr.title, duration = LEARNED[tr.key] and LEARNED[tr.key].duration }
end

local function libraryTrack(args)
  local tr = Library.byKey[tostring(args.key or "")]
  if not tr then return nil, "not in the library: " .. tostring(args.key) end
  return tr
end

local function enqueue(tr, first)
  if #Station.queue >= 100 then return false, "queue is full (100)" end
  table.insert(Station.queue, first and 1 or #Station.queue + 1, tr)
  return true, "queued at " .. (first and 1 or #Station.queue)
end

-- the queue entry an operator meant: the index they saw if it still holds
-- that song, otherwise the first entry with that song (the page lags ~1 s)
local function queueIndex(args)
  local i = tonumber(args.index)
  local q = i and Station.queue[i]
  if q and (not args.key or q.key == args.key) then return i end
  for j, e in ipairs(Station.queue) do if args.key and e.key == args.key then return j end end
end

local function running()
  if not Station.automation then return false, "automation is off" end
  return true
end

Control.handlers = {
  skip = function()
    if not Station.automation then return false, "automation is off" end
    if Station.state ~= "playing" then return false, "nothing to skip right now (" .. Station.state .. ")" end
    Station.skipRequested = true
    return true
  end,
  bumper_now = function(args)
    if not Station.automation then return false, "automation is off" end
    if Station.state == "bumper" or Station.bumperRequested then return false, "a bumper is already playing or due" end
    if args.key then
      local tr, err = libraryTrack(args)
      if not tr then return false, err end
      if tr.kind ~= "imaging" then return false, "not an imaging file" end
      Station.nextBumper = tr
    elseif #Library.imaging == 0 then
      return false, "no imaging files"
    end
    Station.bumperRequested = true
    return true
  end,
  stop_after = function(args)
    if args.on == false then Station.stopAfter = false; return true, "cancelled" end
    local ok, err = running()
    if not ok then return false, err end
    Station.stopAfter = true
    return true, "will stop after the current track"
  end,
  stop_now = function()
    local ok, err = running()
    if not ok then return false, "already stopped" end
    Station.setAutomation(false, "remote")
    return true
  end,
  start = function()
    if Station.automation then return false, "already running" end
    Station.setAutomation(true, "remote")
    return true
  end,
  queue_add = function(args)
    local tr, err = libraryTrack(args)
    if not tr then return false, err end
    if tr.kind ~= "music" then return false, "use bumper_now for imaging" end
    return enqueue(tr, args.position == "next")
  end,
  play_now = function(args)
    local tr, err = libraryTrack(args)
    if not tr then return false, err end
    if tr.kind == "imaging" then
      if Station.state == "bumper" then return false, "a bumper is already playing" end
      Station.nextBumper, Station.bumperRequested = tr, true
    else
      local ok, why = enqueue(tr, true)
      if not ok then return false, why end
    end
    if not Station.automation then
      Station.setAutomation(true, "play_now")
    elseif Station.state == "playing" and tr.kind == "music" then
      Station.skipRequested = true
    end
    return true
  end,
  queue_remove = function(args)
    local i = queueIndex(args)
    if not i then return false, "not in the queue any more" end
    table.remove(Station.queue, i)
    return true
  end,
  queue_move = function(args)
    local i, to = queueIndex(args), tonumber(args.to)
    if not i or not to then return false, "not in the queue any more" end
    local asked = tonumber(args.index)   -- the page may be a moment out of date: keep the direction
    if asked and asked ~= i then to = i + (to - asked) end
    table.insert(Station.queue, clamp(to, 1, #Station.queue), table.remove(Station.queue, i))
    return true
  end,
  queue_clear = function()
    local n = #Station.queue
    Station.queue = {}
    return true, n .. " removed"
  end,
  rescan = function()
    Library.scan()
    return true, string.format("%d music, %d imaging", #Library.music, #Library.imaging)
  end,
}

function Control.run(c)
  local fn = Control.handlers[tostring(c.cmd)]
  local ok, res, note
  if fn then
    ok, res, note = pcall(fn, type(c.args) == "table" and c.args or {})
    if not ok then res, note = false, "error: " .. tostring(res) end
  else
    res, note = false, "unknown command"
  end
  emit("command", { id = c.id, cmd = tostring(c.cmd), ok = res and true or false, note = note, by = c.by })
  return res, note
end

function Station.snapshot(t)
  local function deck(d)
    local info = trackInfo(d.track or d.pending) or {}
    info.state, info.live = d.state, (d == Station.live) or nil
    if d.playStart and (d.state == "playing" or d.state == "fading") then
      info.elapsed = round(t - d.playStart, 1)
      local rem = d:remainingEst(t)
      info.remaining = rem and round(rem, 1)
    end
    info.duration = d.duration and round(d.duration, 1) or info.duration
    return info
  end
  local nxt, queued = Station.peekNext()
  local nextUp = trackInfo(nxt)
  if nextUp then nextUp.queued = queued end
  local queue, settings = {}, {}
  for i, q in ipairs(Station.queue) do queue[i] = trackInfo(q) end
  for k in pairs(DEFAULTS) do settings[k] = SETTINGS[k] end
  return {
    automation = Station.automation, state = Station.state, stop_after = Station.stopAfter,
    decks = { A = deck(DeckA), B = deck(DeckB) }, next_up = nextUp, queue = queue,
    next_bumper = trackInfo(Station.nextBumper), bumper_requested = Station.bumperRequested,
    tracks_since_bumper = Station.tracksSinceBumper, bumper_every = SETTINGS.bumper_every_n_tracks,
    library = { music = #Library.music, imaging = #Library.imaging },
    settings = settings, uptime = round(t - BOOT_T, 0), outbox = #Server.outbox,
  }
end

-- ── OPERATOR PANEL (optional controls) ────────────────────────────────────────
UI = { outputs = {}, shown = {}, lastEvent = "-" }

local function panelControl(name, index, list)
  local c = Controls and Controls[name]
  if not c and Controls and type(Controls[list]) == "table" then c = Controls[list][index] end
  return c
end

function UI.init()
  for i, name in ipairs({ "Status", "NowPlaying", "NextUp", "DeckA", "DeckB", "LastEvent" }) do
    UI.outputs[name] = panelControl(name, i, "Outputs")
  end
  local inputs = {
    Skip = function(ctl) if ctl.Boolean then Control.run({ cmd = "skip", by = "panel" }) end end,
    BumperNow = function(ctl) if ctl.Boolean then Control.run({ cmd = "bumper_now", by = "panel" }) end end,
    Automation = function(ctl) Control.run({ cmd = ctl.Boolean and "start" or "stop_now", by = "panel" }) end,
  }
  for i, name in ipairs({ "Skip", "BumperNow", "Automation" }) do
    local c = panelControl(name, i, "Inputs")
    if c then pcall(assign, c, "EventHandler", inputs[name]) end
    if name == "Automation" then UI.automationCtl = c end
  end
  UI.syncAutomation()
end

function UI.syncAutomation()
  if UI.automationCtl then pcall(assign, UI.automationCtl, "Boolean", Station.automation) end
end

local function show(name, s)
  local c = UI.outputs[name]
  if c and UI.shown[name] ~= s then
    UI.shown[name] = s
    pcall(assign, c, "String", s)
  end
end

function UI.refresh(t)
  if next(UI.outputs) == nil then return end
  local np, live = Station.nowPlaying, Station.live
  show("Status", Station.statusLine(t))
  show("NowPlaying", (np and live and live.playStart) and string.format("%s  %s / %s", label(np),
    fmtClock(t - live.playStart), fmtClock(live.duration)) or "-")
  show("NextUp", label((Station.peekNext())))
  show("DeckA", DeckA:describe(t))
  show("DeckB", DeckB:describe(t))
  show("LastEvent", UI.lastEvent)
end

-- ── SERVER: one sync request a second, plus the library after each scan ───────
Server = { outbox = {}, dropped = 0, lastCmd = 0, settingsRev = "", fails = 0, nextTry = 0,
           busy = {}, libraryDirty = false, sentRev = -1, stateAt = -1e9 }
Outbox = Store.new(FILES.outbox, K.outbox_save_s, function() return Server.outbox end)
CmdState = Store.new(FILES.state, 0, function() return { last_cmd = Server.lastCmd } end)

function Server.enabled() return type(CFG.SERVER_URL) == "string" and CFG.SERVER_URL ~= "" end

function Server.queue(ev)
  if not Server.enabled() then return end
  if push(Server.outbox, ev, K.outbox_max) then Server.dropped = Server.dropped + 1 end
  Outbox:touch()
end

local function stationName() return CFG.STATION_ID ~= "" and CFG.STATION_ID or STATION_NAME end

-- POST JSON on a named channel (one request in flight per channel; a request
-- whose callback never comes is forgotten after the timeout + 5 s)
local function post(channel, path, payload, onDone)
  local since = Server.busy[channel]
  if since and now() - since < K.http_timeout_s + 5 then return false end
  local okEnc, body = pcall(json.encode, payload)
  if not okEnc then log("! encode for %s failed: %s", path, tostring(body)) return false end
  local headers = { ["Content-Type"] = "application/json" }
  if CFG.SERVER_TOKEN ~= "" then headers["X-Radio-Token"] = CFG.SERVER_TOKEN end
  Server.busy[channel] = now()
  local ok, err = pcall(HttpClient.Upload, {
    Url = CFG.SERVER_URL:gsub("/+$", "") .. path, Method = "POST", Data = body, Headers = headers,
    Timeout = K.http_timeout_s,
    EventHandler = function(_, code, data, httpErr) Server.busy[channel] = nil; onDone(code, data, httpErr) end,
  })
  if not ok then Server.busy[channel] = nil; onDone(nil, nil, tostring(err)) end
  return true
end

function Server.applySettings(s, rev, fromFile)
  local new = {}
  for k, v in pairs(SETTINGS) do new[k] = DEFAULTS[k] ~= nil and DEFAULTS[k] or v end
  local n = 0
  for k, v in pairs(s) do
    if DEFAULTS[k] ~= nil and type(v) == type(DEFAULTS[k]) then new[k], n = v, n + 1 end
  end
  new.music_dir, new.imaging_dir = normDir(new.music_dir), normDir(new.imaging_dir)
  local rescan = new.music_dir ~= SETTINGS.music_dir or new.imaging_dir ~= SETTINGS.imaging_dir
  SETTINGS = new
  Server.settingsRev = tostring(rev or "")
  log("settings: %d server value(s) applied (rev %s)%s", n, Server.settingsRev, fromFile and " from file" or "")
  if fromFile then return end         -- boot: the library is scanned right after
  writeJson(FILES.settings, { rev = Server.settingsRev, settings = s })
  emit("settings_applied", { rev = Server.settingsRev, count = n })
  if rescan then Library.scan() end
end

function Server.postLibrary()
  local items = {}
  for _, list in ipairs({ Library.music, Library.imaging }) do
    for _, tr in ipairs(list) do
      local L = LEARNED[tr.key] or {}
      items[#items + 1] = { key = tr.key, dir = tr.dir, file = tr.name, kind = tr.kind, artist = tr.artist,
        title = tr.title, size = tr.size, duration = L.duration, lead_in = L.lead_in_s, trail = L.trail_s,
        peak = L.peak_dbfs }
    end
  end
  local sent = post("library", "/api/v1/core/library", { station = stationName(), boot = BOOT_ID,
    music_dir = SETTINGS.music_dir, imaging_dir = SETTINGS.imaging_dir, items = items }, function(code)
    if not (code and code >= 200 and code < 300) then Server.libraryDirty = true end
  end)
  if sent then Server.libraryDirty = false end
end

local function handleReply(resp)
  if tonumber(resp.ack_reset) then Server.lastCmd = tonumber(resp.ack_reset); CmdState:touch() end
  if type(resp.settings) == "table" then Server.applySettings(resp.settings, resp.settings_rev) end
  for _, c in ipairs(type(resp.commands) == "table" and resp.commands or {}) do
    local id = tonumber(c.id)
    if id and id > Server.lastCmd then
      Server.lastCmd = id
      CmdState:touch()
      Control.run(c)
    end
  end
  CmdState:flush()
end

function Server.sync()
  if not (Server.enabled() and json and HttpClient) or now() < Server.nextTry then return end
  if Server.libraryDirty and Server.fails == 0 then Server.postLibrary() end
  local t = now()
  local body = { station = stationName(), version = RADIO_VERSION, boot = BOOT_ID,
                 ack = Server.lastCmd, settings_rev = Server.settingsRev }
  local rev = STATE_REV
  if rev ~= Server.sentRev or t - Server.stateAt >= K.state_every_s then body.state = Station.snapshot(t) end
  local batch = {}
  for i = 1, math.min(K.sync_batch, #Server.outbox) do batch[i] = Server.outbox[i] end
  if #batch > 0 then body.events, body.dropped = batch, Server.dropped end
  post("sync", "/api/v1/core/sync", body, function(code, data, httpErr)
    local ok, resp = false, nil
    if code == 200 then ok, resp = pcall(json.decode, data or "") end
    if not (ok and type(resp) == "table") then
      Server.fails = Server.fails + 1
      Server.nextTry = now() + math.min(30, K.sync_s * 2 ^ math.min(Server.fails, 5))
      if Server.fails == 1 or Server.fails % 20 == 0 then
        local why = (code == 401 or code == 403) and "CFG.SERVER_TOKEN does not match the server's RADIO_API_TOKEN" or tostring(httpErr or "")
        log("! server: sync failed (%s %s); %d events queued", tostring(math.tointeger(code) or code), why, #Server.outbox)
      end
      return
    end
    if Server.fails > 0 then
      log("server: reachable again")
      if Server.fails >= 10 then Server.libraryDirty = true end   -- long outage: resend the library
    end
    Server.fails = 0
    if body.state then Server.sentRev, Server.stateAt = rev, t end
    if #batch > 0 then
      -- drop exactly the events that were sent (the head may have been trimmed meanwhile)
      local sent, keep = {}, {}
      for _, e in ipairs(batch) do sent[e] = true end
      for _, e in ipairs(Server.outbox) do if not sent[e] then keep[#keep + 1] = e end end
      Server.outbox, Server.dropped = keep, 0
      Outbox:touch()
    end
    handleReply(resp)
  end)
end

function Server.loadLocal()
  local st = readJson(FILES.state)
  if st and tonumber(st.last_cmd) then Server.lastCmd = tonumber(st.last_cmd) end
  local saved = readJson(FILES.settings)
  if saved and type(saved.settings) == "table" then Server.applySettings(saved.settings, saved.rev, true) end
  if Server.enabled() then
    local box = readJson(FILES.outbox)
    if box and #box > 0 then
      Server.outbox = box
      log("server: %d unsent events from the last run queued for replay", #box)
    end
  end
end

-- ── STARTUP ───────────────────────────────────────────────────────────────────
local function every(seconds, fn, name)
  local tm = Timer.New()
  tm.EventHandler = function()
    local ok, err = pcall(fn)
    if not ok then log("! %s error: %s", name, tostring(err)) end
  end
  tm:Start(seconds)
  return tm
end

local function boot()
  local st = {}
  pcall(function() st = Design.GetStatus() end)
  log("radio %s starting on %s (%s)", RADIO_VERSION, tostring(st.Platform), tostring(st.DesignName))
  if not json then log("! no json library: learning, settings and the server link are disabled") end
  local ok, m = pcall(Component.New, CFG.METER)
  if not ok or not m then error("meter component '" .. CFG.METER .. "' not found") end
  Meter = m
  DeckA = Deck.new("A", CFG.PLAYER_A, CFG.METER_A)
  DeckB = Deck.new("B", CFG.PLAYER_B, CFG.METER_B)
  for _, d in ipairs({ DeckA, DeckB }) do
    d:stop()
    set(d.c.mute, "Boolean", false)
    set(d.c.loop, "Boolean", false)
    set(d.c.gain, "Value", K.floor_db)
  end

  math.randomseed(os.time())
  BOOT_T = now()
  BOOT_ID = os.date("!%Y%m%dT%H%M%SZ") .. string.format("-%04x", math.random(0, 0xffff))
  STATION_NAME = tostring(st.DesignName or "core")
  LEARNED = readJson(FILES.learned) or {}
  Server.loadLocal()
  SETTINGS.music_dir, SETTINGS.imaging_dir = normDir(SETTINGS.music_dir), normDir(SETTINGS.imaging_dir)
  Library.scan()
  UI.init()

  Fast = Timer.New()                   -- started by Engine.wake() only while fading/loading
  Fast.EventHandler = function()
    local okFast, err = pcall(Engine.fast)
    if not okFast then log("! fast tick error: %s", tostring(err)) end
  end
  Tick = every(K.tick_s, function() Station.tick(now()) end, "tick")
  Sync = every(K.sync_s, Server.sync, "sync")
  Chores = every(K.chores_s, function()
    log("heartbeat: %s", Station.statusLine(now()))
    Learned:flush()
    Outbox:flush()
  end, "chores")
  emit("boot", { version = RADIO_VERSION, platform = tostring(st.Platform), design = STATION_NAME,
    server = Server.enabled() and CFG.SERVER_URL or "-" })

  if CFG.AUTOSTART then
    Station.kick()
  else
    Station.automation, Station.state = false, "off"
    UI.syncAutomation()
    log("AUTOSTART = false; waiting for the Automation control")
  end
end

boot()
