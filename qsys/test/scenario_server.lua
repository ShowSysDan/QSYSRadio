-- Scenario: play log delivery to the app server. The server is down (timeouts,
-- then 503s) from t=30 to t=120; events must queue in the outbox, persist to
-- outbox.json, and all arrive once it is back (in the batched sync: each
-- station's events in its own part of the body; the Core's own fields at the
-- top: analyze_rev, its analysis players, the analysis list in the reply).
-- Run with run_mock.py.
-- Set MOCK_EVENTS_OUT=/path.json to also write every POST body (for feeding a
-- real server in an integration test).
dofile("qsys/test/mock_core.lua")
local json = require("json")

MockCore.setup{
  seed = 3,
  dirs = {
    ["Test/Tuning/"] = {
      { name = "Alpha - One.flac",    duration = 30, lead = 1.0, trail = 0.5, level = -10 },
      { name = "Bravo - Two.mp3",     duration = 28, lead = 0.5, trail = 0.5, level = -12 },
      { name = "Charlie - Three.mp3", duration = 26, lead = 0.2, trail = 0.4, level = -9  },
      { name = "Delta - Four.wav",    duration = 34, lead = 0.1, trail = 0.3, level = -8  },
      { name = "Echo - Five.flac",    duration = 29, lead = 0.4, trail = 0.5, level = -11 },
      { name = "Bad - File.mp3",      duration = 30, unsupported = true, status = "Unsupported file format" },
    },
    ["Test/Imaging/"] = {
      { name = "bumper_01.wav", duration = 6, lead = 0.1, trail = 0.2, level = -6 },
    },
  },
}

local bodies = {}
local q = MockCore.analysisQueue({ { station = "Main", key = "Test/Imaging/bumper_01.wav" }, { station = "Main", key = "Test/Tuning/Alpha - One.flac" } })
MockCore.serve{
  core = q.core,
  down = function()
    local t = MockCore.clock
    if t >= 30 and t < 80 then return 0, "", "timeout" end
    if t >= 80 and t < 120 then return 503, "" end
  end,
  sync = function(part, body, req)
    if part.events then bodies[#bodies + 1] = req.Data end
    return { commands = {} }
  end,
}

MockCore.loadRadio{ SERVER_URL = "http://radio-server:5410", SERVER_TOKEN = "secret" }
local S = Radio.stations[1]
S.settings.bumper_every_n_tracks = 3

local midOutbox, midFile = 0, 0
MockCore.at(115, function()
  midOutbox = #Server.outbox
  local f = io.open(FILES.outbox, "r")
  if f then midFile = #(json.decode(f:read("*a")) or {}); f:close() end
end)
MockCore.run(240)
Tick:Stop()          -- freeze playback, then let the sender flush what is left
MockCore.run(10)

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end

local got, dupes, okHeaders, okBody = {}, 0, true, true
for _, h in ipairs(MockCore.http) do
  if h.req.Headers["X-Radio-Token"] ~= "secret" or h.req.Method ~= "POST"
     or not h.req.Url:match("^http://radio%-server:5410/api/v1/") then okHeaders = false end
end
local starts = 0
for _, b in ipairs(bodies) do
  local d = json.decode(b)
  if not (d.version == RADIO_VERSION and d.boot == BOOT_ID and d.core == "mock" and type(d.mem_kb) == "number"
          and #d.stations == 1 and d.stations[1].station == "Main") then okBody = false end
  for _, ev in ipairs(d.stations[1].events or {}) do
    if got[ev.seq] then dupes = dupes + 1 end
    got[ev.seq] = ev
    if ev.kind == "track_start" then starts = starts + 1 end
    if ev.station ~= "Main" then okBody = false end
  end
end
local missing = 0
for s = 1, EVENT_SEQ do if not got[s] then missing = missing + 1 end end

check(okHeaders, "POSTs go to /api/v1/... with the token header")
check(okBody, "batched body: version, boot, core, mem_kb; the station's part carries its events (each tagged with the station)")
check(midOutbox > 5, string.format("events queued during the outage (%d at t=115)", midOutbox))
check(midFile > 0 and midFile <= midOutbox, string.format("outbox persisted during the outage (file %d, memory %d; written every 30 s)", midFile, midOutbox))
check(missing == 0, string.format("all %d events delivered after the outage (%d missing)", EVENT_SEQ, missing))
check(#Server.outbox == 0, "outbox empty at the end")
check(starts >= 6, string.format("%d track_start events delivered", starts))
local bStart, bEnd = 0, 0
for _, ev in pairs(got) do
  if ev.kind == "bumper_start" then bStart = bStart + 1 elseif ev.kind == "bumper_end" then bEnd = bEnd + 1 end
end
check(bStart > 0 and bStart == bEnd, string.format("every bumper start has an end (%d/%d)", bStart, bEnd))
local ts = nil
for _, ev in pairs(got) do if ev.kind == "track_start" then ts = ev; break end end
check(ts and ts.boot == BOOT_ID and ts.t and ts.file and ts.dir and ts.title and ts.duration,
  "track_start carries boot, seq, t, file, dir, title, duration")
local topOk, measuring, idle = true, false, false
for _, h in ipairs(MockCore.http) do
  if h.req.Url:match("/core/sync$") then
    local d = json.decode(h.req.Data)
    if type(d.analyze_rev) ~= "string" or d.stations[1].analyze_rev ~= nil then topOk = false end
    local a = d.analyzers and d.analyzers[1]
    if a and a.name == "Analysis_Player" and a.state == "measuring" and a.station == "Main" and a.key then measuring = true end
    if a and a.name == "Analysis_Player" and a.state == "idle" and a.station == nil and a.key == nil then idle = true end
  end
end
check(topOk and measuring and idle, "the body's top: analyze_rev, and each analysis player (idle, or measuring a station's file)")
local an1, an2 = q.results["Main|Test/Imaging/bumper_01.wav"], q.results["Main|Test/Tuning/Alpha - One.flac"]
check(an1 and an1.ok and an2 and an2.ok and #q.owed == 0 and an1.station == "Main" and an1.lead_in and an2.level,
  "the analysis list from the reply's top: both files measured, each result in the station's part")

local out = os.getenv("MOCK_EVENTS_OUT")
if out then
  local f = assert(io.open(out, "w"))
  f:write("[" .. table.concat(bodies, ",") .. "]")
  f:close()
  print("wrote " .. #bodies .. " POST bodies to " .. out)
end
print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
