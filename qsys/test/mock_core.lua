--[[---------------------------------------------------------------------------
  MockCore :: an offline stand-in for the Q-SYS Lua runtime
  File:    qsys/test/mock_core.lua
  Version: 0.6.0

  Enough of Component / Timer / dir / Design / Network / Controls / json / io
  to run radio.lua (and probe.lua) under plain Lua 5.3 with a simulated
  clock. Behaviour is modelled on what probe 0.1.1 measured on a Core 8 Flex:
    - filename loads complete asynchronously after ~0.08 s
    - status only fires its EventHandler when the string changes
    - after a failed load `remaining` keeps the previous file's value
    - progress / remaining tick in whole seconds; stop rewinds to 0
    - the meter is post-gain, floors at -120, releases at ~60 dB/s; rms.N reads
      the file's crest (default 3 dB) under peak.N
    - `playing` goes false at end of file (and `stopped` true)
    - files load from root + directory (MockCore.dirs is relative to Audio/)
  Any number of players, meters and mixers by name (spec.players: the wiring; MockCore.stations / MockCore.zones
  build two-player stations and one-player zones),
  analysis players (a player + meter channel routed nowhere: MockCore.analyzers),
  and a fake server for the 2.1 Core protocol (batched sync, library in parts,
  the per-Core analysis list: MockCore.analysisQueue).
  A file entry may say hang = true (its load never completes) or noEnd = true
  (the player never reports its end: it plays on, silent).

  Usage (see run_mock.py / scenario_basic.lua):
    dofile("qsys/test/mock_core.lua")
    MockCore.setup{ dirs = { ["Test/Tuning/"] = { {name="a.flac", duration=30, lead=1, trail=0.5, level=-10, fade=0, crest=3}, ... } },
                    controls = { inputs = 3, outputs = 6 } }
    MockCore.loadRadio{ SERVER_URL = "http://radio-server:5410" }   -- radio.lua, CFG changed before it boots
    MockCore.at(120, function() MockCore.press(1) end)   -- operator skip at t=120
    MockCore.run(600)
-----------------------------------------------------------------------------]]

MockCore = { clock = 0, timers = {}, pending = {}, actions = {}, players = {}, meters = {}, mixers = {}, dirs = {}, log = {} }

-------------------------------------------------------------------------------
-- minimal JSON (objects, arrays, strings, numbers, booleans, null)
-------------------------------------------------------------------------------
local json = {}
local function isArray(t)
  local n = 0
  for k in pairs(t) do
    if type(k) ~= "number" then return false end
    n = n + 1
  end
  return n == #t
