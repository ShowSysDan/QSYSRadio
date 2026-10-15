--[[---------------------------------------------------------------------------
  Q-SYS Radio Automation :: Discovery Probe
  File:     qsys/probe/probe.lua
  Version:  0.2.0
  Requires: Q-SYS Designer 9.x/10.x, a Text Controller, and Script Access
            enabled on the two Audio Players and the level meter.

  PURPOSE
    Dump everything the deck controller (Lua) and the server (Python) will need
    to know about this design BEFORE either is written: platform, network,
    component properties, every control with its type/range, the media tree,
    which files the player will load (and their durations), and an
    instrumented play -> fade -> stop cycle so we learn real timings and real
    status strings.

  HOW TO RUN
    1. Confirm Audio_Player_01, Audio_Player_02 and PlayerOutput01 have
       Script Access enabled (component Properties > Script Access).
    2. Paste this into the Text Controller, review CFG below, press Run.
    3. Copy the Debug Output from "===== PROBE START" to "===== PROBE END".
    4. A full dump (incl. a JSON block) is also written to
       media/_probe/probe_<utc timestamp>.txt on the Core.

  WHAT IT DOES
    A. Static dump   design status, build, network interfaces, all components,
                     all controls on the players + meter (type, range, value).
    B. Media dump    raw player strings, dir.get() path probes, and a recursive
                     walk of MEDIA_ROOT with file sizes.
    F. Fingerprint   (RUN_FINGERPRINT, silent) what radio.lua 2.4.0 needs to know
                     a file by its content: Crypto.Digest("sha1", ...) (correct?
                     raw bytes or hex?), io.open "rb" + seek on media files, and
                     the time to fingerprint FINGERPRINT_FILES files (ms each).
                     Runs right after the walk; its verdict says whether
                     radio.lua will fingerprint files on this Core.
    C. Directory     if the probe player has no directory selected, sets
                     PROBE_DIRECTORY and waits for it to settle.
    D. Load sweep    (RUN_LOAD_SWEEP, silent) sets filename to each file in the
                     directory, records status, duration (remaining at
                     progress 0) and load latency -> which formats work.
    E. Playback      (RUN_PLAYBACK_PROBE) baseline the meter, play the first
                     file that loaded OK at PROBE_GAIN_DB, sample 4x/s, fade to
                     -100 dB, stop, restore the player, summarise.
                     !! This STOPS whatever the probe player is doing and
                     !! plays audio through it. Set RUN_PLAYBACK_PROBE = false
                     !! for a silent run (the load sweep makes no sound).

  CHANGES IN 0.2.0
    - Section F: fingerprint check and timing (radio.lua 2.4.0).

  CHANGES IN 0.1.1 (from the first run on a Core 8 Flex, build 1-2603002)
    - Control metadata fields are MinValue/MaxValue/MinString/MaxString/
      Choices; the controls table now shows them properly.
    - all.files is a Virtual control that reads empty from script even with
      a directory selected; files are now listed with dir.get() instead.
      Path recipe: "media/" .. root .. directory  (e.g. media/Audio/Test/Tuning/).
    - Added the media walk, the directory step and the load sweep.
    - UTC timestamps. Restores directory as well as gain/loop/filename.
-----------------------------------------------------------------------------]]

PROBE_VERSION = "0.2.0"

-------------------------------------------------------------------------------
-- CONFIG
-------------------------------------------------------------------------------
CFG = {
  PLAYER_1 = "Audio_Player_01",
  PLAYER_2 = "Audio_Player_02",
  METER    = "PlayerOutput01",

  MEDIA_ROOT          = "media/Audio",   -- where the recursive walk starts
  WALK_MAX_DEPTH      = 4,
  MAX_TREE_LINES      = 80,              -- file lines printed; the dump file gets all

  PROBE_PLAYER        = 1,               -- which deck to exercise (1 or 2)
  PROBE_DIRECTORY     = "Test/Tuning/",  -- used only if the probe player has no directory (relative to root "Audio/")
  DIR_SETTLE_SECONDS  = 2,

  RUN_LOAD_SWEEP      = true,            -- load every file in the directory (silent)
  LOAD_SWEEP_MAX      = 25,
  LOAD_SETTLE_SECONDS = 1.5,             -- wait per file for status/duration to populate

  RUN_PLAYBACK_PROBE  = true,            -- false = no audio
  PROBE_FILE          = nil,             -- nil = first file that loaded OK in the sweep
  PROBE_GAIN_DB       = -20,             -- gain while probing (keep it polite)
  BASELINE_SECONDS    = 2,               -- meter samples with nothing playing
  PLAY_SECONDS        = 20,              -- play this long before fading
  FADE_SECONDS        = 4,               -- fade-to-floor duration
  FADE_STEP_SECONDS   = 0.05,            -- gain update interval during the fade
  SAMPLE_INTERVAL     = 0.25,            -- seconds between samples
  SILENCE_DBFS        = -50,             -- only used for "meter rose above / fell below" timings

  RUN_FINGERPRINT     = true,            -- section F (reads files, no sound)
  FINGERPRINT_FILES   = 20,              -- audio files from the walk to fingerprint and time
  FINGERPRINT_CHUNK   = 65536,           -- radio.lua's K.fingerprint_chunk: the first, middle and last this many bytes

  RESTORE_PLAYER      = true,            -- put directory/filename/gain/loop back afterwards
  WRITE_DUMP_FILE     = true,
}

AUDIO_EXTENSIONS = { mp3 = true, wav = true, flac = true, m4a = true, aac = true, ogg = true,
                     aif = true, aiff = true, wma = true, opus = true }

-------------------------------------------------------------------------------
-- OUTPUT HELPERS
-------------------------------------------------------------------------------
LINES = {}   -- every printed line is also kept for the dump file

