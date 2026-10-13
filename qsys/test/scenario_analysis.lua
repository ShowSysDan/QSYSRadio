-- Scenario: file analysis. The server's list of files to analyze is applied
-- directly (no server); checks the figures, that analysis audio never reaches
-- the air (the deck's PlayerSum input is muted) and the live deck never is,
-- that on-air use takes the deck back at once, both decks while stopped, and a
-- design without the mixer. Run with run_mock.py.
dofile("qsys/test/mock_core.lua")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local function evs(kind) local o = {} for _, e in ipairs(EVENTS) do if e.kind == kind then o[#o + 1] = e end end return o end

local MUSIC = {
  { name = "A - Quiet Start.flac", duration = 90, lead = 2.0, trail = 0.5, level = -10 },
  { name = "B - Long Tail.flac", duration = 90, lead = 0.2, trail = 6.0, level = -10 },
  { name = "C - Plain.flac", duration = 90, lead = 0.4, trail = 0.8, level = -10 },
  { name = "D - Plain.flac", duration = 90, lead = 0.3, trail = 1.2, level = -10 },
}
local IMAGING = { { name = "id.wav", duration = 5, lead = 0.5, trail = 1.0, level = -6 } }

local function boot(spec)
  for _, f in ipairs({ "outbox", "settings" }) do os.remove("media/_radio/" .. f .. ".json") end
  MockCore.setup{ seed = 3, noMixer = spec and spec.noMixer, dirs = { ["Test/Tuning/"] = MUSIC, ["Test/Imaging/"] = IMAGING } }
  dofile("qsys/radio/radio.lua")
  SETTINGS.bumper_every_n_tracks, SETTINGS.artist_separation = 0, 0
end
local function all()
  local o = {}
  for _, f in ipairs(MUSIC) do o[#o + 1] = "Test/Tuning/" .. f.name end
  o[#o + 1] = "Test/Imaging/id.wav"
  return o
end

-- every 50 ms: an analyzing deck that plays is muted; the live deck never is
local leaks, mutedLive, usedDecks = 0, 0, {}
local function watch()
  for i, d in ipairs({ DeckA, DeckB }) do
    local mute, playing = MockCore.mixer and MockCore.mixer["input." .. i .. ".mute.1"].Boolean, MockCore.players[i == 1 and "Audio_Player_01" or "Audio_Player_02"].playing
    if d.job and playing and not mute then leaks = leaks + 1 end
    if d.job then usedDecks[d.id] = true end
    if d == Station.live and d.state == "playing" and mute then mutedLive = mutedLive + 1 end
  end
  MockCore.at(MockCore.clock + 0.05, watch)
end

print("-- on air: the idle deck analyzes, off air, and gives way to the station")
boot()
MockCore.at(0.05, watch)
MockCore.at(1, function() Analyzer.apply(all(), "r1") end)
MockCore.run(700)
local got = {}
for _, e in ipairs(evs("analysis")) do if e.ok then got[e.dir .. e.file] = e end end
local n = 0
for _ in pairs(got) do n = n + 1 end
check(n == 5, string.format("every listed file analyzed (%d of 5)", n))
local function near(e, f, what)
  -- the meter falls away after the sound stops, so a quiet end reads up to ~0.7 s short (the safe side: hand-over a bit later)
  return e and math.abs(e.lead_in - f.lead) <= 0.5 and e.trail <= f.trail + 0.3 and e.trail >= f.trail - 0.8, string.format(
    "%s: quiet start %.2f s (file %.1f), quiet end %.2f s (file %.1f)", what, e and e.lead_in or -1, f.lead, e and e.trail or -1, f.trail)
end
for _, f in ipairs(MUSIC) do check(near(got["Test/Tuning/" .. f.name], f, f.name)) end
check(near(got["Test/Imaging/id.wav"], IMAGING[1], "short imaging file (played through)"))
check(leaks == 0, "analysis audio never reached the air (" .. leaks .. " samples unmuted)")
check(mutedLive == 0, "the live deck was never muted")
check(#evs("dead_air_guard") == 0, "no dead air")
local short = 0
for _, e in ipairs(evs("track_end")) do if e.played and e.played < 80 then short = short + 1 end end
check(short == 0, "every song played in full")
local fixed = LEARNED["Test/Tuning/B - Long Tail.flac"]
check(fixed and fixed.analyzed and math.abs(fixed.trail - 6) <= 0.6, "the Core uses the analyzed figures and plays don't learn over them")

print("-- a skip takes the analyzing deck back at once; no bogus play is logged")
boot()
MockCore.at(1, function() Analyzer.apply(all(), "r1") end)
local skipped
local function trySkip()
  if (DeckA.job or DeckB.job) and Station.state == "playing" and not skipped then
    skipped = MockCore.clock
    Control.run({ cmd = "skip" })
  else
    MockCore.at(MockCore.clock + 0.25, trySkip)
  end
end
MockCore.at(2, trySkip)
MockCore.run(150)
local startsAfter = 0
for _, e in ipairs(evs("track_start")) do if skipped and e.at and e.at >= skipped then startsAfter = startsAfter + 1 end end
check(skipped ~= nil and startsAfter >= 1, "skip during an analysis started the next song")
check(#evs("track_end") <= #evs("track_start"), "no play logged for the analysis file")
local live = Station.live
local liveMute = live and MockCore.mixer["input." .. (live == DeckA and 1 or 2) .. ".mute.1"].Boolean
check(live and not liveMute, "the deck taken back is unmuted for air")

print("-- stopped: both decks analyze")
boot()
Station.setAutomation(false, "test")
usedDecks, leaks = {}, 0
MockCore.at(0.05, watch)
MockCore.at(3, function() Analyzer.apply(all(), "r2") end)
MockCore.run(120)
n = 0
for _, e in ipairs(evs("analysis")) do if e.ok then n = n + 1 end end
check(usedDecks.A and usedDecks.B, "both decks used")
check(n == 5, string.format("5 files in under 2 minutes (%d)", n))
check(leaks == 0, "and none of it on air")
check(#evs("track_start") == 0, "nothing played on air while stopped")

print("-- start while both decks analyze: the station takes a deck at once")
boot()
Station.setAutomation(false, "test")
MockCore.at(3, function() Analyzer.apply(all(), "r3") end)
MockCore.at(8, function() Control.run({ cmd = "start" }) end)
MockCore.run(12)
check(#evs("track_start") >= 1 and Station.state == "playing", "on air within seconds of Start")

print("-- a design without the mixer: no analysis, the station plays")
boot({ noMixer = true })
MockCore.at(1, function() Analyzer.apply(all(), "r4") end)
MockCore.run(120)
check(#evs("analysis") == 0 and not DeckA.job and not DeckB.job, "nothing analyzed")
check(#evs("track_start") >= 1, "station still plays")
check(Station.snapshot(Timer.Now()).analyze.can == false, "reported as unable to analyze")

print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
