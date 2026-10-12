--[[---------------------------------------------------------------------------
  Q-SYS Radio Automation :: Deck Controller
  File:     qsys/radio/radio.lua
  Version:  0.2.0
  Requires: Q-SYS Core (tested on Core 8 Flex, build 1-2603002), a Text
            Controller, two Audio Players and a 2-channel meter with Script
            Access enabled. Player A -> meter.1, Player B -> meter.2.

  WHAT THIS IS
    A two-deck radio station that runs entirely on the Core. It scans a music
    folder and an imaging (bumper) folder, shuffles, crossfades, detects
    trailing silence, plays a bumper on the free deck every N tracks with the
    next track starting under its tail, learns per-file lead-in / trailing
    silence / peak level, and never leaves dead air. The server (0.3) will
    override SETTINGS and supply "what's next"; everything here keeps working
    without it.

  OPERATOR CONTROLS (optional)
    Either name controls on a Control Script component, or set the Text
    Controller's Input Count = 3 and Output Count = 6:
      Inputs  / named:  1 Skip   2 BumperNow   3 Automation (toggle)
      Outputs / named:  1 Status 2 NowPlaying  3 NextUp  4 DeckA  5 DeckB  6 LastEvent

  HOW TIMING WORKS (from probe 0.1.1)
    - Loads complete in ~0.1 s; the controller waits for filename.ui to echo
      the name and remaining to carry a fresh duration (status alone is not a
      reliable signal because it only fires on change).
    - progress/remaining tick in whole seconds; sub-second timing comes from
      Timer.Now() plus the duration read at load time.
    - The meter is post-gain, so silence is judged at unity: meter - gain.
    - Fades step gain every tick (50 ms), linear in dB.
-----------------------------------------------------------------------------]]

RADIO_VERSION = "0.2.0"

CFG = {
  PLAYER_A  = "Audio_Player_01", METER_A = 1,
  PLAYER_B  = "Audio_Player_02", METER_B = 2,
  METER     = "PlayerOutput01",
  AUTOSTART = true,
}

-- Defaults. The server will push overrides in 0.3; these keep the Core alive alone.
SETTINGS = {
  music_dir              = "Test/Tuning/",   -- relative to the player's root ("Audio/")
  imaging_dir            = "Test/Imaging/",  -- bumpers / stingers / ads; empty or missing = no bumpers
  play_gain_db           = 0,
  fade_floor_db          = -100,
  crossfade_s            = 4,      -- outgoing track fade when a track ends normally
  fade_in_s              = 0,      -- incoming track fade-in (0 = start at play gain)
  skip_fade_s            = 1.5,    -- outgoing fade on operator skip
  silence_fade_s         = 1.0,    -- outgoing fade when trailing silence is detected
  silence_threshold_dbfs = -50,    -- at unity gain (meter - gain)
  silence_hold_s         = 2.0,    -- how long below threshold counts as silence
  end_guard_s            = 20,     -- silence only counts when remaining is under this
  bumper_every_n_tracks  = 4,      -- 0 = never
  bumper_tail_s          = 1.0,    -- start the next track when the bumper has this much left
  artist_separation      = 2,      -- avoid repeating an artist within the last N tracks
  dead_air_s             = 1.5,    -- nothing playing this long -> start something
  tick_s                 = 0.05,
  load_timeout_s         = 1.5,
  learned_path           = "media/_radio/learned.json",
  heartbeat_s            = 30,
  log_ring               = 200,
}

SUPPORTED_EXT = { flac = true, mp3 = true, wav = true, aac = true, m4a = true, ogg = true, aif = true, aiff = true }

-------------------------------------------------------------------------------
-- UTILITIES
-------------------------------------------------------------------------------
local json = (function()
  local ok, j = pcall(require, "json")
  if ok and type(j) == "table" then return j end
  ok, j = pcall(require, "rapidjson")
  if ok and type(j) == "table" then return j end
  return nil
end)()

