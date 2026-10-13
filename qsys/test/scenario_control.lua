-- Scenario: remote control from the web panel. A fake server answers
-- /api/v1/core/sync with commands at set times and pushes settings; checks
-- that the Core uploads its library, reports state, and obeys play next /
-- play now / bumper now / settings / stop after current / start / stop now.
dofile("qsys/test/mock_core.lua")
local json = require("json")

MockCore.setup{
  seed = 5,
  dirs = {
    ["Test/Tuning/"] = {
      { name = "Alpha - One.flac",    duration = 30, lead = 1.0, trail = 0.5, level = -10 },
      { name = "Bravo - Two.mp3",     duration = 28, lead = 0.5, trail = 0.5, level = -12 },
      { name = "Charlie - Three.mp3", duration = 26, lead = 0.2, trail = 0.4, level = -9  },
      { name = "Delta - Four.wav",    duration = 34, lead = 0.1, trail = 0.3, level = -8  },
      { name = "Echo - Five.flac",    duration = 29, lead = 0.4, trail = 0.5, level = -11 },
      { name = "Foxtrot - Six.mp3",   duration = 31, lead = 0.3, trail = 0.4, level = -10 },
    },
    ["Test/Imaging/"] = {
      { name = "bumper_01.wav", duration = 6, lead = 0.1, trail = 0.2, level = -6 },
      { name = "bumper_02.wav", duration = 5, lead = 0.1, trail = 0.2, level = -6 },
    },
  },
}

local COMMANDS = {
  { id = 1, at = 10,  cmd = "queue_add",  args = { key = "Test/Tuning/Echo - Five.flac", position = "next" } },
  { id = 2, at = 12,  cmd = "queue_add",  args = { key = "Test/Tuning/Foxtrot - Six.mp3" } },
  { id = 3, at = 14,  cmd = "queue_add",  args = { key = "Test/Tuning/Nope.mp3" } },
  { id = 4, at = 15,  cmd = "frobnicate" },
  { id = 10, at = 16, cmd = "queue_add",    args = { key = "Test/Tuning/Bravo - Two.mp3" } },
  -- stale page: index 1 now holds Echo, but the operator clicked Bravo's row as it was drawn earlier
  { id = 11, at = 17, cmd = "queue_remove", args = { index = 1, key = "Test/Tuning/Bravo - Two.mp3" } },
  { id = 12, at = 18, cmd = "queue_remove", args = { index = 1, key = "Test/Tuning/Gone.mp3" } },
  { id = 13, at = 70,  cmd = "play_now",   args = { key = "Test/Tuning/Delta - Four.wav" } },
  { id = 14, at = 80,  cmd = "bumper_now", args = { key = "Test/Imaging/bumper_02.wav" } },
  { id = 15, at = 110, cmd = "stop_after", args = { on = true } },
  { id = 16, at = 170, cmd = "start" },
  { id = 17, at = 200, cmd = "stop_now" },
  -- skip immediately followed by stop: the pending skip must not survive into the next start
  { id = 18, at = 212, cmd = "start" },
  { id = 19, at = 230, cmd = "skip" },
  { id = 20, at = 230, cmd = "stop_now" },
  { id = 21, at = 240, cmd = "start" },
}
local SETTINGS_AT, SETTINGS_REV = 40, "r1"
local SERVER_SETTINGS_VALUES = { crossfade_s = 2, bumper_every_n_tracks = 0 }

