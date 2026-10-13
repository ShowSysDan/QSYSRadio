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
  music_dir = "Test/Tuning/", imaging_dir = "Test/Imaging/", ads_dir = "",
  play_gain_db = 0, imaging_gain_db = 0, ad_gain_db = 0, ad_every_n_tracks = 0, ads_per_break = 2,
  crossfade_s = 4, fade_in_s = 0, skip_fade_s = 1.5, silence_fade_s = 1, stop_fade_s = 2, bumper_fade_s = 2,
  silence_threshold_dbfs = -50, silence_hold_s = 2, end_guard_s = 20, lead_skip_max_s = 3, lead_skip_keep_s = 0.3,
  fade_out_drop_db = 20, fade_out_early_s = 2,
  bumper_every_n_tracks = 4, bumper_tail_s = 1, artist_separation = 2, dead_air_s = 1.5,
}
DEFAULTS = {}
for k, v in pairs(SETTINGS) do DEFAULTS[k] = v end

-- internals (not settings)
local K = {
  floor_db = -100, tick_s = 0.25, fast_s = 0.05, load_timeout_s = 1.5, chores_s = 30,
  quiet_s = 0.5, sync_s = 1, sync_batch = 100, state_every_s = 10, http_timeout_s = 5, outbox_max = 5000,
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
-- append to a bounded list (oldest dropped); returns true when something was dropped
local function push(list, item, max)
  list[#list + 1] = item
  if #list > max then table.remove(list, 1) return true end
  return false
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

local function log(fmt, ...) print(os.date("!%H:%M:%S") .. " " .. string.format(fmt, ...)) end

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
EVENTS = {}          -- last 500 events: { boot, seq, t = utc, at, kind = ..., ... }
BOOT_ID, EVENT_SEQ, STATE_REV = "-", 0, 0
local function emit(kind, data)
  data = data or {}
  data.kind, data.t, data.at = kind, utc(), round(now(), 2)
  EVENT_SEQ = EVENT_SEQ + 1
  data.boot, data.seq = BOOT_ID, EVENT_SEQ   -- unique per event; the server dedupes on it
  STATE_REV = STATE_REV + 1                  -- anything worth an event changes the reported state
  push(EVENTS, data, 500)
  if Server then Server.queue(data) end
  log("%-14s %s", kind, data.file or data.cmd or data.reason or "")   -- the server keeps every field
end

-- ── LIBRARY ───────────────────────────────────────────────────────────────────
-- per file (all kinds): { trail = s, lead = s, fixed = operator's, unsupported = true }: the server's, + this boot's plays
LEARNED = {}
Library = { music = {}, imaging = {}, ad = {}, byKey = {}, root = "Audio/" }

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
  Library.byKey = {}
  for _, kind in ipairs(KIND_ORDER) do
    Library[kind] = listAudio(SETTINGS[KINDS[kind].dir], kind)
    for _, tr in ipairs(Library[kind]) do Library.byKey[tr.key] = tr end
  end
  local keep = {}                      -- keep the shuffle cycle going (new files join at the next refill)
  for _, tr in ipairs(Station.bag) do keep[#keep + 1] = Library.byKey[tr.key] end
  Station.bag = keep
  Server.libraryDirty = true
  STATE_REV = STATE_REV + 1
  if #Library.music > 0 and Station.emptyNoted then   -- music is back: play now, not after the back-off
    Station.emptyNoted = false
    if Station.automation and not (DeckA:isBusy() or DeckB:isBusy()) then Station.kick() end
  end
  log("library: %d music in %s, %d imaging in %s, %d ads in %s", #Library.music, mediaPath(SETTINGS.music_dir),
    #Library.imaging, mediaPath(SETTINGS.imaging_dir), #Library.ad, mediaPath(SETTINGS.ads_dir))
end

local function removeKey(list, key)
  for i = #list, 1, -1 do if list[i].key == key then table.remove(list, i) end end
end

function Library.remove(track)
  removeKey(Library[track.kind], track.key)
  removeKey(Station.queue, track.key)
  removeKey(Station.bag, track.key)
  Library.byKey[track.key] = nil
end

-- the server's per-file figures (reply to the library upload): they replace this boot's (edits, forgotten figures)
function Library.learn(map)
  if type(map) ~= "table" then return end
  for _, L in pairs(LEARNED) do L.trail, L.lead, L.fixed = nil, nil, nil end
  for key, v in pairs(map) do
    if type(v) == "table" then
      local L = LEARNED[key] or {}
      L.trail, L.lead, L.fixed = tonumber(v.trail), tonumber(v.lead), v.fixed == true or nil
      if v.unsupported == true then L.unsupported = true end
      LEARNED[key] = L
      if L.unsupported and Library.byKey[key] then Library.remove(Library.byKey[key]) end
    end
  end
end

-- ── DECK ──────────────────────────────────────────────────────────────────────
Deck = {}
Deck.__index = Deck
local DECK_CONTROLS = { "gain", "play", "stop", "playing", "locate", "filename", "filename.ui", "directory", "root",
                        "status", "remaining", "loop", "mute" }

function Deck.new(id, compName, meterIdx)
  local self = setmetatable({ id = id, state = "idle", gain = K.floor_db }, Deck)
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
    pending.duration = round(rem, 1)
    if cb then cb(self, true) end
  else
    self.state = "idle"
    if cb then cb(self, false, result) end
  end
end

-- skip = seconds of quiet start to jump over (seek right after play: an early seek only plays silence)
function Deck:play(gainDb, fadeInS, skip)
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
  if skip > 0 then set(self.c.locate, "Value", skip) end
  self.state, self.playStart = "playing", now() - skip   -- times stay file positions
  self.firstAudioAt, self.lastAudioAt, self.silentSince, self.peakMax, self.loudAt, self.fadedAt = nil, nil, nil, nil, nil, nil
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
  local unity = (m > -118) and (m - self.gain) or -200
  if unity > SETTINGS.silence_threshold_dbfs then
    self.lastAudioAt, self.firstAudioAt, self.silentSince = t, self.firstAudioAt or t, nil
    if unity > (self.peakMax or -200) then self.peakMax = unity end
    if unity > self.peakMax - SETTINGS.fade_out_drop_db then self.loudAt = t end
  else
    self.silentSince = self.silentSince or t
  end
end

-- in its fade-out: fade_out_drop_db under its own peak for a moment (start the next item early)
function Deck:fadingOut(t)
  return SETTINGS.fade_out_early_s > 0 and self.loudAt ~= nil and t - self.loudAt >= K.quiet_s
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
  state = "idle",          -- idle | playing | crossfading | bumper | ad | off
  live = nil, other = nil, spotDeck = nil, freeDeck = nil,
  tracksSinceBumper = 0, tracksSinceAd = 0,   -- songs started since the last bumper / ad
  history = {}, bag = {}, lastBumper = nil, lastAds = {},
  skipRequested = false, bumperRequested = false, adRequested = false, automation = true,
  idleSince = nil,
  queue = {},              -- operator "play next" list (music), consulted before the bag
  stopAfter = false,       -- stop when the current track ends (no next track)
  nextBumper = nil,        -- operator-chosen bumper for the next bumper slot
  nextAd = nil,            -- operator-chosen ad for the next break
  breakList = {},          -- the rest of the break that is playing (ads, then a bumper)
  adPlan = nil,            -- from the server: { due, mix (ads), next = song, why, ident = ID owed, no_bumpers }
  armed = nil,             -- the crossfade timer's target (deck + play start)
}

local function isSpot(state) return state == "bumper" or state == "ad" end

function Station.refillBag() Station.bag = shuffled(Library.music) end

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

-- the next song (operator queue, then the server's pick, then the local shuffle bag),
-- whether it was queued, and why the server picked it; take = remove it
function Station.nextSong(take)
  local q, plan = Station.queue, Station.adPlan
  while q[1] and not Library.byKey[q[1].key] do table.remove(q, 1) end
  if q[1] then return take and table.remove(q, 1) or q[1], true end
  local pick, last = plan and Library.byKey[plan.next or ""], Station.history[#Station.history]
  if pick and pick.kind == "music" and pick ~= last then   -- a stale plan may still name the song just played
    if take then plan.next, Server.adsRev = nil, ""; removeKey(Station.bag, pick.key) end
    return pick, false, plan.why
  end
  if #Library.music == 0 then return nil end
  local i = bagChoice()
  return take and table.remove(Station.bag, i) or Station.bag[i], false
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

local function adPool()
  if not Station.adPlan then return Library.ad end
  local pool = {}
  for _, k in ipairs(Station.adPlan.mix) do pool[#pool + 1] = Library.byKey[k] end
  return pool
end

-- the ads for a break due now, or nil: the operator's pick, then ads owed this
-- hour, then (at a regular break) mix-ins, up to ads_per_break
local function adBreak()
  local list, seen = {}, {}
  local function add(tr)
    if tr and tr.kind == "ad" and Library.byKey[tr.key] and not seen[tr.key] and #list < SETTINGS.ads_per_break then
      seen[tr.key] = true
      list[#list + 1] = tr
    end
  end
  add(Station.nextAd)
  local due = Station.adPlan and Station.adPlan.due or {}
  for _, k in ipairs(due) do add(Library.byKey[k]) end
  if Station.adRequested or (SETTINGS.ad_every_n_tracks > 0 and Station.tracksSinceAd >= SETTINGS.ad_every_n_tracks) then
    local pool = shuffled(adPool())
    for pass = 1, 2 do                 -- the last break's ads only when there is nothing else
      for _, tr in ipairs(pool) do if pass == 2 or not Station.lastAds[tr.key] then add(tr) end end
    end
  end
  Station.nextAd, Station.adRequested = nil, false
  if #list == 0 then return nil end
  Station.lastAds = seen
  if Station.adPlan then               -- played ads leave the plan; ask the server for a fresh one
    local keep = {}
    for _, k in ipairs(due) do if not seen[k] then keep[#keep + 1] = k end end
    Station.adPlan.due, Server.adsRev = keep, ""
  end
  return list
end

-- what plays between this song and the next: the server's station ID (if owed), ads (if due),
-- then a bumper (if due, no ID and bumpers not held)
local function planBreak()
  local plan = Station.adPlan or {}
  local spots, ident = adBreak() or {}, Library.byKey[plan.ident or ""]
  if ident and ident.kind == "imaging" then
    table.insert(spots, 1, ident)
    plan.ident, Server.adsRev = nil, ""
  elseif Station.bumperRequested or (not plan.no_bumpers and
     SETTINGS.bumper_every_n_tracks > 0 and Station.tracksSinceBumper >= SETTINGS.bumper_every_n_tracks) then
    spots[#spots + 1] = Station.pickBumper()
  end
  return #spots > 0 and spots or nil
end

-- requests only make sense while running: never carry them across a stop
local function clearRequests()
  Station.skipRequested, Station.bumperRequested, Station.nextBumper, Station.stopAfter = false, false, nil, false
  Station.adRequested, Station.nextAd, Station.breakList = false, nil, {}
end

function Station.finishTrack(d, reason)
  local tr = d.track
  if not tr then return end
  d.track = nil                        -- finish each play exactly once
  local ev = { deck = d.id, file = tr.name, dir = tr.dir, reason = reason,
               played = d.playStart and round(now() - d.playStart, 1) or nil }
  local L = LEARNED[tr.key] or {}       -- this play's measurements; the server keeps them per file
  LEARNED[tr.key] = L
  if d.firstAudioAt and d.playStart then
    ev.lead_in = round(d.firstAudioAt - d.playStart, 2)
    if not L.fixed then L.lead = ev.lead_in end
  end
  -- the silent tail counts only if the silence was heard before any fade (a fade hides the rest of the file)
  if reason ~= "cut" and d.lastAudioAt and d.playStart and d.duration and (d.fadedAt or 1e9) - d.lastAudioAt >= K.quiet_s then
    ev.trail = round(math.max(0, d.duration - (d.lastAudioAt - d.playStart)), 2)
    if not L.fixed then L.trail = math.min(L.trail or ev.trail, ev.trail) end
  end
  if d.peakMax then ev.peak = round(d.peakMax, 1) end
  emit(KINDS[tr.kind].event .. "_end", ev)
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
      if permanent then                -- the player can never play it (the server remembers it)
        LEARNED[track.key] = LEARNED[track.key] or {}
        LEARNED[track.key].unsupported = true
      end
      Library.remove(track)            -- otherwise skipped until the next scan
      emit(permanent and "file_unsupported" or "file_failed", { file = track.name, dir = track.dir, status = tostring(err) })
      if attempt < 5 then
        local nxt = (track.kind == "imaging" and Station.pickBumper()) or
                    (track.kind == "ad" and table.remove(Station.breakList, 1)) or nil
        Station.startTrack(deck, nxt or Station.nextSong(true), opts, attempt + 1)
      else
        Station.state = "idle"
      end
      return
    end
    local L = LEARNED[track.key] or {}
    local skip = L.lead and math.min(SETTINGS.lead_skip_max_s, math.max(0, L.lead - SETTINGS.lead_skip_keep_s)) or 0
    d:play(SETTINGS[KINDS[track.kind].gain], opts.fadeIn or 0, skip)
    if track.kind == "music" then
      Station.live, Station.other, Station.state = d, otherDeck(d), "playing"
      Station.tracksSinceBumper, Station.tracksSinceAd = Station.tracksSinceBumper + 1, Station.tracksSinceAd + 1
      push(Station.history, track, 50)
      emit("track_start", { deck = d.id, file = track.name, dir = track.dir, artist = track.artist,
        title = track.title, duration = round(d.duration or 0, 1), skip = skip > 0 and round(skip, 1) or nil })
    else
      Station.spotDeck, Station.state = d, (track.kind == "ad" and "ad" or "bumper")
      if track.kind == "ad" then Station.tracksSinceAd = 0 else Station.tracksSinceBumper = 0 end
      emit(KINDS[track.kind].event .. "_start", { deck = d.id, file = track.name, dir = track.dir, artist = track.artist,
        title = track.title, duration = round(d.duration or 0, 1) })
    end
  end)
end

-- start the next track on a free deck (boot, start, dead-air guard)
function Station.kick(why)
  local nxt = Station.nextSong(true)
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
  local outReason = (reason == "skip" or reason == "break") and "cut" or (reason == "silence" and "silence" or "end")
  if Station.stopAfter then            -- end of the line: fade out, start nothing
    emit("transition", { reason = reason, from = live.id, fade_s = fadeS, spots = "-", stop = true })
    fadeAndFinish(live, fadeS, outReason)
    Station.goOff("stop_after_current")
    return
  end
  local spots = planBreak()
  Station.bumperRequested, Station.adRequested, Station.skipRequested = false, false, false
  local names = {}
  for i, tr in ipairs(spots or {}) do names[i] = tr.name end
  emit("transition", { reason = reason, from = live.id, fade_s = fadeS, spots = #names > 0 and table.concat(names, " + ") or "-" })
  Station.state = "crossfading"
  if spots then
    Station.freeDeck, Station.breakList = live, spots
    Station.startTrack(other, table.remove(spots, 1), {})
    if reason == "end" or reason == "fade" then fadeS = SETTINGS.bumper_fade_s end   -- the song dips under the break
  else
    Station.startTrack(other, Station.nextSong(true), { fadeIn = SETTINGS.fade_in_s })
  end
  fadeAndFinish(live, fadeS, outReason)
end

-- a break item ended (ended = its deck) or is in its tail (ended = nil):
-- the rest of the break back to back, then the next song (under the last item's tail)
local function afterSpot(ended)
  local free = Station.freeDeck
  if free.state == "fading" or free.state == "playing" then   -- the song under the break, or a break item's quiet end
    free.fade = nil
    Station.finishTrack(free, free.state == "fading" and "end" or "cut")
    free:stop()
  end
  if Station.stopAfter then return Station.goOff("stop_after_current") end   -- the break item plays out
  Station.state = "crossfading"
  local nxt = table.remove(Station.breakList, 1)
  if nxt then
    Station.freeDeck = ended or Station.spotDeck
    return Station.startTrack(free, nxt, {})
  end
  Station.startTrack(free, Station.nextSong(true), { fadeIn = ended and 0 or SETTINGS.fade_in_s })
end

function Station.onDeckEnded(d)
  if Station.state == "playing" and d == Station.live then
    log("deck %s reached end of file before the crossfade fired", d.id)
    Station.finishTrack(d, "eof")
    if Station.stopAfter then Station.goOff("stop_after_current") return end
    Station.state = "crossfading"
    Station.startTrack(Station.other, Station.nextSong(true), {})
  elseif isSpot(Station.state) and d == Station.spotDeck then
    Station.finishTrack(d, "end")      -- a break item plays to its end by design
    afterSpot(d)
  else
    Station.finishTrack(d, "end")      -- e.g. a bumper whose tail the next track started under
    emit("deck_idle", { deck = d.id })
  end
end

-- automation off without touching the decks (whatever is playing finishes)
function Station.goOff(why)
  clearRequests()
  Station.automation, Station.state, Station.armed = false, "off", nil
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
  emit("automation", { on = true, why = why or "control" })
  if not (DeckA:isBusy() or DeckB:isBusy()) then Station.kick() end
end

-- the crossfade point is checked every tick; inside the last tick it is armed
-- with a one-shot timer so the fade starts on time without a fast loop
local function crossfadeCheck(live, t)
  local L = (live.track and LEARNED[live.track.key]) or {}
  local due = (live:remainingEst(t) or 999) - (SETTINGS.crossfade_s + (L.trail or 0) + 0.25)
  if due <= 0 then return Station.transition("end") end
  if due <= SETTINGS.fade_out_early_s and live:fadingOut(t) then return Station.transition("fade") end
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
  if not Station.automation then return end

  local live, spot = Station.live, Station.spotDeck
  if Station.state == "playing" and live and live.state == "playing" then
    local playedFor = t - live.playStart
    if Station.skipRequested and playedFor > 1 then
      Station.transition("skip")
    elseif (Station.bumperRequested or Station.adRequested) and playedFor > 1 then
      Station.transition("break")
    elseif playedFor > 5 and live.silentSince and (t - live.silentSince) >= SETTINGS.silence_hold_s
           and (live:remainingEst(t) or 999) <= SETTINGS.end_guard_s then
      Station.transition("silence")
    elseif playedFor > 5 then
      crossfadeCheck(live, t)
    end
  elseif isSpot(Station.state) and spot and spot.state == "playing" then
    -- hand over at the quiet end (silent tail, or a fade-out); under the tail of the break's last item
    local L, rem = (spot.track and LEARNED[spot.track.key]) or {}, spot:remainingEst(t) or 0
    local due = rem - (L.trail or 0) - (#Station.breakList == 0 and SETTINGS.bumper_tail_s or 0)
    if due <= 0 or (due <= SETTINGS.fade_out_early_s and spot:fadingOut(t)) then afterSpot(nil) end
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

function Station.statusLine()
  local tr = Station.live and Station.live.track
  return Station.state .. (tr and (" | " .. tr.name) or "") .. " | bag " .. #Station.bag
end

-- ── COMMANDS (from the web panel, via the server) ─────────────────────────────
Control = {}

local function trackInfo(tr)
  if not tr then return nil end
  return { key = tr.key, file = tr.name, dir = tr.dir, kind = tr.kind, artist = tr.artist,
           title = tr.title, duration = tr.duration }
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

-- bumper now / ad break now, optionally with a chosen file (kind = "imaging" | "ad")
local function breakNow(kind, args)
  local ok, err = running()
  if not ok then return false, err end
  if isSpot(Station.state) then return false, "a break is playing" end
  local flag, pick = kind == "ad" and "adRequested" or "bumperRequested", kind == "ad" and "nextAd" or "nextBumper"
  if Station[flag] then return false, "already due" end
  local tr
  if args.key then
    tr, err = libraryTrack(args)
    if not tr then return false, err end
    if tr.kind ~= kind then return false, kind == "ad" and "not an ad" or "not an imaging file" end
  elseif #Library[kind] == 0 then
    return false, kind == "ad" and "no ads" or "no imaging files"
  end
  Station[pick], Station[flag] = tr, true
  return true
end

Control.handlers = {
  skip = function()
    local ok, err = running()
    if not ok then return false, err end
    if Station.state ~= "playing" then return false, "nothing to skip right now (" .. Station.state .. ")" end
    Station.skipRequested = true
    return true
  end,
  bumper_now = function(args) return breakNow("imaging", args) end,
  ad_now = function(args) return breakNow("ad", args) end,
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
    if tr.kind ~= "music" then return false, "only music can be queued" end
    return enqueue(tr, args.position == "next")
  end,
  play_now = function(args)
    local tr, err = libraryTrack(args)
    if not tr then return false, err end
    if tr.kind ~= "music" then
      if not Station.automation then Station.setAutomation(true, "play_now") end
      return breakNow(tr.kind, args)
    end
    local ok, why = enqueue(tr, true)
    if not ok then return false, why end
    if not Station.automation then
      Station.setAutomation(true, "play_now")
    elseif Station.state == "playing" then
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
    return true, string.format("%d music, %d imaging, %d ads", #Library.music, #Library.imaging, #Library.ad)
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
  local nxt, queued, why = Station.nextSong()
  local nextUp = trackInfo(nxt)
  if nextUp then nextUp.queued, nextUp.pick = queued, why end
  local queue = {}
  for i, q in ipairs(Station.queue) do queue[i] = trackInfo(q) end
  return {
    automation = Station.automation, state = Station.state, stop_after = Station.stopAfter,
    decks = { A = deck(DeckA), B = deck(DeckB) }, next_up = nextUp, queue = queue,
    next_bumper = trackInfo(Station.nextBumper), bumper_requested = Station.bumperRequested,
    tracks_since_bumper = Station.tracksSinceBumper, bumper_every = SETTINGS.bumper_every_n_tracks,
    next_ad = trackInfo(Station.nextAd), ad_requested = Station.adRequested, tracks_since_ad = Station.tracksSinceAd,
    ad_every = SETTINGS.ad_every_n_tracks, ads_due = Station.adPlan and #Station.adPlan.due or 0,
    library = { music = #Library.music, imaging = #Library.imaging, ad = #Library.ad },
    settings = SETTINGS, uptime = round(t - BOOT_T, 0), outbox = #Server.outbox,
  }
end

-- ── SERVER: one sync request a second, plus the library after each scan ───────
Server = { outbox = {}, dropped = 0, lastCmd = 0, settingsRev = "", adsRev = "", fails = 0, nextTry = 0,
           busy = {}, libraryDirty = false, sentRev = -1, stateAt = -1e9, onDisk = false, outboxDirty = false }

-- the outbox is written (by Chores, every 30 s) only while the server is unreachable, and once more to clear it
function Server.saveOutbox()
  if not Server.outboxDirty then return end
  Server.outboxDirty, Server.onDisk = false, #Server.outbox > 0
  writeJson(FILES.outbox, Server.outbox)
end

function Server.enabled() return type(CFG.SERVER_URL) == "string" and CFG.SERVER_URL ~= "" end

function Server.queue(ev)
  if not Server.enabled() then return end
  if push(Server.outbox, ev, K.outbox_max) then Server.dropped = Server.dropped + 1 end
  Server.outboxDirty = Server.outboxDirty or Server.fails > 0 or Server.onDisk
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
  for k, v in pairs(DEFAULTS) do new[k] = v end
  local n = 0
  for k, v in pairs(s) do
    if DEFAULTS[k] ~= nil and type(v) == type(DEFAULTS[k]) then new[k], n = v, n + 1 end
  end
  local rescan = false
  for _, kind in ipairs(KIND_ORDER) do
    local key = KINDS[kind].dir
    new[key] = normDir(new[key])
    rescan = rescan or new[key] ~= SETTINGS[key]
  end
  SETTINGS = new
  Server.settingsRev = tostring(rev or "")
  log("settings: %d server value(s) applied (rev %s)%s", n, Server.settingsRev, fromFile and " from file" or "")
  if fromFile then return end         -- boot: the library is scanned right after
  writeJson(FILES.settings, { rev = Server.settingsRev, settings = s })
  emit("settings_applied", { rev = Server.settingsRev, count = n })
  if rescan then Library.scan() end
end

local function decoded(code, data)
  if code ~= 200 then return nil end
  local ok, resp = pcall(json.decode, data or "")
  return ok and type(resp) == "table" and resp or nil
end

function Server.postLibrary()
  local items = {}
  for _, kind in ipairs(KIND_ORDER) do
    for _, tr in ipairs(Library[kind]) do
      items[#items + 1] = { key = tr.key, dir = tr.dir, file = tr.name, kind = kind, artist = tr.artist,
        title = tr.title, size = tr.size }
    end
  end
  local sent = post("library", "/api/v1/core/library", { station = stationName(), boot = BOOT_ID,
    music_dir = SETTINGS.music_dir, imaging_dir = SETTINGS.imaging_dir, ads_dir = SETTINGS.ads_dir, items = items },
    function(code, data)
      local resp = decoded(code, data)
      if resp then Library.learn(resp.learned) else Server.libraryDirty = true end
    end)
  if sent then Server.libraryDirty = false end
end

function Server.applyAdPlan(p, rev)
  local function keys(list)
    local out = {}
    for _, k in ipairs(type(list) == "table" and list or {}) do if type(k) == "string" then out[#out + 1] = k end end
    return out
  end
  Station.adPlan = { due = keys(p.due), mix = keys(p.mix), next = type(p.next) == "string" and p.next or nil,
                     why = tostring(p.next_why or ""), ident = type(p.ident) == "string" and p.ident or nil,
                     no_bumpers = p.no_bumpers == true }
  Server.adsRev = tostring(rev or "")
  STATE_REV = STATE_REV + 1
end

local function handleReply(resp)
  if tonumber(resp.ack_reset) then Server.lastCmd = tonumber(resp.ack_reset) end
  if type(resp.settings) == "table" then Server.applySettings(resp.settings, resp.settings_rev) end
  if type(resp.ads) == "table" then Server.applyAdPlan(resp.ads, resp.ads_rev) end
  for _, c in ipairs(type(resp.commands) == "table" and resp.commands or {}) do
    local id = tonumber(c.id)
    if id and id > Server.lastCmd then
      Server.lastCmd = id
      Control.run(c)
    end
  end
end

function Server.sync()
  if not (Server.enabled() and json and HttpClient) or now() < Server.nextTry then return end
  if Server.libraryDirty and Server.fails == 0 then Server.postLibrary() end
  local t = now()
  local body = { station = stationName(), version = RADIO_VERSION, boot = BOOT_ID,
                 ack = Server.lastCmd, settings_rev = Server.settingsRev, ads_rev = Server.adsRev }
  local rev = STATE_REV
  if rev ~= Server.sentRev or t - Server.stateAt >= K.state_every_s then body.state = Station.snapshot(t) end
  local batch = {}
  for i = 1, math.min(K.sync_batch, #Server.outbox) do batch[i] = Server.outbox[i] end
  if #batch > 0 then body.events, body.dropped = batch, Server.dropped end
  post("sync", "/api/v1/core/sync", body, function(code, data, httpErr)
    local resp = decoded(code, data)
    if not resp then
      Server.outboxDirty = true
      Server.fails = Server.fails + 1
      Server.nextTry = now() + math.min(30, K.sync_s * 2 ^ math.min(Server.fails, 5))
      if Server.fails == 1 or Server.fails % 20 == 0 then
        log("! server: sync failed (%s %s); %d events queued", tostring(code), tostring(httpErr or ""), #Server.outbox)
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
      Server.outboxDirty = Server.onDisk
    end
    handleReply(resp)
  end)
end

function Server.loadLocal()
  local saved = readJson(FILES.settings)
  if saved and type(saved.settings) == "table" then Server.applySettings(saved.settings, saved.rev, true) end
  if Server.enabled() then
    local box = readJson(FILES.outbox)
    if box and #box > 0 then
      Server.outbox, Server.onDisk = box, true
      log("server: %d unsent events from the last run queued for replay", #box)
    end
  end
end

-- ── STARTUP ───────────────────────────────────────────────────────────────────
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
  local st = {}
  pcall(function() st = Design.GetStatus() end)
  log("radio %s starting on %s (%s)", RADIO_VERSION, tostring(st.Platform), tostring(st.DesignName))
  if not json then log("! no json library: saved settings and the server link are disabled") end
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
  Server.loadLocal()
  for _, kind in ipairs(KIND_ORDER) do SETTINGS[KINDS[kind].dir] = normDir(SETTINGS[KINDS[kind].dir]) end
  Library.scan()

  Fast = timer("fast tick", function() Engine.fast() end)   -- started by Engine.wake() only while fading/loading
  Tick = timer("tick", function() Station.tick(now()) end, K.tick_s)
  Sync = timer("sync", Server.sync, K.sync_s)
  Chores = timer("chores", function()
    log("heartbeat: %s", Station.statusLine())
    Server.saveOutbox()
  end, K.chores_s)
  emit("boot", { version = RADIO_VERSION, platform = tostring(st.Platform), design = STATION_NAME,
    server = Server.enabled() and CFG.SERVER_URL or "-" })

  if CFG.AUTOSTART then
    Station.kick()
  else
    Station.automation, Station.state = false, "off"
    log("AUTOSTART = false; waiting for Start on the web panel")
  end
end

boot()
