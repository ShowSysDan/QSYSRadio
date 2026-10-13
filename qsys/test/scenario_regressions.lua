-- Regression cases for bugs found in review (each case boots a fresh station).
-- Run with run_mock.py.
dofile("qsys/test/mock_core.lua")
local json = require("json")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local function evs(kind) local o = {} for _, e in ipairs(EVENTS) do if e.kind == kind then o[#o + 1] = e end end return o end
local function count(kind) return #evs(kind) end
local function when(cond, fn) local function chk() if cond() then fn() else MockCore.at(MockCore.clock + 0.05, chk) end end MockCore.at(0.05, chk) end

local function boot(music, imaging, before)
  for _, f in ipairs({ "outbox", "settings" }) do os.remove("media/_radio/" .. f .. ".json") end
  MockCore.setup{ seed = 2, dirs = { ["Test/Tuning/"] = music, ["Test/Imaging/"] = imaging or {} } }
  if before then before() end
  dofile("qsys/radio/radio.lua")
end
local function songs(n, dur)
  local o = {}
  for i = 1, n do o[i] = { name = string.char(64 + i) .. " - Song" .. i .. ".flac", duration = dur, lead = 0.3, trail = 0.5, level = -10 } end
  return o
end
local BUMPERS = { { name = "b1.wav", duration = 6, level = -6 }, { name = "b2.wav", duration = 5, level = -6 } }

print("-- short tracks: every start has an end, no dead air")
boot(songs(4, 7), BUMPERS)
SETTINGS.bumper_every_n_tracks = 2
MockCore.run(60)
check(count("track_start") >= 5 and count("track_end") >= count("track_start") - 1,
  string.format("track_end logged for short tracks (%d starts, %d ends)", count("track_start"), count("track_end")))
check(count("dead_air_guard") == 0, "no dead air with 7 s tracks")

print("-- a fade left on a deck never touches the next track; learned peak stays sane")
boot(songs(4, 7), { { name = "b3.wav", duration = 3, level = -6 } })
SETTINGS.bumper_every_n_tracks = 1
MockCore.run(40)
local maxPeak = -200
for _, e in ipairs(evs("track_end")) do if e.peak and e.peak > maxPeak then maxPeak = e.peak end end
check(count("dead_air_guard") == 0, "short bumper: no dead air")
check(maxPeak <= -5, string.format("learned peak not inflated by fades (max %.1f dBFS, files peak at -10)", maxPeak))

print("-- skip / bumper requests never stick")
boot(songs(4, 40), BUMPERS)
SETTINGS.bumper_every_n_tracks = 1
local skipRes
when(function() return Station.state == "bumper" end, function() skipRes = Control.run({ cmd = "skip" }) end)
MockCore.run(90)
check(skipRes == false and not Station.skipRequested, "skip during a bumper is refused")
local early = false
for _, s in ipairs(evs("track_start")) do
  for _, tr in ipairs(evs("transition")) do if tr.reason == "skip" and tr.at > s.at and tr.at < s.at + 5 then early = true end end
end
check(not early, "no track is cut by a stale skip")

boot(songs(4, 40), BUMPERS)
SETTINGS.bumper_every_n_tracks = 1
local bumpRes
when(function() return Station.state == "bumper" end, function() bumpRes = Control.run({ cmd = "bumper_now" }) end)
MockCore.run(100)
check(bumpRes == false and not Station.bumperRequested, "bumper_now during a bumper is refused")

boot(songs(4, 40), BUMPERS)
SETTINGS.bumper_every_n_tracks = 0
MockCore.at(10, function() Station.setAutomation(false, "test") end)
MockCore.at(20, function() Control.run({ cmd = "skip" }) end)    -- Skip while off
MockCore.at(30, function() Station.setAutomation(true, "test") end)
MockCore.run(45)
local restart
for _, s in ipairs(evs("track_start")) do if s.at >= 30 then restart = restart or s end end
local cut = false
for _, tr in ipairs(evs("transition")) do if restart and tr.at > restart.at and tr.at < restart.at + 10 then cut = true end end
check(restart and not cut, "skip while stopped does not cut the first track after start")
check(count("dead_air_guard") == 0, "start plays immediately (no dead-air guard event)")

print("-- empty music folder: no event flood")
boot(songs(4, 40), BUMPERS)
MockCore.run(5)
Server.applySettings({ music_dir = "Nope/" }, "r9")
local n0 = EVENT_SEQ
MockCore.run(600)
check(EVENT_SEQ - n0 < 10 and count("library_empty") == 1,
  string.format("600 s with an empty folder: %d events (library_empty once)", EVENT_SEQ - n0))
local fixedAt = MockCore.clock
Server.applySettings({}, "r10")                     -- folder setting fixed
MockCore.run(3)
local resumed = false
for _, e in ipairs(evs("track_start")) do if e.at >= fixedAt then resumed = true end end
check(resumed, "music plays within 3 s once the folder is fixed")

print("-- track ending during a stop fade is still logged")
boot(songs(4, 40), BUMPERS)
SETTINGS.bumper_every_n_tracks, SETTINGS.crossfade_s = 0, 0.5
MockCore.at(39.2, function() Station.setAutomation(false, "t") end)
MockCore.run(45)
check(count("track_end") == count("track_start"), "track_end for the track that ended inside the stop fade")

print("-- uptime is time since boot")
boot(songs(4, 40), BUMPERS, function() MockCore.clock = 5000 end)
MockCore.run(12)
check(Station.snapshot(Timer.Now()).uptime <= 13, "uptime counts from boot, not from the Core clock's epoch")

print("-- corrupt settings.json is kept, not overwritten")
boot(songs(4, 7), BUMPERS, function()
  local f = io.open("media/_radio/settings.json", "w"); f:write('{"rev": "r1", "settings": {"crossfade_s": 9'); f:close()
end)
MockCore.run(5)
local bad = io.open("media/_radio/settings.json.bad", "r")
check(bad ~= nil and bad:read("*a"):find('"crossfade_s": 9', 1, true) ~= nil and SETTINGS.crossfade_s == DEFAULTS.crossfade_s,
  "unreadable settings.json saved as settings.json.bad; fallback settings used")
if bad then bad:close() end

print("-- transient load failures are not blacklisted forever")
boot({ { name = "A - Song1.flac", duration = 40, level = -10 }, { name = "Z - Gone.flac", duration = 40, unsupported = true, status = "Missing" },
       { name = "B - Song2.flac", duration = 40, level = -10 } }, BUMPERS)
MockCore.run(130)
check(not (LEARNED["Test/Tuning/Z - Gone.flac"] and LEARNED["Test/Tuning/Z - Gone.flac"].unsupported) and count("file_failed") >= 1,
  "a 'Missing' file is skipped for now, not marked unsupported")

print("-- bumper every N songs: exactly N songs between bumpers")
boot(songs(6, 20), BUMPERS)
SETTINGS.bumper_every_n_tracks = 2
MockCore.run(200)
local order = {}
for _, e in ipairs(EVENTS) do
  if e.kind == "track_start" then order[#order + 1] = "S" elseif e.kind == "bumper_start" then order[#order + 1] = "B" end
end
local seqStr = table.concat(order)
check(seqStr:sub(1, 8) == "SSBSSBSS" and not seqStr:find("SSS", 1, true), "every 2 songs: SSBSSB... (" .. seqStr .. ")")

print("-- play now on a bumper while stopped plays that bumper")
boot(songs(4, 40), BUMPERS)
MockCore.run(5)
Station.setAutomation(false, "t")
MockCore.run(5)
Control.run({ cmd = "play_now", args = { key = "Test/Imaging/b2.wav" } })
MockCore.run(10)
local b2
for _, e in ipairs(evs("bumper_start")) do if e.file == "b2.wav" then b2 = e end end
check(Station.automation and b2 ~= nil, "station started and the chosen bumper played")

print("-- a file the server knows is unsupported leaves the rotation")
boot(songs(4, 40), BUMPERS)
Library.learn({ ["Test/Tuning/B - Song2.flac"] = { unsupported = true }, ["Test/Tuning/C - Song3.flac"] = { trail = 1.5 } })
check(Library.byKey["Test/Tuning/B - Song2.flac"] == nil and #Library.music == 3 and LEARNED["Test/Tuning/C - Song3.flac"].trail == 1.5,
  "dropped from the library; learned trail kept")
Library.scan()
check(Library.byKey["Test/Tuning/B - Song2.flac"] == nil, "and stays out after a rescan")

print("-- the outbox is written only while the server is unreachable")
boot(songs(4, 20), BUMPERS, function()
  MockCore.httpHandler = function(req)
    if MockCore.clock >= 60 and MockCore.clock < 100 then return 0, "", "timeout" end
    return 200, '{"commands":[]}'
  end
end)
CFG.SERVER_URL = "http://radio-server:5410"
local function outboxFile()
  local f = io.open(FILES.outbox, "r")
  if not f then return nil end
  local d = json.decode(f:read("*a")); f:close(); return d
end
MockCore.run(59)
check(outboxFile() == nil, "nothing written while the server answers")
MockCore.run(40)
local during = outboxFile()
check(during and #during > 0, string.format("written during the outage (%d events)", during and #during or 0))
MockCore.run(60)
local after = outboxFile()
check(after and #after == 0 and #Server.outbox == 0, "emptied once delivered")

print("-- rescans do not restart the shuffle: every song plays (no server)")
local many = {}
for i = 1, 24 do many[i] = { name = string.format("Artist%d - Song%02d.mp3", i % 8, i), duration = 20 + (i * 7) % 15, level = -10 } end
boot(many, {})
SETTINGS.bumper_every_n_tracks = 0
for t = 60, 1200, 75 do MockCore.at(t, function() Library.scan() end) end
MockCore.run(1260)
local heard = {}
for _, e in ipairs(evs("track_start")) do heard[e.file] = true end
local missing = 0
for _, tr in ipairs(many) do if not heard[tr.name] then missing = missing + 1 end end
check(missing == 0, string.format("all 24 songs played in %d plays with a rescan every 75 s (%d never)", count("track_start"), missing))

print("-- lightweight: fast timer only while fading/loading")
boot(songs(6, 60), BUMPERS)
SETTINGS.bumper_every_n_tracks = 0
local fastTicks = 0
local realFast = Engine.fast
Engine.fast = function() fastTicks = fastTicks + 1; return realFast() end
MockCore.run(300)
check(fastTicks < 300 * 20 * 0.15, string.format("50 ms timer ran %d times in 300 s (always-on would be 6000)", fastTicks))
check(count("track_start") >= 5, "still crossfading normally")

print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
