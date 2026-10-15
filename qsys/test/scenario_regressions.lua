-- Regression cases for bugs found in review (each case boots a fresh station).
-- Run with run_mock.py.
dofile("qsys/test/mock_core.lua")
local json = require("json")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local function evs(kind) local o = {} for _, e in ipairs(EVENTS) do if e.kind == kind then o[#o + 1] = e end end return o end
local function count(kind) return #evs(kind) end
local function when(cond, fn) local function chk() if cond() then fn() else MockCore.at(MockCore.clock + 0.05, chk) end end MockCore.at(0.05, chk) end

local S                                 -- the station (CFG.STATIONS[1]) of the current boot
local function boot(music, imaging, before)
  for _, f in ipairs({ "outbox", "settings" }) do os.remove("media/_radio/" .. f .. ".json") end
  MockCore.setup{ seed = 2, dirs = { ["Test/Tuning/"] = music, ["Test/Imaging/"] = imaging or {} } }
  if before then before() end
  dofile("qsys/radio/radio.lua")
  S = Radio.stations[1]
end
local function songs(n, dur)
  local o = {}
  for i = 1, n do o[i] = { name = string.char(64 + i) .. " - Song" .. i .. ".flac", duration = dur, lead = 0.3, trail = 0.5, level = -10 } end
  return o
end
local BUMPERS = { { name = "b1.wav", duration = 6, level = -6 }, { name = "b2.wav", duration = 5, level = -6 } }

print("-- short tracks: every start has an end, no dead air")
boot(songs(4, 7), BUMPERS)
S.settings.bumper_every_n_tracks = 2
MockCore.run(60)
check(count("track_start") >= 5 and count("track_end") >= count("track_start") - 1,
  string.format("track_end logged for short tracks (%d starts, %d ends)", count("track_start"), count("track_end")))
check(count("dead_air_guard") == 0, "no dead air with 7 s tracks")

print("-- a fade left on a deck never touches the next track; learned peak stays sane")
boot(songs(4, 7), { { name = "b3.wav", duration = 3, level = -6 } })
S.settings.bumper_every_n_tracks = 1
MockCore.run(40)
local maxPeak = -200
for _, e in ipairs(evs("track_end")) do if e.peak and e.peak > maxPeak then maxPeak = e.peak end end
check(count("dead_air_guard") == 0, "short bumper: no dead air")
check(maxPeak <= -5, string.format("learned peak not inflated by fades (max %.1f dBFS, files peak at -10)", maxPeak))

print("-- skip / bumper requests never stick")
boot(songs(4, 40), BUMPERS)
S.settings.bumper_every_n_tracks = 1
local skipRes
when(function() return S.state == "bumper" end, function() skipRes = S:command({ cmd = "skip" }) end)
MockCore.run(90)
check(skipRes == false and not S.skipRequested, "skip during a bumper is refused")
local early = false
for _, s in ipairs(evs("track_start")) do
  for _, tr in ipairs(evs("transition")) do if tr.reason == "skip" and tr.at > s.at and tr.at < s.at + 5 then early = true end end
end
check(not early, "no track is cut by a stale skip")

boot(songs(4, 40), BUMPERS)
S.settings.bumper_every_n_tracks = 1
local bumpRes
when(function() return S.state == "bumper" end, function() bumpRes = S:command({ cmd = "bumper_now" }) end)
MockCore.run(100)
check(bumpRes == false and not S.bumperRequested, "bumper_now during a bumper is refused")

boot(songs(4, 40), BUMPERS)
S.settings.bumper_every_n_tracks = 0
MockCore.at(10, function() S:setAutomation(false, "test") end)
MockCore.at(20, function() S:command({ cmd = "skip" }) end)    -- Skip while off
MockCore.at(30, function() S:setAutomation(true, "test") end)
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
S:applySettings({ music_dir = "Nope/" }, "r9")
local n0 = EVENT_SEQ
MockCore.run(600)
check(EVENT_SEQ - n0 < 10 and count("library_empty") == 1,
  string.format("600 s with an empty folder: %d events (library_empty once)", EVENT_SEQ - n0))
local fixedAt = MockCore.clock
S:applySettings({}, "r10")                     -- folder setting fixed
MockCore.run(3)
local resumed = false
for _, e in ipairs(evs("track_start")) do if e.at >= fixedAt then resumed = true end end
check(resumed, "music plays within 3 s once the folder is fixed")

print("-- track ending during a stop fade is still logged")
boot(songs(4, 40), BUMPERS)
S.settings.bumper_every_n_tracks, S.settings.crossfade_s = 0, 0.5
MockCore.at(39.2, function() S:setAutomation(false, "t") end)
MockCore.run(45)
check(count("track_end") == count("track_start"), "track_end for the track that ended inside the stop fade")

print("-- uptime is time since boot")
boot(songs(4, 40), BUMPERS, function() MockCore.clock = 5000 end)
MockCore.run(12)
check(S:snapshot(Timer.Now()).uptime <= 13, "uptime counts from boot, not from the Core clock's epoch")

print("-- corrupt settings.json is kept, not overwritten")
boot(songs(4, 7), BUMPERS, function()
  local f = io.open("media/_radio/settings.json", "w"); f:write('{"rev": "r1", "settings": {"crossfade_s": 9'); f:close()
end)
MockCore.run(5)
local bad = io.open("media/_radio/settings.json.bad", "r")
check(bad ~= nil and bad:read("*a"):find('"crossfade_s": 9', 1, true) ~= nil and S.settings.crossfade_s == SETTINGS.crossfade_s,
  "unreadable settings.json saved as settings.json.bad; fallback settings used")
if bad then bad:close() end

print("-- transient load failures are not blacklisted forever")
boot({ { name = "A - Song1.flac", duration = 40, level = -10 }, { name = "Z - Gone.flac", duration = 40, unsupported = true, status = "Missing" },
       { name = "B - Song2.flac", duration = 40, level = -10 } }, BUMPERS)
MockCore.run(130)
check(not (S.learned["Test/Tuning/Z - Gone.flac"] and S.learned["Test/Tuning/Z - Gone.flac"].unsupported) and count("file_failed") >= 1,
  "a 'Missing' file is skipped for now, not marked unsupported")

print("-- bumper every N songs: exactly N songs between bumpers")
boot(songs(6, 20), BUMPERS)
S.settings.bumper_every_n_tracks = 2
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
S:setAutomation(false, "t")
MockCore.run(5)
S:command({ cmd = "play_now", args = { key = "Test/Imaging/b2.wav" } })
MockCore.run(10)
local b2
for _, e in ipairs(evs("bumper_start")) do if e.file == "b2.wav" then b2 = e end end
check(S.automation and b2 ~= nil, "station started and the chosen bumper played")

print("-- a file the server knows is unsupported leaves the rotation")
boot(songs(4, 40), BUMPERS)
S:learn({ ["Test/Tuning/B - Song2.flac"] = { unsupported = true }, ["Test/Tuning/C - Song3.flac"] = { trail = 1.5 } })
check(S.lib.byKey["Test/Tuning/B - Song2.flac"] == nil and #S.lib.music == 3 and S.learned["Test/Tuning/C - Song3.flac"].trail == 1.5,
  "dropped from the library; learned trail kept")
S:scan()
check(S.lib.byKey["Test/Tuning/B - Song2.flac"] == nil, "and stays out after a rescan")

print("-- the server's figures replace this boot's; the operator's (fixed) are not learned over")
boot(songs(2, 30), {})
S.settings.bumper_every_n_tracks, S.settings.artist_separation = 0, 0
S:learn({ ["Test/Tuning/A - Song1.flac"] = { trail = 2.5, lead = 0.1, fixed = true } })
MockCore.run(130)
local fixedA, freeB = S.learned["Test/Tuning/A - Song1.flac"], S.learned["Test/Tuning/B - Song2.flac"]
check(fixedA.trail == 2.5 and fixedA.lead == 0.1, "operator's figures kept through plays")
check(freeB and freeB.lead and freeB.lead > 0, "other files still learned this boot")
S:learn({})
check(S.learned["Test/Tuning/A - Song1.flac"].trail == nil and S.learned["Test/Tuning/B - Song2.flac"].lead == nil
  and not S.learned["Test/Tuning/A - Song1.flac"].fixed, "forgotten on the server: forgotten on the Core")

print("-- the outbox is written only while the server is unreachable")
boot(songs(4, 20), BUMPERS, function()
  MockCore.serve{ down = function() if MockCore.clock >= 60 and MockCore.clock < 100 then return 0, "", "timeout" end end }
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
Tick:Stop()                            -- no new events, then let the sender and the 30 s file write catch up
MockCore.run(35)
Tick:Start(0.25)
local after = outboxFile()
check(after and #after == 0 and #Server.outbox == 0, "emptied once delivered")

print("-- rescans do not restart the shuffle: every song plays (no server)")
local many = {}
for i = 1, 24 do many[i] = { name = string.format("Artist%d - Song%02d.mp3", i % 8, i), duration = 20 + (i * 7) % 15, level = -10 } end
boot(many, {})
S.settings.bumper_every_n_tracks = 0
for t = 60, 1200, 75 do MockCore.at(t, function() S:scan() end) end
MockCore.run(1260)
local heard = {}
for _, e in ipairs(evs("track_start")) do heard[e.file] = true end
local missing = 0
for _, tr in ipairs(many) do if not heard[tr.name] then missing = missing + 1 end end
check(missing == 0, string.format("all 24 songs played in %d plays with a rescan every 75 s (%d never)", count("track_start"), missing))

print("-- quiet start skipped from the second play on; lead-in still measured from the file start")
boot({ { name = "A - Quiet.flac", duration = 25, lead = 4.0, trail = 0.5, level = -10 },
       { name = "B - Song.flac", duration = 25, lead = 0.2, trail = 0.5, level = -10 } }, {})
S.settings.bumper_every_n_tracks, S.settings.artist_separation = 0, 0
MockCore.run(140)
local quiet = {}
for _, e in ipairs(EVENTS) do
  if e.file == "A - Quiet.flac" and (e.kind == "track_start" or e.kind == "track_end") then quiet[#quiet + 1] = e end
end
local firstStart, laterStart, laterEnd
for _, e in ipairs(quiet) do
  if e.kind == "track_start" then
    if not firstStart then firstStart = e elseif not laterStart then laterStart = e end
  elseif laterStart and not laterEnd and e.kind == "track_end" then laterEnd = e end
end
check(firstStart and not firstStart.skip and laterStart and laterStart.skip == 3,
  string.format("first play from 0:00, later play skips %s s (capped at 3)", laterStart and tostring(laterStart.skip) or "-"))
check(laterEnd and laterEnd.lead_in and math.abs(laterEnd.lead_in - 4) <= 0.5,
  string.format("lead-in still reported from the file start (%s s)", laterEnd and tostring(laterEnd.lead_in) or "-"))
S.settings.lead_skip_max_s = 0
local before = #evs("track_start")
MockCore.run(60)
local off = true
for i, e in ipairs(evs("track_start")) do if i > before and e.skip then off = false end end
check(off, "lead_skip_max_s = 0: every song from the start")

print("-- a crossfade is not a silent tail: learned trail stays the file's own, songs play to their crossfade point")
boot(songs(3, 30), {})
S.settings.bumper_every_n_tracks = 0
MockCore.run(200)
local maxTrail, minPlayed = 0, 99
for _, e in ipairs(evs("track_end")) do
  if e.trail and e.trail > maxTrail then maxTrail = e.trail end
  if e.played and e.played < minPlayed then minPlayed = e.played end
end
check(maxTrail < 2, string.format("no crossfade learned as a trail (largest %.2f s, files have 0.5 s)", maxTrail))
check(minPlayed > 29, string.format("every song plays ~30 s (shortest %.1f s)", minPlayed))

print("-- a fade-out at the end starts the next song early, by at most fade_out_early_s")
boot({ { name = "A - Fades.flac", duration = 30, lead = 0.2, trail = 0.5, level = -10, fade = 10 },
       { name = "B - Song.flac", duration = 30, lead = 0.2, trail = 0.5, level = -10 } }, {})
S.settings.bumper_every_n_tracks, S.settings.artist_separation = 0, 0
MockCore.run(200)
local fades, fadePlayed = 0, nil
for _, e in ipairs(evs("transition")) do if e.reason == "fade" then fades = fades + 1 end end
for _, e in ipairs(evs("track_end")) do if e.file == "A - Fades.flac" then fadePlayed = e.played end end
check(fades >= 2, string.format("fading song handed over early (%d times)", fades))
check(fadePlayed and fadePlayed >= 27.5 and fadePlayed < 29.5,
  string.format("early by at most 2 s (played %s s of 30, crossfade included)", tostring(fadePlayed)))
boot({ { name = "A - Fades.flac", duration = 30, lead = 0.2, trail = 0.5, level = -10, fade = 10 },
       { name = "B - Song.flac", duration = 30, lead = 0.2, trail = 0.5, level = -10 } }, {})
S.settings.bumper_every_n_tracks, S.settings.artist_separation, S.settings.fade_out_early_s = 0, 0, 0
MockCore.run(120)
fades = 0
for _, e in ipairs(evs("transition")) do if e.reason == "fade" then fades = fades + 1 end end
check(fades == 0, "fade_out_early_s = 0: off")

print("-- break items: quiet start / end learned and skipped like songs")
boot(songs(4, 30), {}, function()
  MockCore.dirs["Test/Ads/"] = { { name = "ad1.wav", duration = 12, lead = 0.2, trail = 3, level = -8 },
                                 { name = "ad2.wav", duration = 12, lead = 0.2, trail = 3, level = -8 } }
end)
S.settings.ads_dir, S.settings.ad_every_n_tracks, S.settings.ads_per_break, S.settings.bumper_every_n_tracks = "Test/Ads/", 1, 2, 0
S:scan()
MockCore.run(240)
local adStarts, learnedAd = evs("ad_start"), S.learned["Test/Ads/ad1.wav"] or {}
local gaps = {}
for i = 2, #adStarts do
  local g = adStarts[i].at - adStarts[i - 1].at
  if g < 15 then gaps[#gaps + 1] = g end          -- back to back in one break
end
check(learnedAd.trail and math.abs(learnedAd.trail - 3) < 0.6, "an ad's silent tail is learned (" .. tostring(learnedAd.trail) .. " s)")
check(#gaps >= 2 and gaps[#gaps] <= 10.5, string.format("the next ad starts at the quiet end (last gap %.1f s of a 12 s ad)", gaps[#gaps] or -1))

print("-- a fade-in is not a quiet start: the lead-in is not learned from it, the skip never creeps")
boot(songs(3, 30), {})
S.settings.bumper_every_n_tracks, S.settings.artist_separation, S.settings.fade_in_s = 0, 0, 2
MockCore.run(240)
local maxLead, maxSkip = 0, 0
for _, e in ipairs(evs("track_end")) do if e.lead_in and e.lead_in > maxLead then maxLead = e.lead_in end end
for _, e in ipairs(evs("track_start")) do if e.skip and e.skip > maxSkip then maxSkip = e.skip end end
check(maxLead < 1, string.format("lead-in stays the file's own (largest %.2f s, files start at 0.3 s)", maxLead))
check(maxSkip < 0.5, string.format("skip stays under the quiet start (largest %.1f s)", maxSkip))

print("-- a station faulted after a long healthy run: retries after 30, 60, 120 s (2.0.0 dev reset the back-off")
print("   in the tick that faulted it: 30, 30, 60)")
boot(songs(4, 40), BUMPERS)
MockCore.run(700)                                 -- more than K.healthy_s on air
local realTick = Station.tick
Station.tick = function() error("injected") end   -- every build of the station fails
MockCore.run(150)                                 -- faults at about +0, +31 and +92 s
Station.tick = realTick
local retries = {}
for _, e in ipairs(evs("station_fault")) do retries[#retries + 1] = string.format("%d", e.retry_s) end
check(table.concat(retries, ",") == "30,60,120", "retry back-off doubles from the first fault (" .. table.concat(retries, ",") .. ")")

print("-- a listed file out of the library for a while (a failed load) is analyzed once a rescan brings it back")
print("   (2.0.0 dev moved the analysis cursor past it for good)")
local FLAKY = { name = "Z - Flaky.flac", duration = 60, lead = 0.4, trail = 0.8, level = -10, unsupported = true, status = "Missing" }
local flakyMusic = songs(3, 40)
flakyMusic[4] = FLAKY
boot(flakyMusic, BUMPERS)
S.settings.bumper_every_n_tracks = 0
local listedAt
when(function() return count("file_failed") >= 1 end, function()
  S:setAutomation(false, "t")                     -- stopped: both decks may analyze
  FLAKY.unsupported = nil                         -- the file loads again
  S:analyze({ "Test/Tuning/Z - Flaky.flac", "Test/Tuning/A - Song1.flac" }, "r1")
  listedAt = MockCore.clock
end)
MockCore.run(200)
local function analyzed(name) for _, e in ipairs(evs("analysis")) do if e.file == name and e.ok then return true end end return false end
check(listedAt and S.lib.byKey["Test/Tuning/Z - Flaky.flac"] == nil and not analyzed(FLAKY.name) and analyzed("A - Song1.flac"),
  "the failed file waits for the next scan; the rest of the list is analyzed")
S:scan()
MockCore.run(120)
check(analyzed(FLAKY.name), "analyzed once a rescan brings it back")

print("-- figures kept only for the files listed now (2.0.0 dev kept one for every file it had ever played,")
print("   through every folder change: growing with uptime)")
boot(songs(3, 30), {}, function() for f = 1, 3 do MockCore.dirs["F" .. f .. "/"] = songs(3, 30) end end)
S.settings.bumper_every_n_tracks, S.settings.artist_separation = 0, 0
for f = 1, 3 do
  MockCore.at(f * 70, function()
    S:applySettings({ music_dir = "F" .. f .. "/", imaging_dir = "", bumper_every_n_tracks = 0, artist_separation = 0 }, "r" .. f)
  end)
end
MockCore.run(260)
local function outside(dir)
  local n = 0
  for key in pairs(S.learned) do if key:sub(1, #dir) ~= dir then n = n + 1 end end
  return n
end
local early = 0
for key in pairs(S.learned) do if key:find("^Test/Tuning/") or key:find("^F1/") then early = early + 1 end end
local heard = {}
for _, e in ipairs(evs("track_start")) do heard[e.dir] = true end
check(heard["Test/Tuning/"] and heard["F1/"] and heard["F2/"] and heard["F3/"] and early == 0 and outside("F3/") <= 2,
  string.format("3 folder changes: nothing kept of the folders before the last two; %d outside F3/ (the songs on the decks)", outside("F3/")))
S:scan()
check(outside("F3/") == 0, "and nothing outside F3/ after the next scan")

print("-- a faulted station's state still says whether it can analyze (2.0.0 dev left it out: the Music page then")
print("   sent the operator to a correct CFG mixer instead of naming the fault)")
local faultedState
boot(songs(4, 40), BUMPERS, function()
  MockCore.serve{ sync = function(part) if part.state and part.state.fault then faultedState = faultedState or part.state end end }
end)
CFG.SERVER_URL = "http://radio-server:5410"
MockCore.run(5)
S.tick = function() error("injected") end
MockCore.run(15)
check(faultedState and faultedState.analyze and faultedState.analyze.can == true and faultedState.analyze.left == 0,
  "its state reports analyze.can: its CFG entry names a mixer")

print("-- a station whose sync part is answered { error } keeps its events on disk: a script restart replays them")
print("   (2.0.0 dev kept them in memory only: the server answered, so outbox.json was never written)")
local function readOutbox()
  local f = io.open("media/_radio/outbox.json", "r")
  if not f then return nil end
  local ok, d = pcall(json.decode, f:read("*a"))
  f:close()
  return ok and d or nil
end
local function scriptStart(serve)               -- the files stay: a paste or design deploy restarts the script
  MockCore.setup{ seed = 2, dirs = { ["Test/Tuning/"] = songs(4, 40), ["Test/Imaging/"] = BUMPERS } }
  MockCore.serve(serve)
  MockCore.loadRadio{ SERVER_URL = "http://radio-server:5410" }
  S = Radio.stations[1]
end
for _, f in ipairs({ "outbox", "settings" }) do os.remove("media/_radio/" .. f .. ".json") end
scriptStart{ sync = function() return { error = "boom" } end }
MockCore.run(125)
local firstBoot, inRam, failsThen, onDisk = BOOT_ID, #Server.outbox, Server.fails, readOutbox()
local replayed = {}
scriptStart{ sync = function(part)
  for _, e in ipairs(part.events or {}) do if e.boot == firstBoot then replayed[e.seq] = true end end
end }
MockCore.run(70)
local missing = 0
for _, e in ipairs(onDisk or {}) do if not replayed[e.seq] then missing = missing + 1 end end
check(failsThen == 0 and inRam >= 10 and onDisk and #onDisk >= inRam - 3,
  string.format("2 min of { error } parts (the server reachable): %d events kept, %d of them in outbox.json", inRam, onDisk and #onDisk or 0))
check(onDisk and missing == 0, string.format("after a script restart every one of them is replayed (%d missing)", missing))
local cleared = readOutbox()
check(cleared and #cleared == 0, "and outbox.json is emptied once the server has them")

print("-- lightweight: fast timer only while fading/loading")
boot(songs(6, 60), BUMPERS)
S.settings.bumper_every_n_tracks = 0
local fastTicks = 0
local realFast = Engine.fast
Engine.fast = function() fastTicks = fastTicks + 1; return realFast() end
MockCore.run(300)
check(fastTicks < 300 * 20 * 0.15, string.format("50 ms timer ran %d times in 300 s (always-on would be 6000)", fastTicks))
check(count("track_start") >= 5, "still crossfading normally")

print("-- Tick and Fast allocate nothing (1.6.0 built a deck list every tick while analyzing and every 50 ms,")
print("   and a table per crossfade check of a file without figures)")
boot(songs(4, 60), BUMPERS)
S.settings.bumper_every_n_tracks = 0
MockCore.run(70)                                  -- the second song, mid-way, first play: no figures yet
local waiting                                     -- a file to analyze, but no deck free for it (the song on air
for _, tr in ipairs(S.lib.music) do               -- ends in under K.scan_need_s): the list waits, checked every tick
  if tr ~= S.live.track and tr ~= S.other.track then waiting = waiting or tr.key end
end
S:analyze({ waiting }, "r1")
local function allocated(fn, n)
  collectgarbage("collect")
  collectgarbage("stop")
  fn()                                            -- once first: the collect shrank the Lua stack, which regrows here
  local before = collectgarbage("count")
  for _ = 1, n do fn() end
  local kb = collectgarbage("count") - before
  collectgarbage("restart")
  return kb
end
local playing = S.state == "playing" and S.live and not S.learned[S.live.track.key]
local tickKb = allocated(function() Radio.tick() end, 2000)
local pending = S.an.pos <= #S.an.todo and not (S.decks[1].job or S.decks[2].job)
S.live:fadeOut(60, function() end)
local fastKb = allocated(function() MockCore.clock = MockCore.clock + 0.001; Engine.fast() end, 2000)
check(playing and pending and tickKb == 0 and fastKb == 0,
  string.format("2,000 ticks (a file waiting to be analyzed): %.2f KB, 2,000 fade steps: %.2f KB allocated", tickKb, fastKb))

print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
