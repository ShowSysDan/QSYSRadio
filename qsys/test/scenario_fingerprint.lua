-- Scenario: file fingerprints (radio.lua 2.4.0): the sha1 of a file's size, its first, middle and last 64 KiB (the
-- whole file when no longer than the three), made with Crypto.Digest over io reads, for the files the server lists (top-level
-- fingerprint / fingerprint_rev), one every 0.5 s on a timer of its own that runs only while files are listed; each
-- reported as a 'fingerprint' event of the station the list named and of every other station of the Core that lists
-- the same file (one read); an unreadable file reported and passed; Crypto's raw or hex digest; the capability check
-- (no Crypto, a wrong digest, no io seek: off, logged once, no fingerprint_rev); Tick and Fast allocate nothing while
-- it works; Lua memory stays flat; the probe's section F makes the same fingerprints. Run with run_mock.py.
dofile("qsys/test/mock_core.lua")

local fails = 0
local function check(cond, msg) print((cond and "PASS  " or "FAIL  ") .. msg); if not cond then fails = fails + 1 end end
local SETTINGS_FILE, OUTBOX_FILE = "media/_radio/settings.json", "media/_radio/outbox.json"
local realPrint, quiet, logged = print, false, {}
print = function(s)                            -- radio.lua's log, captured (and silenced for hours of work)
  if quiet then return end
  if type(s) == "string" and s:find("^%d%d:%d%d:%d%d ") then logged[#logged + 1] = s end
  realPrint(s)
end
local function loggedCount(text) local n = 0 for _, l in ipairs(logged) do if l:find(text, 1, true) then n = n + 1 end end return n end
local function every(dt, fn)
  local function go() fn(); MockCore.at(MockCore.clock + dt, go) end
  MockCore.at(MockCore.clock + dt, go)
end

-- the fake server's list of files to fingerprint ({ station, key }), at most 20 at a time, sent when the Core's
-- fingerprint_rev differs; a file leaves it when its 'fingerprint' event arrives (q.keep: it stays, as on a server
-- that has not taken the event in yet). results[station|key] = the events
local q
local function queue(owed)
  q = { owed = owed or {}, results = {}, lists = {}, bodies = {}, version = 0, last = nil }
  function q.core(body)
    q.bodies[#q.bodies + 1] = body
    for _, part in ipairs(body.stations or {}) do
      for _, e in ipairs(part.events or {}) do
        if e.kind == "fingerprint" then
          local id = part.station .. "|" .. (e.dir or "") .. (e.file or "")
          local r = q.results[id] or {}
          q.results[id], r[#r + 1] = r, e
          for i = #q.owed, 1, -1 do
            if not q.keep and q.owed[i].station .. "|" .. q.owed[i].key == id then table.remove(q.owed, i) end
          end
        end
      end
    end
    if body.fingerprint_rev == nil then return nil end         -- this Core cannot fingerprint
    local list, ids = {}, {}
    for i = 1, math.min(20, #q.owed) do
      list[i], ids[i] = { station = q.owed[i].station, key = q.owed[i].key }, q.owed[i].station .. "|" .. q.owed[i].key
    end
    local now = table.concat(ids, "\n")
    if now ~= q.last then q.version, q.last = q.version + 1, now end
    local rev = #list > 0 and ("f" .. q.version) or ""
    if rev == body.fingerprint_rev then return nil end
    q.lists[#q.lists + 1] = { at = MockCore.clock, list = list, rev = rev }
    return { fingerprint = list, fingerprint_rev = rev }
  end
  return q
end
local function result(station, key) return (q.results[station .. "|" .. key] or {})[1] end
local function results(station, key) return #(q.results[station .. "|" .. key] or {}) end

-- Main (two players) plays Test/Tuning/; Z1 (one player) plays Zone/; Z2 (one player) also plays Test/Tuning/
local function boot(spec)
  for _, f in ipairs({ SETTINGS_FILE, OUTBOX_FILE }) do os.remove(f) end
  local rw, rc = MockCore.stations(1, { ids = { "Main" } })
  local zw, zc = MockCore.zones(2)
  for _, w in ipairs(zw) do rw[#rw + 1] = w end
  MockCore.setup{ seed = 5, dirs = spec.dirs, players = rw, crypto = spec.crypto, ioSeek = spec.ioSeek }
  local f = assert(io.open(SETTINGS_FILE, "w"))
  f:write(require("json").encode({ stations = {
    Main = { rev = "s", settings = { music_dir = "Test/Tuning/", imaging_dir = "" } },
    Z1 = { rev = "s", settings = { music_dir = "Zone/" } }, Z2 = { rev = "s", settings = { music_dir = "Test/Tuning/" } } } }))
  f:close()
  queue(spec.owed)
  MockCore.serve{ core = spec.core or q.core }
  logged = {}
  MockCore.loadRadio(function(cfg)
    cfg.SERVER_URL, cfg.ANALYZERS = "http://radio-server:5410", {}
    cfg.STATIONS = { rc[1], zc[1], zc[2] }
    if spec.cfg then spec.cfg(cfg) end
  end)
end

-- Test/Tuning/: two songs, a small file (read whole), one of exactly 192 KiB (read whole), one that cannot be opened;
-- Zone/: A - One.flac again (the same file: same name, size and content), a copy of it renamed, a file of the same
-- name and size as B - Two.flac with other content
local function tuning()
  return {
    { name = "A - One.flac", duration = 60, level = -10 },
    { name = "B - Two.flac", duration = 60, level = -12, size = 2000000 },
    { name = "C - Small.flac", duration = 40, level = -10, size = 1000 },
    { name = "D - Edge.flac", duration = 40, level = -10, size = 196608 },
    { name = "E - Locked.flac", duration = 40, level = -10, unreadable = true },
  }
end
local function zone()
  return {
    { name = "A - One.flac", duration = 60, level = -10 },
    { name = "A - One (renamed).flac", duration = 60, level = -10, content = "A - One.flac" },
    { name = "B - Two.flac", duration = 60, level = -12, size = 2000000, content = "another recording" },
  }
end
local function entry(dir, name) return MockCore.lookup(dir, name) end
local function own(station, dir, names)
  local o = {}
  for _, n in ipairs(names) do o[#o + 1] = { station = station, key = dir .. n } end
  return o
end
local function hashingOn() return Hashing.running == true end
local function rev(body) return body and body.fingerprint_rev end

-------------------------------------------------------------------------------
do
  print("-- the Core can fingerprint (Crypto.Digest gives raw bytes): nothing read until the server lists files")
  boot{ dirs = { ["Test/Tuning/"] = tuning(), ["Zone/"] = zone() } }
  check(Fingerprint.can == true and loggedCount("fingerprint: on") == 1, "capability check passed at boot (logged once)")
  local opensAtBoot = MockCore.io.opens
  MockCore.run(30)
  local revs = true
  for _, b in ipairs(q.bodies) do if b.fingerprint_rev ~= "" then revs = false end end
  check(#q.bodies >= 25 and revs, string.format("every sync body carries the top-level fingerprint_rev (%d bodies)", #q.bodies))
  check(MockCore.io.opens == opensAtBoot and not hashingOn() and not Fingerprint.on,
    "nothing listed: no file read, the fingerprint timer stopped")

  print("-- the server lists Main's files (one unreadable) and a file of a station not on this Core")
  local listedAt = MockCore.clock
  for _, e in ipairs(own("Main", "Test/Tuning/", { "A - One.flac", "B - Two.flac", "C - Small.flac", "D - Edge.flac", "E - Locked.flac" })) do
    q.owed[#q.owed + 1] = e
  end
  q.owed[#q.owed + 1] = { station = "Elsewhere", key = "Test/Tuning/A - One.flac" }
  local ticks, fastRuns = 0, 0
  local realStep = Fingerprint.step
  Fingerprint.step = function() ticks = ticks + 1; return realStep() end   -- the timer's handler calls through the table
  MockCore.run(1.5)
  check(hashingOn() and Fingerprint.on, "the list arrived: the fingerprint timer runs")
  MockCore.run(10)
  local okAll, sizes = true, true
  for _, n in ipairs({ "A - One.flac", "B - Two.flac", "C - Small.flac", "D - Edge.flac" }) do
    local r, e = result("Main", "Test/Tuning/" .. n), entry("Test/Tuning/", n)
    if not (r and r.fp == MockCore.fingerprint(e) and r.error == nil) then okAll = false end
    if not (r and r.size == MockCore.fileSize(e) and r.dir == "Test/Tuning/" and r.file == n) then sizes = false end
  end
  check(okAll, "each listed file's fingerprint is the sha1 of its size, first, middle and last 64 KiB (the whole file when smaller)")
  check(sizes, "each event names the file (dir, file) and its size in bytes")
  local locked = result("Main", "Test/Tuning/E - Locked.flac")
  check(locked and locked.fp == nil and type(locked.error) == "string" and locked.error:find("cannot open", 1, true),
    "an unreadable file is reported with its error (" .. tostring(locked and locked.error) .. ") and passed")
  check(results("Elsewhere", "Test/Tuning/A - One.flac") == 0, "a station not on this Core: nothing done")
  local same = result("Z2", "Test/Tuning/A - One.flac")
  check(same and same.fp == result("Main", "Test/Tuning/A - One.flac").fp and MockCore.io.opens - opensAtBoot == 5,
    string.format("Z2 lists the same folder: its events come from the same read (%d opens for 5 files)", MockCore.io.opens - opensAtBoot))
  check(MockCore.io.maxRead <= 196608, string.format("at most 192 KiB read per file (%d)", MockCore.io.maxRead))
  local first, last = math.huge, 0
  for _, e in ipairs(EVENTS) do
    if e.kind == "fingerprint" and e.station == "Main" then first, last = math.min(first, e.at), math.max(last, e.at) end
  end
  check(last - first >= 4 * 0.5 - 0.01, string.format("one file per 0.5 s (5 files over %.2f s)", last - first))
  check(not hashingOn() and not Fingerprint.on and ticks <= 8,
    string.format("the list worked through: the timer stopped (%d timer runs)", ticks))
  local before = MockCore.io.opens
  MockCore.run(60)
  check(MockCore.io.opens == before and results("Main", "Test/Tuning/A - One.flac") == 1 and ticks <= 8,
    "nothing read again once done (the server's list is empty)")
  Fingerprint.step = realStep

  print("-- moved, renamed, the same name and size but other content")
  for _, e in ipairs(own("Z1", "Zone/", { "A - One.flac", "A - One (renamed).flac", "B - Two.flac" })) do q.owed[#q.owed + 1] = e end
  MockCore.run(5)
  local a, ren, b2 = result("Z1", "Zone/A - One.flac"), result("Z1", "Zone/A - One (renamed).flac"), result("Z1", "Zone/B - Two.flac")
  local a1, b1 = result("Main", "Test/Tuning/A - One.flac"), result("Main", "Test/Tuning/B - Two.flac")
  check(a and a.fp == a1.fp and ren and ren.fp == a1.fp, "a moved and a renamed copy have the file's fingerprint")
  check(b2 and b2.size == b1.size and b2.fp ~= b1.fp, "same name and size, other content: another fingerprint")

  print("-- the reply: a file done and still listed is not done again; listed again later, it is; an empty list ({}) is taken")
  local A, B = { station = "Main", key = "Test/Tuning/A - One.flac" }, { station = "Main", key = "Test/Tuning/B - Two.flac" }
  local n, listsBefore = MockCore.io.opens, #q.lists
  q.keep, q.owed = true, { A }
  MockCore.run(4)
  q.owed[2] = B                                      -- a new list (a new rev) that still names A
  MockCore.run(4)
  check(MockCore.io.opens == n + 2 and results("Main", A.key) == 2 and results("Main", B.key) == 2 and #q.lists == listsBefore + 2,
    "A done once while the server keeps listing it (a new list with B: only B read)")
  q.keep, q.owed = false, {}
  MockCore.run(3)
  check(#Fingerprint.todo == 0 and not hashingOn() and Fingerprint.rev == "" and q.lists[#q.lists].rev == "",
    "the server's empty list ({} from Lua's json) taken: nothing to do, the timer stopped")
  q.owed = { A }
  MockCore.run(3)
  check(results("Main", A.key) == 3 and MockCore.io.opens == n + 3, "listed again later: done again")
  local bad = { "junk", { station = 1, key = "x" }, { station = "Main" } }
  for i = 1, 80 do bad[#bad + 1] = { station = "Main", key = "Test/Tuning/A - One.flac" } end   -- one file, many times
  for i = 1, 80 do bad[#bad + 1] = { station = "Main", key = "Test/Tuning/x" .. i } end
  Fingerprint.list(bad, "r-big")
  check(#Fingerprint.todo == 50, string.format("a list is capped (%d of 81 distinct entries)", #Fingerprint.todo))
  Fingerprint.list({}, "")
  MockCore.run(1)

  print("-- Crypto.Digest failing on the Core (after the check): each file reported, the stations play on")
  local realDigest = Crypto.Digest
  Crypto.Digest = function() error("crypto engine busy") end
  q.owed = { { station = "Main", key = "Test/Tuning/C - Small.flac" } }
  MockCore.run(3)
  Crypto.Digest = realDigest
  local errs = q.results["Main|Test/Tuning/C - Small.flac"]
  local lastErr = errs and errs[#errs]
  check(lastErr and lastErr.error and lastErr.error:find("crypto engine busy", 1, true) and not Radio.byId.Main.fault
    and Radio.byId.Main.state ~= "off", "reported as the file's error (" .. tostring(lastErr and lastErr.error) .. "); no station fault")

  print("-- Tick and Fast allocate nothing while files are fingerprinted")
  q.owed = own("Z1", "Zone/", { "A - One.flac", "A - One (renamed).flac", "B - Two.flac" })
  for _ = 1, 40 do if hashingOn() then break end MockCore.run(0.05) end
  local function allocated(fn, k)
    collectgarbage("collect")
    collectgarbage("stop")
    fn()
    local b = collectgarbage("count")
    for _ = 1, k do fn() end
    local kb = collectgarbage("count") - b
    collectgarbage("restart")
    return kb
  end
  local busy = hashingOn() and Fingerprint.pos <= #Fingerprint.todo
  local tickKb = allocated(function() Radio.tick() end, 2000)
  local fastKb = allocated(function() MockCore.clock = MockCore.clock + 0.001; Engine.fast() end, 2000)
  check(busy and tickKb == 0 and fastKb == 0,
    string.format("2,000 ticks: %.2f KB, 2,000 fast steps: %.2f KB allocated (the list being worked)", tickKb, fastKb))
end

-------------------------------------------------------------------------------
do
  print("-- Crypto.Digest gives hex (upper case): the same fingerprints")
  boot{ dirs = { ["Test/Tuning/"] = tuning(), ["Zone/"] = zone() }, crypto = "hex",
        owed = own("Main", "Test/Tuning/", { "A - One.flac", "B - Two.flac" }) }
  MockCore.run(5)
  local a, b = result("Main", "Test/Tuning/A - One.flac"), result("Main", "Test/Tuning/B - Two.flac")
  check(Fingerprint.can and a and a.fp == MockCore.fingerprint(entry("Test/Tuning/", "A - One.flac"))
    and b and b.fp == MockCore.fingerprint(entry("Test/Tuning/", "B - Two.flac")), "hex digests read as the raw ones")
end

-------------------------------------------------------------------------------
local function offCase(label, spec, why)
  print("-- " .. label .. ": fingerprints off, files known by name + size")
  spec.dirs = { ["Test/Tuning/"] = tuning(), ["Zone/"] = zone() }
  spec.owed = own("Main", "Test/Tuning/", { "A - One.flac" })
  boot(spec)
  MockCore.run(65)                                  -- past two Chores
  local sent = false
  for _, b in ipairs(q.bodies) do if rev(b) ~= nil then sent = true end end
  local listed = { fingerprint = { { station = "Main", key = "Test/Tuning/A - One.flac" } }, fingerprint_rev = "x" }
  MockCore.serve{ core = function(body) q.bodies[#q.bodies + 1] = body; return listed end }   -- a server that lists all the same
  MockCore.run(5)
  local fps = 0
  for _, e in ipairs(EVENTS) do if e.kind == "fingerprint" then fps = fps + 1 end end
  check(Fingerprint.can == false and loggedCount("fingerprint: off") == 1 and loggedCount(why) == 1,
    "the capability check failed, logged once: " .. why)
  check(not sent and fps == 0 and not hashingOn() and #Fingerprint.todo == 0,
    "no fingerprint_rev sent; a list sent anyway is ignored, nothing read")
  local plays = 0
  for _, e in ipairs(EVENTS) do if e.kind == "track_start" then plays = plays + 1 end end
  check(plays >= 3 and not Radio.byId.Main.fault and not Radio.byId.Z1.fault, string.format("the stations play on (%d songs)", plays))
end
offCase("no Crypto library", { crypto = false }, "Crypto.Digest(\"sha1\") failed")
offCase("io without seek", { ioSeek = false }, "a test read of media/Audio/Test/Tuning/A - One.flac")
offCase("a Crypto.Digest that gives something else", { cfg = function() Crypto.Digest = function() return "nonsense" end end },
  "Crypto.Digest(\"sha1\") gave nil")

-------------------------------------------------------------------------------
do
  print("-- the first file listed cannot be opened: the next one decides (up to 5); none opens: off")
  local dirs = { ["Test/Tuning/"] = tuning(), ["Zone/"] = zone() }
  table.insert(dirs["Test/Tuning/"], 1, { name = "0 - Locked.flac", duration = 40, unreadable = true })
  boot{ dirs = dirs }
  check(Fingerprint.can == true and loggedCount("fingerprint: on (a test read of media/Audio/Zone/") == 1,
    "Main's first file does not open: Z1's first file tested, on")
  local locked = {}
  for i = 1, 6 do locked[i] = { name = string.format("%d - Locked.flac", i), duration = 40, unreadable = true } end
  boot{ dirs = { ["Test/Tuning/"] = locked, ["Zone/"] = locked } }
  check(Fingerprint.can == false and loggedCount("fingerprint: off (a test read of") == 1 and MockCore.io.opens == 3,
    string.format("nothing opens (%d tried: each station's first file): off, logged once", MockCore.io.opens))
end

-------------------------------------------------------------------------------
do
  print("-- no file listed at boot: checked again by Chores once a folder has files")
  boot{ dirs = { ["Test/Tuning/"] = {}, ["Zone/"] = {} } }
  check(Fingerprint.can == nil, "no file to test yet: undecided, no fingerprint_rev")
  MockCore.dirs["Zone/"] = zone()
  Radio.byId.Z1:scan()
  MockCore.run(32)
  check(Fingerprint.can == true and rev(q.bodies[#q.bodies]) == "", "the next Chores tested a file: on, fingerprint_rev sent")
end

-------------------------------------------------------------------------------
do
  print("-- a station whose build failed: its listed files are reported (not built) and the list moves on; after its")
  print("   rebuild (and library upload) they are listed again")
  local owed = own("Z1", "Zone/", { "A - One.flac", "B - Two.flac" })
  owed[3] = { station = "Main", key = "Test/Tuning/A - One.flac" }
  boot{ dirs = { ["Test/Tuning/"] = tuning(), ["Zone/"] = zone() }, owed = owed,
        cfg = function(cfg) cfg.STATIONS[2].players = { "Missing_Player" } end }
  MockCore.run(10)
  local z1, r = Radio.byId.Z1, result("Z1", "Zone/A - One.flac")
  check(z1.fault and r and r.fp == nil and r.error:find("station not built", 1, true) and results("Z1", "Zone/B - Two.flac") == 1
    and result("Main", "Test/Tuning/A - One.flac").fp and not hashingOn(),
    "not built: its files reported as such (" .. tostring(r and r.error) .. "), Main's listed after them done; the timer stopped")
  z1.cfg.players = { "Z1_P" }
  MockCore.run(40)
  q.owed = own("Z1", "Zone/", { "A - One.flac" })              -- the server owes it again after Z1's library upload
  MockCore.run(5)
  local done = q.results["Z1|Zone/A - One.flac"]
  check(not Radio.byId.Z1.fault and #done == 2 and done[2].fp == MockCore.fingerprint(entry("Zone/", "A - One.flac")),
    "rebuilt: listed again, its file fingerprinted")
end

-------------------------------------------------------------------------------
do
  print("-- hours of work: a new list every 10 minutes, Lua memory does not grow")
  local dirs = { ["Test/Tuning/"] = {}, ["Zone/"] = zone() }
  for i = 1, 60 do dirs["Test/Tuning/"][i] = { name = string.format("S%02d - Song.mp3", i), duration = 50 + i % 20, level = -10 - i % 4 } end
  local all = {}
  boot{ dirs = dirs, core = function(body)
    local slot = math.floor(MockCore.clock / 600)
    if body.fingerprint_rev ~= nil and body.fingerprint_rev ~= "h" .. slot then
      local list = {}
      for i = 1, 20 do list[i] = { station = "Main", key = string.format("Test/Tuning/S%02d - Song.mp3", (slot * 20 + i - 1) % 60 + 1) } end
      return { fingerprint = list, fingerprint_rev = "h" .. slot }
    end
  end }
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
  MockCore.run(3600)
  measure("2 h")
  quiet = false
  local done = 0
  for _, e in ipairs(EVENTS) do if e.kind == "fingerprint" then done = done + 1 end end
  print(string.format("        Lua memory after a full collect: %s %.0f KB, %s %.0f KB", mem[1].label, mem[1].kb, mem[2].label, mem[2].kb))
  check(MockCore.io.digests >= 12 * 20, string.format("2 h: %d files fingerprinted (20 every 10 min)", MockCore.io.digests))
  check(mem[2].kb - mem[1].kb < 64, string.format("no growth from hour 1 to hour 2 (%+.0f KB)", mem[2].kb - mem[1].kb))
  check(#Fingerprint.todo <= 50 and #EVENTS <= 500 and #Server.outbox < 50, "bounded: the list, the event ring, the outbox")
end

-------------------------------------------------------------------------------
do
  print("-- the probe's section F (run it on the real Core first): the same fingerprints, the digest form, its verdict")
  local function probe(crypto)
    local files = tuning()
    MockCore.setup{ seed = 1, dirs = { ["Test/Tuning/"] = files }, crypto = crypto,
      players = { { name = "Audio_Player_01", label = "A", meter = "PlayerOutput01", channel = 1 },
                  { name = "Audio_Player_02", label = "B", meter = "PlayerOutput01", channel = 2 } } }
    local f = assert(io.open("qsys/probe/probe.lua", "r"))
    local src = f:read("a")
    f:close()
    quiet = true
    assert(load(src, "@qsys/probe/probe.lua"))()
    MockCore.run(90)
    quiet = false
    local same = true
    for _, e in ipairs(files) do
      local got
      for _, r in ipairs(FP.files) do if r.rel == "Test/Tuning/" .. e.name then got = r end end
      if e.unreadable then same = same and got and got.error ~= nil
      else same = same and got and got.fp == MockCore.fingerprint(e) and got.size == MockCore.fileSize(e) end
    end
    return same, FP
  end
  local same, fp = probe("raw")
  check(same and fp.form == "raw (20 bytes)" and fp.correct and fp.on, "raw digest: each file's fingerprint as radio.lua makes it; verdict on")
  same, fp = probe("hex")
  check(same and fp.form == "hex (40 characters)" and fp.on, "hex digest: the same; verdict on")
  same, fp = probe(false)
  check(not fp.crypto and not fp.on and #fp.files == 5, "no Crypto: files still read (seek tested); verdict off")
end

print = realPrint
print("")
print(fails == 0 and "ALL CHECKS PASSED" or (fails .. " CHECK(S) FAILED"))
