-- Scenario: one radio.lua runs many stations. Three stations with their own settings and folders play
-- independently; a fourth whose player is missing is faulted and retried until it plays; an error in one
-- station's tick faults and rebuilds that station only; a bad reply part touches only its station; the
-- outbox stays bounded through a long outage and every station's events arrive afterwards; a 3,000-file
-- library goes up in parts (and again after a 409); per-file figures follow the folders a station lists now;
-- Lua memory does not grow over hours of play; the 1.x files become the first station's. Run with run_mock.py.
dofile("qsys/test/mock_core.lua")
local json = require("json")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local SETTINGS_FILE, OUTBOX_FILE = "media/_radio/settings.json", "media/_radio/outbox.json"
local OUTBOX_MAX, SYNC_BATCH = 10000, 100      -- radio.lua's K.outbox_max / K.sync_batch

-- radio.lua's file writes, counted (the scenario's own go through realOpen)
local realOpen, realPrint = io.open, print
local writes = { settings = 0, outbox = {} }
io.open = function(path, mode)
  if mode == "w" and path == SETTINGS_FILE then writes.settings = writes.settings + 1 end
  if mode == "w" and path == OUTBOX_FILE then writes.outbox[#writes.outbox + 1] = { at = MockCore.clock, n = #Server.outbox } end
  return realOpen(path, mode)
end
local function writeFile(path, data)
  local f = assert(realOpen(path, "w"))
  f:write(type(data) == "string" and data or json.encode(data))
  f:close()
end
local function readFile(path)
  local f = realOpen(path, "r")
  if not f then return nil end
  local d = json.decode(f:read("*a"))
  f:close()
  return d
end
-- radio.lua's log lines, captured (heartbeats) and optionally silenced (hours of play)
local heartbeats, quiet = {}, false
print = function(s)
  if type(s) == "string" and s:find("heartbeat:", 1, true) then heartbeats[#heartbeats + 1] = { at = MockCore.clock, line = s } end
  if not quiet then realPrint(s) end
end

local function songs(prefix, n, dur, spread)
  local o = {}
  for i = 1, n do
    o[i] = { name = string.format("%s%d - Song%03d.mp3", prefix, i % 7, i), duration = dur + (spread and (i * 37) % spread or 0),
             lead = 0.3, trail = 0.5, level = -10 - i % 5 }
  end
  return o
end
local function every(dt, fn)
  local function go() fn(); MockCore.at(MockCore.clock + dt, go) end
  MockCore.at(MockCore.clock + dt, go)
end
local function boot(spec)
  for _, f in ipairs({ SETTINGS_FILE, OUTBOX_FILE }) do os.remove(f) end
  MockCore.setup{ seed = spec.seed or 7, dirs = spec.dirs, players = spec.players }
  if spec.settings then writeFile(SETTINGS_FILE, spec.settings) end
  if spec.outbox then writeFile(OUTBOX_FILE, spec.outbox) end
  MockCore.serve(spec.server)
  MockCore.loadRadio(function(cfg)
    cfg.SERVER_URL, cfg.STATIONS = "http://radio-server:5410", spec.stations
    if spec.analyzers then cfg.ANALYZERS = spec.analyzers end
  end)
end
local function libraryPosts()
  local o = {}
  for _, h in ipairs(MockCore.http) do if h.req.Url:match("/core/library$") then o[#o + 1] = { at = h.at, body = json.decode(h.req.Data) } end end
  return o
end
local function oneAtATime(posts)
  for i = 2, #posts do if posts[i].at < posts[i - 1].at + 0.05 then return false end end
  return true
end

-------------------------------------------------------------------------------
do
  print("-- three stations play side by side; a fourth whose player is missing is faulted and retried")
  local wiring, cfg = MockCore.stations(4)
  local s4b = table.remove(wiring)                -- S4_B is not in the design yet
  local analyzers = MockCore.analyzers(1, wiring)
  cfg[5] = { id = "" }                            -- bad entries: skipped and logged, never a script error
  cfg[6] = { id = "S1", players = { "S1_A", "S1_B" }, meter = "S1_Meter" }
  cfg[7] = { id = "S5", players = { "S1_A", "S2_B" }, meter = "S1_Meter" }   -- another station's player
  local FILE_SETTINGS = {
    S1 = { rev = "a1", settings = { music_dir = "Rock/", imaging_dir = "Imaging/", crossfade_s = 4, bumper_every_n_tracks = 2 } },
    S2 = { rev = "a2", settings = { music_dir = "Jazz/", imaging_dir = "JazzImaging/", crossfade_s = 2, bumper_every_n_tracks = 0 } },
    S3 = { rev = "a3", settings = { music_dir = "Pop/", imaging_dir = "Imaging/", crossfade_s = 3, bumper_every_n_tracks = 3 } },
    S4 = { rev = "a4", settings = { music_dir = "Rock/", imaging_dir = "", bumper_every_n_tracks = 0 } },
  }
  local SRV = { settings = {}, commands = {
    S2 = { { id = 7, at = 450, cmd = "stop_now" }, { id = 8, at = 480, cmd = "start" } },
    S3 = { { id = 7, at = 500, cmd = "queue_add", args = { key = "Pop/Pop3 - Song003.mp3", position = "next" } } },
  } }
  for id, e in pairs(FILE_SETTINGS) do SRV.settings[id] = { rev = e.rev, values = e.settings } end
  local got, gotAt, faultSeen, noFaultAfter, memOk, partsOk = {}, {}, {}, {}, true, true
  local analyzeRev = { top = 0, parts = 0 }
  local faultAnalyze = {}
  local settingsSent = {}
  boot{
    players = wiring, stations = cfg, settings = { stations = FILE_SETTINGS }, analyzers = analyzers,
    dirs = { ["Rock/"] = songs("Rock", 12, 40, 9), ["Jazz/"] = songs("Jazz", 12, 50, 7), ["Pop/"] = songs("Pop", 12, 35, 11),
             ["Lounge/"] = songs("Lounge", 8, 45, 5),
             ["Imaging/"] = { { name = "id1.wav", duration = 5, level = -6 }, { name = "id2.wav", duration = 4, level = -6 } },
             ["JazzImaging/"] = { { name = "jazz_id.wav", duration = 6, level = -6 } } },
    server = {
      sync = function(part, body)
        local id, t = part.station, MockCore.clock
        if type(body.mem_kb) ~= "number" or body.core ~= "mock" or body.boot ~= BOOT_ID then memOk = false end
        if type(part.ack) ~= "number" or type(part.settings_rev) ~= "string" or type(part.ads_rev) ~= "string" then partsOk = false end
        if part.state and part.state.fault then
          faultSeen[id], faultAnalyze[id] = faultSeen[id] or part.state.fault, faultAnalyze[id] or part.state.analyze or false
        end
        if part.state and not part.state.fault and faultSeen[id] then noFaultAfter[id] = t end
        if part.analyze_rev ~= nil then analyzeRev.parts = analyzeRev.parts + 1 end
        if id == "S1" and type(body.analyze_rev) == "string" then analyzeRev.top = analyzeRev.top + 1 end
        if t >= 300 and t < 330 and id == "S2" then return "garbage" end            -- not a reply at all: its events stay
        if t >= 300 and t < 330 and id == "S3" then return { error = "boom" } end   -- the server failed this station's part
        for _, e in ipairs(part.events or {}) do
          if e.station ~= id then partsOk = false end
          got[e.seq], gotAt[e.seq] = e, gotAt[e.seq] or t
        end
        if t >= 330 and t < 340 and id == "S2" then return { commands = "x", settings = 5, ads = "y", analyze = 7, ack_reset = "z" } end
        local reply, srv = { commands = {} }, SRV.settings[id]
        if srv and part.settings_rev ~= srv.rev then
          reply.settings, reply.settings_rev = srv.values, srv.rev
          settingsSent[#settingsSent + 1] = { at = t, station = id }
        end
        for _, c in ipairs(SRV.commands[id] or {}) do
          if c.at <= t and c.id > part.ack then reply.commands[#reply.commands + 1] = { id = c.id, cmd = c.cmd, args = c.args, by = "tester" } end
        end
        return reply
      end,
    },
  }
  local st = Radio.byId
  check(#Radio.stations == 5 and st.S1 and st.S2 and st.S3 and st.S4 and st.S5, "five stations from seven CFG entries (no id, a taken id: skipped)")
  check(st.S5.fault == "player 'S1_A' is station S1's" and not st.S1.fault,
    "a station naming another station's player is faulted (" .. tostring(st.S5.fault) .. ")")
  local P = MockCore.players
  local ownHandlers = { S1_A = P.S1_A.ctl.playing.EventHandler, S2_B = P.S2_B.ctl.playing.EventHandler }
  check(st.S4.fault and st.S4.fault:find("S4_B", 1, true) and not st.S1.fault and not st.S2.fault and not st.S3.fault,
    "the station whose player is missing is faulted (" .. tostring(st.S4.fault) .. "); the others are not")
  check(st.S1.settings.crossfade_s == 4 and st.S2.settings.crossfade_s == 2 and st.S3.settings.music_dir == "Pop/"
    and st.S2.settingsRev == "a2", "each station boots with its own settings from settings.json")
  check(st.S1.lib.imaging[1] == st.S3.lib.imaging[1] and st.S1.lib.imaging ~= st.S3.lib.imaging,
    "stations listing the same folder share its track records (not the lists)")

  -- the stations that must never go silent (S1 outside its injected fault, S2 outside its stop): by their own
  -- bookkeeping (a deck busy) and on the hardware (one of their players playing)
  local gaps, silent = { S1 = 0, S2 = 0, S3 = 0 }, { S1 = 0, S2 = 0, S3 = 0 }
  every(0.25, function()
    local t = MockCore.clock
    for id in pairs(gaps) do
      local s = Radio.byId[id]
      local excused = (id == "S1" and t >= 200 and t < 236) or (id == "S2" and t >= 449 and t < 482)
      if not excused and t > 1 and not s.fault and s.automation then
        if not s:busy() then gaps[id] = gaps[id] + 1 end
        if not (P[id .. "_A"].playing or P[id .. "_B"].playing) then silent[id] = silent[id] + 1 end
      end
    end
  end)
  local handlersKept, s5Faults                   -- S5's retries (t=30, 90) before S1's own rebuild (t=230)
  MockCore.at(199, function()
    handlersKept = P.S1_A.ctl.playing.EventHandler == ownHandlers.S1_A and P.S2_B.ctl.playing.EventHandler == ownHandlers.S2_B
    s5Faults = 0
    for _, e in ipairs(EVENTS) do if e.kind == "station_fault" and e.station == "S5" then s5Faults = s5Faults + 1 end end
  end)
  local s4FaultsBy60
  MockCore.at(60, function()
    s4FaultsBy60 = 0
    for _, e in ipairs(EVENTS) do if e.kind == "station_fault" and e.station == "S4" then s4FaultsBy60 = s4FaultsBy60 + 1 end end
    MockCore.wire(s4b)                            -- the missing player appears (design fixed)
  end)
  local s1Decks
  MockCore.at(200, function() Radio.byId.S1.tick = function() error("injected") end end)
  MockCore.at(203, function()
    local s1 = Radio.byId.S1
    s1Decks = { fault = s1.fault, a = MockCore.players.S1_A.playing, b = MockCore.players.S1_B.playing }
  end)
  local writesBefore
  MockCore.at(399, function()
    writesBefore = writes.settings
    SRV.settings.S1 = { rev = "b1", values = { music_dir = "Rock/", imaging_dir = "Imaging/", crossfade_s = 5, bumper_every_n_tracks = 2 } }
    SRV.settings.S2 = { rev = "b2", values = { music_dir = "Lounge/", imaging_dir = "JazzImaging/", crossfade_s = 2, bumper_every_n_tracks = 0 } }
  end)
  local writesAfter
  MockCore.at(405, function() writesAfter = writes.settings end)
  MockCore.run(700)
  st = Radio.byId                                 -- the stations as they are now (a rebuild replaces one)

  local function delivered(kind, station, from, to)
    local o = {}
    for _, e in pairs(got) do
      if e.kind == kind and e.station == station and e.at >= (from or 0) and e.at < (to or 1e9) then o[#o + 1] = e end
    end
    table.sort(o, function(a, b) return a.seq < b.seq end)
    return o
  end
  check(s4FaultsBy60 == 2, string.format("the faulted station is retried after 30 s (%d faults by t=60: boot + one retry)", s4FaultsBy60 or -1))
  local s4Starts = delivered("track_start", "S4")
  check(s4Starts[1] and s4Starts[1].at >= 90 and s4Starts[1].at < 94 and not st.S4.fault,
    string.format("once its player exists it is rebuilt at the next retry (60 s later) and plays (first song at t=%s)", s4Starts[1] and s4Starts[1].at or "-"))
  check(faultSeen.S4 and faultSeen.S4:find("S4_B", 1, true) and noFaultAfter.S4, "a faulted station still syncs: its state reports the fault, then clears")
  check(faultAnalyze.S4 and faultAnalyze.S4.can == true and faultAnalyze.S5 and faultAnalyze.S5.can == true,
    "a faulted station's state still says this Core can analyze (S4, S5): the Music page names the fault, not the CFG")
  local s1Fault = delivered("station_fault", "S1")
  local s1Rebuilt = delivered("station_rebuilt", "S1")
  local s1Back = delivered("track_start", "S1", 200)
  check(#s1Fault == 1 and s1Fault[1].error:find("injected", 1, true) and s1Fault[1].at < 202,
    "an error in one station's tick, 5 times in a row: that station is faulted within 1.5 s")
  check(s1Decks and s1Decks.fault and not s1Decks.a and not s1Decks.b, "its decks are stopped")
  local cut = false
  for _, e in ipairs(delivered("track_end", "S1", 200, 203)) do cut = cut or e.reason == "fault" end
  check(cut, "the song it was playing is logged as ended (reason fault)")
  check(#s1Rebuilt == 1 and s1Rebuilt[1].at >= 230 and s1Rebuilt[1].at < 232 and s1Back[1] and s1Back[1].at < s1Rebuilt[1].at + 3,
    string.format("rebuilt from its CFG entry after 30 s and on air again (t=%s)", s1Back[1] and s1Back[1].at or "-"))
  check(faultSeen.S1 and faultSeen.S1:find("injected", 1, true), "its fault reached the server in its state")
  check(st.S4.backoff == 30 and st.S1.backoff == 60,
    "the back-off doubles with each fault and is back to 30 s after 10 min of healthy running (S4 yes, S1 not yet)")
  check(s5Faults == 3 and handlersKept,
    string.format("the owner keeps its players: S5's build and %d retries never took over S1_A's or S2_B's handlers", (s5Faults or 1) - 1))
  check(gaps.S1 == 0 and gaps.S2 == 0 and gaps.S3 == 0 and silent.S1 == 0 and silent.S2 == 0 and silent.S3 == 0,
    string.format("no dead air on the healthy stations (decks idle: S1 %d, S2 %d, S3 %d; players silent: S1 %d, S2 %d, S3 %d)",
      gaps.S1, gaps.S2, gaps.S3, silent.S1, silent.S2, silent.S3))
  local guards = 0
  for _, e in pairs(got) do if e.kind == "dead_air_guard" then guards = guards + 1 end end
  check(guards == 0, "the dead-air guard was never needed")

  local missing, lateS1, earlyS3 = 0, 0, 0
  for s = 1, EVENT_SEQ do if not got[s] then missing = missing + 1 end end
  for s, e in pairs(got) do
    if e.at >= 300 and e.at < 328 then
      if e.station == "S1" and gotAt[s] > e.at + 3 then lateS1 = lateS1 + 1 end
      if e.station == "S3" and gotAt[s] < 330 then earlyS3 = earlyS3 + 1 end
    end
  end
  check(lateS1 == 0 and earlyS3 == 0, "a station answered with { error } keeps its events; the others' go at once")
  check(missing == 0, string.format("garbage or an error in one station's reply part: every event of every station arrived (%d missing)", missing))
  check(#settingsSent >= 2 and settingsSent[#settingsSent].at < 402 and writesAfter - writesBefore == 1,
    string.format("two stations' new settings in one reply: settings.json written once (%d)", (writesAfter or 0) - (writesBefore or 0)))
  local file = readFile(SETTINGS_FILE)
  check(file and file.stations.S1.rev == "b1" and file.stations.S2.rev == "b2" and file.stations.S3.rev == "a3" and file.stations.S4.rev == "a4"
    and file.stations.S1.settings.crossfade_s == 5, "settings.json keeps every station's settings")
  local lounge, jazzLate = 0, 0
  for _, e in ipairs(delivered("track_start", "S2", 401)) do
    if e.dir == "Lounge/" then lounge = lounge + 1 elseif e.at > 482 then jazzLate = jazzLate + 1 end
  end
  check(lounge >= 2 and jazzLate == 0 and st.S1.settings.crossfade_s == 5 and st.S3.settings.crossfade_s == 3,
    "each station applies its own settings (S2's new folder; S3 untouched)")
  local s2Off = delivered("automation", "S2")
  check(#s2Off == 2 and s2Off[1].on == false and s2Off[2].on == true and #delivered("automation", "S3") == 0
    and #delivered("automation", "S1") == 0, "a command to one station (stop, start) moves that station only")
  local ran = {}
  for _, e in pairs(got) do if e.kind == "command" then ran[#ran + 1] = e.station .. "#" .. tostring(e.id) end end
  table.sort(ran)
  local s3cmd = delivered("command", "S3")[1]
  local queued = s3cmd and delivered("track_start", "S3", s3cmd.at)
  check(table.concat(ran, " ") == "S2#7 S2#8 S3#7" and queued[1] and queued[1].file == "Pop3 - Song003.mp3",
    "each station acknowledges its own commands (the same id on two stations: " .. table.concat(ran, " ") .. ")")
  check(memOk and partsOk, "every sync body: core, boot, mem_kb; each station's part: ack, revs, its own events")
  check(analyzeRev.top > 600 and analyzeRev.parts == 0, "analyze_rev once per sync body (the Core's analysis player), never in a station's part")
  local rock = true
  for i, tr in ipairs(st.S1.lib.music) do if st.S4.lib.music[i] ~= tr then rock = false end end
  check(rock and #st.S1.lib.music == 12 and st.S1.lib.music ~= st.S4.lib.music, "a rebuilt station shares the folder's records again")
  local posts = libraryPosts()
  local first = {}
  for _, p in ipairs(posts) do first[#first + 1] = p.body.station end
  check(oneAtATime(posts) and table.concat(first, " "):sub(1, 8) == "S1 S2 S3", "library uploads: one at a time, the stations take turns ("
    .. table.concat(first, " ") .. ")")
  local hb = heartbeats[#heartbeats]
  check(#heartbeats >= 22 and #heartbeats <= 24 and hb.line:find("Lua %d+") and hb.line:find("S1: ") and hb.line:find("S4: "),
    string.format("one heartbeat line every 30 s for the whole Core (%d), with each station and the Lua memory", #heartbeats))

end

-------------------------------------------------------------------------------
do
  print("-- a long outage: the outbox stays bounded; every station's events arrive afterwards, shared fairly")
  local OUT_FROM, OUT_TO = 20, 20 + 4500
  local wiring, cfg = MockCore.stations(5)
  local short = {}
  for id in pairs({ S1 = 1, S2 = 1, S3 = 1, S4 = 1, S5 = 1 }) do short[id] = { rev = "s", settings = { music_dir = "Short/", imaging_dir = "",
    crossfade_s = 1, bumper_every_n_tracks = 0, artist_separation = 0 } } end
  local seen, requests, maxBox, dropped = {}, {}, 0, {}
  writes.outbox = {}
  boot{
    players = wiring, stations = cfg, settings = { stations = short }, seed = 9,
    dirs = { ["Short/"] = songs("Short", 10, 6) },
    server = {
      down = function() if MockCore.clock >= OUT_FROM and MockCore.clock < OUT_TO then return 0, "", "timeout" end end,
      sync = function(part, body, req)
        if requests[#requests] ~= req then requests[#requests + 1] = req end
        req.counts = req.counts or {}
        req.counts[part.station] = part.events and #part.events or 0
        if part.dropped then dropped[part.station] = (dropped[part.station] or 0) + part.dropped end
        for _, e in ipairs(part.events or {}) do seen[e.seq] = true end
        return { commands = {} }
      end,
    },
  }
  quiet = true
  every(10, function() if #Server.outbox > maxBox then maxBox = #Server.outbox end end)
  every(60, function() MockCore.log, MockCore.http = {}, {} end)
  MockCore.run(OUT_TO - 1)
  local queuedAtEnd = #Server.outbox
  MockCore.run(301)
  quiet = false
  local fair, over, lost = false, 0, 0
  for _, req in ipairs(requests) do
    local n, c = 0, req.counts
    for _, k in pairs(c) do n = n + k end
    if n > SYNC_BATCH then over = over + 1 end
    if not fair and n == SYNC_BATCH and c.S1 == 20 and c.S2 == 20 and c.S3 == 20 and c.S4 == 20 and c.S5 == 20 then fair = true end
  end
  for _, n in pairs(dropped) do lost = lost + n end
  local arrived = 0
  for _ in pairs(seen) do arrived = arrived + 1 end
  check(EVENT_SEQ > OUTBOX_MAX + 2000 and maxBox <= OUTBOX_MAX and queuedAtEnd == OUTBOX_MAX,
    string.format("%d events in a 75 min outage: the outbox never held more than %d (%d)", EVENT_SEQ, OUTBOX_MAX, maxBox))
  local recent = #Server.outbox < 10
  for _, e in ipairs(Server.outbox) do if e.at < MockCore.clock - 2 then recent = false end end
  check(dropped.S1 and dropped.S2 and dropped.S3 and dropped.S4 and dropped.S5 and arrived + lost + #Server.outbox == EVENT_SEQ,
    string.format("each station reports its own lost events once (%d in all: the events that never arrived)", lost))
  check(recent, string.format("every other event of every station arrived (%d; %d from the last 2 s on the way)", arrived, #Server.outbox))
  check(over == 0 and fair, "at most 100 events a request, shared fairly between the stations (20 each while all have a backlog)")
  local big, bigFast = false, 0
  for i = 2, #writes.outbox do
    local a, b = writes.outbox[i - 1], writes.outbox[i]
    if b.n > 2000 then big = true; if b.at - a.at < 119 then bigFast = bigFast + 1 end end
  end
  check(#writes.outbox > 0 and big and bigFast == 0, string.format("a big outbox is written at most every 2 min (%d writes)", #writes.outbox))

end

-------------------------------------------------------------------------------
do
  print("-- a library of 3,000 files goes up in parts; one part lost: the whole list again (409), one upload at a time")
  local wiring, cfg = MockCore.stations(2)
  local bigDir = {}
  for i = 1, 3000 do bigDir[i] = { name = string.format("Artist%03d - Song%04d.mp3", i % 400, i), duration = 40 + i % 50, level = -10 } end
  local lostOnce, complete = false, {}
  boot{
    players = wiring, stations = cfg, seed = 4,
    settings = { stations = { S1 = { rev = "x", settings = { music_dir = "Big/", imaging_dir = "" } },
                              S2 = { rev = "x", settings = { music_dir = "Big/", imaging_dir = "" } } } },
    dirs = { ["Big/"] = bigDir },
    server = {
      lose = function(body)
        if body.station == "S1" and body.part == 2 and not lostOnce then lostOnce = true return true end
      end,
      library = function(station, items) complete[#complete + 1] = { at = MockCore.clock, station = station, n = #items } end,
    },
  }
  MockCore.run(60)
  local posts = libraryPosts()
  local okParts, s1Scans, s2Parts = true, {}, 0
  for _, p in ipairs(posts) do
    local b = p.body
    if #b.items > 1000 or b.parts ~= 3 or not b.scan or b.boot ~= BOOT_ID then okParts = false end
    if b.station == "S1" then s1Scans[b.scan] = (s1Scans[b.scan] or 0) + 1 else s2Parts = s2Parts + 1 end
  end
  local nScans = 0
  for _, n in pairs(s1Scans) do nScans = nScans + 1; if n ~= 3 then okParts = false end end
  local libS1, libS2 = MockCore.libraries.S1, MockCore.libraries.S2
  check(okParts and #posts == 9 and nScans == 2 and s2Parts == 3, string.format("3 parts of at most 1,000 items per scan (%d posts)", #posts))
  check(libS1.conflicts == 1 and libS1.uploads == 1 and #libS1.items == 3000 and libS2.uploads == 1 and #libS2.items == 3000,
    "the server staged both stations' 3,000 files; S1's scan with a lost part was refused (409) and sent again whole")
  local s1Posts = {}
  for _, p in ipairs(posts) do if p.body.station == "S1" then s1Posts[#s1Posts + 1] = p end end
  check(complete[1] and complete[1].station == "S2" and complete[2] and complete[2].station == "S1"
    and s1Posts[4] and s1Posts[4].at - s1Posts[3].at >= 9.5 and s1Posts[4].body.scan ~= s1Posts[3].body.scan,
    "after the 409 the other station went first; S1's whole list again after a back-off, with a new scan id")
  check(oneAtATime(posts), "never two library uploads in flight")
  local shared = #Radio.byId.S1.lib.music == 3000
  for i, tr in ipairs(Radio.byId.S1.lib.music) do if Radio.byId.S2.lib.music[i] ~= tr then shared = false end end
  check(shared, "both stations list Big/: one set of 3,000 track records")
  check(Radio.byId.S1.state == "playing" and Radio.byId.S2.state == "playing", "both stations play meanwhile")

end

-------------------------------------------------------------------------------
do
  print("-- a station's folder changes again and again: it keeps figures only for the files it lists now; a library")
  print("   reply for an older listing is not taken (2.0.0 dev kept every file it had ever seen)")
  local wiring, cfg = MockCore.stations(2)
  local function folder(f) return { music_dir = "F" .. f .. "/", imaging_dir = "", bumper_every_n_tracks = 0 } end
  local dirs = { ["Keep/"] = songs("Keep", 8, 30) }
  for f = 1, 6 do dirs["F" .. f .. "/"] = songs("F" .. f .. "x", 8, 30) end
  local UNSUP = "F6/F6x3 - Song003.mp3"
  local want, raced = { S1 = { rev = "f1", values = folder(1) } }, false
  boot{
    players = wiring, stations = cfg, seed = 3,
    settings = { stations = { S1 = { rev = "f1", settings = folder(1) },
                              S2 = { rev = "k", settings = { music_dir = "Keep/", imaging_dir = "", bumper_every_n_tracks = 0 } } } },
    dirs = dirs,
    server = {
      sync = function(part)
        local w = want[part.station]
        if w and part.settings_rev ~= w.rev then return { settings = w.values, settings_rev = w.rev } end
      end,
      library = function(station, items)
        local learned = {}
        for _, it in ipairs(items) do learned[it.key] = it.key == UNSUP and { unsupported = true } or { trail = 1, lead = 0.2, level = -12, peak = -3 } end
        if station == "S1" and items[1] and items[1].dir == "F5/" and not raced then
          raced, want.S1 = true, { rev = "f6", values = folder(6) }   -- the folder changes while this reply is on its way
          Radio.byId.S1:applySettings(want.S1.values, want.S1.rev)
        end
        return learned
      end,
    },
  }
  for f = 2, 5 do MockCore.at(60 * (f - 1), function() want.S1 = { rev = "f" .. f, values = folder(f) } end) end
  local function learnedBy(st)
    local n, by = 0, {}
    for key in pairs(st.learned) do
      local f = key:match("^[^/]*/")
      n, by[f] = n + 1, (by[f] or 0) + 1
    end
    return n, by
  end
  MockCore.run(280)
  local S1, S2 = Radio.byId.S1, Radio.byId.S2
  local n, by = learnedBy(S1)
  local plays = {}
  for _, e in ipairs(EVENTS) do if e.kind == "track_start" and e.station == "S1" then plays[e.dir] = true end end
  check(raced and plays["F1/"] and plays["F4/"] and plays["F6/"] and by["F6/"] == 8 and n - by["F6/"] <= 2,
    string.format("after 5 folder changes: the 8 files of F6/ and at most the 2 songs on its decks at the rescan (%d in all)", n))
  check(S1.learned["F6/F6x1 - Song001.mp3"].level == -12 and not (by["F5/"] and by["F5/"] > 2),
    "the reply for F6/ was taken; the one for F5/ (answered after the rescan) was not")
  S1:scan()
  MockCore.run(80)
  n, by = learnedBy(S1)
  check(n == 8 and by["F6/"] == 8 and S1.learned[UNSUP].unsupported and not S1.lib.byKey[UNSUP] and #S1.lib.music == 7,
    "after a rescan only F6/ is left, its unsupported file still known and still out of the library")
  local n2, by2 = learnedBy(S2)
  check(n2 == 8 and by2["Keep/"] == 8 and S2.learned["Keep/Keep1 - Song001.mp3"].level == -12,
    "the other station's figures are untouched")

end

-------------------------------------------------------------------------------
do
  print("-- hours of play: Lua memory does not grow")
  local wiring, cfg = MockCore.stations(3)
  local analyzers = MockCore.analyzers(1, wiring)
  local plays, analyzed, cmdId = 0, 0, 0
  boot{
    players = wiring, stations = cfg, seed = 11, analyzers = analyzers,
    settings = { stations = {
      S1 = { rev = "m", settings = { music_dir = "A/", imaging_dir = "I/", ads_dir = "Ads/", ad_every_n_tracks = 4, bumper_every_n_tracks = 3 } },
      S2 = { rev = "m", settings = { music_dir = "B/", imaging_dir = "I/", bumper_every_n_tracks = 2, fade_in_s = 1 } },
      S3 = { rev = "m", settings = { music_dir = "C/", imaging_dir = "", bumper_every_n_tracks = 0 } } } },
    dirs = { ["A/"] = songs("A", 25, 60, 60), ["B/"] = songs("B", 25, 70, 50), ["C/"] = songs("C", 25, 50, 70),
             ["I/"] = { { name = "i1.wav", duration = 5, level = -6 }, { name = "i2.wav", duration = 6, level = -6 } },
             ["Ads/"] = { { name = "ad1.wav", duration = 15, level = -8 }, { name = "ad2.wav", duration = 20, level = -8 } } },
    server = {
      sync = function(part)
        for _, e in ipairs(part.events or {}) do
          if e.kind == "track_start" then plays = plays + 1 elseif e.kind == "analysis" then analyzed = analyzed + 1 end
        end
        local t, reply = MockCore.clock, { commands = {} }
        local slot = math.floor(t / 600)                          -- every 10 min: a skip on S2, new settings for S3
        if part.station == "S2" and t % 600 < 1 and part.ack < slot then reply.commands[1] = { id = slot, cmd = "skip" } end
        if part.station == "S3" and part.settings_rev ~= "m" .. slot % 2 then
          reply.settings, reply.settings_rev = { music_dir = "C/", imaging_dir = "", crossfade_s = 3 + slot % 2 }, "m" .. slot % 2
        end
        if part.station == "S1" and part.ads_rev ~= "p" .. slot then
          reply.ads, reply.ads_rev = { due = {}, mix = { "Ads/ad1.wav", "Ads/ad2.wav" }, next = "A/A3 - Song003.mp3", next_why = "rotation" }, "p" .. slot
        end
        return reply
      end,
      core = function(body)                                       -- every hour: two other files to analyze (S1's, S2's)
        local hour = math.floor(MockCore.clock / 3600)
        if body.analyze_rev ~= "z" .. hour then
          local a = hour % 2 * 2 + 1
          return { analyze = { { station = "S1", key = string.format("A/A%d - Song%03d.mp3", a % 7, a) },
                               { station = "S2", key = string.format("B/B%d - Song%03d.mp3", (a + 1) % 7, a + 1) } }, analyze_rev = "z" .. hour }
        end
      end,
    },
  }
  quiet = true
  heartbeats = {}
  every(60, function() MockCore.log, MockCore.http, heartbeats = {}, {}, {} end)
  local mem = {}
  local function measure(label)
    collectgarbage("collect")
    collectgarbage("collect")
    mem[#mem + 1] = { label = label, kb = collectgarbage("count") }
  end
  MockCore.run(3600)
  measure("1 h")
  for h = 2, 4 do MockCore.run(3600); measure(h .. " h") end
  quiet = false
  local line = {}
  for _, m in ipairs(mem) do line[#line + 1] = string.format("%s %.0f KB", m.label, m.kb) end
  print("        Lua memory after a full collect: " .. table.concat(line, ", "))
  check(plays > 300 and analyzed >= 4, string.format("4 h of play on 3 stations (%d songs, %d analyses)", plays, analyzed))
  local grew = mem[#mem].kb - mem[1].kb
  check(grew < 64, string.format("no growth from hour 1 to hour 4 (%+.0f KB)", grew))
  check(#Server.outbox < 50 and #EVENTS == 500, "bounded: the outbox drained, the event ring at 500")

end

-------------------------------------------------------------------------------
do
  print("-- upgrade from 1.x: the old settings.json and unsent events become the first station's")
  local wiring, cfg = MockCore.stations(2)
  local firstParts
  boot{
    players = wiring, stations = cfg, seed = 5,
    settings = { rev = "old", settings = { music_dir = "Rock/", crossfade_s = 7 } },
    outbox = { { kind = "track_start", boot = "20260101T000000Z-abcd", seq = 41, t = "2026-01-01T00:00:00Z", file = "x.mp3", dir = "Rock/" },
               { kind = "track_end", boot = "20260101T000000Z-abcd", seq = 42, t = "2026-01-01T00:03:00Z", file = "x.mp3", dir = "Rock/" } },
    dirs = { ["Rock/"] = songs("Rock", 6, 40), ["Test/Tuning/"] = songs("Tune", 6, 40) },
    server = {
      sync = function(part)
        firstParts = firstParts or {}
        if not firstParts[part.station] then firstParts[part.station] = part end
        if MockCore.clock > 5 and part.settings_rev ~= "n" then return { settings = { music_dir = "Rock/" }, settings_rev = "n" } end
      end,
    },
  }
  check(Radio.byId.S1.settings.crossfade_s == 7 and Radio.byId.S1.settingsRev == "old" and Radio.byId.S2.settings.crossfade_s == SETTINGS.crossfade_s,
    "a 1.x settings.json is the first station's; the others start from the fallback")
  MockCore.run(10)
  local old1 = firstParts and firstParts.S1 and firstParts.S1.events or {}
  local oldOk = 0
  for _, e in ipairs(old1) do if e.boot == "20260101T000000Z-abcd" and e.station == "S1" then oldOk = oldOk + 1 end end
  check(oldOk == 2, "1.x unsent events are replayed in the first station's part")
  local file = readFile(SETTINGS_FILE)
  check(file and file.stations and file.stations.S1.rev == "n" and file.stations.S2.rev == "n" and file.rev == nil,
    "settings.json is rewritten in the 2.x shape")

end

-------------------------------------------------------------------------------
do
  print("-- a CFG without a usable station: logged, nothing else happens")
  boot{ stations = { { id = "" }, "nonsense" }, dirs = {} }
  MockCore.run(5)
  check(#Radio.stations == 0 and #MockCore.http == 0, "no station, no requests, no script error")
end

io.open, print = realOpen, realPrint
print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
