-- Scenario: one-player stations (background-music zones) on the same radio.lua as a two-player station. A zone
-- plays its folder with fair rotation and artist separation, level matching and the quiet start skipped; at the
-- learned quiet end the song fades out and the next one loads on the same player (the gap is the load time);
-- silence at the end and a song that ends before its fade are handled; skip, play now, the queue, stop after
-- current, stop and start work as on two players; a file that cannot load moves on to the next song; an empty
-- folder plays once files arrive; never a bumper, an ad or a station ID, even when asked. The two-player station
-- beside it plays its breaks as before; Tick and Fast allocate nothing; Lua memory stays flat. Run with run_mock.py.
dofile("qsys/test/mock_core.lua")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local SETTINGS_FILE, OUTBOX_FILE = "media/_radio/settings.json", "media/_radio/outbox.json"
local LOAD_TIMEOUT = 1.5                       -- radio.lua's K.load_timeout_s
local realPrint, quiet = print, false
print = function(s) if not quiet then realPrint(s) end end   -- radio.lua's log, silenced for hours of play

local function every(dt, fn)
  local function go() fn(); MockCore.at(MockCore.clock + dt, go) end
  MockCore.at(MockCore.clock + dt, go)
end
local function boot(spec)
  for _, f in ipairs({ SETTINGS_FILE, OUTBOX_FILE }) do os.remove(f) end
  MockCore.setup{ seed = spec.seed or 7, dirs = spec.dirs, players = spec.players }
  local f = assert(io.open(SETTINGS_FILE, "w"))
  f:write(require("json").encode({ stations = spec.settings }))
  f:close()
  MockCore.serve(spec.server)
  MockCore.loadRadio(function(cfg)
    cfg.SERVER_URL, cfg.STATIONS, cfg.ANALYZERS = "http://radio-server:5410", spec.stations, {}
  end)
end
local function songs(prefix, n, dur, spread)
  local o = {}
  for i = 1, n do
    o[i] = { name = string.format("%s%d - Song%03d.mp3", prefix, i % 7, i), duration = dur + (spread and (i * 37) % spread or 0),
             lead = 0.3, trail = 0.5, level = -10 - i % 5 }
  end
  return o
end
local BREAKS = { ["Imaging/"] = { { name = "id1.wav", duration = 5, level = -6 }, { name = "id2.wav", duration = 4, level = -6 } },
                 ["Ads/"] = { { name = "ad1.wav", duration = 15, level = -8 }, { name = "ad2.wav", duration = 12, level = -8 } } }

-- the zone's folder: 18 songs (7 artists; half with a 1.2 s quiet start; levels -10 to -18 dB), one with a long
-- silent end nobody measured yet, one shorter than a fade, one the player cannot play, one whose load never completes
local function lobby()
  local o = {}
  for i = 1, 18 do
    o[i] = { name = string.format("Artist%d - Song%02d.mp3", i % 7, i), duration = 40 + (i * 13) % 25,
             lead = i % 2 == 1 and 1.2 or 0.3, trail = 2, level = -10 - (i % 5) * 2 }
  end
  o[19] = { name = "Artist2 - Tail.mp3", duration = 50, lead = 0.3, trail = 12, level = -12 }
  o[20] = { name = "Artist4 - Short.mp3", duration = 4, lead = 0.3, trail = 0.2, level = -12 }
  o[21] = { name = "Artist5 - Broken.mp3", duration = 30, unsupported = true }
  o[22] = { name = "Artist6 - Hang.mp3", duration = 30, hang = true }
  return o
end