local libraryPosts, snapshots, delivered, syncs = {}, {}, {}, 0
MockCore.httpHandler = function(req)
  local body = json.decode(req.Data)
  if req.Url:match("/core/library$") then libraryPosts[#libraryPosts + 1] = body; return 200, '{"ok":true}' end
  if req.Url:match("/core/sync$") then
    syncs = syncs + 1
    if body.state then snapshots[#snapshots + 1] = { t = MockCore.clock, state = body.state } end
    local out = {}
    for _, c in ipairs(COMMANDS) do
      if c.at <= MockCore.clock and c.id > (body.ack or 0) then
        out[#out + 1] = { id = c.id, cmd = c.cmd, args = c.args, by = "tester" }
        delivered[c.id] = (delivered[c.id] or 0) + 1
      end
    end
    local resp = { commands = out }
    if MockCore.clock >= SETTINGS_AT and body.settings_rev ~= SETTINGS_REV then
      resp.settings, resp.settings_rev = SERVER_SETTINGS_VALUES, SETTINGS_REV
    end
    return 200, json.encode(resp)
  end
  return 200, '{"ok":true}'
end

dofile("qsys/radio/radio.lua")
CFG.SERVER_URL = "http://radio-server:5410"
SETTINGS.bumper_every_n_tracks = 0
Server.libraryDirty = true   -- boot scanned before SERVER_URL was set

local stateAt = {}
for _, t in ipairs({ 11, 13, 60 }) do MockCore.at(t, function() stateAt[t] = Station.snapshot(MockCore.clock) end) end
MockCore.run(200)
local offAt200 = nil
MockCore.at(208, function() offAt200 = { automation = Station.automation, state = Station.state, busy = DeckA:isBusy() or DeckB:isBusy() } end)
MockCore.run(70)

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local function evs(kind) local o = {} for _, e in ipairs(EVENTS) do if e.kind == kind then o[#o + 1] = e end end return o end
local function firstAfter(kind, t) for _, e in ipairs(EVENTS) do if e.kind == kind and e.at >= t then return e end end end

local lib = libraryPosts[1]
check(lib and #lib.items == 8 and lib.items[1].key and lib.music_dir == "Test/Tuning/", "library uploaded (8 items with keys)")
check(syncs > 200 and #snapshots > 20 and #snapshots < syncs / 2 and snapshots[#snapshots].state.decks.A
  and snapshots[#snapshots].state.library.music == 6,
  string.format("synced every second (%d); full state only on change or every 10 s (%d times)", syncs, #snapshots))
check(stateAt[11].next_up and stateAt[11].next_up.file == "Echo - Five.flac" and stateAt[11].next_up.queued,
  "queue_add next: Echo is next up (queued)")
check(#stateAt[13].queue == 2 and stateAt[13].queue[2].file == "Foxtrot - Six.mp3", "queue_add end: queue = Echo, Foxtrot")
local cmds = {}
for _, e in ipairs(evs("command")) do cmds[e.id] = e end
check(cmds[11] and cmds[11].ok and cmds[12] and cmds[12].ok == false,
  "queue_remove with a stale index removes the song by key; an unknown key is refused")
local starts = evs("track_start")
local afterQueue = {}
for _, s in ipairs(starts) do if s.at > 15 then afterQueue[#afterQueue + 1] = s.file end end
check(afterQueue[1] == "Echo - Five.flac" and afterQueue[2] == "Foxtrot - Six.mp3", "queued tracks played in order")


check(cmds[3] and cmds[3].ok == false and cmds[4] and cmds[4].ok == false, "bad key and unknown command are refused")
local once = true
for id, n in pairs(delivered) do if n ~= 1 and cmds[id] then once = false end end
check(once and #evs("command") == #COMMANDS, "each command ran exactly once (ack)")

local pn = firstAfter("track_start", 70)
local tr = firstAfter("transition", 70)
check(pn and pn.file == "Delta - Four.wav" and pn.at < 73 and tr and tr.reason == "skip", "play_now: Delta starts within 3 s via skip")
local b = firstAfter("bumper_start", 80)
check(b and b.file == "bumper_02.wav" and b.at < 83, "bumper_now with a chosen bumper")

check(SETTINGS.crossfade_s == 2 and SETTINGS.bumper_every_n_tracks == 0 and Server.settingsRev == "r1"
  and #evs("settings_applied") == 1, "server settings applied once (rev r1)")
local f = io.open(FILES.settings, "r")
local saved = f and json.decode(f:read("*a")); if f then f:close() end
check(saved and saved.rev == "r1" and saved.settings.crossfade_s == 2, "settings saved on the Core")
check(stateAt[60].settings.crossfade_s == 2, "state reports the applied settings")

local off = nil
for _, e in ipairs(evs("automation")) do if e.on == false and e.why == "stop_after_current" then off = e end end
local startedWhileOff = false
for _, s in ipairs(starts) do if off and s.at > off.at and s.at < 170 then startedWhileOff = true end end
check(off and off.at > 110 and not startedWhileOff, "stop_after: station stopped after the current track, nothing new started")
local resumed = firstAfter("track_start", 170)
check(resumed and resumed.at < 173, "start: playback resumes")
check(offAt200 and not offAt200.automation and offAt200.state == "off" and not offAt200.busy,
  "stop_now: automation off and both decks stopped")
local restart = firstAfter("track_start", 240)
local skipAfter = firstAfter("transition", 240)
check(restart and not (skipAfter and skipAfter.at < restart.at + 20),
  "a skip pending when the station was stopped does not fire after the next start")

print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
