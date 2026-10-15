-- QSYSRadio deck controller (Q-SYS Lua 5.3): every station on this Core (two Audio Players each) and its file
-- analysis players. Docs: README.md.
-- Station settings are edited on the server's web page and pushed here; SETTINGS below are only the
-- fallback until the server has been reached once.
RADIO_VERSION = "2.1.0"

CFG = {
  SERVER_URL = "",     -- e.g. "http://10.0.0.20:5410"; "" = run standalone
  SERVER_TOKEN = "",   -- RADIO_API_TOKEN from the server's .env
  AUTOSTART = true,
  CORE_ID = "",        -- "" = the design name; names this Core to the server
  STATIONS = {         -- id: unique, never ""; players: its two Audio Players; meter / meters: their peak + rms channels
    { id = "Main", players = { "Audio_Player23", "Audio_Player24" }, meter = "PlayerMeter", meters = { 1, 2 } },
  },
  ANALYZERS = {        -- file analysis: an Audio Player routed nowhere + a meter channel like the stations'; {} = none
    { player = "Analysis_Player", meter = "AnalysisMeter", meter_idx = 1 },
  },
}

-- each station holds its own copy, replaced by the server's values for that station
SETTINGS = {
  music_dir = "Test/Tuning/", imaging_dir = "Test/Imaging/", ads_dir = "",
  play_gain_db = 0, imaging_gain_db = 0, ad_gain_db = 0, ad_every_n_tracks = 0, ads_per_break = 2,
  crossfade_s = 4, fade_in_s = 0, skip_fade_s = 1.5, silence_fade_s = 1, stop_fade_s = 2, bumper_fade_s = 2,
  silence_threshold_dbfs = -50, silence_hold_s = 2, end_guard_s = 20, lead_skip_max_s = 3, lead_skip_keep_s = 0.3,
  fade_out_drop_db = 20, fade_out_early_s = 2,
  level_target_db = 0, level_raise_max_db = 6, level_cut_max_db = 20, level_peak_max_db = 18,
  bumper_every_n_tracks = 4, bumper_tail_s = 1, artist_separation = 2, dead_air_s = 1.5,
}

-- internals (not settings)
local K = {
  floor_db = -100, tick_s = 0.25, fast_s = 0.05, load_timeout_s = 1.5, chores_s = 30,
  quiet_s = 0.5, scan_head_s = 6, scan_body_s = 20, scan_tail_s = 15, scan_settle_s = 0.5, scan_spare_s = 30, level_min_s = 10,
  sync_s = 1, sync_batch = 100, state_every_s = 10, http_timeout_s = 5, library_chunk = 1000,
  outbox_max = 10000, outbox_big = 2000, outbox_big_s = 120, events_max = 500, history_max = 50, queue_max = 100, analyze_max = 100,
  fault_after = 5, retry_s = 30, retry_max_s = 300, healthy_s = 600,
}
-- only what must survive a reboot while the server is unreachable; everything else lives on the server
FILES = { outbox = "media/_radio/outbox.json", settings = "media/_radio/settings.json" }

SUPPORTED_EXT = { flac = true, mp3 = true, wav = true, aac = true, m4a = true, ogg = true, aif = true, aiff = true }
-- per kind: folder setting, level setting, event prefix
KINDS = {
  music = { dir = "music_dir", gain = "play_gain_db", event = "track" },
  imaging = { dir = "imaging_dir", gain = "imaging_gain_db", event = "bumper" },
  ad = { dir = "ads_dir", gain = "ad_gain_db", event = "ad" },
}
KIND_ORDER = { "music", "imaging", "ad" }

-- ── UTILITIES ─────────────────────────────────────────────────────────────────
-- RapidJSON first: Q-SYS recommends it for large data (no execution-count limit)
local json = (function()
  for _, name in ipairs({ "rapidjson", "json" }) do
    local ok, j = pcall(require, name)
    if ok and type(j) == "table" then return j end
  end
end)()

