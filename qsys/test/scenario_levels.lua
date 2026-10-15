-- Scenario: level matching. Plays and file analysis measure each file's average
-- level (the meter's rms at unity gain, sound only); every item plays at its
-- kind's level plus the trim that brings it to level_target_db, within the
-- raise / cut limits and the peak ceiling; fades start and end at that level
-- (never a jump to 0 dB first); the panel's meter readout rides the sync.
-- Run with run_mock.py.
dofile("qsys/test/mock_core.lua")
local json = require("json")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local function evs(kind) local o = {} for _, e in ipairs(EVENTS) do if e.kind == kind then o[#o + 1] = e end end return o end

-- level = the file's program level (its peak meter reads level + 2), crest = peak minus rms
local MUSIC = {
  { name = "A - Hot.flac", duration = 60, lead = 0.3, trail = 0.5, level = 10, crest = 6 },
  { name = "B - Quiet.flac", duration = 60, lead = 0.3, trail = 0.5, level = -12, crest = 4 },
  { name = "C - Loud.flac", duration = 60, lead = 0.3, trail = 0.5, level = 14, crest = 3 },
  { name = "D - Peaky.flac", duration = 60, lead = 0.3, trail = 0.5, level = 14, crest = 18 },
}
local IMAGING = { { name = "id.wav", duration = 6, lead = 0.2, trail = 0.3, level = 6, crest = 4 } }
local ADS = { { name = "ad.wav", duration = 20, lead = 0.2, trail = 0.3, level = 12, crest = 5 } }
local function key(dir, f) return dir .. f.name end
local function avg(f) return f.level - f.crest end    -- what the meter's rms averages at unity (±1 dB: the program wobbles)

local S                                 -- the station (CFG.STATIONS[1]) of the current boot
local function boot(learned)
  for _, f in ipairs({ "outbox", "settings" }) do os.remove("media/_radio/" .. f .. ".json") end
  MockCore.setup{ seed = 5, dirs = { ["Test/Tuning/"] = MUSIC, ["Test/Imaging/"] = IMAGING, ["Test/Ads/"] = ADS } }
  dofile("qsys/radio/radio.lua")
  S = Radio.stations[1]
  S.settings.ads_dir, S.settings.artist_separation = "Test/Ads/", 0
  S:scan()
  if learned then S:learn(learned) end
end

print("-- plays measure each file's average level")
boot()
S.settings.bumper_every_n_tracks = 0
MockCore.run(260)
local measured = {}
for _, e in ipairs(evs("track_end")) do
  if e.level and not measured[e.dir .. e.file] then measured[e.dir .. e.file] = e end
end
for _, f in ipairs(MUSIC) do
  local e = measured[key("Test/Tuning/", f)]
  check(e and math.abs(e.level - avg(f)) <= 1 and e.level_s >= 40, string.format("%s: average %s dB over %s s (rms at unity %d)",
    f.name, e and e.level or "-", e and e.level_s or "-", avg(f)))
end
local first = evs("track_start")[1]
check(first and first.trim == nil, "a file not measured yet plays without a trim")
local again, hot                        -- the last start of a song played before
for _, e in ipairs(evs("track_start")) do
  local m = measured[e.dir .. e.file]
  if m and m.at < e.at then again, hot = e, m end
end
check(again and hot and math.abs(again.trim + hot.level) <= 0.05,
  string.format("its next play is brought to 0 dB (trim %s for an average of %s)", again and again.trim or "-", hot and hot.level or "-"))

print("-- the trim: target, raise / cut limits, peak ceiling, kind levels, off")
local LEARN = {
  [key("Test/Tuning/", MUSIC[1])] = { level = 4, peak = 12 },     -- lowered 4
  [key("Test/Tuning/", MUSIC[2])] = { level = -16, peak = -10 },  -- raised, at most level_raise_max_db
  [key("Test/Tuning/", MUSIC[3])] = { level = 30, peak = 36 },    -- lowered, at most level_cut_max_db
  [key("Test/Tuning/", MUSIC[4])] = { level = -4, peak = 16 },    -- raised only to the peak ceiling (18)
  [key("Test/Imaging/", IMAGING[1])] = { level = 2, peak = 8 },
  [key("Test/Ads/", ADS[1])] = { level = 7, peak = 14 },
}
local WANT = { [MUSIC[1].name] = -4, [MUSIC[2].name] = 6, [MUSIC[3].name] = -20, [MUSIC[4].name] = 2, ["id.wav"] = -2, ["ad.wav"] = -7 }
boot(LEARN)
S.settings.play_gain_db, S.settings.imaging_gain_db, S.settings.ad_gain_db = -3, -1, 1
S.settings.bumper_every_n_tracks, S.settings.ad_every_n_tracks = 2, 3
local gains, bad = {}, 0
local function seeGain()
  for i, d in ipairs(S.decks) do
    if d.track and d.state == "playing" and not d.fade and not d.job then
      local want = WANT[d.track.name] + S.settings[KINDS[d.track.kind].gain]
      local ctl = MockCore.players[i == 1 and "Audio_Player23" or "Audio_Player24"].ctl.gain.Value
      gains[d.track.name] = ctl
      if math.abs(ctl - want) > 0.05 then bad = bad + 1 end
    end
  end
  MockCore.at(MockCore.clock + 0.25, seeGain)
end
MockCore.at(0.1, seeGain)
MockCore.run(400)
for name, want in pairs(WANT) do
  check(gains[name] ~= nil, string.format("%s played at %s dB (trim %+d on its kind's level)", name, gains[name] or "-", want))
end
check(bad == 0, "every item plays at its kind's level + its trim (" .. bad .. " samples off)")
local logged = 0
for _, e in ipairs(EVENTS) do if e.kind:match("_start$") and e.trim and e.trim == WANT[e.file] then logged = logged + 1 end end
check(logged >= 6, "start events carry the trim (" .. logged .. ")")
local snap = S:snapshot(Timer.Now()).decks
local live = snap.A.live and snap.A or snap.B
check(live and live.trim == WANT[live.file] and live.gain == WANT[live.file] + S.settings.play_gain_db, "the panel sees the deck's gain and trim")
S.settings.level_raise_max_db, S.settings.level_cut_max_db = 0, 0
S.settings.bumper_every_n_tracks, S.settings.ad_every_n_tracks = 0, 0       -- the next song comes straight after a skip
S:command({ cmd = "skip" })
MockCore.run(5)
local d = S.live
check(d and d.trim == 0 and d.playGain == S.settings.play_gain_db, "both limits at 0: level matching is off")
S.settings.level_raise_max_db, S.settings.level_cut_max_db, S.settings.level_target_db = 6, 20, -6
S:command({ cmd = "skip" })
MockCore.run(5)
d = S.live
local L = d and d.track and LEARN[d.track.key]
local expect = L and math.max(-20, math.min(6, -6 - L.level))
if expect and expect > 0 then expect = math.max(0, math.min(expect, 18 - L.peak)) end
check(expect and d.trim == expect and d.trim < WANT[d.track.name] or (expect == 6 and d.trim == 6),
  string.format("a lower target: %s trim %s (was %s)", d and d.track and d.track.name or "-", d and d.trim or "-", L and WANT[d.track.name] or "-"))

print("-- fades start and end at the trimmed level: no jump to 0 dB first")
for _, fadeIn in ipairs({ 0, 2 }) do
  boot(LEARN)
  S.settings.fade_in_s, S.settings.bumper_every_n_tracks, S.settings.ad_every_n_tracks = fadeIn, 2, 3
  local over, badFrom, fades, prev = 0, 0, 0, {}
  local function watch()
    for i, dk in ipairs(S.decks) do
      local ctl = MockCore.players[i == 1 and "Audio_Player23" or "Audio_Player24"].ctl.gain.Value
      if dk.track and dk.playGain and (dk.state == "playing" or dk.state == "fading") then
        if ctl > dk.playGain + 0.05 then over = over + 1 end
        local f = dk.fade
        if f and f.to <= -100 and prev[i] ~= f then       -- a fade-out starts where the deck was
          prev[i], fades = f, fades + 1
          if fadeIn == 0 and math.abs(f.from - dk.playGain) > 0.05 then badFrom = badFrom + 1 end
        end
        if f and f.to > -100 and math.abs(f.to - dk.playGain) > 0.05 then badFrom = badFrom + 1 end   -- a fade-in ends there
      end
    end
    MockCore.at(MockCore.clock + 0.05, watch)
  end
  MockCore.at(0.05, watch)
  MockCore.at(30, function() S:command({ cmd = "skip" }) end)
  MockCore.at(100, function() S:command({ cmd = "play_now", args = { key = key("Test/Tuning/", MUSIC[3]) } }) end)
  MockCore.at(170, function() S:command({ cmd = "ad_now" }) end)
  MockCore.at(330, function() S:command({ cmd = "stop_now" }) end)
  MockCore.run(340)
  local kinds = {}
  for _, e in ipairs(evs("transition")) do kinds[e.reason] = true end
  check(kinds["end"] and kinds.skip and kinds["break"], string.format("fade-in %d s: crossfades, skips and breaks happened", fadeIn))
  check(fades >= 6 and badFrom == 0, string.format("fade-in %d s: every fade starts or ends at the deck's trimmed level (%d fades, %d off)", fadeIn, fades, badFrom))
  check(over == 0, string.format("fade-in %d s: no deck ever went above its trimmed level (%d samples)", fadeIn, over))
  check(#evs("dead_air_guard") == 0 and not S.automation, string.format("fade-in %d s: no dead air, stopped cleanly", fadeIn))
end

print("-- analysis (the analysis player) measures the average level in the middle of the file")
boot()
S:setAutomation(false, "test")
MockCore.at(1, function()
  local list = {}
  for i, k in ipairs({ key("Test/Tuning/", MUSIC[1]), key("Test/Tuning/", MUSIC[2]), key("Test/Imaging/", IMAGING[1]) }) do list[i] = { station = "Main", key = k } end
  Analyzer:list(list, "r1")
end)
MockCore.run(120)
local an = {}
for _, e in ipairs(evs("analysis")) do if e.ok then an[e.dir .. e.file] = e end end
for i = 1, 2 do
  local f, e = MUSIC[i], an[key("Test/Tuning/", MUSIC[i])]
  check(e and math.abs(e.level - avg(f)) <= 1 and e.level_s >= 15 and e.level_s <= 20 and e.peak and math.abs(e.peak - (f.level + 2)) <= 1,
    string.format("%s: average %s dB over %s s from the middle, peak %s", f.name, e and e.level or "-", e and e.level_s or "-", e and e.peak or "-"))
  check(S.learned[key("Test/Tuning/", f)].level == (e and e.level), f.name .. ": the Core uses it at once")
end
local e = an[key("Test/Imaging/", IMAGING[1])]
check(e and math.abs(e.level - avg(IMAGING[1])) <= 1, string.format("a short file: average %s dB from everything heard", e and e.level or "-"))
local plays = 0
for _, x in ipairs(evs("analysis")) do plays = plays + (x.ok and 1 or 0) end
check(plays == 3 and #evs("track_start") == 0, "nothing played on air")

print("-- the meter readout rides the sync (after the deck's gain)")
boot(LEARN)
S.settings.bumper_every_n_tracks = 0
CFG.SERVER_URL = "http://radio-server:5410"
local bodies = {}
MockCore.serve{ sync = function(part) bodies[#bodies + 1] = { at = MockCore.clock, body = part } end }
MockCore.run(20)
local okRead, idle = 0, 0
for _, b in ipairs(bodies) do
  local m, dk = b.body.meters, S.live
  if m and m.A and m.B and dk and b.at > 5 then
    local f = dk.track and (dk.track.name == MUSIC[1].name and MUSIC[1] or dk.track.name == MUSIC[2].name and MUSIC[2]
      or dk.track.name == MUSIC[3].name and MUSIC[3] or MUSIC[4])
    local mine, other = m[dk.id], m[dk.id == "A" and "B" or "A"]
    if f and math.abs(mine.peak - (f.level + 2 + dk.playGain)) <= 1.5 and math.abs(mine.rms - (f.level - f.crest + dk.playGain)) <= 2.5
       and math.abs(mine.gain - dk.playGain) <= 0.05 then okRead = okRead + 1 end
    if other.peak <= -119 then idle = idle + 1 end
  end
end
check(#bodies >= 15 and okRead >= 10, string.format("the live deck's gain, and its peak / rms after that gain (%d of %d syncs)", okRead, #bodies))
check(idle >= 10, "the idle deck reads the floor")
local fading = 0
MockCore.at(MockCore.clock + 0.5, function() S:command({ cmd = "skip" }) end)
local from = #bodies
MockCore.run(4)
for i = from + 1, #bodies do
  local m = bodies[i].body.meters
  if m and (m.A.gain < -10 and m.A.gain > -100 or m.B.gain < -10 and m.B.gain > -100) then fading = fading + 1 end
end
check(fading >= 1, "a fade shows in the gain readout")

print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
