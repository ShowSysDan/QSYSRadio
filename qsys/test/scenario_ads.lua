-- Scenario: ads. A fake server sends an ad plan (mix-ins, one hourly ad owed
-- from t=200, one ad off); checks ad breaks every N songs, ads back to back,
-- the owed ad forcing a break, ads + bumper in one break, ad_now / play_now.
-- Run with run_mock.py.
dofile("qsys/test/mock_core.lua")
local json = require("json")

local function songs(n, dur)
  local o = {}
  for i = 1, n do o[i] = { name = string.char(64 + i) .. " - Song" .. i .. ".flac", duration = dur, lead = 0.3, trail = 0.5, level = -10 } end
  return o
end
MockCore.setup{
  seed = 4,
  dirs = {
    ["Test/Tuning/"] = songs(8, 30),
    ["Test/Imaging/"] = { { name = "id.wav", duration = 4, level = -6 } },
    ["Ads/"] = {
      { name = "mix1.wav", duration = 8, level = -8 }, { name = "mix2.wav", duration = 9, level = -8 },
      { name = "hourly.wav", duration = 7, level = -8 }, { name = "off.wav", duration = 6, level = -8 },
    },
  },
}

local HOURLY, DUE_FROM = "Ads/hourly.wav", 200
local hourlyPlays, libraryPosts, revsSeen = 0, {}, {}
-- the server's fill (song picked by length for the next slot) from t=380 to 440, still named after it
-- started (a stale plan); a missing file is ignored
local FILL, FILL_FROM, FILL_TO = "Test/Tuning/H - Song8.flac", 380, 440
local function plan()
  local due = (MockCore.clock >= DUE_FROM and hourlyPlays == 0) and { HOURLY } or {}
  local fill = (MockCore.clock >= FILL_FROM and MockCore.clock < FILL_TO) and FILL
               or (MockCore.clock >= 300 and MockCore.clock < 330 and "Test/Tuning/Gone.flac") or nil
  return { due = due, mix = { "Ads/mix1.wav", "Ads/mix2.wav" }, fill = fill }, "p" .. #due .. tostring(fill)
