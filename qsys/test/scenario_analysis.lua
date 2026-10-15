-- Scenario: file analysis on the Core's analysis players (CFG.ANALYZERS: an Audio Player + a meter channel,
-- routed nowhere). The fake server keeps the per-Core list (MockCore.analysisQueue). Checks the figures (quiet
-- start and end, the average level from the middle, the peak; at unity gain); that on-air decks are never
-- touched (no mute, no take-over, every song played in full) while every station plays; two analysis players
-- sharing the list; figures shared at once by every station that lists the same file; failures (a load that
-- never completes, an unsupported file, a player that never reports its end) reported and passed; the players'
-- roots; the protocol (top-level analyze_rev / analyze / analyzers); a design without an analysis player.
-- Run with run_mock.py.
dofile("qsys/test/mock_core.lua")
local json = require("json")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local function evs(kind, station)
  local o = {}
  for _, e in ipairs(EVENTS) do if e.kind == kind and (not station or e.station == station) then o[#o + 1] = e end end
  return o
end
local function every(dt, fn)
  local function go() fn(); MockCore.at(MockCore.clock + dt, go) end
  MockCore.at(MockCore.clock + dt, go)
end
-- radio.lua's log lines, captured
local realPrint, logged = print, {}
print = function(s) if type(s) == "string" and s:find("^%d%d:%d%d:%d%d ") then logged[#logged + 1] = s end realPrint(s) end
local function loggedCount(text) local n = 0 for _, l in ipairs(logged) do if l:find(text, 1, true) then n = n + 1 end end return n end

local q                                  -- the fake server's analysis list of the current boot
local function boot(spec)
  for _, f in ipairs({ "outbox", "settings" }) do os.remove("media/_radio/" .. f .. ".json") end
  MockCore.setup{ seed = spec.seed or 3, dirs = spec.dirs, players = spec.players, noAnalyzer = spec.noAnalyzer }
  if spec.settings then
    local f = assert(io.open("media/_radio/settings.json", "w"))
    f:write(json.encode({ stations = spec.settings }))
    f:close()
  end
  if spec.before then spec.before() end
  q = MockCore.analysisQueue(spec.owed)
  MockCore.serve{ core = spec.core or q.core, sync = spec.sync }
  logged = {}
  MockCore.loadRadio(function(cfg)
    cfg.SERVER_URL = "http://radio-server:5410"
    if spec.stations then cfg.STATIONS = spec.stations end
    if spec.analyzers then cfg.ANALYZERS = spec.analyzers end
    if spec.cfg then spec.cfg(cfg) end
  end)
end
local function owe(station, dir, files)
  local o = {}
  for _, f in ipairs(files) do o[#o + 1] = { station = station, key = dir .. f.name } end
  return o
end
local function join(...)
  local o = {}
  for _, list in ipairs({ ... }) do for _, e in ipairs(list) do o[#o + 1] = e end end
  return o
end
local function result(station, key) return q.results[station .. "|" .. key] end

-- every 50 ms: a station player plays only what its station put on air; the station mixers' inputs are never
-- muted; an analysis player that plays is at unity gain; how many analysis players are busy at once
local watch
local function watcher()
  watch = { takeover = 0, muted = 0, notUnity = 0, both = 0, busy = {}, firstJob = nil, lastJob = nil }
  every(0.05, function()
    for _, st in ipairs(Radio.stations) do
      for i, d in ipairs(st.decks) do
        local p = MockCore.players[st.cfg.players[i]]
        if p and p.playing and not (d.track and p.file and p.file.name == d.track.name) then watch.takeover = watch.takeover + 1 end
      end
    end
    for _, mixer in pairs(MockCore.mixers) do
      for k, c in pairs(mixer) do if k:match("%.mute%.1$") and c.Boolean then watch.muted = watch.muted + 1 end end
    end
    local busy = 0
    for _, d in ipairs(Analyzer.decks) do
      local p = MockCore.players[d.id]
      if p.playing and math.abs(p.ctl.gain.Value) > 0.01 then watch.notUnity = watch.notUnity + 1 end
      if d.job then
        busy = busy + 1
        watch.busy[d.id] = (watch.busy[d.id] or 0) + 0.05
        watch.firstJob, watch.lastJob = watch.firstJob or MockCore.clock, MockCore.clock
      end
    end
    if busy >= 2 then watch.both = watch.both + 1 end
  end)
end
local function untouched(label)
  check(watch.takeover == 0, label .. ": the station players only ever played what their station put on air (" .. watch.takeover .. " samples)")
  check(watch.muted == 0, label .. ": no mixer input was ever muted")
  check(watch.notUnity == 0, label .. ": the analysis player measured at unity gain")
end
local function playedInFull(station, minPlayed)
  local short, cut = 0, 0
  for _, e in ipairs(evs("track_end", station)) do
    if e.reason == "cut" then cut = cut + 1 end
    if e.played and e.played < minPlayed then short = short + 1 end
  end
  return short == 0 and cut == 0, string.format("%d short, %d cut", short, cut)
end

local MUSIC = {
  { name = "A - Quiet Start.flac", duration = 90, lead = 2.0, trail = 0.5, level = -10 },
  { name = "B - Long Tail.flac", duration = 90, lead = 0.2, trail = 6.0, level = -10 },
  { name = "C - Plain.flac", duration = 90, lead = 0.4, trail = 0.8, level = -10 },
  { name = "D - Plain.flac", duration = 90, lead = 0.3, trail = 1.2, level = -10 },
}
local IMAGING = { { name = "id.wav", duration = 5, lead = 0.5, trail = 1.0, level = -6 } }
local function near(e, f, what)
  -- the meter falls away after the sound stops, so a quiet end reads up to ~0.7 s short (the safe side: hand-over a bit later)
  return e and e.ok and math.abs(e.lead_in - f.lead) <= 0.5 and e.trail <= f.trail + 0.3 and e.trail >= f.trail - 0.8, string.format(
    "%s: quiet start %.2f s (file %.1f), quiet end %.2f s (file %.1f)", what, e and e.lead_in or -1, f.lead, e and e.trail or -1, f.trail)
end

-------------------------------------------------------------------------------
do
  print("-- one station on air; the analysis player measures every file on the server's list, at any hour")
  boot{ dirs = { ["Test/Tuning/"] = MUSIC, ["Test/Imaging/"] = IMAGING },
        owed = join(owe("Main", "Test/Imaging/", IMAGING), owe("Main", "Test/Tuning/", MUSIC)) }
  local S = Radio.stations[1]
  S.settings.bumper_every_n_tracks, S.settings.artist_separation = 0, 0
  watcher()
  MockCore.run(260)
  local n = 0
  for _, f in ipairs(MUSIC) do if result("Main", "Test/Tuning/" .. f.name) then n = n + 1 end end
  if result("Main", "Test/Imaging/id.wav") then n = n + 1 end
  check(n == 5 and #q.owed == 0, string.format("every listed file analyzed and reported in its station's part (%d of 5)", n))
  for _, f in ipairs(MUSIC) do check(near(result("Main", "Test/Tuning/" .. f.name), f, f.name)) end
  check(near(result("Main", "Test/Imaging/id.wav"), IMAGING[1], "short imaging file (played through)"))
  local e = result("Main", "Test/Tuning/" .. MUSIC[3].name)
  check(e and math.abs(e.level - (-13)) <= 1 and e.level_s >= 15 and e.level_s <= 20 and e.peak and math.abs(e.peak - (-8)) <= 1
    and e.duration == 90, string.format("the average level from the middle (%s dB over %s s), the peak (%s), the length",
      e and e.level or "-", e and e.level_s or "-", e and e.peak or "-"))
  local X = MockCore.players.Analysis_Player
  local locates = 0
  for _, l in ipairs(MockCore.log) do if l.msg:find("^X1 LOCATE") then locates = locates + 1 end end
  check(locates == 8, string.format("head, then a jump to the middle and one to the tail of each long file (%d jumps)", locates))
  untouched("on air")
  check(#evs("dead_air_guard") == 0, "no dead air")
  check(playedInFull("Main", 80))
  local fixed = S.learned["Test/Tuning/B - Long Tail.flac"]
  check(fixed and fixed.analyzed and math.abs(fixed.trail - 6) <= 0.6, "the Core uses the analyzed figures and plays don't learn over them")
  check(not X.playing and not Analyzer.decks[1].job and S.state == "playing", "the analysis player is idle again; the station plays on")

  -- the protocol
  local partRev, topRev, measuring, idleAfter, canOnly = false, true, nil, nil, true
  for _, b in ipairs(q.bodies) do
    if type(b.analyze_rev) ~= "string" then topRev = false end
    for _, p in ipairs(b.stations) do
      if p.analyze_rev ~= nil then partRev = true end
      local a = p.state and p.state.analyze
      if p.state and not (a and a.can == true and a.left == nil) then canOnly = false end
    end
    local a = b.analyzers and b.analyzers[1]
    if a and a.state == "measuring" and a.station == "Main" and a.name == "Analysis_Player" then measuring = measuring or a.key end
  end
  check(topRev and not partRev, "analyze_rev rides at the top of every sync body, never in a station's part")
  check(measuring ~= nil, "the sync reports what the analysis player measures (" .. tostring(measuring) .. ")")
  check(canOnly, "a station's state says only analyze.can (this Core has an analysis player)")
  check(#q.lists >= 5 and q.lists[1].list[1].station == "Main", string.format("the list came at the top of the reply (%d lists)", #q.lists))
  local quiet, from = 0, #q.bodies
  MockCore.run(35)
  for i = from + 1, #q.bodies do
    local a = q.bodies[i].analyzers
    if a then quiet = quiet + 1; idleAfter = a[1] and a[1].state == "idle" and a[1].station == nil end
  end
  check(idleAfter and quiet >= 3 and quiet <= 4, string.format("idle and unchanged: its state every 10 s only (%d times in 35 s)", quiet))
end

-------------------------------------------------------------------------------
do
  print("-- three stations play while two analysis players share the Core's list; a file two stations list is")
  print("   measured once and both use its figures at once")
  local wiring, cfg = MockCore.stations(3)
  local analyzers = MockCore.analyzers(2, wiring)
  local function songs(prefix, n)
    local o = {}
    for i = 1, n do o[i] = { name = string.format("%s%d - Song%d.mp3", prefix, i, i), duration = 70 + i * 7, lead = 0.2 + i * 0.3, trail = 0.5 + i * 0.4, level = -9 - i } end
    return o
  end
  local ROCK, JAZZ, POP = songs("Rock", 6), songs("Jazz", 6), songs("Pop", 6)
  local IDS = { { name = "id1.wav", duration = 5, lead = 0.2, trail = 0.6, level = -6 }, { name = "id2.wav", duration = 6, lead = 0.3, trail = 0.4, level = -6 } }
  local function folders(music) return { rev = "f", settings = { music_dir = music, imaging_dir = "Imaging/", bumper_every_n_tracks = 0, artist_separation = 0 } } end
  boot{ players = wiring, stations = cfg, analyzers = analyzers,
        settings = { S1 = folders("Rock/"), S2 = folders("Jazz/"), S3 = folders("Pop/") },
        dirs = { ["Rock/"] = ROCK, ["Jazz/"] = JAZZ, ["Pop/"] = POP, ["Imaging/"] = IDS },
        owed = join(owe("S1", "Imaging/", IDS), owe("S1", "Rock/", { ROCK[1], ROCK[2], ROCK[3] }),
                    owe("S2", "Jazz/", { JAZZ[1], JAZZ[2], JAZZ[3] }), owe("S3", "Pop/", { POP[1], POP[2], POP[3] })) }
  watcher()
  MockCore.run(400)
  local count, twice, done = {}, 0, 0
  for _, e in ipairs(EVENTS) do
    if e.kind == "analysis" then
      local id = e.station .. "|" .. e.dir .. e.file
      count[id] = (count[id] or 0) + 1
      if count[id] == 2 then twice = twice + 1 end
      if count[id] == 1 and e.ok then done = done + 1 end
    end
  end
  check(done == 11 and twice == 0 and #q.owed == 0, string.format("11 files analyzed, each once (%d; %d twice)", done, twice))
  local x1, x2 = watch.busy.Analysis_Player or 0, watch.busy.Analysis_Player2 or 0
  local wall = (watch.lastJob or 0) - (watch.firstJob or 0)
  check(x1 > 100 and x2 > 100 and watch.both > 1000, string.format("both analysis players worked, side by side (%.0f s and %.0f s busy)", x1, x2))
  check(wall < 0.6 * (x1 + x2), string.format("the list took %.0f s instead of %.0f s on one player", wall, x1 + x2))
  untouched("three stations on air")
  local gaps = 0
  for _, st in ipairs(Radio.stations) do
    check(playedInFull(st.id, 60))
    gaps = gaps + #evs("dead_air_guard", st.id)
    check(#evs("track_start", st.id) >= 4 and #evs("analysis", st.id) == (st.id == "S1" and 5 or 3),
      string.format("%s played all along (%d songs); its files' results ride in its own part", st.id, #evs("track_start", st.id)))
  end
  check(gaps == 0, "no dead air")
  local s1, s3 = Radio.byId.S1.learned["Imaging/id1.wav"], Radio.byId.S3.learned["Imaging/id1.wav"]
  check(s1 and s3 and s1.analyzed and s3.analyzed and s3.lead == s1.lead and s3.trail == s1.trail and s3.level == s1.level,
    "the shared imaging file: listed once (for S1), its figures used at once by S3 too")
  check(Radio.byId.S2.learned["Imaging/id1.wav"] and Radio.byId.S2.learned["Imaging/id1.wav"].analyzed, "and by S2, which lists it as well")
end

-------------------------------------------------------------------------------
do
  print("-- failures: a load that never completes, an unsupported file, a player that never reports the end, a listed")
  print("   file that is not there: each reported and passed; a station this Core does not run: passed; the rest measured")
  local BREAKS = {
    { name = "hang.wav", duration = 20, hang = true },
    { name = "ok1.wav", duration = 20, lead = 0.4, trail = 0.6, level = -6 },
    { name = "bad.wav", duration = 20, unsupported = true, status = "Unsupported file format" },
    { name = "stuck.wav", duration = 20, lead = 0.3, trail = 0.3, level = -6, noEnd = true },
    { name = "ok2.wav", duration = 20, lead = 0.6, trail = 0.9, level = -6 },
  }
  boot{ dirs = { ["Test/Tuning/"] = MUSIC, ["Test/Imaging/"] = BREAKS },
        owed = join({ { station = "Elsewhere", key = "Test/Imaging/ok1.wav" }, { station = "Main", key = "Test/Imaging/gone.wav" } },
                    owe("Main", "Test/Imaging/", BREAKS)) }
  local S = Radio.stations[1]
  S.settings.bumper_every_n_tracks, S.settings.artist_separation = 0, 0     -- the break files never go on air
  watcher()
  MockCore.run(200)
  local starts = {}
  for _, l in ipairs(MockCore.log) do
    local f = l.msg:match("^X1 PLAY (.+)$") or l.msg:match("^X1 LOAD HANGS (.+)$")
    if f and not starts[f] then starts[f] = l.t end
  end
  local hang, bad, stuck = result("Main", "Test/Imaging/hang.wav"), result("Main", "Test/Imaging/bad.wav"), result("Main", "Test/Imaging/stuck.wav")
  check(hang and hang.ok == false and tostring(hang.status):find("^timeout") and hang.at - starts["hang.wav"] < 2.5,
    string.format("a load that never completes: reported (%s) within 2.5 s", hang and hang.status or "-"))
  check(near(result("Main", "Test/Imaging/ok1.wav"), BREAKS[2], "the file after it"))
  check(bad and bad.ok == false and bad.status == "Unsupported file format" and #evs("file_unsupported") == 1
    and S.lib.byKey["Test/Imaging/bad.wav"] == nil and S.learned["Test/Imaging/bad.wav"].unsupported,
    "an unsupported file: reported, also as file_unsupported, and out of the station's library at once")
  check(stuck and stuck.ok == false and stuck.status == "timed out" and math.abs(stuck.at - starts["stuck.wav"] - 50) < 1.5,
    string.format("a player that never reports the end: given up after its length + 30 s (%.1f s)", stuck and stuck.at - starts["stuck.wav"] or -1))
  check(near(result("Main", "Test/Imaging/ok2.wav"), BREAKS[5], "the file after it"))
  local gone = result("Main", "Test/Imaging/gone.wav")
  check(gone and gone.ok == false and #evs("file_unsupported") == 1, "a listed file that is not there: tried, reported failed (" ..
    tostring(gone and gone.status) .. "), never as unsupported (the station does not list it)")
  check(result("Elsewhere", "Test/Imaging/ok1.wav") == nil and #q.owed == 1 and q.owed[1].station == "Elsewhere",
    "a file of a station not on this Core: passed, never reported (the server's list keeps it)")
  check(not Analyzer.decks[1].job and S.state == "playing" and #evs("dead_air_guard") == 0, "the analysis player is idle; the station played on")
  untouched("failures")

  print("-- the list emptied while a file is measured (Stop): that file finishes, nothing else starts")
  q.add("Main", "Test/Tuning/" .. MUSIC[1].name)
  q.add("Main", "Test/Tuning/" .. MUSIC[2].name)
  MockCore.run(5)
  local first = Analyzer.decks[1].job and Analyzer.decks[1].job.key
  q.owed = {}
  MockCore.run(80)
  check(first == "Test/Tuning/" .. MUSIC[1].name and result("Main", first) and result("Main", first).ok
    and not result("Main", "Test/Tuning/" .. MUSIC[2].name) and not Analyzer.decks[1].job, "the file on the player finished; the next was not started")
end

-------------------------------------------------------------------------------
do
  print("-- a faulted station's files are measured all the same (the analysis player is not its); the result rides in")
  print("   its part, which a faulted station still sends")
  boot{ dirs = { ["Test/Tuning/"] = MUSIC, ["Test/Imaging/"] = IMAGING } }
  MockCore.run(3)
  local realTick = Station.tick
  Station.tick = function() error("injected") end   -- every build of the station fails
  MockCore.run(3)
  local faulted = Radio.byId.Main.fault ~= nil
  q.add("Main", "Test/Tuning/" .. MUSIC[2].name)
  MockCore.run(50)
  local e = result("Main", "Test/Tuning/" .. MUSIC[2].name)
  check(faulted and Radio.byId.Main.fault and e and e.station == "Main" and near(e, MUSIC[2], "faulted station: " .. MUSIC[2].name),
    "measured while its station is faulted")
  Station.tick = realTick
end

-------------------------------------------------------------------------------
do
  print("-- roots: the analysis player's root a folder above or below the station's is translated; another fails clearly")
  local ROOTS = { ["Test/Tuning/"] = { MUSIC[1], MUSIC[3] }, ["Test/Imaging/"] = IMAGING }
  local function rootBoot(stationRoot, analysisRoot, music)
    boot{ dirs = ROOTS, owed = owe("Main", music, { MUSIC[1], MUSIC[3] }),
          settings = { Main = { rev = "r", settings = { music_dir = music, imaging_dir = "", bumper_every_n_tracks = 0, artist_separation = 0 } } },
          before = function()
            for _, p in ipairs({ "Audio_Player23", "Audio_Player24" }) do MockCore.players[p].ctl.root.String = stationRoot end
            MockCore.players.Analysis_Player.ctl.root.String = analysisRoot
          end }
    MockCore.run(110)
  end
  rootBoot("Audio/Test/", "Audio/", "Tuning/")
  local e = result("Main", "Tuning/" .. MUSIC[1].name)
  check(near(e, MUSIC[1], "station root Audio/Test/, analysis root Audio/: " .. MUSIC[1].name) and e.dir == "Tuning/" and #q.owed == 0,
    "the station's root is a folder of the analysis player's: measured, reported with the station's folder")
  rootBoot("Audio/", "Audio/Test/", "Test/Tuning/")
  e = result("Main", "Test/Tuning/" .. MUSIC[3].name)
  check(near(e, MUSIC[3], "station root Audio/, analysis root Audio/Test/: " .. MUSIC[3].name) and #q.owed == 0,
    "the analysis player's root is a folder of the station's: measured")
  rootBoot("Audio/", "Other/", "Test/Tuning/")
  e = result("Main", "Test/Tuning/" .. MUSIC[1].name)
  check(e and e.ok == false and tostring(e.status):find("outside the analysis player's root Other/", 1, true)
    and tostring(e.status):find("normally Audio/", 1, true) and result("Main", "Test/Tuning/" .. MUSIC[3].name),
    "unrelated roots: each file fails with a clear error (" .. tostring(e and e.status) .. ")")
  check(loggedCount("outside the analysis player's root") == 1 and #evs("track_start") >= 1, "logged once; the station plays")
end

-------------------------------------------------------------------------------
do
  print("-- a design without an analysis player (and a 2.0.0 station entry with mixer / mix): the station plays, no analysis")
  local sent = 0
  boot{ dirs = { ["Test/Tuning/"] = MUSIC, ["Test/Imaging/"] = IMAGING }, noAnalyzer = true,
        before = function() MockCore.mixers.RadioOut["input.2.mute.1"].Boolean = true end,   -- as 2.0.0 left it after an analysis
        cfg = function(cfg) cfg.STATIONS[1].mixer, cfg.STATIONS[1].mix = "RadioOut", { 1, 2 } end,
        core = function(body)                     -- a server that sends a list anyway (and a 2.0.0 per-station one)
          q.bodies[#q.bodies + 1] = body
          sent = sent + 1
          return { analyze = { { station = "Main", key = "Test/Tuning/" .. MUSIC[1].name } }, analyze_rev = "x" .. sent }
        end,
        sync = function() return { commands = {}, analyze = { "Test/Tuning/" .. MUSIC[1].name }, analyze_rev = "y" .. sent } end }
  local S = Radio.stations[1]
  local unmuted = MockCore.mixers.RadioOut["input.2.mute.1"].Boolean == false
  MockCore.run(120)
  local noRev, emptyList, cannot = true, false, true
  for _, b in ipairs(q.bodies) do
    if b.analyze_rev ~= nil then noRev = false end
    if b.analyzers and next(b.analyzers) == nil then emptyList = true end
    for _, p in ipairs(b.stations) do if p.state and p.state.analyze.can ~= false then cannot = false end end
  end
  check(#evs("analysis") == 0 and not Analyzer:can(), "nothing analyzed")
  check(unmuted and #evs("track_start") >= 1 and S.state == "playing" and MockCore.mixers.RadioOut["input.1.mute.1"].Boolean == false,
    "the station plays; the input 2.0.0 left muted after an analysis is unmuted at boot")
  check(noRev and emptyList and cannot, "no analyze_rev, an empty analyzers list, and each station reported as unable to analyze")
  check(loggedCount("mixer / mix no longer used (Main; their inputs unmuted once)") == 1 and loggedCount("CFG.ANALYZERS[1]: meter 'AnalysisMeter' not found: left out") == 1
    and loggedCount("analysis: 0 player(s)") == 1, "the 2.0.0 entry and the missing analysis player: one log line each")

  print("-- an analysis player named in a station's entry is left out; the station keeps its player")
  boot{ dirs = { ["Test/Tuning/"] = MUSIC, ["Test/Imaging/"] = IMAGING },
        analyzers = { { player = "Audio_Player24", meter = "PlayerMeter", meter_idx = 2 }, { player = "Analysis_Player", meter = "AnalysisMeter" },
                      { player = "Analysis_Player", meter = "AnalysisMeter" }, "nonsense" } }
  MockCore.run(200)
  check(#Analyzer.decks == 1 and Analyzer.decks[1].id == "Analysis_Player" and loggedCount("player 'Audio_Player24' is station Main's") == 1
    and loggedCount("player 'Analysis_Player' is listed twice") == 1 and loggedCount("CFG.ANALYZERS[4]: no player named") == 1,
    "a station's player, a player listed twice and a bad entry: each left out with a log line")
  check(#evs("track_start") >= 2 and #evs("dead_air_guard") == 0 and Radio.stations[1].decks[2].st == Radio.stations[1],
    "the station plays both decks as before")
end

print = realPrint
print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