local function out(s)
  s = tostring(s)
  print(s)
  LINES[#LINES + 1] = s
end

local function outf(fmt, ...) out(string.format(fmt, ...)) end

local function hr(title)
  out("")
  out("----- " .. title .. " -----")
end

-- make control characters visible so we can see delimiters
local function esc(s)
  if s == nil then return "<nil>" end
  s = tostring(s)
  s = s:gsub("\\", "\\\\")
  s = s:gsub("\r", "\\r")
  s = s:gsub("\n", "\\n")
  s = s:gsub("\t", "\\t")
  return s
end

local function num(v, fmt)
  if type(v) == "number" then return string.format(fmt or "%.2f", v) end
  return esc(v)
end

local function b2s(b)
  if b == nil then return "?" end
  return b and "1" or "0"
end

local function utcNow() return os.date("!%Y-%m-%dT%H:%M:%SZ") end

local json
do
  local ok, lib = pcall(require, "json")
  if ok and type(lib) == "table" then
    json = lib
  else
    ok, lib = pcall(require, "rapidjson")
    if ok and type(lib) == "table" then json = lib end
  end
end

local function tojson(v)
  if not json then return "<json library unavailable>" end
  local ok, s = pcall(json.encode, v)
  if ok then return s end
  return "<json encode failed: " .. tostring(s) .. ">"
end

local function dumpTable(t, indent, depth)
  indent = indent or "  "
  depth = depth or 0
  if depth > 5 then out(indent .. "<max depth>") return end
  local keys = {}
  for k in pairs(t) do keys[#keys + 1] = k end
  table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
  for _, k in ipairs(keys) do
    local v = t[k]
    if type(v) == "table" then
      out(indent .. tostring(k) .. ":")
      dumpTable(v, indent .. "  ", depth + 1)
    else
      out(indent .. tostring(k) .. " = " .. esc(v))
    end
  end
end

-- copy only plain values so the result survives json.encode
local function plain(t, depth)
  depth = depth or 0
  local r = {}
  if depth > 6 then return r end
  for k, v in pairs(t) do
    local tv = type(v)
    if tv == "string" or tv == "number" or tv == "boolean" then
      r[k] = v
    elseif tv == "table" then
      r[k] = plain(v, depth + 1)
    end
  end
  return r
end

-------------------------------------------------------------------------------
-- SAFE CONTROL ACCESS (never let a missing control kill the probe)
-------------------------------------------------------------------------------
local function ctlGet(comp, name, field)
  local ok, v = pcall(function() return comp[name][field] end)
  if ok then return v end
  return nil, v
end

local function ctlSet(comp, name, field, value)
  local ok, err = pcall(function() comp[name][field] = value end)
  if not ok then outf("  ! set %s.%s failed: %s", tostring(name), tostring(field), tostring(err)) end
  return ok
end

local function ctlTrigger(comp, name)
  local ok, err = pcall(function() comp[name]:Trigger() end)
  if not ok then outf("  ! trigger %s failed: %s", tostring(name), tostring(err)) end
  return ok
end

-------------------------------------------------------------------------------
-- CLOCK / STATE
-------------------------------------------------------------------------------
HAVE_TIMER_NOW = (function()
  local ok, v = pcall(function() return Timer.Now() end)
  return ok and type(v) == "number"
end)()

P = { phase = "idle", ticks = 0 }   -- probe state (player, timings, results)
D = {}                              -- directory step state
S = { idx = 0, results = {} }       -- load sweep state

local function probeClock()
  if HAVE_TIMER_NOW and P.t0 then return Timer.Now() - P.t0 end
  return P.ticks * CFG.SAMPLE_INTERVAL
end

local function setPhase(name)
  P.phase = name
  P.tPhaseStart = probeClock()
end

local function elapsedInPhase() return probeClock() - (P.tPhaseStart or 0) end

local function probeLog(fmt, ...)
  outf("[%7.2fs] " .. fmt, probeClock(), ...)
end

local function fmtDelta(t, base)
  if type(t) == "number" and type(base) == "number" then return string.format("%.2fs", t - base) end
  return "n/a"
end

-------------------------------------------------------------------------------
-- A. STATIC DUMP
-------------------------------------------------------------------------------
local function sectionDesign()
  hr("DESIGN / PLATFORM")
  outf("probe version        = %s", PROBE_VERSION)
  outf("time (UTC)           = %s", utcNow())
  outf("time (Core local)    = %s   (os.time=%s)", os.date("%Y-%m-%d %H:%M:%S %Z"), tostring(os.time()))
  outf("Lua _VERSION         = %s", tostring(_VERSION))
  outf("Timer.Now()          = %s", HAVE_TIMER_NOW and "available" or "NOT available (using tick time)")
  outf("json library         = %s", json and "available" or "NOT available")
  outf("System.BuildVersion  = %s", esc(System and System.BuildVersion))
  outf("System.IsEmulating   = %s", esc(System and System.IsEmulating))

  local ok, st = pcall(Design.GetStatus)
  if ok and type(st) == "table" then
    out("Design.GetStatus():")
    dumpTable(st, "    ")
  else
    outf("Design.GetStatus failed: %s", tostring(st))
  end

  out("Text Controller Controls table:")
  local n = 0
  for k, v in pairs(Controls or {}) do
    n = n + 1
    outf("    %s (%s)", tostring(k), type(v))
  end
  if n == 0 then out("    (none)") end
end

local function sectionNetwork()
  hr("NETWORK INTERFACES")
  local ok, ifs = pcall(function() return Network.Interfaces() end)
  if ok and type(ifs) == "table" then
    for i, nic in ipairs(ifs) do
      outf("interface %d:", i)
      if type(nic) == "table" then dumpTable(nic, "    ") else outf("    %s", esc(nic)) end
    end
  else
    outf("Network.Interfaces failed: %s", tostring(ifs))
  end
end

COMPONENTS = {}
COMPONENTS_OK = false

local function sectionComponents()
  hr("ALL COMPONENTS IN DESIGN (name | type)")
  local ok, comps = pcall(Component.GetComponents)
  if not ok or type(comps) ~= "table" then
    outf("Component.GetComponents failed: %s", tostring(comps))
    return
  end
  COMPONENTS_OK = true
  table.sort(comps, function(a, b) return tostring(a.Name) < tostring(b.Name) end)
  for _, c in ipairs(comps) do
    COMPONENTS[tostring(c.Name)] = c
    outf("  %-40s | %s", tostring(c.Name), tostring(c.Type))
  end
end

local KNOWN_CTL_FIELDS = {
  Name = true, Type = true, Direction = true, Value = true, String = true, Position = true,
  MinValue = true, MaxValue = true, MinString = true, MaxString = true, Choices = true,
}

local function describeComponent(name)
  hr("COMPONENT DETAIL: " .. name)
  local info = COMPONENTS[name]
  if COMPONENTS_OK and not info then
    outf("!! '%s' NOT FOUND in design. Names that look similar:", name)
    local needle = name:lower():gsub("[^%w]", "")
    for n in pairs(COMPONENTS) do
      local hay = n:lower():gsub("[^%w]", "")
      if hay:find(needle:sub(1, 6), 1, true) or needle:find(hay:sub(1, 6), 1, true) then
        outf("     ? %s", n)
      end
    end
    return nil
  end

  if info then
    outf("Type = %s", tostring(info.Type))
    if type(info.Properties) == "table" then
      out("Properties:")
      for _, prop in ipairs(info.Properties) do
        if type(prop) == "table" then
          outf("    %-24s = %-12s (%s)", tostring(prop.Name), esc(prop.Value), tostring(prop.PrettyName))
        else
          outf("    %s", esc(prop))
        end
      end
    end
  end

  local ok, ctls = pcall(Component.GetControls, name)
  if not ok or type(ctls) ~= "table" then
    outf("Component.GetControls failed: %s", tostring(ctls))
  else
    outf("Controls (%d):", #ctls)
    for _, c in ipairs(ctls) do
      local range = ""
      if c.MinValue ~= nil or c.MaxValue ~= nil then
        range = string.format(" range=[%s..%s]", esc(c.MinValue), esc(c.MaxValue))
      end
      local srange = ""
      if (c.MinString ~= nil and c.MinString ~= "") or (c.MaxString ~= nil and c.MaxString ~= "") then
        srange = string.format(" strRange=[\"%s\"..\"%s\"]", esc(c.MinString), esc(c.MaxString))
      end
      local choices = ""
      if type(c.Choices) == "table" and #c.Choices > 0 then
        local cs = {}
        for _, ch in ipairs(c.Choices) do cs[#cs + 1] = esc(ch) end
        choices = " choices={" .. table.concat(cs, "|") .. "}"
      end
      outf("  %-22s %-13s %-10s val=%-14s str=%-28s pos=%s%s%s%s",
        tostring(c.Name), tostring(c.Type), tostring(c.Direction),
        esc(c.Value), '"' .. esc(c.String):sub(1, 26) .. '"', num(c.Position, "%.3f"),
        range, srange, choices)
      local extras = {}
      for k, v in pairs(c) do
        if not KNOWN_CTL_FIELDS[k] then extras[k] = v end
      end
      if next(extras) then dumpTable(extras, "        ") end
    end
  end

  local okc, comp = pcall(Component.New, name)
  if not okc then
    outf("Component.New failed: %s", tostring(comp))
    return nil
  end
  return comp
end

-------------------------------------------------------------------------------
-- B. MEDIA DUMP
-------------------------------------------------------------------------------
local function sectionMedia(comp, name)
  hr("MEDIA STRINGS: " .. name)
  if not comp then
    out("(component unavailable)")
    return
  end
  for _, c in ipairs({ "root", "directory", "directory.ui", "filename", "filename.ui", "playlist.file", "status" }) do
    local s, err = ctlGet(comp, c, "String")
    if s ~= nil then
      outf("  %-14s len=%-4d \"%s\"", c, #s, esc(s))
    else
      outf("  %-14s ERR %s", c, tostring(err))
    end
  end
  for _, c in ipairs({ "gain", "progress", "remaining", "loop", "playlist.repeat", "playlist.shuffle",
                       "play.on.startup", "playing", "paused", "stopped" }) do
    local v = ctlGet(comp, c, "Value")
    local b = ctlGet(comp, c, "Boolean")
    local s = ctlGet(comp, c, "String")
    outf("  %-16s Value=%-14s Boolean=%-5s String=\"%s\"", c, esc(v), tostring(b), esc(s))
  end
  local af = ctlGet(comp, "all.files", "String") or ""
  outf("  all.files        len=%d%s", #af, #af > 0 and ("  first 200: " .. esc(af:sub(1, 200))) or "  (Virtual control; empty from script)")
end

local function normDir(d)
  d = tostring(d or ""):gsub("^/+", "")
  if d ~= "" and d:sub(-1) ~= "/" then d = d .. "/" end
  return d
end

-- "media/" .. root .. directory, with duplicate slashes collapsed
local function mediaPathFor(comp)
  local root = ctlGet(comp, "root", "String") or ""
  local directory = ctlGet(comp, "directory", "String") or ""
  return (("media/" .. root .. "/" .. directory):gsub("/+", "/"))
end

local function isAudio(name)
  local e = tostring(name):match("%.([%w]+)$")
  return e ~= nil and AUDIO_EXTENSIONS[e:lower()] == true
end

-- dir.get wrapper: returns (entries sorted by name) or (nil, reason)
local function dirList(path)
  local ok, entries = pcall(function() return dir.get(path) end)
  if not ok then return nil, esc(entries) end
  if type(entries) ~= "table" then return nil, "(returned nil)" end
  table.sort(entries, function(a, b) return tostring(a.name) < tostring(b.name) end)
  return entries
end

local function listFiles(path)
  local entries, err = dirList(path)
  if not entries then return {}, err end
  local files = {}
  for _, e in ipairs(entries) do
    if e.type ~= "directory" then files[#files + 1] = tostring(e.name) end
  end
  return files
end

local function sectionDirGet(comp, name)
  hr("dir.get() PATH PROBES (derived from " .. name .. ")")
  local root = ctlGet(comp, "root", "String") or ""
  local directory = ctlGet(comp, "directory", "String") or ""
  local cands, seen = {}, {}
  local function add(p)
    if p and p ~= "" and not seen[p] then
      seen[p] = true
      cands[#cands + 1] = p
    end
  end
  add("media")
  add(CFG.MEDIA_ROOT)
  add(root)
  add("media/" .. root)
  if directory ~= "" then
    add(directory)
    add(root .. directory)
    add(mediaPathFor(comp))
  end
  for _, path in ipairs(cands) do
    local entries, err = dirList(path)
    if entries then
      outf("  %-45s OK   %d entries", path, #entries)
    else
      outf("  %-45s FAIL %s", path, tostring(err))
    end
  end
end

TREE = {}       -- flat list of every entry found in the walk
DIRS = {}       -- one record per directory: rel, files, audio, subdirs, bytes
TREE_LINES = 0

local function walk(path, rel, depth)
  local entries, err = dirList(path)
  if not entries then
    outf("  %-50s FAIL %s", rel == "" and "(root)" or rel, tostring(err))
    return
  end
  local d = { rel = rel, path = path, files = 0, audio = 0, subdirs = 0, bytes = 0 }
  DIRS[#DIRS + 1] = d
  for _, e in ipairs(entries) do
    local name = tostring(e.name)
    local size = tonumber(e.size) or 0
    local isDir = (e.type == "directory")
    TREE[#TREE + 1] = { rel = (rel == "" and name or rel .. "/" .. name), type = tostring(e.type), size = size }
    if isDir then
      d.subdirs = d.subdirs + 1
    else
      d.files = d.files + 1
      d.bytes = d.bytes + size
      if isAudio(name) then d.audio = d.audio + 1 end
    end
  end
  outf("  %-50s %d files (%d audio), %d subdirs, %.1f MB",
    rel == "" and "(root)" or rel .. "/", d.files, d.audio, d.subdirs, d.bytes / 1048576)
  for _, e in ipairs(entries) do
    if e.type ~= "directory" then
      TREE_LINES = TREE_LINES + 1
      if TREE_LINES <= CFG.MAX_TREE_LINES then
        outf("      %-70s %10s", esc(e.name), tostring(e.size))
      elseif TREE_LINES == CFG.MAX_TREE_LINES + 1 then
        out("      ... (more files; full tree in the dump file)")
      end
    end
  end
  if depth < CFG.WALK_MAX_DEPTH then
    for _, e in ipairs(entries) do
      if e.type == "directory" then
        local name = tostring(e.name)
        walk(path .. "/" .. name, rel == "" and name or rel .. "/" .. name, depth + 1)
      end
    end
  end
end

local function sectionTree()
  hr("MEDIA TREE (dir.get walk from " .. CFG.MEDIA_ROOT .. ")")
  walk(CFG.MEDIA_ROOT, "", 0)
  local totalFiles, totalAudio = 0, 0
  for _, d in ipairs(DIRS) do totalFiles = totalFiles + d.files; totalAudio = totalAudio + d.audio end
  outf("  total: %d directories, %d files, %d with audio extensions", #DIRS, totalFiles, totalAudio)
end

-------------------------------------------------------------------------------
-- DUMP FILE / END
-------------------------------------------------------------------------------
local function writeDump()
  if not CFG.WRITE_DUMP_FILE then return end
  pcall(function() dir.create("media/_probe") end)
  local stamp = os.date("!%Y%m%d_%H%M%SZ")
  local f, path, lastErr
  for _, candidate in ipairs({
    "media/_probe/probe_" .. stamp .. ".txt",
    "media/probe_" .. stamp .. ".txt",
    "media/Audio/probe_" .. stamp .. ".txt",
  }) do
    local ok, fh, err = pcall(io.open, candidate, "w")
    if ok and fh then f, path = fh, candidate break end
    lastErr = ok and err or fh
  end
  if not f then
    outf("(dump file not written anywhere: %s)", tostring(lastErr))
    return
  end
  f:write(table.concat(LINES, "\n"), "\n\n===== FULL MEDIA TREE =====\n")
  for _, e in ipairs(TREE) do f:write(e.type, "\t", e.size, "\t", e.rel, "\n") end
  f:write("\n===== JSON (machine readable) =====\n")
  f:write(tojson({
    probe_version = PROBE_VERSION,
    generated_utc = utcNow(),
    media_root = CFG.MEDIA_ROOT,
    tree = TREE,
    dirs = DIRS,
    sweep = plain(S.results),
    playback = plain(P),
    fingerprint = plain(FP),
  }))
  f:write("\n")
  f:close()
  outf("dump written to %s", path)
end

local function endProbe()
  writeDump()
  out("")
  out("===== PROBE END =====")
end

-------------------------------------------------------------------------------
-- EVENT LOGGERS (attached to the probe player for phases C-E)
-------------------------------------------------------------------------------
LOGGED_CTLS = { "status", "playing", "paused", "stopped", "filename", "filename.ui", "directory", "directory.ui" }

local function attachLoggers(p)
  P.statusValues = P.statusValues or {}
  for _, c in ipairs(LOGGED_CTLS) do
    local ok = pcall(function()
      p[c].EventHandler = function(ctl)
        local s = tostring(ctl.String or "")
        if c == "status" then
          P.statusValues[s] = true
          if P.phase == "sweep" and S.cur and not S.cur.tStatus then
            S.cur.tStatus = probeClock()
            S.cur.firstStatus = s
          end
        end
        -- keep the sweep quiet: filename churn is expected there
        if P.phase == "sweep" and (c == "filename" or c == "filename.ui") then return end
        probeLog("EVENT %-12s String=\"%s\" Value=%s Boolean=%s", c, esc(s), esc(ctl.Value), tostring(ctl.Boolean))
        if c == "playing" and ctl.Boolean and P.tPlay and not P.tPlayingEvent then P.tPlayingEvent = probeClock() end
      end
    end)
    if not ok then outf("  (could not attach EventHandler to %s)", c) end
  end
end

local function detachLoggers(p)
  for _, c in ipairs(LOGGED_CTLS) do pcall(function() p[c].EventHandler = nil end) end
  pcall(function() p["remaining"].EventHandler = nil end)
end

local function restorePlayer()
  if not (CFG.RESTORE_PLAYER and P.saved and P.player) then return end
  local p = P.player
  probeLog("--- RESTORE: directory=\"%s\" filename=\"%s\" gain=%s loop=%s",
    esc(P.saved.directory), esc(P.saved.filename), esc(P.saved.gain), tostring(P.saved.loop))
  if P.saved.directory ~= nil and P.saved.directory ~= (ctlGet(p, "directory", "String") or "") then
    ctlSet(p, "directory", "String", P.saved.directory)
  end
  if P.saved.filename ~= nil then ctlSet(p, "filename", "String", P.saved.filename) end
  if P.saved.gain ~= nil then ctlSet(p, "gain", "Value", P.saved.gain) end
  if P.saved.loop ~= nil then ctlSet(p, "loop", "Boolean", P.saved.loop) end
end

local function cleanup()
  if P.player then detachLoggers(P.player) end
  restorePlayer()
end

local function abortProbe(msg)
  outf("!! %s", msg)
  cleanup()
  endProbe()
end

-------------------------------------------------------------------------------
-- E. PLAYBACK PROBE
-------------------------------------------------------------------------------
local function sample()
  local p = P.player
  local st      = ctlGet(p, "status", "String") or "?"
  local playing = ctlGet(p, "playing", "Boolean")
  local paused  = ctlGet(p, "paused", "Boolean")
  local stopped = ctlGet(p, "stopped", "Boolean")
  local prog    = ctlGet(p, "progress", "Value")
  local rem     = ctlGet(p, "remaining", "Value")
  local gain    = ctlGet(p, "gain", "Value")
  local m       = ctlGet(Meter, P.meterCtl, "Value")
  local pk      = ctlGet(Meter, P.peakCtl, "Value")
  local rms     = ctlGet(Meter, P.rmsCtl, "Value")
  local mOther  = ctlGet(Meter, P.otherMeterCtl, "Value")

  probeLog("%-8s st=%-24s P=%s p=%s s=%s prog=%-8s rem=%-8s gain=%-7s m=%-7s pk=%-7s rms=%-7s other=%s",
    P.phase, '"' .. esc(st):sub(1, 22) .. '"', b2s(playing), b2s(paused), b2s(stopped),
    num(prog), num(rem), num(gain, "%.1f"),
    num(m, "%.1f"), num(pk, "%.1f"), num(rms, "%.1f"), num(mOther, "%.1f"))

  local t = probeClock()
  if type(mOther) == "number" then P.otherMax = math.max(P.otherMax or -200, mOther) end

  if P.phase == "baseline" and type(m) == "number" then
    P.baseMin = math.min(P.baseMin or m, m)
    P.baseMax = math.max(P.baseMax or m, m)
  end

  if P.phase == "play" or P.phase == "fade" then
    if type(m) == "number" then
      P.maxMeter = math.max(P.maxMeter or -200, m)
      if not P.tMeterUp and m > CFG.SILENCE_DBFS then P.tMeterUp = t end
      if P.phase == "fade" and not P.tMeterDown and m < CFG.SILENCE_DBFS then P.tMeterDown = t end
    end
    if type(pk) == "number" then P.maxPeak = math.max(P.maxPeak or -200, pk) end
    if type(prog) == "number" and type(rem) == "number" then
      P.duration = math.max(P.duration or 0, prog + rem)
    end
    if type(rem) == "number" then
      if P.lastRem and rem > P.lastRem then P.remIncreases = (P.remIncreases or 0) + 1 end
      P.lastRem = rem
    end
    if playing and not P.tPlayingSeen then P.tPlayingSeen = t end
  end

  if P.phase == "stopped" then
    if type(m) == "number" then P.meterAfterStop = m end
    P.statusAfterStop = st
    P.ledsAfterStop = string.format("playing=%s paused=%s stopped=%s", b2s(playing), b2s(paused), b2s(stopped))
  end
end

local function startPlay()
  local p = P.player
  probeLog("--- PLAY: loop=false, gain=%s, filename=\"%s\", then play:Trigger()", tostring(CFG.PROBE_GAIN_DB), esc(P.file))
  ctlSet(p, "loop", "Boolean", false)
  ctlSet(p, "gain", "Value", CFG.PROBE_GAIN_DB)
  ctlSet(p, "filename", "String", P.file)
  probeLog("    after set: filename.ui=\"%s\" status=\"%s\" prog=%s rem=%s (%s)",
    esc(ctlGet(p, "filename.ui", "String")), esc(ctlGet(p, "status", "String")),
    num(ctlGet(p, "progress", "Value")), num(ctlGet(p, "remaining", "Value")), esc(ctlGet(p, "remaining", "String")))
  P.tPlay = probeClock()
  ctlTrigger(p, "play")
  setPhase("play")
end

local function startFade()
  local steps = math.max(1, math.floor(CFG.FADE_SECONDS / CFG.FADE_STEP_SECONDS))
  probeLog("--- FADE: %s dB -> -100 dB over %ss in %d steps", tostring(CFG.PROBE_GAIN_DB), tostring(CFG.FADE_SECONDS), steps)
  setPhase("fade")
  P.fadeTicks = 0
  P.tFadeStart = probeClock()
  FadeTimer = Timer.New()
  FadeTimer.EventHandler = function()
    P.fadeTicks = P.fadeTicks + 1
    local frac = math.min(1, P.fadeTicks / steps)
    local g = CFG.PROBE_GAIN_DB + (-100 - CFG.PROBE_GAIN_DB) * frac
    ctlSet(P.player, "gain", "Value", g)
    if frac >= 1 then
      FadeTimer:Stop()
      P.tFadeEnd = probeClock()
      P.meterAtFadeEnd = ctlGet(Meter, P.meterCtl, "Value")
      probeLog("--- FADE complete: gain reads %s, meter %s",
        num(ctlGet(P.player, "gain", "Value"), "%.1f"), num(P.meterAtFadeEnd, "%.1f"))
    end
  end
  FadeTimer:Start(CFG.FADE_STEP_SECONDS)
end

local function stopPlayer()
  probeLog("--- STOP: stop:Trigger()")
  ctlTrigger(P.player, "stop")
  P.tStop = probeClock()
  setPhase("stopped")
end

local function finishProbe()
  ProbeTimer:Stop()
  cleanup()

  hr("PLAYBACK PROBE SUMMARY")
  local seen = {}
  for s in pairs(P.statusValues or {}) do seen[#seen + 1] = '"' .. esc(s) .. '"' end
  table.sort(seen)
  outf("  file                               = \"%s\"", esc(P.file))
  outf("  status strings seen (all phases)   = %s", #seen > 0 and table.concat(seen, ", ") or "(none)")
  outf("  baseline meter, nothing playing    = %s .. %s dBFS", num(P.baseMin, "%.1f"), num(P.baseMax, "%.1f"))
  outf("  play trigger -> playing=true       = %s (event) / %s (sample)",
    fmtDelta(P.tPlayingEvent, P.tPlay), fmtDelta(P.tPlayingSeen, P.tPlay))
  outf("  play trigger -> meter > %d dBFS    = %s", CFG.SILENCE_DBFS, fmtDelta(P.tMeterUp, P.tPlay))
  outf("  max meter / max peak while playing = %s / %s dBFS", num(P.maxMeter, "%.1f"), num(P.maxPeak, "%.1f"))
  outf("  implied duration (progress+remain) = %s s", num(P.duration))
  outf("  'remaining' increased %d times during play (expect 0)", P.remIncreases or 0)
  outf("  fade start -> meter < %d dBFS      = %s", CFG.SILENCE_DBFS, fmtDelta(P.tMeterDown, P.tFadeStart))
  outf("  meter at end of fade               = %s dBFS", num(P.meterAtFadeEnd, "%.1f"))
  outf("  ~2 s after stop: meter=%s status=\"%s\" %s",
    num(P.meterAfterStop, "%.1f"), esc(P.statusAfterStop), tostring(P.ledsAfterStop))
  outf("  other deck's meter max during run  = %s dBFS (should stay at the floor)", num(P.otherMax, "%.1f"))
  outf("  clock source                       = %s", HAVE_TIMER_NOW and "Timer.Now()" or ("tick count x " .. CFG.SAMPLE_INTERVAL .. "s"))

  endProbe()
end

ProbeTimer = Timer.New()
ProbeTimer.EventHandler = function()
  P.ticks = P.ticks + 1
  sample()
  local e = elapsedInPhase()
  if P.phase == "baseline" and e >= CFG.BASELINE_SECONDS then
    startPlay()
  elseif P.phase == "play" and e >= CFG.PLAY_SECONDS then
    startFade()
  elseif P.phase == "fade" and P.tFadeEnd and probeClock() >= P.tFadeEnd + 1 then
    stopPlayer()
  elseif P.phase == "stopped" and e >= 2 then
    finishProbe()
  end
end

local function beginPlaybackProbe()
  hr("PLAYBACK PROBE")
  if not CFG.RUN_PLAYBACK_PROBE then
    out("RUN_PLAYBACK_PROBE = false; skipping.")
    cleanup()
    endProbe()
    return
  end
  if not Meter then
    abortProbe("meter component unavailable; cannot run the playback probe")
    return
  end
  if not P.file then
    abortProbe("no file to play (sweep found nothing that loads and PROBE_FILE is not set)")
    return
  end

  local n = tostring(CFG.PROBE_PLAYER)
  local o = (CFG.PROBE_PLAYER == 2) and "1" or "2"
  P.meterCtl, P.peakCtl, P.rmsCtl, P.otherMeterCtl = "meter." .. n, "peak." .. n, "rms." .. n, "meter." .. o

  outf("  player=%s  file=\"%s\"  meter=%s  (other deck watched on %s)", P.pname, esc(P.file), P.meterCtl, P.otherMeterCtl)
  outf("  timeline: baseline %ss -> play %ss -> fade %ss -> stop (+2 s)   sample every %ss",
    tostring(CFG.BASELINE_SECONDS), tostring(CFG.PLAY_SECONDS), tostring(CFG.FADE_SECONDS), tostring(CFG.SAMPLE_INTERVAL))
  out("  columns: P=playing LED  p=paused LED  s=stopped LED  m/pk/rms=this deck's meter  other=other deck's meter")

  ctlTrigger(P.player, "stop")   -- make sure the deck is quiet before we baseline
  P.ticks = 0
  setPhase("baseline")
  ProbeTimer:Start(CFG.SAMPLE_INTERVAL)
end

-------------------------------------------------------------------------------
-- D. LOAD SWEEP (silent): learn which files load, and their durations
-------------------------------------------------------------------------------
local function choosePlaybackFile()
  if CFG.PROBE_FILE then P.file = CFG.PROBE_FILE return end
  for _, r in ipairs(S.results) do
    if r.ok then P.file = r.name return end
  end
  P.file = P.files and P.files[1] or nil
end

local function sweepDone()
  pcall(function() P.player["remaining"].EventHandler = nil end)
  local okCount = 0
  for _, r in ipairs(S.results) do if r.ok then okCount = okCount + 1 end end
  outf("  sweep complete: %d of %d files loaded OK", okCount, #S.results)
  choosePlaybackFile()
  beginPlaybackProbe()
end

local function sweepRecord()
  local r = S.cur
  local p = P.player
  r.status      = ctlGet(p, "status", "String") or ""
  r.remaining   = ctlGet(p, "remaining", "Value")
  r.progress    = ctlGet(p, "progress", "Value")
  r.filename_ui = ctlGet(p, "filename.ui", "String") or ""
  r.ok = r.status:sub(1, 2):upper() == "OK"
  if type(r.remaining) == "number" and (r.progress or 0) == 0 and r.remaining > 0 then
    r.duration = r.remaining
  end
  local evt = r.tStatus or r.tRemaining
  if evt then r.load_s = evt - r.tSet end
  if r.name == "" then
    outf("  %2s  %-4s %9s  %6s  %-28s %s", "0", r.ok and "OK" or "-", "-", r.load_s and string.format("%.2fs", r.load_s) or "n/a",
      '"' .. esc(r.status):sub(1, 26) .. '"', "(no file: filename cleared)")
    return
  end
  outf("  %2d  %-4s %9s  %6s  %-28s %s", S.idx, r.ok and "OK" or "FAIL",
    r.duration and string.format("%.2fs", r.duration) or "-",
    r.load_s and string.format("%.2fs", r.load_s) or "n/a",
    '"' .. esc(r.status):sub(1, 26) .. '"', esc(r.name))
  S.results[#S.results + 1] = r
end

local function sweepNext()
  S.idx = S.idx + 1
  if S.idx > #P.files or S.idx > CFG.LOAD_SWEEP_MAX then
    sweepDone()
    return
  end
  S.cur = { name = P.files[S.idx], tSet = probeClock() }
  ctlSet(P.player, "filename", "String", S.cur.name)
  SweepTimer:Start(CFG.LOAD_SETTLE_SECONDS)
end

SweepTimer = Timer.New()
SweepTimer.EventHandler = function()
  SweepTimer:Stop()
  sweepRecord()
  sweepNext()
end

local function beginLoadSweep()
  hr("LOAD SWEEP (silent; " .. tostring(CFG.LOAD_SETTLE_SECONDS) .. "s per file)")
  setPhase("sweep")
  pcall(function()
    P.player["remaining"].EventHandler = function()
      if S.cur and not S.cur.tRemaining then S.cur.tRemaining = probeClock() end
    end
  end)
  out("   #  ok    duration    load  status                       file")
  -- step 0: clear the filename so every load below is a real load, and see what status says with no file
  S.idx = 0
  S.cur = { name = "", tSet = probeClock() }
  ctlSet(P.player, "filename", "String", "")
  SweepTimer:Start(CFG.LOAD_SETTLE_SECONDS)
end

-------------------------------------------------------------------------------
-- C. DIRECTORY STEP
-------------------------------------------------------------------------------
local function afterDirectory()
  local directory = ctlGet(P.player, "directory", "String") or ""
  local path = mediaPathFor(P.player)
  if D.tSet then
    outf("  directory now \"%s\" (directory.ui \"%s\"), %.2fs after set",
      esc(directory), esc(ctlGet(P.player, "directory.ui", "String")), probeClock() - D.tSet)
  end
  local files, err = listFiles(path)
  outf("  dir.get(\"%s\") -> %d files%s", path, #files, err and ("  (" .. tostring(err) .. ")") or "")
  P.files = files
  P.mediaPath = path
  if #files == 0 then
    abortProbe("no files found in that directory; set PROBE_DIRECTORY to a folder with audio")
    return
  end
  if CFG.RUN_LOAD_SWEEP then
    beginLoadSweep()
  else
    P.file = CFG.PROBE_FILE or files[1]
    beginPlaybackProbe()
  end
end

local function beginDirectoryStep()
  hr("DIRECTORY STEP")
  P.pname  = (CFG.PROBE_PLAYER == 2) and CFG.PLAYER_2 or CFG.PLAYER_1
  P.player = (CFG.PROBE_PLAYER == 2) and Player2 or Player1
  if not P.player then
    abortProbe("probe player component unavailable")
    return
  end
  P.saved = {
    gain      = ctlGet(P.player, "gain", "Value"),
    loop      = ctlGet(P.player, "loop", "Boolean"),
    filename  = ctlGet(P.player, "filename", "String"),
    directory = ctlGet(P.player, "directory", "String"),
  }
  P.t0 = HAVE_TIMER_NOW and Timer.Now() or 0
  attachLoggers(P.player)
  setPhase("directory")

  local cur = ctlGet(P.player, "directory", "String") or ""
  if cur ~= "" then
    outf("  %s already has directory \"%s\"; keeping it", P.pname, esc(cur))
    afterDirectory()
    return
  end
  local want = normDir(CFG.PROBE_DIRECTORY)
  if want == "" then
    abortProbe("player has no directory and PROBE_DIRECTORY is not set")
    return
  end
  outf("  %s has no directory; setting directory.String = \"%s\" and waiting %ss", P.pname, esc(want), tostring(CFG.DIR_SETTLE_SECONDS))
  D.tSet = probeClock()
  ctlSet(P.player, "directory", "String", want)
  DirTimer = Timer.New()
  DirTimer.EventHandler = function()
    DirTimer:Stop()
    afterDirectory()
  end
  DirTimer:Start(CFG.DIR_SETTLE_SECONDS)
end

-------------------------------------------------------------------------------
-- F. FINGERPRINT (radio.lua 2.4.0 knows a file by the sha1 of its size in bytes as decimal text, a newline, its
--    first, middle (from floor(size / 2) - chunk / 2) and last FINGERPRINT_CHUNK bytes; the whole file when no longer
--    than the three)
-------------------------------------------------------------------------------
FP = { files = {} }

local SHA1_ABC = "a9993e364706816aba3e25717850c26c9cd0d89d"

local function hexOf(raw) return (raw:gsub(".", function(c) return string.format("%02x", c:byte()) end)) end

-- Crypto.Digest's sha1 as lowercase hex (raw 20 bytes or 40 hex characters), or nil
local function sha1Hex(data)
  local d = Crypto.Digest("sha1", data)
  if type(d) ~= "string" then return nil end
  return (#d == 20 and hexOf(d) or d:lower())
end

-- what radio.lua does for one file: returns fingerprint (nil without Crypto), size
local function fingerprintFile(path)
  local f = assert(io.open(path, "rb"))
  local ok, fp, size = pcall(function()
    local n = CFG.FINGERPRINT_CHUNK
    local size = f:seek("end")
    if type(size) ~= "number" then error("seek(\"end\") gave " .. tostring(size)) end
    local function at(pos, len)
      assert(f:seek(pos < 0 and "end" or "set", pos), "seek to " .. pos .. " failed")
      return f:read(len) or ""
    end
    local data
    if size <= 3 * n then
      data = at(0, size)
    else
      data = at(0, n) .. at(size // 2 - n // 2, n) .. at(-n, n)
    end
    if #data ~= math.min(size, 3 * n) then error("short read: " .. #data .. " bytes") end
    return FP.crypto and sha1Hex(string.format("%d\n", size) .. data) or nil, size
  end)
  f:close()
  if not ok then error(fp, 0) end
  return fp, size
end

local function sectionFingerprint()
  if not CFG.RUN_FINGERPRINT then return end
  hr("FINGERPRINT (Crypto.Digest sha1 + io seek, for radio.lua 2.4.0)")
  FP.crypto = type(Crypto) == "table" and type(Crypto.Digest) == "function"
  outf("  Crypto.Digest          = %s", FP.crypto and "present" or "MISSING")
  if FP.crypto then
    local ok, d = pcall(Crypto.Digest, "sha1", "abc")
    if not ok then FP.form = "error: " .. esc(d)
    elseif type(d) ~= "string" then FP.form = "a " .. type(d)
    elseif #d == 20 then FP.form = "raw (20 bytes)"
    elseif #d == 40 then FP.form = "hex (40 characters)"
    else FP.form = "unknown (" .. #d .. " bytes)" end
    local hex = ok and type(d) == "string" and (#d == 20 and hexOf(d) or d:lower()) or nil
    FP.correct = hex == SHA1_ABC
    outf("  sha1(\"abc\") form      = %s", FP.form)
    outf("  sha1(\"abc\")           = %s (%s)", esc(hex), FP.correct and "correct" or ("WRONG, expected " .. SHA1_ABC))
    FP.crypto = FP.correct
  end
  local clock = HAVE_TIMER_NOW and Timer.Now or os.clock
  local total, cpuTotal, n, failed = 0, 0, 0, 0
  for _, e in ipairs(TREE) do
    if n >= CFG.FINGERPRINT_FILES then break end
    if e.type ~= "directory" and isAudio(e.rel) then
      n = n + 1
      local path = CFG.MEDIA_ROOT .. "/" .. e.rel
      local t0, c0 = clock(), os.clock()
      local ok, fp, size = pcall(fingerprintFile, path)
      local ms, cpu = (clock() - t0) * 1000, (os.clock() - c0) * 1000
      if ok then total, cpuTotal = total + ms, cpuTotal + cpu else failed = failed + 1 end
      FP.files[#FP.files + 1] = { rel = e.rel, ms = ms, cpu_ms = cpu, size = ok and size or nil, fp = ok and fp or nil,
                                  error = not ok and tostring(fp) or nil }
      outf("  %8.1f ms (CPU %6.1f)  %-40s %s", ms, cpu, ok and (fp or ("size " .. tostring(size) .. ", read; no sha1"))
        or ("FAIL " .. esc(fp)), esc(e.rel))
    end
  end
  if n == 0 then out("  no audio file found by the walk: io seek not tested") end
  FP.tested, FP.failed, FP.avg_ms = n, failed, n > failed and total / (n - failed) or nil
  if FP.avg_ms then
    outf("  average %.1f ms per file (CPU %.1f ms; %d read, %d failed); radio.lua does one file every 0.5 s", FP.avg_ms,
      cpuTotal / (n - failed), n - failed, failed)
  end
  FP.on = FP.crypto and n > 0 and failed < n
  outf("  VERDICT: radio.lua 2.4.0 will %s", FP.on and "fingerprint files (files keep their figures when renamed)"
    or "NOT fingerprint files on this Core (files stay known by name + size)")
end

-------------------------------------------------------------------------------
-- MAIN
-------------------------------------------------------------------------------
out("===== PROBE START (v" .. PROBE_VERSION .. ") =====")
sectionDesign()
sectionNetwork()
sectionComponents()
Player1 = describeComponent(CFG.PLAYER_1)
Player2 = describeComponent(CFG.PLAYER_2)
Meter   = describeComponent(CFG.METER)
sectionMedia(Player1, CFG.PLAYER_1)
sectionMedia(Player2, CFG.PLAYER_2)
sectionDirGet(Player1 or Player2, Player1 and CFG.PLAYER_1 or CFG.PLAYER_2)
sectionTree()
sectionFingerprint()
beginDirectoryStep()
