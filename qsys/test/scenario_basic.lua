-- Scenario: 8 short tracks (one with a long silent tail, one unsupported),
-- 2 bumpers, bumper every 3 tracks, operator skip at t=65. Run with run_mock.py.
dofile("qsys/test/mock_core.lua")

MockCore.setup{
  seed = 1,
  dirs = {
    ["Test/Tuning/"] = {
      { name = "Alpha - One.flac",     duration = 30, lead = 1.0, trail = 0.5, level = -10 },
      { name = "Alpha - Two.flac",     duration = 28, lead = 0.5, trail = 0.5, level = -12 },
      { name = "Bravo - Three.mp3",    duration = 32, lead = 0.2, trail = 8.0, level = -9  },  -- long silent tail
      { name = "Charlie - Four.mp3",   duration = 26, lead = 2.5, trail = 1.0, level = -14 },
      { name = "Delta - Five.wav",     duration = 34, lead = 0.1, trail = 0.3, level = -8  },
      { name = "Echo - Six.flac",      duration = 29, lead = 0.4, trail = 0.5, level = -11 },
      { name = "Foxtrot - Seven.mp3",  duration = 30, unsupported = true, status = "Unsupported file format" },  -- corrupt file
      { name = "Golf - Eight.mp3",     duration = 31, lead = 0.6, trail = 0.4, level = -10 },
    },
    ["Test/Imaging/"] = {
      { name = "bumper_01.wav", duration = 6, lead = 0.1, trail = 0.2, level = -6 },
      { name = "bumper_02.wav", duration = 5, lead = 0.1, trail = 0.2, level = -6 },
    },
  },
}

dofile("qsys/radio/radio.lua")
local S = Radio.stations[1]
S.settings.bumper_every_n_tracks = 3

MockCore.at(65, function() print("        >>> operator presses SKIP"); S:command({ cmd = "skip", by = "test" }) end)
MockCore.run(260)

-------------------------------------------------------------------------------
-- checks
-------------------------------------------------------------------------------
print("")
print("----- MockCore timeline -----")
for _, e in ipairs(MockCore.log) do print(string.format("  %7.2fs  %s", e.t, e.msg)) end

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end

local starts, bumpers, transitions, unsupported, deadAir = {}, {}, {}, 0, 0
for _, ev in ipairs(EVENTS) do
  if ev.kind == "track_start" then starts[#starts + 1] = ev
  elseif ev.kind == "bumper_start" then bumpers[#bumpers + 1] = ev
  elseif ev.kind == "transition" then transitions[#transitions + 1] = ev
  elseif ev.kind == "file_unsupported" then unsupported = unsupported + 1
  elseif ev.kind == "dead_air_guard" then deadAir = deadAir + 1 end
end
check(#starts >= 7, string.format("played %d tracks in 260 s", #starts))
check(#bumpers >= 2, string.format("%d bumpers played", #bumpers))
check(deadAir == 0, "dead-air guard never needed")

-- decks alternate on plain crossfades; bumpers land on the deck the track is NOT on
local bumperOnOtherDeck = true
for _, b in ipairs(bumpers) do
  local liveAtThatTime
  for _, s in ipairs(starts) do if s.at <= b.at then liveAtThatTime = s.deck end end
  if liveAtThatTime == b.deck then bumperOnOtherDeck = false end
end
check(bumperOnOtherDeck, "every bumper played on the free deck")

local sawSilence, sawSkip, sawEnd = false, false, false
for _, tr in ipairs(transitions) do
  if tr.reason == "silence" then sawSilence = true end
  if tr.reason == "skip" then sawSkip = true end
  if tr.reason == "end" then sawEnd = true end
end
check(sawEnd, "normal end-of-track crossfade happened")
check(sawSkip, "operator skip caused a transition")
-- only meaningful if 'Bravo - Three' (8 s silent tail) ran to its end at least once, not just skipped
local bravoEnded = false
for _, ev in ipairs(EVENTS) do
  if ev.kind == "track_end" and ev.file == "Bravo - Three.mp3" and ev.reason ~= "cut" then bravoEnded = true end
end
local bravo = S.learned["Test/Tuning/Bravo - Three.mp3"]
check(bravoEnded, "'Bravo - Three' played to its end (scenario coverage; pick another seed if not)")
check(sawSilence or (bravo and bravo.trail and bravo.trail > 5), "silent tail was detected or learned on 'Bravo - Three'")
check(S.learned["Test/Tuning/Foxtrot - Seven.mp3"] and S.learned["Test/Tuning/Foxtrot - Seven.mp3"].unsupported and unsupported == 1, "unsupported file was tried once, learned and excluded")

local artistRepeat = false
for i = 2, #starts do
  local a1 = starts[i - 1].file:match("^(.-)%s+%-")
  local a2 = starts[i].file:match("^(.-)%s+%-")
  if a1 and a1 == a2 then artistRepeat = true end
end
check(not artistRepeat, "no back-to-back artist repeats")

print("")
print("----- learned (this boot; the server keeps them per file) -----")
for k, v in pairs(S.learned) do print("  " .. k .. "  " .. require("json").encode(v)) end
print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