end
function json.encode(v)
  local tv = type(v)
  if tv == "nil" then return "null"
  elseif tv == "boolean" then return v and "true" or "false"
  elseif tv == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    return (math.type(v) == "integer") and tostring(v) or string.format("%.14g", v)
  elseif tv == "string" then
    return '"' .. v:gsub('[%c"\\]', function(c)
      local map = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
      return map[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
  elseif tv == "table" then
    local parts = {}
    if next(v) == nil then return "{}" end
    if isArray(v) then
      for _, x in ipairs(v) do parts[#parts + 1] = json.encode(x) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    for _, k in ipairs(keys) do parts[#parts + 1] = json.encode(k) .. ":" .. json.encode(v[k]) end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  return "null"
end
function json.decode(s)
  local pos = 1
  local function ws() pos = s:find("%S", pos) or (#s + 1) end
  local value
  local function str()
    local out = {}
    pos = pos + 1
    while true do
      local c = s:sub(pos, pos)
      if c == "" then error("unterminated string") end
      if c == '"' then pos = pos + 1 return table.concat(out) end
      if c == "\\" then
        local n = s:sub(pos + 1, pos + 1)
        local map = { n = "\n", r = "\r", t = "\t", ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f" }
        if n == "u" then out[#out + 1] = utf8.char(tonumber(s:sub(pos + 2, pos + 5), 16)); pos = pos + 6
        else out[#out + 1] = map[n] or n; pos = pos + 2 end
      else out[#out + 1] = c; pos = pos + 1 end
    end
  end
  value = function()
    ws()
    local c = s:sub(pos, pos)
    if c == "{" then
      local t = {}
      pos = pos + 1
      ws()
      if s:sub(pos, pos) == "}" then pos = pos + 1 return t end
      while true do
        ws()
        local k = str()
        ws()
        pos = pos + 1 -- ':'
        t[k] = value()
        ws()
        local d = s:sub(pos, pos)
        pos = pos + 1
        if d == "}" then return t end
      end
    elseif c == "[" then
      local t = {}
      pos = pos + 1
      ws()
      if s:sub(pos, pos) == "]" then pos = pos + 1 return t end
      while true do
        t[#t + 1] = value()
        ws()
        local d = s:sub(pos, pos)
        pos = pos + 1
        if d == "]" then return t end
      end
    elseif c == '"' then return str()
    elseif s:sub(pos, pos + 3) == "true" then pos = pos + 4 return true
    elseif s:sub(pos, pos + 4) == "false" then pos = pos + 5 return false
    elseif s:sub(pos, pos + 3) == "null" then pos = pos + 4 return nil
    else
      local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
      pos = pos + #num
      return tonumber(num)
    end
  end
  return value()
end
package.loaded["json"] = json

-------------------------------------------------------------------------------
-- controls
-------------------------------------------------------------------------------
local function mkctl(spec)
  spec = spec or {}
  local c = { String = spec.s or "", Value = spec.v or 0, Boolean = spec.b or false, Position = 0 }
  function c:Trigger() if self.onTrig then self.onTrig() end end
  return c
end
local function fire(c) if c.EventHandler then c.EventHandler(c) end end
local function setBool(c, b) if c.Boolean ~= b then c.Boolean = b; c.String = tostring(b); c.Value = b and 1 or 0; fire(c) end end
local function setStr(c, s) if c.String ~= s then c.String = s; fire(c) end end
local function setVal(c, v) if c.Value ~= v then c.Value = v; fire(c) end end

-------------------------------------------------------------------------------
-- player
-------------------------------------------------------------------------------
local Player = {}
Player.__index = Player

local function mkPlayer(name, meterCtl)
  local p = setmetatable({ name = name, pos = 0, playing = false, file = nil, meterCtl = meterCtl, meterVal = -120 }, Player)
  p.ctl = {}
  for _, k in ipairs({ "all.files", "root", "directory", "directory.ui", "filename", "filename.ui", "playlist.file", "status" }) do
    p.ctl[k] = mkctl{}
  end
  p.ctl["root"].String = "Audio/"
  for _, k in ipairs({ "gain", "progress", "remaining", "locate" }) do p.ctl[k] = mkctl{ v = 0 } end
  for _, k in ipairs({ "loop", "mute", "playlist.repeat", "playlist.shuffle", "play.on.startup", "playing", "paused", "stopped" }) do
    p.ctl[k] = mkctl{}
  end
  p.ctl["stopped"].Boolean = true
  for _, k in ipairs({ "play", "stop", "pause" }) do p.ctl[k] = mkctl{} end
  p.ctl["play"].onTrig = function() p:doPlay() end
  p.ctl["stop"].onTrig = function() p:doStop() end
  -- the component object seen by scripts: control access by name, filename writes trigger loads
  p.comp = setmetatable({}, {
    __index = function(_, k)
      local c = p.ctl[k]
      if not c then error("no control '" .. tostring(k) .. "' on " .. name) end
      if k == "filename" then
        return setmetatable({}, {
          __index = function(_, f) return c[f] end,
          __newindex = function(_, f, v)
            if f == "String" then p:requestLoad(v) else c[f] = v end
          end,
        })
      elseif k == "locate" then       -- write-only seek (seconds) on the Core
        return setmetatable({}, {
          __index = function(_, f) return c[f] end,
          __newindex = function(_, f, v)
            c[f] = v
            if f == "Value" and p.file then
              p.pos = math.max(0, math.min(v, p.file.duration))
              MockCore.note(p.name .. string.format(" LOCATE %.1f", p.pos))
            end
          end,
        })
      elseif k == "directory" then
        return setmetatable({}, {
          __index = function(_, f) return c[f] end,
          __newindex = function(_, f, v)
            c[f] = v
            if f == "String" then p.ctl["directory.ui"].String = v end
          end,
        })
      end
      return c
    end,
  })
  return p
end

-- the folder a player's root + directory names, relative to Audio/ (MockCore.dirs' keys); nil = outside Audio/
local function audioDir(root, dirRel)
  local full = ((root or "") .. "/" .. (dirRel or "")):gsub("/+", "/"):gsub("^/", "")
  return full:match("^Audio/(.*)$")
end

function Player:requestLoad(fname)
  self.ctl["filename"].String = fname
  local dirRel = audioDir(self.ctl["root"].String, self.ctl["directory"].String)
  local entry = dirRel and MockCore.lookup(dirRel, fname)
  if entry and entry.hang then MockCore.note(self.name .. " LOAD HANGS " .. fname) return end
  local at = MockCore.clock + 0.08
  MockCore.pending[#MockCore.pending + 1] = { at = at, fn = function()
    setStr(self.ctl["filename.ui"], fname)
    fire(self.ctl["filename"])
    if fname == "" then
      self.file = nil
    elseif not entry or entry.unsupported then
      self.file = nil
      setStr(self.ctl["status"], entry and entry.status or "Unsupported file format")
      -- remaining keeps the previous value (as observed)
    else
      self.file = entry
      self.pos = 0
      setStr(self.ctl["status"], entry.statusOk or "OK: 44.1kHz, stereo")
      setVal(self.ctl["progress"], 0)
      setVal(self.ctl["remaining"], entry.duration)
    end
  end }
end

function Player:doPlay()
  if not self.file then return end
  self.playing = true
  self.pos = 0
  setBool(self.ctl["stopped"], false)
  setBool(self.ctl["playing"], true)
  MockCore.note(self.name .. " PLAY " .. self.file.name)
end

function Player:doStop()
  local was = self.playing
  self.playing = false
  self.pos = 0
  if self.file then
    setVal(self.ctl["progress"], 0)
    setVal(self.ctl["remaining"], self.file.duration)
  end
  setBool(self.ctl["playing"], false)
  setBool(self.ctl["stopped"], true)
  if was then MockCore.note(self.name .. " STOP") end
end

function Player:advance(dt)
  local program = -120
  if self.playing and self.file then
    self.pos = self.pos + dt
    local f = self.file
    if self.pos >= f.duration and f.noEnd then
      self.pos = f.duration              -- a stuck player: plays on, silent, never reports the end
    elseif self.pos >= f.duration then
      MockCore.note(self.name .. " EOF " .. f.name)
      self.playing = false
      self.pos = 0
      setVal(self.ctl["progress"], 0)
      setVal(self.ctl["remaining"], f.duration)
      setBool(self.ctl["playing"], false)
      setBool(self.ctl["stopped"], true)
    else
      setVal(self.ctl["progress"], math.floor(self.pos))
      setVal(self.ctl["remaining"], f.duration - math.floor(self.pos))
      local lead, trail = f.lead or 0, f.trail or 0
      local stop, fade = f.duration - trail, f.fade or 0
      if self.pos >= lead and self.pos <= stop then
        program = (f.level or -10) + 2 * math.sin(self.pos * 3)
        if fade > 0 and self.pos > stop - fade then program = program - 40 * (self.pos - stop + fade) / fade end
      end
    end
  end
  local target = (program <= -119) and -120 or math.max(-120, program + self.ctl["gain"].Value)
  if target >= self.meterVal then self.meterVal = target
  else self.meterVal = math.max(target, self.meterVal - 60 * dt) end
  local crest = self.file and self.file.crest or 3
  for i, c in ipairs(self.meterCtl) do c.Value = (i == 3 and self.meterVal > -117) and self.meterVal - crest or self.meterVal end
end

-------------------------------------------------------------------------------
-- environment
-------------------------------------------------------------------------------
function MockCore.note(s)
  MockCore.log[#MockCore.log + 1] = { t = MockCore.clock, msg = s }
end

function MockCore.lookup(dirRel, fname)
  local d = MockCore.dirs[dirRel]
  if not d then return nil end
  for _, e in ipairs(d) do if e.name == fname then return e end end
  return nil
end

-- n analysis players added to wiring (routed nowhere): Analysis_Player, Analysis_Player2, ... on AnalysisMeter 1, 2, ...
-- Returns the CFG.ANALYZERS entries (n = 1: radio.lua's default)
function MockCore.analyzers(n, wiring)
  local cfg = {}
  for i = 1, n do
    local name = "Analysis_Player" .. (i > 1 and i or "")
    wiring[#wiring + 1] = { name = name, label = "X" .. i, meter = "AnalysisMeter", channel = i }
    cfg[i] = { player = name, meter = "AnalysisMeter", meter_idx = i }
  end
  return cfg
end

-- the classic design (radio.lua's default CFG): two players on PlayerMeter 1 / 2 into the RadioOut mixer
-- (the station's output; radio.lua never touches it), and an analysis player (noAnalyzer: none)
local function classicWiring(noAnalyzer)
  local w = { { name = "Audio_Player23", label = "A", meter = "PlayerMeter", channel = 1, mixer = "RadioOut", input = 1 },
              { name = "Audio_Player24", label = "B", meter = "PlayerMeter", channel = 2, mixer = "RadioOut", input = 2 } }
  if not noAnalyzer then MockCore.analyzers(1, w) end
  return w
end

-- n two-player stations: ids <prefix>1.. (or opts.ids), players <id>_A / <id>_B on meter <id>_Meter 1 / 2 into
-- their output mixer <id>_Mix. Returns the wiring (spec.players) and the CFG.STATIONS entries.
function MockCore.stations(n, opts)
  opts = opts or {}
  local wiring, cfg = {}, {}
  for i = 1, n do
    local id = opts.ids and opts.ids[i] or ((opts.prefix or "S") .. i)
    for k, deck in ipairs({ "A", "B" }) do
      wiring[#wiring + 1] = { name = id .. "_" .. deck, label = id .. deck, meter = id .. "_Meter", channel = k, mixer = id .. "_Mix", input = k }
    end
    cfg[i] = { id = id, players = { id .. "_A", id .. "_B" }, meter = id .. "_Meter", meters = { 1, 2 } }
  end
  return wiring, cfg
end

-- n one-player stations (background-music zones): ids <prefix>1.. (or opts.ids; prefix "Z"), player <id>_P (label <id>)
-- on channel opts.first (1), opts.first + 1, ... of one shared meter opts.meter ("ZoneMeter"), no mixer. Returns the
-- wiring (spec.players) and the CFG.STATIONS entries.
function MockCore.zones(n, opts)
  opts = opts or {}
  local wiring, cfg, meter, first = {}, {}, opts.meter or "ZoneMeter", opts.first or 1
  for i = 1, n do
    local id = opts.ids and opts.ids[i] or ((opts.prefix or "Z") .. i)
    wiring[i] = { name = id .. "_P", label = id, meter = meter, channel = first + i - 1 }
    cfg[i] = { id = id, players = { id .. "_P" }, meter = meter, meters = { first + i - 1 } }
  end
  return wiring, cfg
end

-- one player: it feeds a meter channel (meter.N / peak.N / rms.N follow it) and, optionally, a mixer input
-- (input.N.mute.1 / gain.1 / delay.1); w = { name, label, meter, channel, mixer, input }
function MockCore.wire(w)
  local meter = MockCore.meters[w.meter]
  if not meter then
    meter = { ["response.time"] = mkctl{ v = 0.3 }, ["select"] = mkctl{ s = "Peak" } }
    MockCore.meters[w.meter] = meter
  end
  local set = {}
  for _, k in ipairs({ "meter." .. w.channel, "peak." .. w.channel, "rms." .. w.channel }) do
    meter[k] = mkctl{ v = -120 }
    set[#set + 1] = meter[k]
  end
  MockCore.players[w.name] = mkPlayer(w.label or w.name, set)
  if w.mixer then
    local mixer = MockCore.mixers[w.mixer] or {}
    MockCore.mixers[w.mixer] = mixer
    local i = w.input
    mixer["input." .. i .. ".mute.1"], mixer["input." .. i .. ".gain.1"], mixer["input." .. i .. ".delay.1"] = mkctl{ b = false }, mkctl{}, mkctl{}
  end
end

function MockCore.setup(spec)
  MockCore.dirs = spec.dirs or {}
  MockCore.clock = 0
  MockCore.timers, MockCore.pending, MockCore.actions, MockCore.log = {}, {}, {}, {}
  if spec.seed then
    -- keep runs repeatable: radio.lua's own math.randomseed(os.time()) re-applies the scenario seed
    local seed, reseed = spec.seed, MockCore.realRandomseed or math.randomseed
    MockCore.realRandomseed = reseed
    reseed(seed)
    math.randomseed = function() reseed(seed) end
  end

  MockCore.players, MockCore.meters, MockCore.mixers = {}, {}, {}
  for _, w in ipairs(spec.players or classicWiring(spec.noAnalyzer)) do MockCore.wire(w) end
  MockCore.meter, MockCore.mixer = MockCore.meters["PlayerMeter"], MockCore.mixers["RadioOut"]

  Component = {
    New = function(n)
      if MockCore.players[n] then return MockCore.players[n].comp end
      if MockCore.meters[n] then return MockCore.meters[n] end
      if MockCore.mixers[n] then return MockCore.mixers[n] end
      error("component '" .. tostring(n) .. "' not found")
    end,
    GetComponents = function()
      local out = {}
      for n in pairs(MockCore.players) do out[#out + 1] = { Name = n, Type = "audio_file_player" } end
      for n in pairs(MockCore.meters) do out[#out + 1] = { Name = n, Type = "meter2" } end
      for n in pairs(MockCore.mixers) do out[#out + 1] = { Name = n, Type = "mixer" } end
      table.sort(out, function(a, b) return a.Name < b.Name end)
      return out
    end,
    GetControls = function() return {} end,
  }
  Timer = { Now = function() return MockCore.clock end }
  -- wall clock follows the simulated clock, so event times are realistic
  MockCore.epoch = MockCore.epoch or os.time()
  MockCore.realDate = MockCore.realDate or os.date
  os.date = function(fmt, when)
    return MockCore.realDate(fmt, when or (MockCore.epoch + math.floor(MockCore.clock)))
  end
  function Timer.New()
    local t = { running = false }
    function t:Start(iv) self.running = true; self.iv = iv; self.next = MockCore.clock + iv
      for _, x in ipairs(MockCore.timers) do if x == self then return end end
      MockCore.timers[#MockCore.timers + 1] = self
    end
    function t:Stop() self.running = false end
    return t
  end
  function Timer.CallAfter(fn, s) MockCore.at(MockCore.clock + s, fn) end
  -- HttpClient: requests are answered after 50 ms by MockCore.httpHandler(req) (default: the fake
  -- server, MockCore.serve), and every request is recorded in MockCore.http.
  MockCore.http = {}
  MockCore.serve()
  HttpClient = {
    Upload = function(req)
      MockCore.http[#MockCore.http + 1] = { at = MockCore.clock, req = req }
      MockCore.pending[#MockCore.pending + 1] = { at = MockCore.clock + 0.05, fn = function()
        local code, body, err = MockCore.httpHandler(req)
        req.EventHandler(req, code, body, err, {})
      end }
    end,
  }
  Design = { GetStatus = function() return { Platform = "MockCore", DesignName = "mock", IsRedundant = false } end }
  System = { BuildVersion = "mock", IsEmulating = true }
  Network = { Interfaces = function() return { { Interface = "LAN A", Address = "127.0.0.1" } } end }
  dir = {
    get = function(path)
      local rel = path:match("^media/Audio/(.*)$")
      if rel == nil then return nil end
      if rel == "" then
        local out = {}
        for d in pairs(MockCore.dirs) do out[#out + 1] = { name = d:gsub("/$", ""), size = 4096, type = "directory" } end
        return out
      end
      local entries = MockCore.dirs[rel] or MockCore.dirs[rel .. "/"]
      if not entries then return nil end
      local out = {}
      for _, e in ipairs(entries) do out[#out + 1] = { name = e.name, size = e.size or 1000000, type = "file" } end
      return out
    end,
    create = function(path) os.execute("mkdir -p '" .. path .. "'") end,
  }
  os.execute("mkdir -p media/_radio media/_probe")

  Controls = {}
  local ctrls = spec.controls or {}
  if ctrls.inputs then
    Controls.Inputs = {}
    for i = 1, ctrls.inputs do Controls.Inputs[i] = mkctl{} end
  end
  if ctrls.outputs then
    Controls.Outputs = {}
    for i = 1, ctrls.outputs do Controls.Outputs[i] = mkctl{} end
  end
end

-- radio.lua with CFG changed before it boots: patch = { KEY = value, ... } or function(CFG)
function MockCore.loadRadio(patch, path)
  local f = assert(io.open(path or "qsys/radio/radio.lua", "r"))
  local src = f:read("*a")
  f:close()
  local body, n = src:gsub("\nboot%(%)%s*$", "\nreturn boot\n")
  assert(n == 1, "radio.lua must end with boot()")
  local boot = assert(load(body, "@qsys/radio/radio.lua"))()
  if type(patch) == "function" then patch(CFG)
  elseif patch then for k, v in pairs(patch) do CFG[k] = v end end
  boot()
end

-------------------------------------------------------------------------------
-- a fake QSYSRadio server speaking the 2.1 Core protocol
--   h.sync(part, body, req) -> that station's reply entry (nil = { commands = {} })
--   h.core(body, req) -> more top-level reply fields (e.g. analyze, analyze_rev: MockCore.analysisQueue), or nil
--   h.library(station, items, body) -> the learned figures for a complete scan (nil = none sent)
--   h.down(req) -> code, body, err: fail the whole request (an outage), or nil
--   h.lose(body) -> true: this library part is lost (answered, not staged: its last part gets 409)
-- MockCore.libraries[station] = { items (the last complete scan), uploads, parts, conflicts, last (part body) }
-------------------------------------------------------------------------------
function MockCore.serve(h)
  h = h or {}
  MockCore.libraries = {}
  MockCore.httpHandler = function(req)
    if h.down then
      local code, body, err = h.down(req)
      if code then return code, body, err end
    end
    local body = json.decode(req.Data)
    if req.Url:match("/api/v1/core/sync$") then
      local out = {}
      for _, part in ipairs(body.stations or {}) do out[part.station] = (h.sync and h.sync(part, body, req)) or { commands = {} } end
      local reply = { stations = out }
      for k, v in pairs(h.core and h.core(body, req) or {}) do reply[k] = v end
      return 200, json.encode(reply)
    elseif req.Url:match("/api/v1/core/library$") then
      local lib = MockCore.libraries[body.station] or { items = {}, uploads = 0, parts = 0, conflicts = 0 }
      MockCore.libraries[body.station] = lib
      lib.parts, lib.last = lib.parts + 1, body
      local items = body.items or {}
      if body.scan ~= nil then
        if body.part == 1 or lib.scan ~= body.scan then lib.scan, lib.got, lib.staged = body.scan, {}, {} end
        local partReply = json.encode({ ok = true, scan = body.scan, part = body.part, parts = body.parts })
        if h.lose and h.lose(body) then return 200, partReply end
        lib.got[body.part] = true
        for _, it in ipairs(items) do lib.staged[#lib.staged + 1] = it end
        if body.part < body.parts then return 200, partReply end
        for p = 1, body.parts do
          if not lib.got[p] then
            lib.conflicts = lib.conflicts + 1
            return 409, json.encode({ error = "scan " .. body.scan .. ": part " .. p .. " of " .. body.parts .. " missing" })
          end
        end
        items = lib.staged
      end
      lib.items, lib.uploads, lib.staged, lib.got = items, lib.uploads + 1, nil, nil
      local learned = h.library and h.library(body.station, items, body)
      return 200, json.encode({ ok = true, items = #items, learned = learned })
    end
    return 404, ""
  end
end

-- the server's per-Core analysis job, faked: owed = { { station, key }, ... } in the order to measure. Up to
-- q.batch (20) at a time, sent when the Core's analyze_rev differs; a file leaves the list when its 'analysis'
-- event arrives (ok or not). q.results[station .. "|" .. key] = that event; q.lists = lists sent; q.bodies = sync
-- bodies seen. Use as MockCore.serve{ core = q.core } (or call q.core(body) from your own h.core).
function MockCore.analysisQueue(owed)
  local q = { owed = owed or {}, batch = 20, results = {}, lists = {}, bodies = {}, version = 0, last = nil }
  function q.add(station, key) q.owed[#q.owed + 1] = { station = station, key = key } end
  function q.core(body)
    q.bodies[#q.bodies + 1] = body
    for _, part in ipairs(body.stations or {}) do
      for _, e in ipairs(part.events or {}) do
        if e.kind == "analysis" then
          local id = part.station .. "|" .. (e.dir or "") .. (e.file or "")
          q.results[id] = e
          for i = #q.owed, 1, -1 do if q.owed[i].station .. "|" .. q.owed[i].key == id then table.remove(q.owed, i) end end
        end
      end
    end
    if body.analyze_rev == nil then return nil end         -- this Core has no analysis player
    local list, ids = {}, {}
    for i = 1, math.min(q.batch, #q.owed) do
      list[i], ids[i] = { station = q.owed[i].station, key = q.owed[i].key }, q.owed[i].station .. "|" .. q.owed[i].key
    end
    local now = table.concat(ids, "\n")
    if now ~= q.last then q.version, q.last = q.version + 1, now end
    local rev = #list > 0 and ("q" .. q.version) or ""
    if rev == body.analyze_rev then return nil end
    q.lists[#q.lists + 1] = { at = MockCore.clock, list = list, rev = rev }
    return { analyze = list, analyze_rev = rev }
  end
  return q
end

-- simulate an operator button press (momentary)
function MockCore.press(i)
  local c = Controls.Inputs and Controls.Inputs[i]
  if not c then return end
  c.Boolean = true; fire(c)
  c.Boolean = false; fire(c)
end

function MockCore.at(t, fn)
  MockCore.actions[#MockCore.actions + 1] = { at = t, fn = fn }
end

local function deliverDue()
  for _, list in ipairs({ MockCore.pending, MockCore.actions }) do
    local i = 1
    while i <= #list do
      if list[i].at <= MockCore.clock then
        local item = table.remove(list, i)
        item.fn()
      else
        i = i + 1
      end
    end
  end
end

-- advance the simulated clock, firing timers in order
function MockCore.run(seconds)
  local stopAt = MockCore.clock + seconds
  while true do
    local nextT, nextTimer = nil, nil
    for _, tm in ipairs(MockCore.timers) do
      if tm.running and (not nextT or tm.next < nextT) then nextT, nextTimer = tm.next, tm end
    end
    for _, list in ipairs({ MockCore.pending, MockCore.actions }) do
      for _, item in ipairs(list) do if not nextT or item.at < nextT then nextT, nextTimer = item.at, nil end end
    end
    if not nextT or nextT > stopAt then
      local dt = stopAt - MockCore.clock
      if dt > 0 then for _, p in pairs(MockCore.players) do p:advance(dt) end end
      MockCore.clock = stopAt
      return
    end
    local dt = nextT - MockCore.clock
    MockCore.clock = nextT
    if dt > 0 then for _, p in pairs(MockCore.players) do p:advance(dt) end end
    deliverDue()
    if nextTimer and nextTimer.running then
      nextTimer.next = MockCore.clock + nextTimer.iv
      nextTimer.EventHandler()
    end
  end
end