end
local fillSeen
MockCore.at(FILL_FROM + 5, function() fillSeen = Station.snapshot(Timer.Now()).next_up end)
MockCore.httpHandler = function(req)
  local body = json.decode(req.Data)
  if req.Url:match("/core/library$") then
    libraryPosts[#libraryPosts + 1] = body
    return 200, json.encode({ ok = true, learned = { ["Test/Tuning/A - Song1.flac"] = { trail = 0.4 } } })
  end
  for _, e in ipairs(body.events or {}) do
    if e.kind == "ad_start" and e.dir .. e.file == HOURLY then hourlyPlays = hourlyPlays + 1 end
  end
  revsSeen[body.ads_rev or "?"] = true
  local p, rev = plan()
  local resp = { commands = {} }
  if body.ads_rev ~= rev then resp.ads, resp.ads_rev = p, rev end
  if MockCore.clock >= 330 and MockCore.clock < 331 then resp.commands = { { id = 1, cmd = "ad_now", args = {} } } end
  if MockCore.clock >= 445 and MockCore.clock < 446 then
    resp.commands = { { id = 2, cmd = "play_now", args = { key = "Ads/mix2.wav" } } }
  end
  return 200, json.encode(resp)
end

dofile("qsys/radio/radio.lua")
CFG.SERVER_URL = "http://radio-server:5410"
Server.applySettings({ ads_dir = "Ads", ad_every_n_tracks = 3, ads_per_break = 2, bumper_every_n_tracks = 6 }, "r1")
MockCore.run(520)

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end

-- the timeline: songs (S), ads (A), bumpers (B) in start order
local seq, ads, adEnds = {}, {}, {}
for _, e in ipairs(EVENTS) do
  if e.kind == "track_start" then seq[#seq + 1] = { k = "S", e = e }
  elseif e.kind == "ad_start" then seq[#seq + 1] = { k = "A", e = e }; ads[#ads + 1] = e
  elseif e.kind == "bumper_start" then seq[#seq + 1] = { k = "B", e = e }
  elseif e.kind == "ad_end" then adEnds[#adEnds + 1] = e end
end
local pattern = {}
for i, x in ipairs(seq) do pattern[i] = x.k end
print("        timeline: " .. table.concat(pattern))

local lib = libraryPosts[#libraryPosts]
local adItems = 0
for _, it in ipairs(lib and lib.items or {}) do if it.kind == "ad" then adItems = adItems + 1 end end
check(lib and lib.ads_dir == "Ads/" and adItems == 4, "library upload carries the ads folder and 4 ads")
check(LEARNED["Test/Tuning/A - Song1.flac"] and LEARNED["Test/Tuning/A - Song1.flac"].trail <= 0.4,
  "per-file figures from the server's reply are used")

local offPlayed, overlap = false, false
for _, a in ipairs(ads) do if a.file == "off.wav" then offPlayed = true end end
check(#ads >= 6 and not offPlayed, string.format("%d ads played; the 'off' ad never", #ads))
for i = 2, #seq do
  if seq[i].k == "A" and seq[i - 1].k == "A" then
    local prevEnd
    for _, e in ipairs(adEnds) do if e.at <= seq[i].e.at and e.file == seq[i - 1].e.file then prevEnd = e end end
    if not prevEnd then overlap = true end
  end
end
check(not overlap, "ads in a break play back to back (each starts after the previous one ended)")

-- first regular break: after 3 songs, 2 different mix-ins, then music
local s = table.concat(pattern)
check(s:sub(1, 6) == "SSSAAS", "first break after 3 songs: two ads, then the next song")
check(ads[1].file ~= ads[2].file, "no ad twice in one break")

local hourly
for _, a in ipairs(ads) do if a.file == "hourly.wav" then hourly = hourly or a end end
local songsBefore = 0                 -- songs since the previous break
for _, x in ipairs(seq) do
  if hourly and x.e.at < hourly.at then songsBefore = (x.k == "S") and songsBefore + 1 or 0 end
end
check(hourly and hourly.at > DUE_FROM and hourly.at < DUE_FROM + 40 and hourlyPlays == 1 and songsBefore < 3,
  string.format("owed hourly ad played once, at the first song change after it fell due (t=%s, %d songs into the cycle)",
    hourly and hourly.at or "-", songsBefore))
check(revsSeen.p1nil and revsSeen.p0nil, "ads plan revision reported back to the server")

local adThenBumper = s:find("AB", 1, true) ~= nil
check(adThenBumper and not s:find("BA", 1, true), "ads and a bumper due together: ads first, bumper last")

local cmds = {}
for _, e in ipairs(EVENTS) do if e.kind == "command" then cmds[e.id] = e end end
local function firstAdAfter(t) for _, a in ipairs(ads) do if a.at >= t then return a end end end
local a1, a2 = firstAdAfter(330), firstAdAfter(445)
check(cmds[1] and cmds[1].ok and a1 and a1.at < 334, "ad_now: a break starts within 3 s")
check(cmds[2] and cmds[2].ok and a2 and a2.file == "mix2.wav" and a2.at < 449, "play_now with an ad: that ad within 3 s")

local songsAfterFill = {}
for _, x in ipairs(seq) do if x.k == "S" and x.e.at > FILL_FROM then songsAfterFill[#songsAfterFill + 1] = x.e.file end end
local fillPlays = 0
for _, x in ipairs(seq) do if x.k == "S" and x.e.at > FILL_FROM and x.e.file == "H - Song8.flac" then fillPlays = fillPlays + 1 end end
check(songsAfterFill[1] == "H - Song8.flac" and fillPlays == 1 and fillSeen and fillSeen.fill and fillSeen.file == "H - Song8.flac",
  "the server's fill song plays next (shown as such), once")

local paired = #ads == #adEnds or #ads == #adEnds + 1
check(paired, string.format("every ad start has an end (%d/%d)", #ads, #adEnds))
check(Station.snapshot(Timer.Now()).library.ad == 4, "state reports the number of ads")

print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