local function now() return Timer.Now() end
local function utc() return os.date("!%Y-%m-%dT%H:%M:%SZ") end
local function startsWith(s, p) return type(s) == "string" and s:sub(1, #p) == p end
local function round(v, places) local m = 10 ^ (places or 2); return math.floor(v * m + 0.5) / m end
local function fmtClock(sec)
  if type(sec) ~= "number" then return "-:--" end
  sec = math.max(0, math.floor(sec + 0.5))
  return string.format("%d:%02d", math.floor(sec / 60), sec % 60)
end
local function normDir(d)
  d = tostring(d or ""):gsub("^/+", "")
  if d ~= "" and d:sub(-1) ~= "/" then d = d .. "/" end
  return d
end

LOG = {}
local function log(fmt, ...)
  local line = os.date("!%H:%M:%S") .. " " .. string.format(fmt, ...)
  print(line)
  LOG[#LOG + 1] = line
  if #LOG > SETTINGS.log_ring then table.remove(LOG, 1) end
end

EVENTS = {}          -- for the server (0.3): { t = utc, kind = ..., ... }
local function emit(kind, data)
  data = data or {}
  data.kind = kind
  data.t = utc()
  data.at = round(now(), 2)      -- Core clock (Timer.Now), for ordering within a second
  EVENTS[#EVENTS + 1] = data
  if #EVENTS > 500 then table.remove(EVENTS, 1) end
  local parts = {}
  for k, v in pairs(data) do
    if k ~= "kind" and k ~= "t" and k ~= "at" then parts[#parts + 1] = k .. "=" .. tostring(v) end
  end
  table.sort(parts)
  log("%-14s %s", kind, table.concat(parts, "  "))
  if UI then UI.lastEvent = kind end
end

-- safe control access
local function cget(comp, name, field)
  local ok, v = pcall(function() return comp[name][field] end)
  if ok then return v end
  return nil
end
local function cset(comp, name, field, value)
  local ok, err = pcall(function() comp[name][field] = value end)
  if not ok then log("! set %s.%s failed: %s", tostring(name), tostring(field), tostring(err)) end
  return ok
end
local function ctrig(comp, name)
  local ok, err = pcall(function() comp[name]:Trigger() end)
  if not ok then log("! trigger %s failed: %s", tostring(name), tostring(err)) end
  return ok
end

-------------------------------------------------------------------------------
-- LEARNED PER-FILE DATA (persisted on the Core)
-------------------------------------------------------------------------------
LEARNED = {}

local function learnedLoad()
  if not json then return end
  local ok, data = pcall(function()
    local f = io.open(SETTINGS.learned_path, "r")
    if not f then return nil end
    local s = f:read("*a")
    f:close()
    return json.decode(s)
  end)
  if ok and type(data) == "table" then
    LEARNED = data
    local n = 0
    for _ in pairs(LEARNED) do n = n + 1 end
    log("learned: %d files from %s", n, SETTINGS.learned_path)
  end
end

local function learnedSave()
  if not json then return end
  pcall(function() dir.create("media/_radio") end)
  local ok, err = pcall(function()
    local f = assert(io.open(SETTINGS.learned_path, "w"))
    f:write(json.encode(LEARNED))
    f:close()
  end)
  if not ok then log("! learned save failed: %s", tostring(err)) end
end

-------------------------------------------------------------------------------
-- LIBRARY
-------------------------------------------------------------------------------
Library = { music = {}, imaging = {}, root = "Audio/" }

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
  dirRel = normDir(dirRel)
  if dirRel == "" then return out end
  local path = mediaPath(dirRel)
  local ok, entries = pcall(function() return dir.get(path) end)
  if not ok or type(entries) ~= "table" then
    log("library: cannot list %s", path)
    return out
  end
  table.sort(entries, function(a, b) return tostring(a.name) < tostring(b.name) end)
  for _, e in ipairs(entries) do
    if e.type ~= "directory" then
      local name = tostring(e.name)
      local ext = name:match("%.([%w]+)$")
      ext = ext and ext:lower()
      local key = dirRel .. name
      local known = LEARNED[key]
      if ext and SUPPORTED_EXT[ext] and not (known and known.unsupported) then
        local artist, title = parseName(name)
        out[#out + 1] = { key = key, dir = dirRel, name = name, size = tonumber(e.size) or 0,
                          kind = kind, artist = artist, title = title }
      end
    end
  end
  return out
end

function Library.scan()
  Library.root = normDir(cget(DeckA.comp, "root", "String") or "Audio/")
  Library.music = listAudio(SETTINGS.music_dir, "music")
  Library.imaging = listAudio(SETTINGS.imaging_dir, "imaging")
  log("library: %d music in %s, %d imaging in %s", #Library.music, mediaPath(normDir(SETTINGS.music_dir)),
    #Library.imaging, mediaPath(normDir(SETTINGS.imaging_dir)))
end

function Library.remove(track)
  local list = (track.kind == "imaging") and Library.imaging or Library.music
  for i = #list, 1, -1 do
    if list[i].key == track.key then table.remove(list, i) end
  end
end

-------------------------------------------------------------------------------
-- DECK
-------------------------------------------------------------------------------
Deck = {}
Deck.__index = Deck

function Deck.new(id, compName, meterIdx)
  local self = setmetatable({}, Deck)
  self.id = id
  self.name = compName
  self.meterIdx = meterIdx
  self.state = "idle"        -- idle | loading | ready | playing | fading
  self.track = nil           -- track table once loaded
  self.duration = nil
  self.gain = SETTINGS.fade_floor_db
  self.meter = -120
  self.unity = -200
  self.expectStop = false
  local ok, comp = pcall(Component.New, compName)
  if not ok then error("Deck " .. id .. ": component '" .. compName .. "' not found (" .. tostring(comp) .. ")") end
  self.comp = comp
  pcall(function()
    comp["playing"].EventHandler = function(ctl)
      if not ctl.Boolean and (self.state == "playing" or self.state == "fading") and not self.expectStop then
        self.state = "idle"
        Station.onDeckEnded(self, "eof")
      end
    end
  end)
  return self
end

function Deck:setGain(g)
  g = math.max(SETTINGS.fade_floor_db, math.min(20, g))
  cset(self.comp, "gain", "Value", g)
  self.gain = g
end

function Deck:isBusy()
  return self.state == "loading" or self.state == "playing" or self.state == "fading"
end

function Deck:load(track, cb)
  if self.state == "playing" or self.state == "fading" then
    Station.finishTrack(self, "cut")
    self:stop()
  end
  self.prevRem = cget(self.comp, "remaining", "Value")
  self.prevStatus = cget(self.comp, "status", "String") or ""
  local curDir = cget(self.comp, "directory", "String") or ""
  if curDir ~= track.dir then cset(self.comp, "directory", "String", track.dir) end
  cset(self.comp, "loop", "Boolean", false)
  cset(self.comp, "filename", "String", track.name)
  self.pending = track
  self.loadCb = cb
  self.loadStart = now()
  self.state = "loading"
end

function Deck:pollLoad(t)
  local pending = self.pending
  if not pending then self.state = "idle" return end
  local fnui = cget(self.comp, "filename.ui", "String") or ""
  local st = cget(self.comp, "status", "String") or ""
  local rem = cget(self.comp, "remaining", "Value")
  local elapsed = t - self.loadStart
  local cb = self.loadCb

  local function accept()
    self.track = pending
    self.duration = rem
    self.pending, self.loadCb = nil, nil
    self.state = "ready"
    if cb then cb(self, true) end
  end
  local function fail(err)
    self.pending, self.loadCb = nil, nil
    self.state = "idle"
    if cb then cb(self, false, err) end
  end

  if fnui == pending.name then
    if startsWith(st, "OK") and type(rem) == "number" and rem > 0
       and (rem ~= self.prevRem or st ~= self.prevStatus or elapsed > 0.4) then
      return accept()
    elseif st ~= "" and not startsWith(st, "OK") and st ~= self.prevStatus then
      return fail(st)
    end
  end
  if elapsed > SETTINGS.load_timeout_s then
    if fnui == pending.name and startsWith(st, "OK") and type(rem) == "number" and rem > 0 then
      return accept()
    end
    return fail("timeout (" .. st .. ")")
  end
end

function Deck:play(gainDb, fadeInS)
  if fadeInS and fadeInS > 0 then
    self:setGain(SETTINGS.fade_floor_db)
    self.fade = { from = SETTINGS.fade_floor_db, to = gainDb, t0 = now(), dur = fadeInS }
  else
    self:setGain(gainDb)
  end
  self.expectStop = false
  ctrig(self.comp, "play")
  self.state = "playing"
  self.playStart = now()
  self.firstAudioAt, self.lastAudioAt, self.silentSince, self.peakMax = nil, nil, nil, nil
end

function Deck:stop()
  self.expectStop = true
  ctrig(self.comp, "stop")
  self.state = "idle"
  self.fade = nil
  self.track = nil
end

function Deck:fadeOut(seconds, onDone)
  self.state = "fading"
  self.fade = { from = self.gain, to = SETTINGS.fade_floor_db, t0 = now(), dur = seconds, onDone = onDone }
end

-- sub-second remaining: duration minus wall time, resynced to the control if they drift
function Deck:remainingEst(t)
  if not self.duration or not self.playStart then return nil end
  local est = self.duration - (t - self.playStart)
  local ctl = cget(self.comp, "remaining", "Value")
  if type(ctl) == "number" and math.abs(ctl - est) > 2.5 then
    est = ctl
    self.playStart = t - (self.duration - ctl)
  end
  return math.max(0, est)
end

function Deck:tick(t)
  if self.fade then
    local f = self.fade
    local frac = (f.dur <= 0) and 1 or math.min(1, (t - f.t0) / f.dur)
    self:setGain(f.from + (f.to - f.from) * frac)
    if frac >= 1 then
      self.fade = nil
      if f.onDone then f.onDone(self) end
    end
  end
  if self.state == "loading" then self:pollLoad(t) end

  local m = cget(Meter, "peak." .. self.meterIdx, "Value")
  if type(m) == "number" then
    self.meter = m
    local unity = -200
    if m > -118 and self.gain > SETTINGS.fade_floor_db + 1 then unity = m - self.gain end
    self.unity = unity
    if self.state == "playing" or self.state == "fading" then
      if unity > SETTINGS.silence_threshold_dbfs then
        self.lastAudioAt = t
        self.firstAudioAt = self.firstAudioAt or t
        self.silentSince = nil
        if unity > (self.peakMax or -200) then self.peakMax = unity end
      else
        self.silentSince = self.silentSince or t
      end
    end
  end
end

function Deck:describe(t)
  local tr = self.track or self.pending
  local name = tr and (tr.title or tr.name) or "-"
  if self.state == "playing" or self.state == "fading" then
    local played = t - (self.playStart or t)
    return string.format("%s %s %s/%s g=%.0f m=%.0f", self.state, name, fmtClock(played), fmtClock(self.duration), self.gain, self.meter)
  end
  return string.format("%s %s", self.state, name)
end

-------------------------------------------------------------------------------
-- STATION
-------------------------------------------------------------------------------
Station = {
  state = "idle",          -- idle | playing | crossfading | bumper | off
  live = nil, other = nil, bumperDeck = nil, freeDeck = nil,
  tracksSinceBumper = 0, history = {}, bag = {}, lastBumper = nil,
  skipRequested = false, bumperRequested = false, automation = true,
  nowPlaying = nil, idleSince = nil, uiTick = 0,
}

local function shuffle(t)
  for i = #t, 2, -1 do
    local j = math.random(i)
    t[i], t[j] = t[j], t[i]
  end
end

function Station.refillBag()
  local bag = {}
  for _, tr in ipairs(Library.music) do bag[#bag + 1] = tr end
  shuffle(bag)
  Station.bag = bag
end

function Station.pickNext()
  if #Library.music == 0 then return nil end
  if #Station.bag == 0 then Station.refillBag() end
  local recentArtists = {}
  local h = Station.history
  for i = math.max(1, #h - SETTINGS.artist_separation + 1), #h do
    if h[i].artist then recentArtists[h[i].artist:lower()] = true end
  end
  local lastKey = h[#h] and h[#h].key
  for i = #Station.bag, 1, -1 do
    local tr = Station.bag[i]
    if tr.key ~= lastKey and not (tr.artist and recentArtists[tr.artist:lower()]) then
      return table.remove(Station.bag, i)
    end
  end
  return table.remove(Station.bag)     -- nothing satisfies separation; take one anyway
end

function Station.pickBumper()
  local n = #Library.imaging
  if n == 0 then return nil end
  if n == 1 then return Library.imaging[1] end
  local tr
  repeat tr = Library.imaging[math.random(n)] until tr ~= Station.lastBumper
  Station.lastBumper = tr
  return tr
end

function Station.finishTrack(d, reason)
  local tr = d.track
  if not tr then return end
  if tr.kind ~= "music" then
    emit("bumper_end", { deck = d.id, file = tr.name, reason = reason })
    return
  end
  local L = LEARNED[tr.key] or {}
  L.duration = d.duration and round(d.duration, 2) or L.duration
  L.plays = (L.plays or 0) + 1
  if d.firstAudioAt and d.playStart then L.lead_in_s = round(d.firstAudioAt - d.playStart, 2) end
  if (reason == "end" or reason == "silence" or reason == "eof") and d.lastAudioAt and d.playStart and d.duration then
    local trail = math.max(0, d.duration - (d.lastAudioAt - d.playStart))
    L.trail_s = L.trail_s and math.min(L.trail_s, round(trail, 2)) or round(trail, 2)
  end
  if d.peakMax then L.peak_dbfs = round(d.peakMax, 1) end
  L.last_played = utc()
  LEARNED[tr.key] = L
  learnedSave()
  emit("track_end", { deck = d.id, file = tr.name, reason = reason,
    lead_in = L.lead_in_s, trail = L.trail_s, peak = L.peak_dbfs })
end

function Station.startTrack(deck, track, opts, attempt)
  opts = opts or {}
  attempt = attempt or 1
  if not track then
    log("! nothing to play (library empty?)")
    Station.state = "idle"
    return
  end
  deck:load(track, function(d, ok, err)
    if not ok then
      log("! load failed on deck %s: %s (%s)", d.id, track.name, tostring(err))
      LEARNED[track.key] = LEARNED[track.key] or {}
      LEARNED[track.key].unsupported = true
      LEARNED[track.key].status = tostring(err)
      learnedSave()
      Library.remove(track)
      emit("file_unsupported", { file = track.name, status = tostring(err) })
      if attempt < 5 then
        local nxt = (track.kind == "imaging") and Station.pickBumper() or Station.pickNext()
        if track.kind == "imaging" and not nxt then
          -- no usable bumper: fall through to a plain track on this deck
          nxt = Station.pickNext()
        end
        Station.startTrack(deck, nxt, opts, attempt + 1)
      else
        Station.state = "idle"
      end
      return
    end
    d:play(SETTINGS.play_gain_db, opts.fadeIn or 0)
    if track.kind == "music" then
      Station.nowPlaying = track
      Station.history[#Station.history + 1] = track
      if #Station.history > 50 then table.remove(Station.history, 1) end
      Station.live = d
      Station.other = (d == DeckA) and DeckB or DeckA
      Station.state = "playing"
      emit("track_start", { deck = d.id, file = track.name, duration = round(d.duration or 0, 1) })
    else
      Station.bumperDeck = d
      Station.state = "bumper"
      emit("bumper_start", { deck = d.id, file = track.name, duration = round(d.duration or 0, 1) })
    end
  end)
end

function Station.transition(reason)
  local live, other = Station.live, Station.other
  if not live or not other then return end
  local fadeS = SETTINGS.crossfade_s
  if reason == "skip" then fadeS = SETTINGS.skip_fade_s
  elseif reason == "silence" then fadeS = SETTINGS.silence_fade_s end
  local bumperDue = Station.bumperRequested or
    (SETTINGS.bumper_every_n_tracks > 0 and Station.tracksSinceBumper >= SETTINGS.bumper_every_n_tracks)
  local bumper = bumperDue and Station.pickBumper() or nil
  Station.bumperRequested, Station.skipRequested = false, false
  emit("transition", { reason = reason, from = live.id, fade_s = fadeS, bumper = bumper and bumper.name or "-" })

  local endReason = (reason == "skip" or reason == "bumper") and "cut" or reason
  local function finishOutgoing(d)
    Station.finishTrack(d, endReason == "cut" and "cut" or (reason == "silence" and "silence" or "end"))
    d:stop()
  end

  if bumper then
    Station.tracksSinceBumper = 0
    Station.freeDeck = live
    Station.state = "crossfading"
    Station.startTrack(other, bumper, {})
  else
    Station.tracksSinceBumper = Station.tracksSinceBumper + 1
    Station.state = "crossfading"
    Station.startTrack(other, Station.pickNext(), { fadeIn = SETTINGS.fade_in_s })
  end
  live:fadeOut(fadeS, finishOutgoing)
end

function Station.onDeckEnded(d, reason)
  if Station.state == "playing" and d == Station.live then
    log("deck %s reached end of file before the crossfade fired", d.id)
    Station.finishTrack(d, "eof")
    d.track = nil
    Station.state = "crossfading"
    Station.startTrack(Station.other, Station.pickNext(), {})
  elseif Station.state == "bumper" and d == Station.bumperDeck then
    emit("bumper_end", { deck = d.id, reason = "eof" })
    d.track = nil
    Station.state = "crossfading"
    Station.startTrack(Station.freeDeck, Station.pickNext(), {})
  else
    d.track = nil
    emit("deck_idle", { deck = d.id })
  end
end

function Station.setAutomation(on)
  if on == Station.automation then return end
  Station.automation = on
  if on then
    log("automation ON")
    Station.state = "idle"
  else
    log("automation OFF: fading out")
    for _, d in ipairs({ DeckA, DeckB }) do
      if d.state == "playing" or d.state == "fading" then
        d:fadeOut(2, function(x) Station.finishTrack(x, "cut"); x:stop() end)
      elseif d.state == "loading" then
        d:stop()
      end
    end
    Station.state = "off"
  end
end

function Station.tick(t)
  DeckA:tick(t)
  DeckB:tick(t)
  if not Station.automation then return end

  local live = Station.live
  if Station.state == "playing" and live and live.state == "playing" then
    local rem = live:remainingEst(t) or 999
    local L = (live.track and LEARNED[live.track.key]) or {}
    local trail = L.trail_s or 0
    local playedFor = t - live.playStart
    if Station.skipRequested and playedFor > 1 then
      Station.transition("skip")
    elseif Station.bumperRequested and playedFor > 1 then
      Station.transition("bumper")
    elseif playedFor > 5 and rem <= SETTINGS.crossfade_s + trail + 0.25 then
      Station.transition("end")
    elseif playedFor > 5 and live.silentSince and (t - live.silentSince) >= SETTINGS.silence_hold_s
           and rem <= SETTINGS.end_guard_s then
      Station.transition("silence")
    end
  elseif Station.state == "bumper" and Station.bumperDeck and Station.bumperDeck.state == "playing" then
    local rem = Station.bumperDeck:remainingEst(t) or 0
    if rem <= SETTINGS.bumper_tail_s then
      local free = Station.freeDeck
      if free.state == "fading" then
        free.fade = nil
        Station.finishTrack(free, "end")
        free:stop()
      end
      Station.state = "crossfading"
      Station.startTrack(free, Station.pickNext(), { fadeIn = SETTINGS.fade_in_s })
    end
  end

  -- dead-air guard
  if DeckA:isBusy() or DeckB:isBusy() then
    Station.idleSince = nil
  else
    Station.idleSince = Station.idleSince or t
    if t - Station.idleSince >= SETTINGS.dead_air_s then
      Station.idleSince = nil
      emit("dead_air_guard", { state = Station.state })
      Station.state = "crossfading"
      Station.startTrack(DeckA, Station.pickNext(), {})
    end
  end

  Station.uiTick = Station.uiTick + 1
  if Station.uiTick % 5 == 0 then UI.refresh(t) end
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

-------------------------------------------------------------------------------
-- OPERATOR UI (optional controls)
-------------------------------------------------------------------------------
UI = { outputs = {}, lastEvent = "-" }

function UI.init()
  local outs = { { "Status", 1 }, { "NowPlaying", 2 }, { "NextUp", 3 }, { "DeckA", 4 }, { "DeckB", 5 }, { "LastEvent", 6 } }
  for _, m in ipairs(outs) do
    local c = Controls[m[1]] or (type(Controls.Outputs) == "table" and Controls.Outputs[m[2]]) or nil
    if c then UI.outputs[m[1]] = c end
  end
  local ins = {
    { "Skip",       1, function(ctl) if ctl.Boolean then Station.skipRequested = true; log("operator: skip") end end },
    { "BumperNow",  2, function(ctl) if ctl.Boolean then Station.bumperRequested = true; log("operator: bumper now") end end },
    { "Automation", 3, function(ctl) Station.setAutomation(ctl.Boolean) end },
  }
  local found = 0
  for _, m in ipairs(ins) do
    local c = Controls[m[1]] or (type(Controls.Inputs) == "table" and Controls.Inputs[m[2]]) or nil
    if c then
      found = found + 1
      pcall(function() c.EventHandler = m[3] end)
    end
  end
  local nOut = 0
  for _ in pairs(UI.outputs) do nOut = nOut + 1 end
  log("ui: %d input controls, %d output controls", found, nOut)
end

function UI.set(name, s)
  local c = UI.outputs[name]
  if c then pcall(function() c.String = s end) end
end

function UI.refresh(t)
  if next(UI.outputs) == nil then return end
  UI.set("Status", Station.statusLine(t))
  local np = Station.nowPlaying
  local live = Station.live
  if np and live and live.playStart then
    UI.set("NowPlaying", string.format("%s%s  %s / %s", np.artist and (np.artist .. " - ") or "", np.title,
      fmtClock(t - live.playStart), fmtClock(live.duration)))
  else
    UI.set("NowPlaying", "-")
  end
  local nxt = Station.bag[#Station.bag]
  UI.set("NextUp", nxt and ((nxt.artist and (nxt.artist .. " - ") or "") .. nxt.title) or "-")
  UI.set("DeckA", DeckA:describe(t))
  UI.set("DeckB", DeckB:describe(t))
  UI.set("LastEvent", UI.lastEvent)
end

-------------------------------------------------------------------------------
-- STARTUP
-------------------------------------------------------------------------------
local function boot()
  local st = {}
  pcall(function() st = Design.GetStatus() end)
  log("radio %s starting on %s (%s)", RADIO_VERSION, tostring(st.Platform), tostring(st.DesignName))
  if not json then log("! json library unavailable: learning will not persist") end

  DeckA = Deck.new("A", CFG.PLAYER_A, CFG.METER_A)
  DeckB = Deck.new("B", CFG.PLAYER_B, CFG.METER_B)
  local ok, m = pcall(Component.New, CFG.METER)
  if not ok then error("meter component '" .. CFG.METER .. "' not found") end
  Meter = m
  for _, d in ipairs({ DeckA, DeckB }) do
    d:stop()
    cset(d.comp, "mute", "Boolean", false)
    cset(d.comp, "loop", "Boolean", false)
    d:setGain(SETTINGS.fade_floor_db)
  end

  SETTINGS.music_dir = normDir(SETTINGS.music_dir)
  SETTINGS.imaging_dir = normDir(SETTINGS.imaging_dir)
  math.randomseed(os.time())
  learnedLoad()
  Library.scan()
  UI.init()

  Tick = Timer.New()
  Tick.EventHandler = function()
    local ok, err = pcall(Station.tick, now())
    if not ok then log("! tick error: %s", tostring(err)) end
  end
  Tick:Start(SETTINGS.tick_s)

  Heartbeat = Timer.New()
  Heartbeat.EventHandler = function() log("heartbeat: %s", Station.statusLine(now())) end
  Heartbeat:Start(SETTINGS.heartbeat_s)

  if CFG.AUTOSTART then
    Station.state = "crossfading"
    Station.startTrack(DeckA, Station.pickNext(), {})
  else
    Station.automation = false
    Station.state = "off"
    log("AUTOSTART = false; waiting for the Automation control")
  end
end

boot()