-------------------------------------------------------------------------------
do
  print("-- a zone (one player) and a radio station (two players) on one radio.lua, an hour of play")
  local zw, zc = MockCore.zones(2, { first = 3 })                       -- Z1, Z2 on ZoneMeter 3 / 4
  local rw, rc = MockCore.stations(1, { ids = { "R1" } })
  local wiring, cfg = rw, { rc[1], zc[1], zc[2],
    { id = "Bad0", players = {}, meter = "ZoneMeter" },                -- bad entries: faulted, the others never notice
    { id = "Bad3", players = { "x", "y", "z" }, meter = "ZoneMeter" } }
  for _, w in ipairs(zw) do wiring[#wiring + 1] = w end
  local LOBBY = lobby()
  local entry = {}
  for _, e in ipairs(LOBBY) do entry[e.name] = e end
  local dirs = { ["Lobby/"] = LOBBY, ["Empty/"] = {}, ["Rock/"] = songs("Rock", 12, 40, 9) }
  for k, v in pairs(BREAKS) do dirs[k] = v end
  local PICK, NOW_KEY, NEXT_KEY = "Artist3 - Song10.mp3", "Artist1 - Song08.mp3", "Artist6 - Song13.mp3"

  -- the fake server: every event kept; Z1 gets break plans (ads owed, mix-ins, an ID) all the time and, from
  -- t=1550 until it has played, the server's pick for the next song; commands for Z1 go out when it is mid-song
  local got, states, meters, uploads = {}, {}, {}, {}
  local meterSplit, pickSent, pickedAt, learnedAt
  local COMMANDS = {
    { id = 1, at = 600, cmd = "bumper_now", any = true }, { id = 2, at = 600, cmd = "ad_now", any = true },
    { id = 3, at = 600, cmd = "play_now", args = { key = "Imaging/id1.wav" }, any = true },
    { id = 4, at = 700, cmd = "skip" }, { id = 5, at = 800, cmd = "play_now", args = { key = "Lobby/" .. NOW_KEY } },
    { id = 6, at = 900, cmd = "queue_add", args = { key = "Lobby/" .. NEXT_KEY, position = "next" } },
    { id = 7, at = 1000, cmd = "stop_after" }, { id = 8, at = 1200, cmd = "start", any = true },
    { id = 9, at = 1400, cmd = "stop_now" }, { id = 10, at = 1500, cmd = "start", any = true },
  }
  local sentAt = {}
  local function midSong()
    local z = Radio.byId.Z1
    local d = z.state == "playing" and z.live
    return d and d.state == "playing" and MockCore.clock - d.playStart > 4 and (d:remainingEst(MockCore.clock) or 0) > 15
  end
  boot{
    players = wiring, stations = cfg, dirs = dirs,
    settings = {
      R1 = { rev = "r", settings = { music_dir = "Rock/", imaging_dir = "Imaging/", ads_dir = "Ads/", bumper_every_n_tracks = 2,
                                     ad_every_n_tracks = 3 } },
      Z1 = { rev = "z", settings = { music_dir = "Lobby/", imaging_dir = "Imaging/", ads_dir = "Ads/", bumper_every_n_tracks = 1,
                                     ad_every_n_tracks = 1, crossfade_s = 3, level_target_db = -14 } },
      Z2 = { rev = "z", settings = { music_dir = "Empty/", imaging_dir = "", crossfade_s = 2 } },
    },
    server = {
      sync = function(part)
        local id, t = part.station, MockCore.clock
        for _, e in ipairs(part.events or {}) do
          got[#got + 1] = e
          if id == "Z1" and e.kind == "track_start" and e.file == PICK and t >= 1550 then pickedAt = pickedAt or e.at end
        end
        if part.state then states[id] = part.state end
        if part.meters then meters[id] = part.meters end
        if id == "Z2" and t > 200 and t < 205 and meters.Z1 then
          meterSplit = meterSplit or { z1 = meters.Z1.A and meters.Z1.A.peak, z2 = part.meters and part.meters.A and part.meters.A.peak }
        end
        local reply = { commands = {} }
        if id == "Z1" then
          for _, c in ipairs(COMMANDS) do
            if c.at <= t and c.id > part.ack and (sentAt[c.id] or c.any or midSong()) then
              sentAt[c.id] = sentAt[c.id] or t
              reply.commands[#reply.commands + 1] = { id = c.id, cmd = c.cmd, args = c.args, by = "tester" }
            end
          end
          local plan, rev = { due = { "Ads/ad1.wav" }, mix = { "Ads/ad1.wav", "Ads/ad2.wav" }, ident = "Imaging/id1.wav" }, "p1"
          pickSent = pickSent or (t >= 1550 and midSong() and t)
          if pickSent and not pickedAt then plan.next, plan.next_why, rev = "Lobby/" .. PICK, "rotation", "p2" end
          if pickedAt then rev = "p3" end
          if part.ads_rev ~= rev then reply.ads, reply.ads_rev = plan, rev end
        end
        return reply
      end,
      library = function(station, items, body)
        uploads[#uploads + 1] = { station = station, items = items, body = body }
        if station ~= "Z1" then return nil end
        learnedAt = learnedAt or MockCore.clock
        local learned = {}
        for _, it in ipairs(items) do
          local e = entry[it.file]
          if e and it.file:find("Song") then    -- analyzed (final): the quiet start and end, the level and peak
            learned[it.key] = { trail = e.trail, lead = e.lead, level = e.level, peak = e.level + 8, analyzed = true }
          end
        end
        return learned
      end,
    },
  }
  local Z1, Z2, R1 = Radio.byId.Z1, Radio.byId.Z2, Radio.byId.R1
  check(Z1.single and #Z1.decks == 1 and Z2.single and not R1.single and #R1.decks == 2 and not Z1.fault and not Z2.fault and not R1.fault,
    "a CFG entry with one player builds a one-deck station; two players a two-deck one")
  check(Radio.byId.Bad0.fault == "players must name one or two Audio Players" and Radio.byId.Bad3.fault == "players must name one or two Audio Players",
    "an entry with no player or three is faulted (" .. tostring(Radio.byId.Bad3.fault) .. ")")
  check(#Radio.decks == 4, string.format("the timers walk 4 decks (R1's two, Z1's, Z2's): %d", #Radio.decks))
  check(#Z1.lib.music == 22 and #Z1.lib.imaging == 0 and #Z1.lib.ad == 0,
    "the zone lists only its music (its imaging and ads folders are not even read)")

  -- the zone's player, sampled every 50 ms while its automation is on: how long it is ever silent; its gain
  local P = MockCore.players.Z1_P
  local stretches, silentFrom, gainBad, gainSeen = {}, nil, 0, 0
  every(0.05, function()
    local t, z = MockCore.clock, Radio.byId.Z1
    if z.automation and not P.playing then silentFrom = silentFrom or t
    elseif silentFrom then stretches[#stretches + 1] = { from = silentFrom, to = t }; silentFrom = nil end
    local d = z.live
    if z.state == "playing" and d and d.state == "playing" and not d.fade and d.track then   -- music level 0 dB: gain = trim
      gainSeen = gainSeen + 1
      if math.abs(P.ctl.gain.Value - d.trim) > 0.05 then gainBad = gainBad + 1 end
    end
  end)
  local r1Gaps = 0
  every(0.25, function()
    local r = Radio.byId.R1
    if MockCore.clock > 1 and r.automation and not r:busy() then r1Gaps = r1Gaps + 1 end
  end)
  -- the shuffle's picks with nothing left in its cycle that keeps artist separation (the cycle's last songs)
  local forced, realChoice = {}, Station.bagChoice
  Station.bagChoice = function(st)
    local i = realChoice(st)
    local h, n, any = st.history, st.settings.artist_separation, false
    for _, tr in ipairs(st.id == "Z1" and st.bag or {}) do
      local ok = true
      for j = math.max(1, #h - n + 1), #h do if tr.artist and h[j].artist == tr.artist then ok = false end end
      any = any or ok
    end
    if st.id == "Z1" and not any then forced[st.bag[i].name] = MockCore.clock end
    return i
  end
  MockCore.at(300, function() MockCore.dirs["Empty/"] = songs("Late", 8, 45, 10) end)   -- files arrive in Z2's folder
  MockCore.run(3600)
  Z1, Z2, R1 = Radio.byId.Z1, Radio.byId.Z2, Radio.byId.R1

  local function of(station, kind, from, to)
    local o = {}
    for _, e in ipairs(got) do
      if e.station == station and (not kind or e.kind == kind) and e.at >= (from or 0) and e.at < (to or 1e9) then o[#o + 1] = e end
    end
    return o
  end
  local function after(list, at)
    for _, e in ipairs(list) do if e.at >= at then return e end end
  end
  local zStarts = of("Z1", "track_start")
  check(#zStarts >= 55, string.format("the zone played %d songs in an hour", #zStarts))

  -- never a break
  local breaks = #of("Z1", "bumper_start") + #of("Z1", "ad_start") + #of("Z2", "bumper_start") + #of("Z2", "ad_start")
  local cmds = {}
  for _, e in ipairs(of("Z1", "command")) do cmds[e.id] = e end
  check(breaks == 0, "the zones never played a bumper, an ad or a station ID (bumper and ad every song, ads owed, an ID owed)")
  check(cmds[1] and not cmds[1].ok and cmds[1].note:find("one player", 1, true) and cmds[2] and not cmds[2].ok
    and cmds[2].note:find("one player", 1, true) and cmds[3] and not cmds[3].ok,
    "Bumper now, Ad break now and Play now on an imaging file are refused (" .. tostring(cmds[1] and cmds[1].note) .. ")")
  local libOk = true
  for _, u in ipairs(uploads) do
    if u.station == "Z1" then for _, it in ipairs(u.items) do if it.kind ~= "music" then libOk = false end end end
  end
  check(libOk and MockCore.libraries.Z1 and #MockCore.libraries.Z1.items >= 20, "its library upload lists its music only")

  -- state and meters: one deck
  local zs, zm = states.Z1 or {}, meters.Z1 or {}
  check(zs.players == 1 and zs.decks and zs.decks.A and zs.decks.B == nil and zm.A and zm.B == nil
    and states.R1.players == 2 and states.R1.decks.B and meters.R1.B,
    "its state: players = 1, decks { A } only; its meters { A } (the radio station: 2, A and B)")
  check(meterSplit and meterSplit.z1 > -60 and meterSplit.z2 <= -119,
    string.format("two zones on one meter read their own channels (Z1 %s, Z2 %s dB while only Z1 plays)",
      tostring(meterSplit and meterSplit.z1), tostring(meterSplit and meterSplit.z2)))

  -- rotation and artist separation
  local counts, picked, sepBad, sepForced = {}, { [PICK] = true, [NOW_KEY] = true, [NEXT_KEY] = true }, 0, 0
  for _, e in ipairs(LOBBY) do if not (e.unsupported or e.hang) then counts[e.name] = 0 end end
  for i, s in ipairs(zStarts) do
    counts[s.file] = (counts[s.file] or 0) + 1
    for j = math.max(1, i - 2), i - 1 do
      if not picked[s.file] and s.artist and zStarts[j].artist == s.artist then
        if forced[s.file] and s.at - forced[s.file] < 2 then sepForced = sepForced + 1 else sepBad = sepBad + 1 end
      end
    end
  end
  local lo, hi = 1e9, 0
  for _, n in pairs(counts) do lo, hi = math.min(lo, n), math.max(hi, n) end
  check(lo >= 2 and hi - lo <= 2, string.format("fair rotation: every playable song %d to %d times", lo, hi))
  check(sepBad == 0, string.format("artist separation (2): no artist again within two songs (%d), but when the rest of the " ..
    "shuffle cycle has none other (%d)", sepBad, sepForced))

  -- level matching and the quiet start
  local trimBad, skipBad, skipped = 0, 0, 0
  for _, s in ipairs(zStarts) do
    local e = entry[s.file]
    if s.file:find("Song") and s.at > learnedAt + 0.1 then       -- once the server's figures are in
      local want = -14 - e.level
      if (s.trim or 0) ~= want then trimBad = trimBad + 1 end
      if e.lead > 1 then skipped = skipped + 1; if s.skip ~= 0.9 then skipBad = skipBad + 1 end
      elseif s.skip then skipBad = skipBad + 1 end
    end
  end
  check(trimBad == 0 and gainBad == 0 and gainSeen > 1000,
    string.format("level matching: each song's trim brings it to -14 dB (%d wrong); the player's gain is its trim (%d of %d samples off)",
      trimBad, gainBad, gainSeen))
  check(skipped >= 20 and skipBad == 0, string.format("the measured quiet start is skipped (%d starts at 0.9 s; %d wrong)", skipped, skipBad))

  -- the hand-over: at the learned quiet end, the gap only the load time
  local ends, late, slow, gapMax = 0, 0, 0, 0
  for _, tr in ipairs(of("Z1", "transition")) do
    if tr.reason == "end" and not tr.stop then
      local st, en, nx
      for _, s in ipairs(zStarts) do if s.at <= tr.at then st = s end end
      en = after(of("Z1", "track_end"), tr.at)
      nx = en and after(zStarts, en.at)
      if st and en and nx and entry[st.file].trail == 2 then
        ends = ends + 1
        local quietEnd = st.at - (st.skip or 0) + st.duration - 2
        if math.abs(en.at - (quietEnd - 0.25)) > 0.2 or math.abs(en.at - tr.at - 3) > 0.1 then late = late + 1 end
        gapMax = math.max(gapMax, nx.at - en.at)
        if nx.at - en.at > 0.5 then slow = slow + 1 end
      end
    end
  end
  check(ends >= 30 and late == 0, string.format("%d songs faded out over the crossfade (3 s) to end at the learned quiet end (%d off)", ends, late))
  check(slow == 0, string.format("the next song plays on the same player after the load only (longest gap %.2f s)", gapMax))
  local tail = of("Z1", "transition")
  local silence, eofEnd = nil, nil
  for _, tr in ipairs(tail) do if tr.reason == "silence" then silence = silence or tr end end
  for _, e in ipairs(of("Z1", "track_end")) do if e.reason == "eof" and e.file == "Artist4 - Short.mp3" then eofEnd = eofEnd or e end end
  local afterSilence, afterEof = silence and after(zStarts, silence.at), eofEnd and after(zStarts, eofEnd.at)
  check(silence and silence.fade_s == 1 and afterSilence and afterSilence.at - silence.at < 1.5,
    "an unmeasured silent end: silence detected, faded over silence_fade_s, the next song after the load")
  check(eofEnd and afterEof and afterEof.at - eofEnd.at < 0.5, "a song shorter than its fade plays to its end; the next follows at once")

  -- the player is never silent longer than a load (a failed load: its timeout, then the next song)
  local longest, failLongest, failed = 0, 0, {}
  for _, k in ipairs({ "file_unsupported", "file_failed" }) do for _, e in ipairs(of("Z1", k)) do failed[#failed + 1] = e end end
  for _, s in ipairs(stretches) do
    local withFail = false
    for _, e in ipairs(failed) do if e.at >= s.from - 0.1 and e.at <= s.to + 0.1 then withFail = true end end
    if withFail then failLongest = math.max(failLongest, s.to - s.from) else longest = math.max(longest, s.to - s.from) end
  end
  local unsup, hung = false, false
  for _, e in ipairs(failed) do
    if e.file == "Artist5 - Broken.mp3" and e.kind == "file_unsupported" then unsup = true end
    if e.file == "Artist6 - Hang.mp3" and e.kind == "file_failed" then hung = true end
  end
  check(unsup and hung and failLongest > 0 and failLongest <= LOAD_TIMEOUT + 0.5 and #of("Z1", "dead_air_guard") == 0,
    string.format("a file that cannot load (unsupported, a load that never completes): the next song plays (silent %.2f s at most)", failLongest))
  check(longest < 0.5, string.format("otherwise the player is never silent longer than a load while on (%.2f s at most)", longest))

  -- the operator's commands
  local skip = cmds[4] and after(of("Z1", "transition"), cmds[4].at)
  local skipEnd = skip and after(of("Z1", "track_end"), skip.at)
  local skipNext = skipEnd and after(zStarts, skipEnd.at)
  check(cmds[4] and cmds[4].ok and skip and skip.reason == "skip" and skip.at - cmds[4].at < 0.3 and skipEnd.reason == "cut"
    and math.abs(skipEnd.at - skip.at - 1.5) < 0.1 and skipNext.at - skipEnd.at < 0.5,
    "Skip: the song fades over skip_fade_s (1.5 s), the next one follows after the load")
  local nowStart = cmds[5] and after(zStarts, cmds[5].at)
  check(cmds[5] and cmds[5].ok and nowStart and nowStart.file == NOW_KEY and nowStart.at - cmds[5].at < 2.5,
    "Play now: that song within the skip fade and a load")
  local qs = cmds[6] and of("Z1", "track_start", cmds[6].at)
  check(cmds[6] and cmds[6].ok and qs[1] and qs[1].file == NEXT_KEY, "Play next: the queued song is the next one")
  local offs, ons = of("Z1", "automation"), {}
  local stopAfter = offs[1]
  check(cmds[7] and cmds[7].ok and stopAfter and stopAfter.on == false and stopAfter.why == "stop_after_current"
    and #of("Z1", "track_start", stopAfter.at, cmds[8] and cmds[8].at or 1e9) == 0,
    string.format("Stop after current: the song plays to its end, fades, nothing follows (stopped at t=%s)", stopAfter and stopAfter.at or "-"))
  local back = cmds[8] and after(zStarts, cmds[8].at)
  check(cmds[8] and cmds[8].ok and back and back.at - cmds[8].at < 0.5, "Start: a song at once")
  local stopEnd = cmds[9] and after(of("Z1", "track_end"), cmds[9].at)
  check(cmds[9] and cmds[9].ok and stopEnd and stopEnd.reason == "cut" and math.abs(stopEnd.at - cmds[9].at - 2) < 0.15
    and #of("Z1", "track_start", cmds[9].at, cmds[10] and cmds[10].at or 1e9) == 0, "Stop now: faded over stop_fade_s (2 s), silent until Start")
  check(pickedAt and pickedAt >= 1550 and pickedAt < 1700, string.format("the server's pick for the next song is played (t=%s)", tostring(pickedAt)))

  -- the empty folder, then files
  local z2Empty, z2First = of("Z2", "library_empty"), of("Z2", "track_start")[1]
  check(#z2Empty == 1 and z2First and z2First.at >= 300 and z2First.at <= 332 and #of("Z2", "track_start") >= 50,
    string.format("an empty folder waits (library_empty once); files arriving play within the guard's 30 s rescan (t=%s), and on",
      z2First and z2First.at or "-"))

  -- the radio station beside them
  local rDecks = {}
  for _, s in ipairs(of("R1", "track_start")) do rDecks[s.deck] = true end
  check(#of("R1", "bumper_start") >= 10 and #of("R1", "ad_start") >= 10 and rDecks.A and rDecks.B and r1Gaps == 0,
    string.format("the two-player station plays its bumpers (%d) and ads (%d) on both decks, never idle", #of("R1", "bumper_start"), #of("R1", "ad_start")))
end

-------------------------------------------------------------------------------
do
  print("-- Stop after current pressed during the end fade: nothing follows (2.2.0 dev played one more whole song)")
  local zw, zc = MockCore.zones(1)
  boot{ players = zw, stations = zc, dirs = { ["Lobby/"] = songs("Lobby", 6, 40) },
        settings = { Z1 = { rev = "z", settings = { music_dir = "Lobby/", crossfade_s = 4 } } } }
  local res, at, shown
  local function watch()
    local z = Radio.byId.Z1
    if z.state == "crossfading" and z.decks[1].state == "fading" then
      shown = z:snapshot(MockCore.clock).decks.A
      res, at = z:command({ id = 1, cmd = "stop_after" }), MockCore.clock
    else
      MockCore.at(MockCore.clock + 0.05, watch)
    end
  end
  MockCore.at(1, watch)
  MockCore.run(120)
  local z, starts, off = Radio.byId.Z1, 0, nil
  for _, e in ipairs(EVENTS) do
    if e.kind == "track_start" and at and e.at > at then starts = starts + 1 end
    if e.kind == "automation" and not e.on then off = e end
  end
  check(res and at and starts == 0 and off and off.why == "stop_after_current" and off.at - at < 4.1 and not z.automation
    and not MockCore.players.Z1_P.playing, string.format("accepted at t=%s; stopped when the fade ended (t=%s), no song after",
      tostring(at), tostring(off and off.at)))
  check(shown and shown.state == "fading" and shown.live and shown.fade_left and math.abs(shown.fade_left - 4) <= 0.1,
    "while it fades its state says so, still on air, with the fade's time left (for the panel and Coming up)")
end

-------------------------------------------------------------------------------
do
  print("-- a zone's Tick and Fast allocate nothing (one deck: the same loops)")
  local zw, zc = MockCore.zones(1)
  boot{ players = zw, stations = zc, dirs = { ["Lobby/"] = songs("Lobby", 6, 60) },
        settings = { Z1 = { rev = "z", settings = { music_dir = "Lobby/" } } } }
  MockCore.run(75)                                -- the second song, mid-way, first play: no figures yet
  local Z1 = Radio.byId.Z1
  local function allocated(fn, n)
    collectgarbage("collect")
    collectgarbage("stop")
    fn()
    local before = collectgarbage("count")
    for _ = 1, n do fn() end
    local kb = collectgarbage("count") - before
    collectgarbage("restart")
    return kb
  end
  local playing = Z1.state == "playing" and Z1.live and not Z1.learned[Z1.live.track.key]
  local tickKb = allocated(function() Radio.tick() end, 2000)
  Z1.live:fadeOut(60, function() end)
  local fastKb = allocated(function() MockCore.clock = MockCore.clock + 0.001; Engine.fast() end, 2000)
  check(playing and tickKb == 0 and fastKb == 0, string.format("2,000 ticks: %.2f KB, 2,000 fade steps: %.2f KB allocated", tickKb, fastKb))
end

-------------------------------------------------------------------------------
do
  print("-- hours of play: three zones and a radio station, Lua memory does not grow")
  local zw, zc = MockCore.zones(3)
  local rw, rc = MockCore.stations(1, { ids = { "R1" } })
  for _, w in ipairs(zw) do rw[#rw + 1] = w end
  local cfg = { rc[1], zc[1], zc[2], zc[3] }
  local plays, spots, zoneSpots = 0, 0, 0
  local dirs = { ["A/"] = songs("A", 25, 60, 60), ["B/"] = songs("B", 25, 70, 50), ["C/"] = songs("C", 25, 50, 70),
                 ["D/"] = songs("D", 25, 55, 40) }
  for k, v in pairs(BREAKS) do dirs[k] = v end
  boot{
    players = rw, stations = cfg, dirs = dirs, seed = 13,
    settings = {
      R1 = { rev = "m", settings = { music_dir = "A/", imaging_dir = "Imaging/", ads_dir = "Ads/", ad_every_n_tracks = 4, bumper_every_n_tracks = 3 } },
      Z1 = { rev = "m", settings = { music_dir = "B/", imaging_dir = "Imaging/", bumper_every_n_tracks = 1, fade_in_s = 1 } },
      Z2 = { rev = "m", settings = { music_dir = "C/", crossfade_s = 2 } },
      Z3 = { rev = "m", settings = { music_dir = "D/" } },
    },
    server = {
      sync = function(part)
        for _, e in ipairs(part.events or {}) do
          if e.kind == "track_start" then plays = plays + 1
          elseif e.kind == "bumper_start" or e.kind == "ad_start" then
            spots = spots + 1
            if part.station ~= "R1" then zoneSpots = zoneSpots + 1 end
          end
        end
        local t, reply = MockCore.clock, { commands = {} }
        local slot = math.floor(t / 600)                          -- every 10 min: a skip on Z1, new settings for Z2, a pick for Z3
        if part.station == "Z1" and t % 600 < 1 and part.ack < slot then reply.commands[1] = { id = slot, cmd = "skip" } end
        if part.station == "Z2" and part.settings_rev ~= "m" .. slot % 2 then
          reply.settings, reply.settings_rev = { music_dir = "C/", crossfade_s = 2 + slot % 2 }, "m" .. slot % 2
        end
        if part.station == "Z3" and part.ads_rev ~= "p" .. slot then
          reply.ads, reply.ads_rev = { due = {}, mix = {}, next = string.format("D/D%d - Song%03d.mp3", slot % 7, slot % 25 + 1),
                                       next_why = "rotation" }, "p" .. slot
        end
        return reply
      end,
    },
  }
  quiet = true
  every(60, function() MockCore.log, MockCore.http = {}, {} end)
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
  check(plays > 600 and spots > 50 and zoneSpots == 0,
    string.format("4 h on 4 stations: %d songs, %d breaks items on the radio station, none on the zones", plays, spots))
  local grew = mem[#mem].kb - mem[1].kb
  check(grew < 64, string.format("no growth from hour 1 to hour 4 (%+.0f KB)", grew))
  check(#Server.outbox < 50 and #EVENTS == 500, "bounded: the outbox drained, the event ring at 500")
end

print = realPrint
print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