local NONE = {}      -- read-only "nothing" (no table built per tick)
local function now() return Timer.Now() end
local function utc() return os.date("!%Y-%m-%dT%H:%M:%SZ") end
local function startsWith(s, p) return type(s) == "string" and s:sub(1, #p) == p end
local function round(v, places) local m = 10 ^ (places or 2); return math.floor(v * m + 0.5) / m end
local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end
local function normDir(d)
  d = tostring(d or ""):gsub("^/+", "")
  if d ~= "" and d:sub(-1) ~= "/" then d = d .. "/" end
  return d
end
-- append to a bounded list; returns the oldest item when it had to go
local function push(list, item, max)
  list[#list + 1] = item
  if #list > max then return table.remove(list, 1) end
end
local function shuffled(list)               -- a shuffled copy
  local out = {}
  for i, v in ipairs(list) do
    local j = math.random(i)
    out[i] = out[j]
    out[j] = v
  end
  return out
end
local function memKb()                      -- the Lua memory in use, KB
  local ok, kb = pcall(collectgarbage, "count")
  return ok and math.floor(kb + 0.5) or nil
end

local function log(fmt, ...) print(os.date("!%H:%M:%S") .. " " .. string.format(fmt, ...)) end

-- fn(...) for one station in a pcall: an error is logged and returned, never raised (the other stations play on)
local function guard(st, what, fn, ...)
  local ok, err = pcall(fn, ...)
  if not ok then log("! %s: %s failed: %s", st.id, what, tostring(err)) end
  return ok, err
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
local function writeJson(path, data, raw)
  local ok, err = pcall(function()
    raw = raw or json.encode(data)
    pcall(dir.create, "media/_radio")
    local f = assert(io.open(path, "w"))
    f:write(raw)
    f:close()
  end)
  if not ok then log("! save %s failed: %s", path, tostring(err)) end
end

-- a file that exists but does not decode is kept as <path>.bad, never overwritten
local function readJson(path)
  local f = json and io.open(path, "r")
  if not f then return nil end
  local raw = f:read("*a")
  f:close()
  local ok, data = pcall(json.decode, raw)
  if ok and type(data) == "table" then return data end
  if raw ~= "" then
    log("! %s is unreadable; kept as %s.bad", path, path)
    writeJson(path .. ".bad", nil, raw)
  end
end

-- ── EVENTS ────────────────────────────────────────────────────────────────────
EVENTS = {}          -- last 500 events of every station: { station, boot, seq, t = utc, at, kind = ..., ... }
BOOT_ID, EVENT_SEQ = "-", 0
Radio = { stations = {}, byId = {}, decks = {}, players = {} }   -- the stations (CFG order), the arrays the timers walk, player owners
Server = {}
local function emit(st, kind, data)
  data = data or {}
  data.kind, data.station, data.t, data.at = kind, st.id, utc(), round(now(), 2)
  EVENT_SEQ = EVENT_SEQ + 1
  data.boot, data.seq = BOOT_ID, EVENT_SEQ   -- unique per event; the server dedupes on it
  st.rev = st.rev + 1                        -- anything worth an event changes the station's reported state
  push(EVENTS, data, K.events_max)
  Server.queue(data)
  log("%-14s %s %s", kind, st.id, data.file or data.cmd or data.reason or data.error or "")   -- the server keeps every field
end

-- ── LIBRARY ───────────────────────────────────────────────────────────────────
-- per station: lib = { music, imaging, ad, byKey, root }; learned[key] = { trail = s, lead = s, fixed = operator's,
-- level = average dB, peak = dB, unsupported = true }: the server's, + this boot's plays (levels at unity gain);
-- each scan keeps only the files it listed
-- [kind|dir|media path] = { [file name] = track }: one record per file, shared by every station that lists the folder
local SHARED = {}

local function parseName(name)
  local base = tostring(name):gsub("%.[%w]+$", "")
  local artist, title = base:match("^(.-)%s+%-%s+(.+)$")
  if artist and artist ~= "" then return artist, title end
  return nil, base
end

-- forget folder listings no station uses any more (Chores)
local function pruneShared()
  local used = {}
  for _, st in ipairs(Radio.stations) do for _, id in pairs(st.lib.folders) do used[id] = true end end
  for id in pairs(SHARED) do if not used[id] then SHARED[id] = nil end end
end

local function removeKey(list, key)
  for i = #list, 1, -1 do if list[i].key == key then table.remove(list, i) end end
end

-- a measurement's level figures for this boot: the first average (the server keeps the average of all), the highest peak
local function learnLevel(L, ev)
  L.level = L.level or ev.level
  if ev.peak then L.peak = math.max(L.peak or ev.peak, ev.peak) end
end

local function fallback()                   -- a fresh copy of SETTINGS (folders normalised)
  local s = {}
  for k, v in pairs(SETTINGS) do s[k] = v end
  for _, kind in ipairs(KIND_ORDER) do s[KINDS[kind].dir] = normDir(s[KINDS[kind].dir]) end
  return s
end

-- ── DECK ──────────────────────────────────────────────────────────────────────
Deck = {}
Deck.__index = Deck
local DECK_CONTROLS = { "gain", "play", "stop", "playing", "locate", "filename", "filename.ui", "directory", "root",
                        "status", "remaining", "loop", "mute" }

local function control(comp, name)
  local ok, ctl = pcall(function() return comp[name] end)
  return ok and ctl or nil
end

-- st: its owner (its id names it in the log; onDeckEnded): a station, or the Analyzer for an analysis player
function Deck.new(st, id, player, meter, meterName, meterIdx)
  local self = setmetatable({ st = st, id = id, state = "idle", gain = K.floor_db, pow = 0, powN = 0, shownPeak = -120 }, Deck)
  local ok, comp = pcall(Component.New, player)
  if not ok or not comp then error("player '" .. tostring(player) .. "' not found", 0) end
  self.c = {}
  for _, name in ipairs(DECK_CONTROLS) do
    self.c[name] = control(comp, name)
    if not self.c[name] then error("player '" .. tostring(player) .. "' has no control '" .. name .. "' (Script Access on?)", 0) end
  end
  self.peak = control(meter, "peak." .. tostring(meterIdx))
  if not self.peak then error("meter '" .. meterName .. "' has no control peak." .. tostring(meterIdx), 0) end
  self.rms = control(meter, "rms." .. tostring(meterIdx))
  if not self.rms then log("! %s: meter '%s' has no control rms.%s: average levels are not measured", st.id, meterName, tostring(meterIdx)) end
  self.c.playing.EventHandler = function(ctl) guard(st, "player", Deck.onPlaying, self, ctl) end
  self:stop()
  set(self.c.mute, "Boolean", false)
  set(self.c.loop, "Boolean", false)
  set(self.c.gain, "Value", K.floor_db)
  return self
end

function Deck:onPlaying(ctl)
  if not ctl.Boolean and (self.state == "playing" or self.state == "fading") and not self.expectStop then self:ended() end
end

-- the folder its files are found under: its root, Audio/ when blank
function Deck:root()
  local root = self.c.root.String
  return normDir(root ~= "" and root or "Audio/")
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
  self.state, self.fade, self.track, self.pending, self.loadCb, self.trim = "idle", nil, nil, nil, nil, nil
end

function Deck:load(track, cb)
  if self.state == "playing" or self.state == "fading" then
    local f = self.fade
    if f and f.onDone then f.onDone(self) else self.st:finishTrack(self, "cut") end
  end
  self:stop()
  self.prevRem, self.prevStatus, self.prevName = self.c.remaining.Value, self.c.status.String, self.c["filename.ui"].String
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
    -- a second unsupported file in a row leaves the status string as it was: the echo of the new name says it was refused
    result = loaded or (fnui == pending.name and fnui ~= self.prevName and startsWith(st, "Unsupported") and st) or ("timeout (" .. st .. ")")
  end
  if result == nil then return end
  self.pending, self.loadCb = nil, nil
  if result == true then
    self.track, self.duration, self.state = pending, rem, "ready"
    pending.duration = round(rem, 1)
    if cb then cb(self, true) end
  else
    self.state = "idle"
    if cb then cb(self, false, result) end
  end
end

-- skip = seconds of quiet start to jump over (seek right after play: an early seek only plays silence)
function Deck:play(gainDb, fadeInS, skip)
  self.fade, self.playGain = nil, gainDb
  if fadeInS and fadeInS > 0 then
    self:setGain(K.floor_db)
    self.fade = { from = K.floor_db, to = gainDb, t0 = now(), dur = fadeInS }
    Engine.wake()
  else
    self:setGain(gainDb)
  end
  self.expectStop = false
  trigger(self.c.play)
  if skip > 0 then set(self.c.locate, "Value", skip) end
  self.state, self.playStart, self.fadedIn = "playing", now() - skip, fadeInS and fadeInS > 0   -- times stay file positions
  self.firstAudioAt, self.lastAudioAt, self.silentSince, self.peakMax, self.loudAt, self.fadedAt = nil, nil, nil, nil, nil, nil
  self.pow, self.powN = 0, 0
end

function Deck:fadeOut(seconds, onDone)
  self.state, self.fadedAt = "fading", self.fadedAt or now()
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
  else self.state = "idle"; self.st:onDeckEnded(self) end
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

-- one meter read per tick (S: the settings it is measured by); it is learned from only during clean playback
-- (no fade), so learned lead-in / trail / peak / average level are never skewed by the gain moving
function Deck:sample(t, S)
  local m = self.peak.Value
  if type(m) ~= "number" then return end
  if m > self.shownPeak then self.shownPeak = m end    -- the panel's readout: the loudest since the last sync
  if self.state ~= "playing" or self.fade or t < (self.settleUntil or 0) then return end
  local unity = (m > -118) and (m - self.gain) or -200
  if unity > S.silence_threshold_dbfs then
    self.lastAudioAt, self.firstAudioAt, self.silentSince = t, self.firstAudioAt or t, nil
    if unity > (self.peakMax or -200) then self.peakMax = unity end
    if unity > self.peakMax - S.fade_out_drop_db then self.loudAt = t end
    local r = self.rms and self.rms.Value             -- the average level counts sound only (energy average)
    if type(r) == "number" and r > -118 then self.pow, self.powN = self.pow + 10 ^ ((r - self.gain) / 10), self.powN + 1 end
  else
    self.silentSince = self.silentSince or t
  end
end

-- the average level (dB at unity gain) and seconds measured since play, once there is enough to go on
function Deck:level()
  local secs = self.powN * K.tick_s
  if secs <= 0 or secs < math.min(K.level_min_s, (self.duration or 0) / 2) then return nil end
  return round(10 * math.log(self.pow / self.powN, 10), 1), round(secs, 1)
end

-- in its fade-out: fade_out_drop_db under its own peak for a moment (start the next item early)
function Deck:fadingOut(t)
  return self.st.settings.fade_out_early_s > 0 and self.loudAt ~= nil and t - self.loudAt >= K.quiet_s
end

-- ── ENGINE: the 250 ms timer + a 50 ms timer that runs only while a deck fades or loads ───
Engine = { fastOn = false }

function Engine.wake()
  if Engine.fastOn or not Fast then return end
  Engine.fastOn = true
  Fast:Start(K.fast_s)
end

local function fastStep(d, t)
  if d.fade then d:stepFade(t) end
  if d.state == "loading" then d:pollLoad(t) end
end

function Engine.fast()
  local t, busy, decks = now(), false, Radio.decks
  for i = 1, #decks do
    local d = decks[i]
    if d.fade or d.state == "loading" then
      if not guard(d.st, "fade / load", fastStep, d, t) then   -- never the same error every 50 ms
        d.fade = nil
        if d.state == "loading" then d:stop() end
      end
      busy = busy or d.fade ~= nil or d.state == "loading"
    end
  end
  if not busy then Fast:Stop(); Engine.fastOn = false end
end

-- ── STATION ───────────────────────────────────────────────────────────────────
Station = {}
Station.__index = Station

-- prev: the faulted station this one replaces (its settings, command ack and back-off carry over);
-- saved: its settings from settings.json (boot)
function Station.new(cfg, prev, saved)
  local st = setmetatable({
    cfg = cfg, id = cfg.id, decks = {}, rev = prev and prev.rev or 0, sentRev = -1, stateAt = -1e9,
    lib = { music = {}, imaging = {}, ad = {}, byKey = {}, root = "Audio/", folders = {}, gen = 0 },
    learned = {}, settings = fallback(), settingsRev = "", adsRev = "", lastCmd = 0, dropped = 0, replyFails = 0, libraryDirty = false,
    state = "idle",          -- idle | playing | crossfading | bumper | ad | off | fault
    automation = false, live = nil, other = nil, spotDeck = nil, freeDeck = nil,
    tracksSinceBumper = 0, tracksSinceAd = 0,   -- songs started since the last bumper / ad
    history = {}, bag = {}, lastBumper = nil, lastAds = {},
    skipRequested = false, bumperRequested = false, adRequested = false, idleSince = nil,
    queue = {},              -- operator "play next" list (music), consulted before the bag
    stopAfter = false,       -- stop when the current track ends (no next track)
    nextBumper = nil, nextAd = nil,   -- operator-chosen bumper / ad for the next slot
    breakList = {},          -- the rest of the break that is playing (ads, then a bumper)
    adPlan = nil,            -- from the server: { due, mix (ads), next = song, why, ident = ID owed, no_bumpers }
    armed = nil,             -- the crossfade timer's target (deck + play start)
    fails = 0, backoff = prev and prev.backoff or K.retry_s, builtAt = now(),
  }, Station)
  if prev then
    st.settings, st.saved, st.settingsRev, st.lastCmd, st.dropped, st.wantOn =
      prev.settings, prev.saved, prev.settingsRev, prev.lastCmd, prev.dropped, prev.wantOn
  else
    st.wantOn = CFG.AUTOSTART ~= false
    if saved then st:applySettings(saved.settings, saved.rev, true) end
  end
  local ok, err = pcall(Station.build, st)
  if ok then st.built = true else st:faultNow(err) end
  return st
end

-- its players and meter from CFG; then the first library scan
function Station:build()
  local cfg = self.cfg
  local players, meters = cfg.players, type(cfg.meters) == "table" and cfg.meters or { 1, 2 }
  if type(players) ~= "table" or #players ~= 2 or players[1] == players[2] then error("players must name two Audio Players", 0) end
  for _, p in ipairs(players) do                 -- a player belongs to one station (its handlers are that station's)
    local owner = Radio.players[p]
    if owner and owner ~= self.id then error("player '" .. tostring(p) .. "' is station " .. owner .. "'s", 0) end
  end
  local ok, meter = pcall(Component.New, cfg.meter)
  if not ok or not meter then error("meter '" .. tostring(cfg.meter) .. "' not found", 0) end
  self.decks[1] = Deck.new(self, "A", players[1], meter, tostring(cfg.meter), meters[1])
  self.decks[2] = Deck.new(self, "B", players[2], meter, tostring(cfg.meter), meters[2])
  Radio.players[players[1]], Radio.players[players[2]] = self.id, self.id
  self:scan()
end

-- automation on (play now) or off, at boot and after a rebuild
function Station:start(on)
  if on then
    self.automation, self.state = true, "idle"
    self:kick()
  else
    self.automation, self.state = false, "off"
    log("%s: automation off; waiting for Start on the web panel", self.id)
  end
end

-- a tick failed: K.fault_after in a row and the station is faulted
function Station:failed(err)
  self.fails = self.fails + 1
  if self.fails >= K.fault_after then self:faultNow("tick: " .. tostring(err)) end
end

-- stop its decks, report it, and rebuild it from its CFG entry after a back-off (Radio.tick); the others play on
function Station:faultNow(reason)
  reason = tostring(reason):sub(1, 300)
  if self.built and not self.fault then self.wantOn = self.automation end
  self.fault, self.retryAt = reason, now() + self.backoff
  self.backoff = math.min(self.backoff * 2, K.retry_max_s)
  for _, d in ipairs(self.decks) do
    if d.track then pcall(self.finishTrack, self, d, "fault") end   -- a play cut short
    pcall(Deck.stop, d)
  end
  self.automation, self.state, self.live, self.other, self.spotDeck, self.freeDeck, self.armed = false, "fault", nil, nil, nil, nil, nil
  emit(self, "station_fault", { error = reason, retry_s = round(self.retryAt - now(), 0) })
  Radio.refresh()
end

function Station:mediaPath(dirRel)
  return (("media/" .. self.lib.root .. "/" .. dirRel):gsub("/+", "/"))
end

-- its files of one kind in dirRel; the records are shared with every station that lists the same folder.
-- learned: gets the figures of each file listed (unsupported ones too)
function Station:listAudio(dirRel, kind, learned)
  local out, lib = {}, self.lib
  lib.folders[kind] = nil
  if dirRel == "" then return out end
  local path = self:mediaPath(dirRel)
  local ok, entries = pcall(dir.get, path)
  if not ok or type(entries) ~= "table" then
    log("%s: library: cannot list %s", self.id, path)
    return out
  end
  table.sort(entries, function(a, b) return tostring(a.name) < tostring(b.name) end)
  local id = kind .. "|" .. dirRel .. "|" .. path
  local old, new = SHARED[id] or NONE, {}
  for _, e in ipairs(entries) do
    local name = tostring(e.name)
    local ext = (name:match("%.([%w]+)$") or ""):lower()
    if e.type ~= "directory" and SUPPORTED_EXT[ext] then
      local size, tr = tonumber(e.size) or 0, old[name]
      if not (tr and tr.size == size) then
        local artist, title = parseName(name)
        tr = { key = dirRel .. name, dir = dirRel, name = name, size = size, kind = kind, artist = artist, title = title }
      end
      new[name] = tr
      local L = self.learned[tr.key]
      learned[tr.key] = L
      if not (L and L.unsupported) then out[#out + 1] = tr end
    end
  end
  SHARED[id], lib.folders[kind] = new, id
  return out
end

function Station:scan()
  local lib, S = self.lib, self.settings
  lib.root = self.decks[1]:root()
  local byKey, learned = {}, {}
  for _, kind in ipairs(KIND_ORDER) do
    lib[kind] = self:listAudio(S[KINDS[kind].dir], kind, learned)
    for _, tr in ipairs(lib[kind]) do byKey[tr.key] = tr end
  end
  lib.byKey, lib.gen = byKey, lib.gen + 1
  self.learned = learned               -- figures only for the files listed now: nothing grows with uptime
  local keep = {}                      -- keep the shuffle cycle going (new files join at the next refill)
  for _, tr in ipairs(self.bag) do keep[#keep + 1] = byKey[tr.key] end
  self.bag, self.libraryDirty, self.rev = keep, true, self.rev + 1
  if #lib.music > 0 and self.emptyNoted then   -- music is back: play now, not after the back-off
    self.emptyNoted = false
    if self.automation and not self:busy() then self:kick() end
  end
  log("%s: library: %d music in %s, %d imaging in %s, %d ads in %s", self.id, #lib.music, self:mediaPath(S.music_dir),
    #lib.imaging, self:mediaPath(S.imaging_dir), #lib.ad, self:mediaPath(S.ads_dir))
end

function Station:removeTrack(track)
  removeKey(self.lib[track.kind], track.key)
  removeKey(self.queue, track.key)
  removeKey(self.bag, track.key)
  self.lib.byKey[track.key] = nil
end

function Station:figures(key)            -- its figures for a file (made when it has none yet)
  local L = self.learned[key] or {}
  self.learned[key] = L
  return L
end

-- the server's per-file figures (reply to the library upload): they replace this boot's (edits, forgotten figures)
function Station:learn(map)
  if type(map) ~= "table" then return end
  for _, L in pairs(self.learned) do L.trail, L.lead, L.fixed, L.analyzed, L.level, L.peak = nil, nil, nil, nil, nil, nil end
  for key, v in pairs(map) do
    if type(v) == "table" then
      local L = self:figures(key)
      L.trail, L.lead, L.fixed, L.analyzed = tonumber(v.trail), tonumber(v.lead), v.fixed == true or nil, v.analyzed == true or nil
      L.level, L.peak = tonumber(v.level), tonumber(v.peak)
      if v.unsupported == true then L.unsupported = true end
      if L.unsupported and self.lib.byKey[key] then self:removeTrack(self.lib.byKey[key]) end
    end
  end
end

-- level matching: the gain that brings a file's average to level_target_db, within the raise / cut
-- limits; a raise never lifts its peak past level_peak_max_db. Fades start and end at the result
function Station:levelTrim(L)
  if not L.level then return 0 end
  local S = self.settings
  local g = clamp(S.level_target_db - L.level, -S.level_cut_max_db, S.level_raise_max_db)
  if g > 0 and L.peak then g = clamp(S.level_peak_max_db - L.peak, 0, g) end
  return round(g, 1)
end

function Station:busy() return self.decks[1]:isBusy() or self.decks[2]:isBusy() end
function Station:otherDeck(d) return d == self.decks[1] and self.decks[2] or self.decks[1] end
local function isSpot(state) return state == "bumper" or state == "ad" end

function Station:refillBag() self.bag = shuffled(self.lib.music) end

-- index in the bag of the track the rotation would play next
function Station:bagChoice()
  if #self.bag == 0 then self:refillBag() end
  local recent, h, bag = {}, self.history, self.bag
  for i = math.max(1, #h - self.settings.artist_separation + 1), #h do
    if h[i].artist then recent[h[i].artist:lower()] = true end
  end
  local lastKey = h[#h] and h[#h].key
  for i = #bag, 1, -1 do
    local tr = bag[i]
    if tr.key ~= lastKey and not (tr.artist and recent[tr.artist:lower()]) then return i end
  end
  return #bag                          -- nothing satisfies separation; take one anyway
end

-- the next song (operator queue, then the server's pick, then the local shuffle bag),
-- whether it was queued, and why the server picked it; take = remove it
function Station:nextSong(take)
  local q, plan, byKey = self.queue, self.adPlan, self.lib.byKey
  while q[1] and not byKey[q[1].key] do table.remove(q, 1) end
  if q[1] then return take and table.remove(q, 1) or q[1], true end
  local pick, last = plan and byKey[plan.next or ""], self.history[#self.history]
  if pick and pick.kind == "music" and pick ~= last then   -- a stale plan may still name the song just played
    if take then plan.next, self.adsRev = nil, ""; removeKey(self.bag, pick.key) end
    return pick, false, plan.why
  end
  if #self.lib.music == 0 then return nil end
  local i = self:bagChoice()
  return take and table.remove(self.bag, i) or self.bag[i], false
end

function Station:pickBumper()
  local b, imaging = self.nextBumper, self.lib.imaging
  self.nextBumper = nil
  if not (b and self.lib.byKey[b.key]) then
    local n = #imaging
    if n == 0 then return nil end
    repeat b = imaging[math.random(n)] until n == 1 or b ~= self.lastBumper
  end
  self.lastBumper = b
  return b
end

function Station:adPool()
  if not self.adPlan then return self.lib.ad end
  local pool = {}
  for _, k in ipairs(self.adPlan.mix) do pool[#pool + 1] = self.lib.byKey[k] end
  return pool
end

-- the ads for a break due now, or nil: the operator's pick, then ads owed this
-- hour, then (at a regular break) mix-ins, up to ads_per_break
function Station:adBreak()
  local list, seen, S, byKey = {}, {}, self.settings, self.lib.byKey
  local function add(tr)
    if tr and tr.kind == "ad" and byKey[tr.key] and not seen[tr.key] and #list < S.ads_per_break then
      seen[tr.key] = true
      list[#list + 1] = tr
    end
  end
  add(self.nextAd)
  local due = self.adPlan and self.adPlan.due or {}
  for _, k in ipairs(due) do add(byKey[k]) end
  if self.adRequested or (S.ad_every_n_tracks > 0 and self.tracksSinceAd >= S.ad_every_n_tracks) then
    local pool = shuffled(self:adPool())
    for pass = 1, 2 do                 -- the last break's ads only when there is nothing else
      for _, tr in ipairs(pool) do if pass == 2 or not self.lastAds[tr.key] then add(tr) end end
    end
  end
  self.nextAd, self.adRequested = nil, false
  if #list == 0 then return nil end
  self.lastAds = seen
  if self.adPlan then                  -- played ads leave the plan; ask the server for a fresh one
    local keep = {}
    for _, k in ipairs(due) do if not seen[k] then keep[#keep + 1] = k end end
    self.adPlan.due, self.adsRev = keep, ""
  end
  return list
end

-- what plays between this song and the next: the server's station ID (if owed), ads (if due),
-- then a bumper (if due, no ID and bumpers not held)
function Station:planBreak()
  local plan, S = self.adPlan or {}, self.settings
  local spots, ident = self:adBreak() or {}, self.lib.byKey[plan.ident or ""]
  if ident and ident.kind == "imaging" then
    table.insert(spots, 1, ident)
    plan.ident, self.adsRev = nil, ""
  elseif self.bumperRequested or (not plan.no_bumpers and
     S.bumper_every_n_tracks > 0 and self.tracksSinceBumper >= S.bumper_every_n_tracks) then
    spots[#spots + 1] = self:pickBumper()
  end
  return #spots > 0 and spots or nil
end

-- requests only make sense while running: never carry them across a stop
function Station:clearRequests()
  self.skipRequested, self.bumperRequested, self.nextBumper, self.stopAfter = false, false, nil, false
  self.adRequested, self.nextAd, self.breakList = false, nil, {}
end

function Station:finishTrack(d, reason)
  local tr = d.track
  if not tr then return end
  d.track = nil                        -- finish each play exactly once
  local ev = { deck = d.id, file = tr.name, dir = tr.dir, reason = reason,
               played = d.playStart and round(now() - d.playStart, 1) or nil }
  local L = self:figures(tr.key)       -- this play's measurements; the server keeps them per file
  local keep = L.fixed or L.analyzed    -- set by an operator or analyzed: plays don't learn over it
  if d.firstAudioAt and d.playStart and not d.fadedIn then   -- a fade-in hides where the sound starts
    ev.lead_in = round(d.firstAudioAt - d.playStart, 2)
    if not keep then L.lead = ev.lead_in end
  end
  -- the silent tail counts only if the silence was heard before any fade (a fade hides the rest of the file)
  if reason ~= "cut" and d.lastAudioAt and d.playStart and d.duration and (d.fadedAt or 1e9) - d.lastAudioAt >= K.quiet_s then
    ev.trail = round(math.max(0, d.duration - (d.lastAudioAt - d.playStart)), 2)
    if not keep then L.trail = math.min(L.trail or ev.trail, ev.trail) end
  end
  if d.peakMax then ev.peak = round(d.peakMax, 1) end
  ev.level, ev.level_s = d:level()
  learnLevel(L, ev)
  emit(self, KINDS[tr.kind].event .. "_end", ev)
end

function Station:fadeAndFinish(d, seconds, reason)
  d:fadeOut(seconds, function(x) self:finishTrack(x, reason); x:stop() end)
end

function Station:startTrack(deck, track, opts, attempt)
  opts, attempt = opts or {}, attempt or 1
  if not track then
    self.state = "idle"
    return
  end
  deck:load(track, function(d, ok, err)
    local S = self.settings
    if not ok then
      local permanent = startsWith(err, "Unsupported")
      log("! %s: load failed on deck %s: %s (%s)", self.id, d.id, track.name, tostring(err))
      if permanent then self:figures(track.key).unsupported = true end   -- never playable (the server remembers it)
      self:removeTrack(track)          -- otherwise skipped until the next scan
      emit(self, permanent and "file_unsupported" or "file_failed", { file = track.name, dir = track.dir, status = tostring(err) })
      if attempt < 5 then
        local nxt = (track.kind == "imaging" and self:pickBumper()) or
                    (track.kind == "ad" and table.remove(self.breakList, 1)) or nil
        self:startTrack(deck, nxt or self:nextSong(true), opts, attempt + 1)
      else
        self.state = "idle"
      end
      return
    end
    local L = self.learned[track.key] or NONE
    local skip = L.lead and math.min(S.lead_skip_max_s, math.max(0, L.lead - S.lead_skip_keep_s)) or 0
    d.trim = self:levelTrim(L)
    d:play(S[KINDS[track.kind].gain] + d.trim, opts.fadeIn or 0, skip)
    if track.kind == "music" then
      self.live, self.other, self.state = d, self:otherDeck(d), "playing"
      self.tracksSinceBumper, self.tracksSinceAd = self.tracksSinceBumper + 1, self.tracksSinceAd + 1
      push(self.history, track, K.history_max)
      emit(self, "track_start", { deck = d.id, file = track.name, dir = track.dir, artist = track.artist,
        title = track.title, duration = round(d.duration or 0, 1), skip = skip > 0 and round(skip, 1) or nil,
        trim = d.trim ~= 0 and d.trim or nil })
    else
      self.spotDeck, self.state = d, (track.kind == "ad" and "ad" or "bumper")
      if track.kind == "ad" then self.tracksSinceAd = 0 else self.tracksSinceBumper = 0 end
      emit(self, KINDS[track.kind].event .. "_start", { deck = d.id, file = track.name, dir = track.dir, artist = track.artist,
        title = track.title, duration = round(d.duration or 0, 1), trim = d.trim ~= 0 and d.trim or nil })
    end
  end)
end

-- start the next track on a free deck (boot, start, dead-air guard)
function Station:kick(why)
  local nxt = self:nextSong(true)
  if not nxt then
    if not self.emptyNoted then emit(self, "library_empty", { music_dir = self.settings.music_dir }) end
    self.emptyNoted, self.state = true, "idle"
    return false
  end
  self.emptyNoted = false
  if why then emit(self, "dead_air_guard", { state = self.state }) end
  self.state = "crossfading"
  self:startTrack(self.decks[1]:isBusy() and self.decks[2] or self.decks[1], nxt, {})
  return true
end

function Station:transition(reason)
  local live, other, S = self.live, self.other, self.settings
  if not live or not other then return end
  self.armed = nil
  local fadeS = (reason == "skip" and S.skip_fade_s) or (reason == "silence" and S.silence_fade_s) or S.crossfade_s
  local outReason = (reason == "skip" or reason == "break") and "cut" or (reason == "silence" and "silence" or "end")
  if self.stopAfter then               -- end of the line: fade out, start nothing
    emit(self, "transition", { reason = reason, from = live.id, fade_s = fadeS, spots = "-", stop = true })
    self:fadeAndFinish(live, fadeS, outReason)
    self:goOff("stop_after_current")
    return
  end
  local spots = self:planBreak()
  self.bumperRequested, self.adRequested, self.skipRequested = false, false, false
  local names = {}
  for i, tr in ipairs(spots or {}) do names[i] = tr.name end
  emit(self, "transition", { reason = reason, from = live.id, fade_s = fadeS, spots = #names > 0 and table.concat(names, " + ") or "-" })
  self.state = "crossfading"
  if spots then
    self.freeDeck, self.breakList = live, spots
    self:startTrack(other, table.remove(spots, 1), {})
    if reason == "end" or reason == "fade" then fadeS = S.bumper_fade_s end   -- the song dips under the break
  else
    self:startTrack(other, self:nextSong(true), { fadeIn = S.fade_in_s })
  end
  self:fadeAndFinish(live, fadeS, outReason)
end

-- a break item ended (ended = its deck) or is in its tail (ended = nil):
-- the rest of the break back to back, then the next song (under the last item's tail)
function Station:afterSpot(ended)
  local free = self.freeDeck
  if free.state == "fading" or free.state == "playing" then   -- the song under the break, or a break item's quiet end
    free.fade = nil
    self:finishTrack(free, free.state == "fading" and "end" or "cut")
    free:stop()
  end
  if self.stopAfter then return self:goOff("stop_after_current") end   -- the break item plays out
  self.state = "crossfading"
  local nxt = table.remove(self.breakList, 1)
  if nxt then
    self.freeDeck = ended or self.spotDeck
    return self:startTrack(free, nxt, {})
  end
  self:startTrack(free, self:nextSong(true), { fadeIn = ended and 0 or self.settings.fade_in_s })
end

function Station:onDeckEnded(d)
  if self.state == "playing" and d == self.live then
    log("%s: deck %s reached end of file before the crossfade fired", self.id, d.id)
    self:finishTrack(d, "eof")
    if self.stopAfter then self:goOff("stop_after_current") return end
    self.state = "crossfading"
    self:startTrack(self.other, self:nextSong(true), {})
  elseif isSpot(self.state) and d == self.spotDeck then
    self:finishTrack(d, "end")         -- a break item plays to its end by design
    self:afterSpot(d)
  else
    self:finishTrack(d, "end")         -- e.g. a bumper whose tail the next track started under
    emit(self, "deck_idle", { deck = d.id })
  end
end

-- automation off without touching the decks (whatever is playing finishes)
function Station:goOff(why)
  self:clearRequests()
  self.automation, self.state, self.armed = false, "off", nil
  emit(self, "automation", { on = false, why = why })
end

function Station:setAutomation(on, why)
  if on == self.automation then return end
  self:clearRequests()
  if not on then
    for _, d in ipairs(self.decks) do
      if d.state == "playing" or d.state == "fading" then self:fadeAndFinish(d, self.settings.stop_fade_s, "cut")
      elseif d.state == "loading" then d:stop() end
    end
    return self:goOff(why or "control")
  end
  self.automation, self.state = true, "idle"
  emit(self, "automation", { on = true, why = why or "control" })
  if not self:busy() then self:kick() end
end

-- the crossfade point is checked every tick; inside the last tick it is armed
-- with a one-shot timer so the fade starts on time without a fast loop
function Station:crossfadeCheck(live, t)
  local S = self.settings
  local L = (live.track and self.learned[live.track.key]) or NONE
  local due = (live:remainingEst(t) or 999) - (S.crossfade_s + (L.trail or 0) + 0.25)
  if due <= 0 then return self:transition("end") end
  if due <= S.fade_out_early_s and live:fadingOut(t) then return self:transition("fade") end
  if due < K.tick_s and self.armed ~= live.playStart then
    self.armed = live.playStart
    local start = live.playStart
    Timer.CallAfter(function()
      if self.state == "playing" and self.live == live and live.playStart == start and self.armed == start then
        guard(self, "crossfade", self.transition, self, "end")
      end
    end, due)
  end
end

function Station:tick(t)
  local d1, d2, S = self.decks[1], self.decks[2], self.settings
  d1:sample(t, S)
  d2:sample(t, S)
  if not self.automation then return end

  local live, spot = self.live, self.spotDeck
  if self.state == "playing" and live and live.state == "playing" then
    local playedFor = t - live.playStart
    if self.skipRequested and playedFor > 1 then
      self:transition("skip")
    elseif (self.bumperRequested or self.adRequested) and playedFor > 1 then
      self:transition("break")
    elseif playedFor > 5 and live.silentSince and (t - live.silentSince) >= S.silence_hold_s
           and (live:remainingEst(t) or 999) <= S.end_guard_s then
      self:transition("silence")
    elseif playedFor > 5 then
      self:crossfadeCheck(live, t)
    end
  elseif isSpot(self.state) and spot and spot.state == "playing" then
    -- hand over at the quiet end (silent tail, or a fade-out); under the tail of the break's last item
    local L, rem = (spot.track and self.learned[spot.track.key]) or NONE, spot:remainingEst(t) or 0
    local due = rem - (L.trail or 0) - (#self.breakList == 0 and S.bumper_tail_s or 0)
    if due <= 0 or (due <= S.fade_out_early_s and spot:fadingOut(t)) then self:afterSpot(nil) end
  end

  -- dead-air guard (backs off while the library is empty)
  if d1:isBusy() or d2:isBusy() then
    self.idleSince = nil
  else
    self.idleSince = self.idleSince or t
    if t - self.idleSince >= (self.emptyNoted and 30 or S.dead_air_s) then
      self.idleSince = nil
      if #self.lib.music == 0 and #self.queue == 0 then self:scan() end
      self:kick(not self.emptyNoted)
    end
  end
end

function Station:statusLine()
  if self.fault then return self.id .. ": FAULT (" .. self.fault .. ")" end
  local tr = self.live and self.live.track
  return self.id .. ": " .. self.state .. (tr and (" " .. tr.name) or "")
end

-- ── ANALYZER: the Core's analysis players (CFG.ANALYZERS; routed nowhere, never a station's) measure the files the
-- server lists for any station of this Core, at unity gain: the quiet start (the first K.scan_head_s), K.scan_body_s
-- from the middle (the average level), then the last K.scan_tail_s. On-air decks are never touched.
-- todo: the server's list ({ id, station, key }), worked through from pos; done[id]: measured from this list
Analyzer = { id = "analysis", decks = {}, todo = {}, done = {}, rev = "", pos = 1, changed = 0, sentChanged = -1, stateAt = -1e9 }

function Analyzer:can() return #self.decks > 0 end

-- one CFG.ANALYZERS entry; taken[player] = the station that has it (or true: an analysis player already)
function Analyzer:add(a, taken)
  if type(a) ~= "table" or type(a.player) ~= "string" or a.player == "" then error("no player named", 0) end
  local owner = taken[a.player]
  if owner then error("player '" .. a.player .. "' is " .. (owner == true and "listed twice" or ("station " .. owner .. "'s")), 0) end
  local ok, meter = pcall(Component.New, a.meter)
  if not ok or not meter then error("meter '" .. tostring(a.meter) .. "' not found", 0) end
  self.decks[#self.decks + 1] = Deck.new(self, a.player, a.player, meter, tostring(a.meter), a.meter_idx or 1)
  taken[a.player] = true
end

-- boot: an entry that cannot be used is logged and left out (the stations never notice)
function Analyzer:build(list, cfgs)
  local taken = {}
  for _, c in ipairs(cfgs) do for _, p in ipairs(type(c.players) == "table" and c.players or NONE) do taken[p] = c.id end end
  for i, a in ipairs(type(list) == "table" and list or NONE) do
    local ok, err = pcall(self.add, self, a, taken)
    if not ok then log("! CFG.ANALYZERS[%d]: %s: left out", i, tostring(err)) end
  end
  log("analysis: %d player(s)%s", #self.decks, self:can() and "" or ": files are not analyzed (figures are learned from plays)")
end

-- the server's list for this Core: { station, key } in the order to measure
function Analyzer:list(list, rev)
  local todo, keep = {}, {}
  for _, e in ipairs(list) do
    if type(e) == "table" and type(e.station) == "string" and type(e.key) == "string" and #todo < K.analyze_max then
      local id = e.station .. "|" .. e.key
      todo[#todo + 1], keep[id] = { id = id, station = e.station, key = e.key }, true
    end
  end
  for id in pairs(self.done) do if not keep[id] then self.done[id] = nil end end   -- asked again later = analyze again
  self.todo, self.rev, self.pos = todo, tostring(rev or ""), 1
end

-- measured (or given up): not again from this list. An id no longer listed is not marked: listed again later
-- (Stop, then Analyze again; out of the first K.analyze_max and back), it is measured again
function Analyzer:markDone(id)
  local todo = self.todo
  for i = 1, #todo do if todo[i].id == id then self.done[id] = true return end end
end

function Analyzer:busyWith(id)
  local decks = self.decks
  for i = 1, #decks do if decks[i].job and decks[i].job.id == id then return true end end
  return false
end

-- an idle player takes the first listed file not measured yet nor on another player (of a station of this Core).
-- A station whose build failed has no scanned library (its players' root is unknown): its files wait until it is built
function Analyzer:start(d, t)
  local todo = self.todo
  for i = self.pos, #todo do
    local e = todo[i]
    local st = not self.done[e.id] and Radio.byId[e.station]
    if st and st.built and not self:busyWith(e.id) then return self:begin(d, e, st, t) end
    if i == self.pos and not st then self.pos = i + 1 end   -- measured, or not a station of this Core
  end
end

-- the file's folder as this player sees it: its root is the station's players' root (normally Audio/), or one
-- of the two roots is a folder of the other
function Analyzer:dirFor(d, st, dir)
  local mine, theirs = d:root(), st.lib.root
  if mine == theirs then return dir end
  if startsWith(theirs, mine) then return theirs:sub(#mine + 1) .. dir end
  local sub = mine:sub(#theirs + 1)
  if startsWith(mine, theirs) and startsWith(dir, sub) then return dir:sub(#sub + 1) end
  return nil, "outside the analysis player's root " .. mine .. " (station " .. st.id .. "'s players' root is " .. theirs
    .. "): give the analysis player the same root, normally Audio/"
end

-- a file its station does not list now (a load failed on air, or the list is a moment old) is tried all the same:
-- the list never sticks
function Analyzer:begin(d, e, st, t)
  local tr = st.lib.byKey[e.key]
  local job = { id = e.id, station = st.id, key = e.key, listed = tr ~= nil, S = st.settings, started = t }
  if not tr then
    local folder = e.key:match("^(.*/)") or ""
    tr = { key = e.key, dir = folder, name = e.key:sub(#folder + 1) }
  end
  job.track, job.path = tr, st:mediaPath(tr.dir) .. tr.name
  d.job, self.changed = job, self.changed + 1
  local dir, err = self:dirFor(d, st, tr.dir)
  if not dir then
    d.warned = d.warned or {}
    if not d.warned[st.id] then d.warned[st.id] = true; log("! %s: %s", d.id, err) end
    return self:finish(d, false, err)
  end
  d:load(dir == tr.dir and tr or { key = tr.key, dir = dir, name = tr.name, kind = tr.kind }, function(x, ok, loadErr)
    if x.job ~= job then return end
    if not ok then return self:finish(x, false, loadErr) end
    job.duration, job.phase = x.duration, "head"
    x:play(0, 0, 0)                     -- unity gain: the figures are the file's own
  end)
end

-- jump within the file being analyzed: times stay file positions; the meter settles before it counts again
local function seek(d, t, pos)
  set(d.c.locate, "Value", pos)
  d.playStart, d.lastAudioAt, d.silentSince, d.settleUntil = t - pos, nil, nil, t + K.scan_settle_s
end

function Analyzer:step(d, t)
  local j = d.job
  if t - j.started > (j.duration or 0) + K.scan_spare_s then return self:finish(d, false, "timed out") end   -- never stuck
  if not d.playStart or not (j.phase == "head" or j.phase == "body") then return end
  local played, dur = t - d.playStart, d.duration
  if j.phase == "head" then
    if not d.firstAudioAt and played < K.scan_head_s and played < dur - 1 then return end
    j.lead = d.firstAudioAt and d.firstAudioAt - d.playStart or K.scan_head_s
    local body = (dur - K.scan_body_s) / 2
    if body > played + 1 and body + K.scan_body_s <= dur - K.scan_tail_s then   -- room for the middle
      seek(d, t, body)
      d.pow, d.powN, j.phase, j.bodyEnd = 0, 0, "body", body + K.scan_body_s
      return
    end
  elseif played < j.bodyEnd then
    return
  else
    j.level, j.levelS = d:level()
  end
  j.phase = "tail"
  local pos = dur - K.scan_tail_s
  if pos > played + 1 then seek(d, t, pos) end   -- jump to the end (a short file just plays on)
end

-- figures (ev.ok) or "never playable", at once, on every station of this Core that lists the file at path
function Analyzer:share(path, ev, unsupported)
  if not (ev.ok or unsupported) then return end
  for _, st in ipairs(Radio.stations) do
    local root = st:mediaPath("")
    local key = startsWith(path, root) and path:sub(#root + 1)
    local tr = key and st.lib.byKey[key]
    if tr and unsupported then
      st:figures(key).unsupported = true
      st:removeTrack(tr)
    elseif tr then
      local L = st:figures(key)
      if not L.fixed then L.lead, L.trail, L.analyzed = ev.lead_in, ev.trail, true end   -- final: plays don't learn over them
      learnLevel(L, ev)
    end
  end
end

-- ok: played to its end; else err says why. The result goes to the server as the job's station's event (the
-- server keeps it for the file); a listed file the player cannot play (Unsupported...) is also reported as such
function Analyzer:finish(d, ok, err)
  local j = d.job
  local ev = { file = j.track.name, dir = j.track.dir, ok = ok and j.lead ~= nil, status = err }
  if ev.ok then
    ev.lead_in, ev.duration = round(j.lead, 2), round(d.duration, 1)
    ev.trail = round(d.lastAudioAt and math.max(0, d.duration - (d.lastAudioAt - d.playStart)) or math.min(K.scan_tail_s, d.duration), 2)
    ev.level, ev.level_s = j.level, j.levelS
    if not ev.level then ev.level, ev.level_s = d:level() end   -- no middle (a short file): everything heard
    if d.peakMax then ev.peak = round(d.peakMax, 1) end
  end
  d.job, d.settleUntil, self.changed = nil, nil, self.changed + 1
  self:markDone(j.id)
  d:stop()
  local st, never = Radio.byId[j.station], not ok and j.listed and startsWith(err, "Unsupported")
  self:share(j.path, ev, never)
  emit(st, "analysis", ev)
  if never then emit(st, "file_unsupported", { file = ev.file, dir = ev.dir, status = err }) end
end

function Analyzer:onDeckEnded(d) if d.job then self:finish(d, true) end end

function Analyzer:deckTick(d, t)
  local j = d.job
  if j then
    d:sample(t, j.S)
    self:step(d, t)
  elseif d.state == "idle" and self.pos <= #self.todo then
    self:start(d, t)
  end
end

-- every tick, whatever the stations do; an error fails that player's file (or passes a listed one): never every tick
function Analyzer:tick(t)
  local decks = self.decks
  for i = 1, #decks do
    local d = decks[i]
    if not guard(self, "analysis", self.deckTick, self, d, t) then
      local j = d.job
      if not j then
        self.pos = self.pos + 1
      else
        self:markDone(j.id)
        if not guard(self, "analysis", self.finish, self, d, false, "error on the Core") then d.job = nil; pcall(Deck.stop, d) end
      end
    end
  end
end

-- each analysis player for the server (the Library page): idle, or measuring a station's file
function Analyzer:state()
  local out = {}
  for i, d in ipairs(self.decks) do
    local j = d.job
    out[i] = { name = d.id, state = j and "measuring" or "idle", station = j and j.station, key = j and j.key }
  end
  return out
end

function Analyzer:statusLine()
  local parts, left = {}, 0
  for i, d in ipairs(self.decks) do parts[i] = d.id .. " " .. (d.job and (d.job.station .. " " .. d.job.track.name) or "idle") end
  for _, e in ipairs(self.todo) do if not self.done[e.id] then left = left + 1 end end
  return "analysis: " .. table.concat(parts, ", ") .. ", " .. left .. " listed"
end

-- ── COMMANDS (from the web panel, via the server) ─────────────────────────────
local function trackInfo(tr)
  if not tr then return nil end
  return { key = tr.key, file = tr.name, dir = tr.dir, kind = tr.kind, artist = tr.artist,
           title = tr.title, duration = tr.duration }
end

local function libraryTrack(st, args)
  local tr = st.lib.byKey[tostring(args.key or "")]
  if not tr then return nil, "not in the library: " .. tostring(args.key) end
  return tr
end

local function enqueue(st, tr, first)
  local q = st.queue
  if #q >= K.queue_max then return false, "queue is full (" .. K.queue_max .. ")" end
  table.insert(q, first and 1 or #q + 1, tr)
  return true, "queued at " .. (first and 1 or #q)
end

-- the queue entry an operator meant: the index they saw if it still holds
-- that song, otherwise the first entry with that song (the page lags ~1 s)
local function queueIndex(st, args)
  local i = tonumber(args.index)
  local q = i and st.queue[i]
  if q and (not args.key or q.key == args.key) then return i end
  for j, e in ipairs(st.queue) do if args.key and e.key == args.key then return j end end
end

local function running(st)
  if not st.automation then return false, "automation is off" end
  return true
end

-- bumper now / ad break now, optionally with a chosen file (kind = "imaging" | "ad")
local function breakNow(st, kind, args)
  local ok, err = running(st)
  if not ok then return false, err end
  if isSpot(st.state) then return false, "a break is playing" end
  local flag, pick = kind == "ad" and "adRequested" or "bumperRequested", kind == "ad" and "nextAd" or "nextBumper"
  if st[flag] then return false, "already due" end
  local tr
  if args.key then
    tr, err = libraryTrack(st, args)
    if not tr then return false, err end
    if tr.kind ~= kind then return false, kind == "ad" and "not an ad" or "not an imaging file" end
  elseif #st.lib[kind] == 0 then
    return false, kind == "ad" and "no ads" or "no imaging files"
  end
  st[pick], st[flag] = tr, true
  return true
end

Control = {
  skip = function(st)
    local ok, err = running(st)
    if not ok then return false, err end
    if st.state ~= "playing" then return false, "nothing to skip right now (" .. st.state .. ")" end
    st.skipRequested = true
    return true
  end,
  bumper_now = function(st, args) return breakNow(st, "imaging", args) end,
  ad_now = function(st, args) return breakNow(st, "ad", args) end,
  stop_after = function(st, args)
    if args.on == false then st.stopAfter = false; return true, "cancelled" end
    local ok, err = running(st)
    if not ok then return false, err end
    st.stopAfter = true
    return true, "will stop after the current track"
  end,
  stop_now = function(st)
    if not running(st) then return false, "already stopped" end
    st:setAutomation(false, "remote")
    return true
  end,
  start = function(st)
    if st.automation then return false, "already running" end
    st:setAutomation(true, "remote")
    return true
  end,
  queue_add = function(st, args)
    local tr, err = libraryTrack(st, args)
    if not tr then return false, err end
    if tr.kind ~= "music" then return false, "only music can be queued" end
    return enqueue(st, tr, args.position == "next")
  end,
  play_now = function(st, args)
    local tr, err = libraryTrack(st, args)
    if not tr then return false, err end
    if tr.kind ~= "music" then
      if not st.automation then st:setAutomation(true, "play_now") end
      return breakNow(st, tr.kind, args)
    end
    local ok, why = enqueue(st, tr, true)
    if not ok then return false, why end
    if not st.automation then
      st:setAutomation(true, "play_now")
    elseif st.state == "playing" then
      st.skipRequested = true
    end
    return true
  end,
  queue_remove = function(st, args)
    local i = queueIndex(st, args)
    if not i then return false, "not in the queue any more" end
    table.remove(st.queue, i)
    return true
  end,
  queue_move = function(st, args)
    local i, to = queueIndex(st, args), tonumber(args.to)
    if not i or not to then return false, "not in the queue any more" end
    local asked = tonumber(args.index)   -- the page may be a moment out of date: keep the direction
    if asked and asked ~= i then to = i + (to - asked) end
    table.insert(st.queue, clamp(to, 1, #st.queue), table.remove(st.queue, i))
    return true
  end,
  queue_clear = function(st)
    local n = #st.queue
    st.queue = {}
    return true, n .. " removed"
  end,
  rescan = function(st)
    st:scan()
    return true, string.format("%d music, %d imaging, %d ads", #st.lib.music, #st.lib.imaging, #st.lib.ad)
  end,
}

-- one operator command; every outcome (refused, failed, done) is reported as a 'command' event
function Station:command(c)
  local fn = Control[tostring(c.cmd)]
  local ok, res, note
  if self.fault then
    res, note = false, "station fault: " .. self.fault
  elseif fn then
    ok, res, note = pcall(fn, self, type(c.args) == "table" and c.args or {})
    if not ok then
      log("! %s: command %s failed: %s", self.id, tostring(c.cmd), tostring(res))
      res, note = false, "error: " .. tostring(res)
    end
  else
    res, note = false, "unknown command"
  end
  emit(self, "command", { id = c.id, cmd = tostring(c.cmd), ok = res and true or false, note = note, by = c.by })
  return res, note
end

function Station:snapshot(t)
  local function deck(d)
    local info = trackInfo(d.track or d.pending) or {}
    info.state, info.live = d.state, (d == self.live) or nil
    if d.playStart and (d.state == "playing" or d.state == "fading") then
      info.elapsed, info.gain, info.trim = round(t - d.playStart, 1), d.playGain and round(d.playGain, 1), d.trim
      local rem = d:remainingEst(t)
      info.remaining = rem and round(rem, 1)
    end
    info.duration = d.duration and round(d.duration, 1) or info.duration
    return info
  end
  local S, lib = self.settings, self.lib
  local nxt, queued, why = self:nextSong()
  local nextUp = trackInfo(nxt)
  if nextUp then nextUp.queued, nextUp.pick = queued, why end
  local queue = {}
  for i, q in ipairs(self.queue) do queue[i] = trackInfo(q) end
  return {
    automation = self.automation, state = self.state, stop_after = self.stopAfter,
    decks = { A = deck(self.decks[1]), B = deck(self.decks[2]) }, next_up = nextUp, queue = queue,
    next_bumper = trackInfo(self.nextBumper), bumper_requested = self.bumperRequested,
    tracks_since_bumper = self.tracksSinceBumper, bumper_every = S.bumper_every_n_tracks,
    next_ad = trackInfo(self.nextAd), ad_requested = self.adRequested, tracks_since_ad = self.tracksSinceAd,
    ad_every = S.ad_every_n_tracks, ads_due = self.adPlan and #self.adPlan.due or 0,
    library = { music = #lib.music, imaging = #lib.imaging, ad = #lib.ad },
    analyze = { can = Analyzer:can() },
    settings = S, uptime = round(t - BOOT_T, 0), outbox = #Server.outbox,
  }
end

-- a faulted station's state: why, and when it is tried again
function Station:faultState(t)
  return { fault = self.fault, automation = false, state = "fault", retry_s = round(math.max(0, self.retryAt - t), 0),
           decks = {}, queue = {}, library = { music = 0, imaging = 0, ad = 0 }, settings = self.settings,
           analyze = { can = Analyzer:can() }, uptime = round(t - BOOT_T, 0), outbox = #Server.outbox }
end

-- ── SERVER: one sync request a second for every station, plus each station's library after a scan ────────
Server.outbox, Server.fails, Server.nextTry, Server.busy, Server.onDisk, Server.outboxDirty = {}, 0, 0, {}, false, false
Server.savedAt, Server.upload, Server.scans, Server.libFails, Server.libNextTry, Server.libNext = -1e9, nil, 0, 0, 0, 1

local function coreName() return type(CFG.CORE_ID) == "string" and CFG.CORE_ID ~= "" and CFG.CORE_ID or DESIGN_NAME end

function Server.enabled() return type(CFG.SERVER_URL) == "string" and CFG.SERVER_URL ~= "" end

-- written (by Chores) only while the server is unreachable or keeps a station's events (its part answered
-- { error }), and once more to clear it; a big one at most every 2 min
function Server.saveOutbox()
  if not Server.outboxDirty then return end
  local n = #Server.outbox
  if n > K.outbox_big and now() - Server.savedAt < K.outbox_big_s then return end
  Server.outboxDirty, Server.onDisk, Server.savedAt = false, n > 0, now()
  writeJson(FILES.outbox, Server.outbox)
end

-- every station's events in one bounded outbox (the oldest go first; each station counts its own losses)
function Server.queue(ev)
  if not Server.enabled() then return end
  local lost = push(Server.outbox, ev, K.outbox_max)
  local st = lost and Radio.byId[lost.station]
  if st then st.dropped = st.dropped + 1 end
  Server.outboxDirty = Server.outboxDirty or Server.fails > 0 or Server.onDisk
end

-- every station's last server settings, in one write (once per sync reply that changed any)
function Server.saveSettings()
  local out = {}
  for _, st in ipairs(Radio.stations) do
    if st.saved then out[st.id] = { rev = st.settingsRev, settings = st.saved } end
  end
  writeJson(FILES.settings, { stations = out })
end

local function free(channel)
  local since = Server.busy[channel]
  return not since or now() - since >= K.http_timeout_s + 5
end

-- POST JSON on a named channel (one request in flight per channel; a request
-- whose callback never comes is forgotten after the timeout + 5 s)
local function post(channel, path, payload, onDone)
  if not free(channel) then return false end
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

function Station:applySettings(s, rev, fromFile)
  local new, saved, n = fallback(), {}, 0
  for k, v in pairs(s) do
    if SETTINGS[k] ~= nil and type(v) == type(SETTINGS[k]) then new[k], saved[k], n = v, v, n + 1 end
  end
  local rescan = false
  for _, kind in ipairs(KIND_ORDER) do
    local key = KINDS[kind].dir
    new[key] = normDir(new[key])
    rescan = rescan or new[key] ~= self.settings[key]
  end
  self.settings, self.saved, self.settingsRev, self.rev = new, saved, tostring(rev or ""), self.rev + 1
  log("%s: settings: %d server value(s) applied (rev %s)%s", self.id, n, self.settingsRev, fromFile and " from file" or "")
  if fromFile then return end          -- boot: the library is scanned right after
  emit(self, "settings_applied", { rev = self.settingsRev, count = n })
  if rescan and self.built and not self.fault then self:scan() end
end

function Station:applyAdPlan(p, rev)
  local function keys(list)
    local out = {}
    for _, k in ipairs(type(list) == "table" and list or NONE) do if type(k) == "string" then out[#out + 1] = k end end
    return out
  end
  self.adPlan = { due = keys(p.due), mix = keys(p.mix), next = type(p.next) == "string" and p.next or nil,
                  why = tostring(p.next_why or ""), ident = type(p.ident) == "string" and p.ident or nil,
                  no_bumpers = p.no_bumpers == true }
  self.adsRev, self.rev = tostring(rev or ""), self.rev + 1
end

-- this station's entry of the sync reply (the fields mean what they meant for the 1.x single-station reply)
function Station:applyReply(r)
  if tonumber(r.ack_reset) then self.lastCmd = tonumber(r.ack_reset) end
  if type(r.settings) == "table" then self:applySettings(r.settings, r.settings_rev) end
  if type(r.ads) == "table" then self:applyAdPlan(r.ads, r.ads_rev) end
  for _, c in ipairs(type(r.commands) == "table" and r.commands or NONE) do
    local id = type(c) == "table" and tonumber(c.id)
    if id and id > self.lastCmd then
      self.lastCmd = id
      self:command(c)
    end
  end
end

local function decoded(code, data)
  if code ~= 200 then return nil end
  local ok, resp = pcall(json.decode, data or "")
  return ok and type(resp) == "table" and resp or nil
end

-- the next part of one station's library (one upload in flight for the whole Core; the stations take turns).
-- No answer, or 409 on the last part (the server lacks a part): the whole scan again, after a back-off
function Server.libraryStep()
  local u = Server.upload
  if u and (Radio.byId[u.st.id] ~= u.st or u.st.fault or u.gen ~= u.st.lib.gen) then u, Server.upload = nil, nil end   -- rebuilt / rescanned: start over
  if not u then
    local list, n = Radio.stations, #Radio.stations
    if now() < Server.libNextTry then return end
    for k = 0, n - 1 do
      local i = (Server.libNext + k - 1) % n + 1
      local st = list[i]
      if st.libraryDirty and not st.fault then
        local all = {}
        for _, kind in ipairs(KIND_ORDER) do for _, tr in ipairs(st.lib[kind]) do all[#all + 1] = tr end end
        Server.scans, Server.libNext, st.libraryDirty = Server.scans + 1, i % n + 1, false
        u = { st = st, gen = st.lib.gen, list = all, part = 1, parts = math.max(1, math.ceil(#all / K.library_chunk)) }
        u.scan = BOOT_ID .. "-" .. Server.scans
        Server.upload = u
        break
      end
    end
    if not u then return end
  end
  if not free("library") then return end
  local st, items, first = u.st, {}, (u.part - 1) * K.library_chunk
  for i = first + 1, math.min(first + K.library_chunk, #u.list) do
    local tr = u.list[i]
    items[#items + 1] = { key = tr.key, dir = tr.dir, file = tr.name, kind = tr.kind, artist = tr.artist, title = tr.title, size = tr.size }
  end
  local S = st.settings
  local sent = post("library", "/api/v1/core/library", { station = st.id, boot = BOOT_ID, scan = u.scan, part = u.part,
    parts = u.parts, music_dir = S.music_dir, imaging_dir = S.imaging_dir, ads_dir = S.ads_dir, items = items },
    function(code, data) Server.libraryDone(u, code, data) end)
  if not sent then Server.libraryDone(u, nil) end
end

function Server.libraryDone(u, code, data)
  if Server.upload ~= u then return end
  local resp = decoded(code, data)
  if resp and resp.error ~= nil then resp = nil end
  if resp and u.part < u.parts then u.part = u.part + 1 return end     -- the next part with the next sync
  Server.upload = nil
  if resp then
    Server.libFails = 0
    if Radio.byId[u.st.id] == u.st and u.gen == u.st.lib.gen then   -- rescanned since: the new scan's reply brings them
      guard(u.st, "library reply", u.st.learn, u.st, resp.learned)
    end
    return
  end
  Server.libFails = Server.libFails + 1
  local wait = math.min(120, 5 * 2 ^ Server.libFails)
  Server.libNextTry, u.st.libraryDirty = now() + wait, true
  log("! server: %s library part %d of %d not taken (%s); the whole list again in %d s", u.st.id, u.part, u.parts, tostring(code), wait)
end

-- a deck's readout for the panel: its player gain now, and its meter (after the gain): the loudest peak
-- since the last sync, the rms now
local function meterReadout(d)
  local r, g = d.rms and d.rms.Value, d.c.gain.Value
  local m = { peak = round(d.shownPeak, 1), rms = type(r) == "number" and round(r, 1) or nil,
              gain = round(type(g) == "number" and g or d.gain, 1) }
  d.shownPeak = -120
  return m
end

-- this station's part of the sync body (its events are added by Server.sync)
function Station:syncPart(t)
  local part = { station = self.id, ack = self.lastCmd, settings_rev = self.settingsRev, ads_rev = self.adsRev }
  local fresh = self.rev ~= self.sentRev or t - self.stateAt >= K.state_every_s
  if self.fault then
    if fresh then part.state = self:faultState(t) end
    return part
  end
  local d = self.decks
  part.meters = { A = meterReadout(d[1]), B = meterReadout(d[2]) }
  if fresh then part.state = self:snapshot(t) end
  return part
end

-- each station's share of the outbox, oldest first: K.sync_batch events in all, shared out fairly (round-robin)
local function shareEvents()
  local per, have, cap = {}, 0, K.sync_batch * #Radio.stations
  for _, e in ipairs(Server.outbox) do
    local st = Radio.byId[e.station]
    if st then
      local b = per[st] or {}
      per[st] = b
      if #b < K.sync_batch then
        b[#b + 1], have = e, have + 1
        if have >= cap then break end
      end
    end
  end
  local quota, left, level, took = {}, K.sync_batch, 0, true
  while left > 0 and took do
    level, took = level + 1, false
    for _, st in ipairs(Radio.stations) do
      local b = per[st]
      if left > 0 and b and #b >= level then quota[st], left, took = level, left - 1, true end
    end
  end
  for st, b in pairs(per) do for i = #b, (quota[st] or 0) + 1, -1 do b[i] = nil end end
  return per
end

function Server.sync()
  if not (Server.enabled() and json and HttpClient) or now() < Server.nextTry or #Radio.stations == 0 then return end
  if Server.fails == 0 then Server.libraryStep() end
  if not free("sync") then return end
  local t, share, parts, sent = now(), shareEvents(), {}, {}
  for i, st in ipairs(Radio.stations) do
    local ok, part = guard(st, "sync", st.syncPart, st, t)
    if not ok then part = { station = st.id, ack = st.lastCmd, settings_rev = st.settingsRev, ads_rev = st.adsRev } end
    part.events = share[st]
    if st.dropped > 0 then part.dropped = st.dropped end
    parts[i], sent[i] = part, { st = st, rev = st.rev, state = part.state ~= nil, events = part.events, dropped = part.dropped }
  end
  local body = { version = RADIO_VERSION, boot = BOOT_ID, core = coreName(), mem_kb = memKb(), stations = parts }
  local A, shown = Analyzer, nil                  -- the analysis players: their list's rev, and how they are (when that changed)
  if A:can() then body.analyze_rev = A.rev end
  if A.changed ~= A.sentChanged or t - A.stateAt >= K.state_every_s then body.analyzers, shown = A:state(), A.changed end
  post("sync", "/api/v1/core/sync", body, function(code, data, httpErr) Server.synced(sent, shown, t, code, data, httpErr) end)
end

-- the reply: each station's entry applied on its own; a station answered with { error } (or nothing) keeps its events;
-- the analysis list (top level) when it changed
function Server.synced(sent, shown, t, code, data, httpErr)
  local resp = decoded(code, data)
  local replies = resp and resp.stations
  if type(replies) ~= "table" then
    Server.outboxDirty = true
    Server.fails = Server.fails + 1
    Server.nextTry = now() + math.min(30, K.sync_s * 2 ^ math.min(Server.fails, 5))
    if Server.fails == 1 or Server.fails % 20 == 0 then
      log("! server: sync failed (%s %s); %d events queued", tostring(code), resp and "reply without stations" or tostring(httpErr or ""), #Server.outbox)
    end
    return
  end
  if Server.fails > 0 then
    log("server: reachable again")
    if Server.fails >= 10 then for _, st in ipairs(Radio.stations) do st.libraryDirty = true end end   -- long outage: resend the libraries
  end
  Server.fails = 0
  local done, changed, kept = {}, false, false
  for _, s in ipairs(sent) do
    local st = Radio.byId[s.st.id] or s.st          -- rebuilt meanwhile: the station as it is now
    local r = replies[st.id]
    if type(r) ~= "table" or r.error ~= nil then
      st.replyFails, kept = st.replyFails + 1, kept or s.events ~= nil
      if st.replyFails == 1 or st.replyFails % 60 == 0 then
        log("! server: %s: %s; its events are sent again", st.id, type(r) == "table" and tostring(r.error) or "no reply")
      end
    else
      st.replyFails = 0
      for _, e in ipairs(s.events or NONE) do done[e] = true end
      if s.state and s.st == st then st.sentRev, st.stateAt = s.rev, t end
      if s.dropped then st.dropped = math.max(0, st.dropped - s.dropped) end
      changed = changed or type(r.settings) == "table"
      guard(st, "reply", st.applyReply, st, r)
    end
  end
  if next(done) then
    local keep = {}
    for _, e in ipairs(Server.outbox) do if not done[e] then keep[#keep + 1] = e end end
    Server.outbox, Server.outboxDirty = keep, Server.onDisk
  end
  if kept then Server.outboxDirty = true end        -- events the server did not take: on disk, as in an outage
  if changed then Server.saveSettings() end
  if shown then Analyzer.sentChanged, Analyzer.stateAt = shown, t end
  if type(resp.analyze) == "table" and Analyzer:can() then guard(Analyzer, "analysis list", Analyzer.list, Analyzer, resp.analyze, resp.analyze_rev) end
end

-- settings.json: { stations = { [id] = { rev, settings } } } (a 1.x { rev, settings } is the first station's);
-- outbox.json: every station's unsent events (a 1.x event without a station is the first station's)
function Server.loadLocal(ids)
  local file, saved = readJson(FILES.settings), {}
  if file and type(file.stations) == "table" then
    for id, e in pairs(file.stations) do if type(e) == "table" and type(e.settings) == "table" then saved[id] = e end end
  elseif file and type(file.settings) == "table" and ids[1] then
    saved[ids[1]] = file
  end
  local box = Server.enabled() and readJson(FILES.outbox)
  if box and #box > 0 then
    local known, keep, lost = {}, {}, 0
    for _, id in ipairs(ids) do known[id] = true end
    for _, e in ipairs(box) do
      if type(e) == "table" then e.station = e.station or ids[1] end
      if type(e) == "table" and known[e.station] then keep[#keep + 1] = e else lost = lost + 1 end
    end
    while #keep > K.outbox_max do table.remove(keep, 1); lost = lost + 1 end
    Server.outbox, Server.onDisk = keep, true
    log("server: %d unsent events from the last run queued for replay%s", #keep,
      lost > 0 and string.format(" (%d of stations no longer configured dropped)", lost) or "")
  end
  return saved
end

-- ── CORE: every station, the global timers ────────────────────────────────────
-- the arrays the timers walk: rebuilt only when a station is built or faulted (the analysis players: always)
function Radio.refresh()
  local byId, decks = {}, {}
  for _, st in ipairs(Radio.stations) do
    byId[st.id] = st
    if not st.fault then for _, d in ipairs(st.decks) do decks[#decks + 1] = d end end
  end
  for _, d in ipairs(Analyzer.decks) do decks[#decks + 1] = d end
  Radio.byId, Radio.decks = byId, decks
end

function Radio.start(st, on)
  local ok, err = guard(st, "start", st.start, st, on)
  if not ok then st:faultNow("start: " .. tostring(err)) end
end

-- a faulted station, built again from its CFG entry (its settings, command ack and back-off carry over)
function Radio.rebuild(i)
  local old = Radio.stations[i]
  log("%s: rebuilding after a fault (%s)", old.id, old.fault)
  local st = Station.new(old.cfg, old)
  Radio.stations[i] = st
  Radio.refresh()
  if st.fault then return end
  emit(st, "station_rebuilt", { after = old.fault })
  Radio.start(st, old.wantOn)
end

function Radio.tick()
  local t, list = now(), Radio.stations
  for i = 1, #list do
    local st = list[i]
    if st.fault then
      if t >= st.retryAt then Radio.rebuild(i) end
    else
      local ok, err = guard(st, "tick", st.tick, st, t)
      if ok then st.fails = 0 else st:failed(err) end
      if not st.fault and st.backoff > K.retry_s and t - st.builtAt >= K.healthy_s then st.backoff = K.retry_s end   -- healthy again
    end
  end
  if Analyzer.decks[1] then Analyzer:tick(t) end
end

function Radio.chores()
  local parts = {}
  for i, st in ipairs(Radio.stations) do parts[i] = st:statusLine() end
  if Analyzer:can() then parts[#parts + 1] = Analyzer:statusLine() end
  log("heartbeat: Lua %s KB, outbox %d | %s", tostring(memKb()), #Server.outbox, table.concat(parts, " | "))
  Server.saveOutbox()
  pruneShared()
end

-- ── STARTUP ───────────────────────────────────────────────────────────────────
-- a 2.0.0 entry's mixer inputs (mixer / mix): 2.0.0 left a deck's input muted after it analyzed, until that deck's
-- next play on air; unmuted once here, never touched again
local function unmute2(c)
  local ok, mx = pcall(Component.New, c.mixer)
  for _, i in ipairs(type(c.mix) == "table" and c.mix or { 1, 2 }) do
    local ctl = ok and mx and control(mx, "input." .. tostring(i) .. ".mute.1")
    if ctl then set(ctl, "Boolean", false) end
  end
end

local function timer(name, fn, seconds)    -- seconds = nil: started later
  local tm = Timer.New()
  tm.EventHandler = function()
    local ok, err = pcall(fn)
    if not ok then log("! %s error: %s", name, tostring(err)) end
  end
  if seconds then tm:Start(seconds) end
  return tm
end

local function boot()
  local info = {}
  pcall(function() info = Design.GetStatus() end)
  log("radio %s starting on %s (%s)", RADIO_VERSION, tostring(info.Platform), tostring(info.DesignName))
  if not json then log("! no json library: saved settings and the server link are disabled") end
  math.randomseed(os.time())
  BOOT_T = now()
  BOOT_ID = os.date("!%Y%m%dT%H%M%SZ") .. string.format("-%04x", math.random(0, 0xffff))
  DESIGN_NAME = tostring(info.DesignName or "core")
  local cfgs, ids, seen, mixers = {}, {}, {}, {}   -- every station needs an id of its own; a bad entry is skipped
  for i, c in ipairs(type(CFG.STATIONS) == "table" and CFG.STATIONS or NONE) do
    local id = type(c) == "table" and c.id
    if type(id) ~= "string" or id == "" then log("! CFG.STATIONS[%d] has no id: skipped", i)
    elseif seen[id] then log("! CFG.STATIONS[%d]: the id '%s' is taken: skipped", i, id)
    else
      seen[id], cfgs[#cfgs + 1], ids[#ids + 1] = true, c, id
      if c.mixer ~= nil or c.mix ~= nil then mixers[#mixers + 1] = id; unmute2(c) end
    end
  end
  if #cfgs == 0 then log("! CFG.STATIONS lists no station: nothing to play") end
  if #mixers > 0 then   -- a 2.0.0 entry: analysis has its own players now
    log("CFG.STATIONS: mixer / mix no longer used (%s; their inputs unmuted once): file analysis uses CFG.ANALYZERS", table.concat(mixers, ", "))
  end
  local saved = Server.loadLocal(ids)

  Fast = timer("fast tick", function() Engine.fast() end)   -- started by Engine.wake() only while a deck fades or loads
  Tick = timer("tick", Radio.tick, K.tick_s)
  Sync = timer("sync", Server.sync, K.sync_s)
  Chores = timer("chores", Radio.chores, K.chores_s)
  for i, c in ipairs(cfgs) do Radio.stations[i] = Station.new(c, nil, saved[c.id]) end
  Analyzer:build(CFG.ANALYZERS, cfgs)
  Radio.refresh()
  for _, st in ipairs(Radio.stations) do
    emit(st, "boot", { version = RADIO_VERSION, platform = tostring(info.Platform), design = DESIGN_NAME, core = coreName(),
      server = Server.enabled() and CFG.SERVER_URL or "-" })
    if not st.fault then Radio.start(st, st.wantOn) end
  end
end

boot()
