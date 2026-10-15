# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Every change
ships as its own version: the top section always matches `VERSION`.

## [2.0.0] - 2026-10-15

### Changed (breaking)
- **One radio.lua runs every station on the Core.** `CFG.STATIONS` lists
  them, each with two Audio Players, a meter (peak + rms per player) and,
  for file analysis, the players' mixer:
  ```lua
  CFG = {
    SERVER_URL = "", SERVER_TOKEN = "", AUTOSTART = true,
    CORE_ID = "",            -- "" = the design name; names this Core to the server
    STATIONS = {
      { id = "Main", players = { "Audio_Player23", "Audio_Player24" }, meter = "PlayerMeter", meters = { 1, 2 },
        mixer = "RadioOut", mix = { 1, 2 } },      -- mixer = "" (or absent): no file analysis for this station
    },
  }
  ```
  Each station has its own decks, library, settings (a copy of `SETTINGS`,
  replaced by the server's values for that station), queue, shuffle, ad
  plan, file analysis and command ack, and behaves exactly as a 1.6.0
  station did. The Core has one each of the `Tick`, `Fast`, `Sync` and
  `Chores` timers for all of them: still one 250 ms timer and one sync
  request a second, whatever the number of stations. Stations listing the
  same folder share its track records (not the lists). Station ids must be
  unique and not empty: an entry without an id, or with an id already used,
  is skipped (logged); a player that another station already uses faults
  the later station.
- **Core ↔ server protocol 2.** One sync request a second carries every
  station: `{version, boot, core, mem_kb, stations: [{station, ack,
  settings_rev, ads_rev, analyze_rev (with a mixer), meters, state (when it
  changed, or every 10 s), events, dropped (when > 0)}]}`; the reply is
  `{stations: {<id>: {commands, settings, settings_rev, ads, ads_rev,
  analyze, analyze_rev, ack_reset, accepted, duplicates, rejected} |
  {error}}}`, every field meaning what it meant in 1.6.0. A station
  answered with `{error}` (or not answered) keeps its events for the next
  request, in `outbox.json` too (as in an outage, so a script restart
  meanwhile loses none); the others are unaffected. At most 100 events go per request,
  shared fairly between the stations. Every event also names its station.
- **The library goes up in parts.** After a scan the Core sends a station's
  files in parts of at most 1,000 (`{station, boot, scan, part, parts,
  music_dir, imaging_dir, ads_dir, items}`), one upload at a time for the
  whole Core, the stations taking turns. Parts before the last are answered
  `{ok, scan, part, parts}`, the last `{ok, items, learned}` as before. The
  server stages each scan (`library.scan`, `stations.scan` / `scan_parts`):
  files from any part are in the library at once, but only the last part,
  with every part of that scan in, marks the files not in it gone, so an
  interrupted or partial upload never shrinks a library. A last part
  without the others gets 409 (journal: `LIBRARY library scan … not
  received; send the whole scan again`) and the Core sends the whole scan
  again, with a new scan id, after a back-off (10 s, doubling to 2 min).
- **The server accepts the 1.6.0 Core** (one station, the old body and the
  whole library in one upload, answered as before) for this one version, so
  it can be upgraded first. A 2.0.0 Core needs a 2.0.0 server: against
  1.6.0 it keeps playing on its own and logs `! server: sync failed (400 )`.

### Added
- **A station that fails is faulted on its own.** Each station's tick, each
  deck's `playing` handler, each fade or load step, each command and each
  station's part of a sync reply run in their own pcall. A station whose
  tick fails 5 times in a row, or that cannot be built (a missing player,
  meter or control, no Script Access, a bad `CFG` entry), is faulted: its
  decks stop (the song on air is logged as ended, reason `fault`), a
  `station_fault` event says why (journal: `WARNING STATION_FAULT
  station=… error=… retry_s=…`), and it is built again from its `CFG` entry
  after 30 s, then 1, 2, 4 and every 5 min, back to 30 s after 10 min of
  healthy running. It keeps its settings, command ack and whether it was on
  air; a command sent to it meanwhile is answered "station fault: …". It
  still syncs, so the panel shows it. The other stations never notice.
- **Fault on the panel.** A red banner on the Dashboard with the reason and
  the next try; the header pill reads `fault · <station>`, the station
  drop-down `(fault)`, and the transport keys are off for that station.
  The library pages' file analysis line names the fault, not the `mixer`
  hint (a faulted station's state still says whether its `CFG` entry names
  a mixer). `/api/v1/now` gives each station's `fault` and `core`.
- **Bounded memory on the Core.** One outbox for every station, at most
  10,000 events (1.6.0: 5,000 for its one station), the oldest dropped and
  counted per station (each station reports its own losses once, as
  `dropped`); event ring 500, history 50, queue 100, files to analyze 5,000.
  A station keeps per-file figures only for the files its latest scan listed
  (1.6.0 kept one entry for every file it had played, analyzed or been told
  about since boot, through every folder change); a library reply that
  arrives after the station scanned again is not used (the new scan's upload
  brings the figures). The 250 ms and 50 ms timers allocate nothing (1.6.0
  allocated about 280 KB per 2,000 ticks). `outbox.json` is written at most
  every 2 min while it holds more than 2,000 events.
- **Lua memory, reported and watched.** The Core sends its Lua memory
  (`collectgarbage("count")`, `mem_kb`) with every sync and prints it in its
  heartbeat, now one line every 30 s for the whole Core:
  `heartbeat: Lua 840 KB, outbox 0 | Main: playing <file> | Lobby: FAULT (…)`.
  The server keeps each Core in a new table `cores` (boot, version, last
  contact, memory; migration 0016). One sample includes garbage not yet
  collected (it saws up and down), so the server keeps the lowest sample
  of each 10-minute window: the lowest window low from 10 minutes after a
  boot is the settled low, and when a whole window's low stays above twice
  that and 32 MB it logs one warning an hour: `core=<id> Lua memory <n> MB
  at its lowest in 10 min, was <m> MB after start: possible leak`. A new
  boot starts over.
- **Cores have names.** `CFG.CORE_ID` (default: the design name) names the
  Core to the server; `stations.core` keeps the Core each station was last
  seen on, and the Dashboard shows `· Main on <core> · radio.lua 2.0.0`.
- **MockCore 0.4.0** runs any number of players, meters and mixers by name
  and has a fake 2.x server; new `scenario_multi.lua` (three to five
  stations: faults and rebuilds, a long outage, a 3,000-file library in
  parts with a 409, folder changes with per-file figures kept only for the
  files listed, four hours of play with flat memory, the 1.x upgrade).
  `live_core.py --stations N --core NAME` runs several stations against a
  real server.

### Changed
- **Each station is its own transaction on the server.** A batched sync
  uses one pooled connection; each station's part runs under its own
  station lock and is committed before the next. Every web change to a
  station (commands, holds, ID marks, ad rules, settings, analysis, quiet
  start / end, Forget measured) takes that lock first, before it touches
  any row (`db.lock_station`; All stations settings: every station's, in
  name order), so none can deadlock with that station's sync. A file's
  figures are shared by every station that has it, so a change to many
  files at once (Forget measured for a whole kind) can rarely meet another
  station's sync on the same files; PostgreSQL then cancels one of the two:
  that sync part is answered `{error}` (sent again), the web change answers
  409 "Another station was saving the same files at that moment, so nothing
  was changed. Try again." (journal: `POST /api/v1/library/forget cancelled
  by the database, nothing changed (409): deadlock detected`), never a
  server error. A part that fails (bad data, a database error) is rolled
  back alone and answered `{error}`; the journal says `station=X sync
  failed, rolled back (the other stations go on): …` at most once a minute.
  The database itself gone answers 503 for the whole request, and the Core
  backs off as before.
- **The ads plan is cached per station** (`stations.plan`, `plan_rev`,
  `plan_at`, `plan_until`): made again when the station's events come in,
  when anything it is made from changes (ad rules, holds, IDs, settings of
  the station or All stations, a complete library scan, any command: one
  helper, `ads.stale`), when it changes by the clock alone (an ad or ID slot
  opens, a slot's window closes, a hold ends, the hour turns) or after
  30 s; not on every sync of every station. A batched sync of 14 stations
  with 3,000 files each takes about 0.05 s (0.2 s when every plan is made
  again).
- **Event log lines and `core_events.data`** name the station of the sync
  part that carried the event, once (the event's own `station` field is
  not stored).
- **Core log lines** start with the station's id (`Main: library: …`,
  `! Main: load failed on deck A: …`).
- **File analysis needs the station's own mixer.** When it is off, the
  library pages now say to set `mixer` and `mix` in that station's
  `CFG.STATIONS` entry (was `CFG.MIXER`); the "Analyze new files daily at"
  help names the station's mixer instead of RadioOut.

Redeploy **both**, in this order:
1. Back up (README, Installation step 11).
2. **The server first:** `git pull && sudo systemctl restart qsysradio`
   (runs migration 0016: the `cores` table and new columns; reload open
   panel pages). The 1.6.0 Core plays on as before; the journal logs
   `station=Main runs radio.lua 1.6.0, server is 2.0.0` until step 3.
3. **Then the Core:** paste the new radio.lua into the **same** Text
   Controller and copy the old `CFG` into `CFG.STATIONS[1]`, with the same
   id so the station's history, settings and library carry on: the old
   `STATION_ID`, or, if it was `""`, the design name (the name in the
   panel's station drop-down). `PLAYER_A` / `PLAYER_B` become
   `players = { A, B }`, `METER` `meter`, `METER_A` / `METER_B`
   `meters = { A, B }`, `MIXER` `mixer`, `MIX_A` / `MIX_B` `mix = { A, B }`;
   `SERVER_URL`, `SERVER_TOKEN` and `AUTOSTART` stay where they are.
   `settings.json` and any unsent events on the Core become the first
   station's by themselves. One restart, so a short gap on air, as with any
   paste.

Going back: the 1.6.0 server runs on the new schema (migration 0016 only
adds), and needs the 1.6.0 radio.lua with the old `CFG` on the Core.

## [1.6.0] - 2026-10-15

### Changed
- **A file's figures are shared by every station.** Length, quiet start and
  end (measured or analyzed), the operator's quiet start / end, average
  level with its seconds, peak, unsupported and the analysis state now
  belong to the file, not to the station: one row per file name (no folder)
  and size in bytes (new table `media_files`, migration 0015), used by every
  station whose library holds that file, on any Core. A file moved to
  another folder or station keeps its figures; an edited file (new size)
  starts afresh, and now loses the operator's quiet start / end and the
  measured quiet start / end and length too (1.5.0 kept those and reset only
  the level, peak, unsupported and analysis state); a file whose size is
  unknown is never shared (a size of 0 from the Core counts as unknown). A
  station's Core gets the shared figures in its library upload reply, and
  every play, analysis or unsupported report from any station adds to them.
- **Trims and Forget measured apply on every station** that has the file:
  the server queues a rescan on each of those stations, so every Core takes
  the change at once; the Library page says so. A file analyzed on one
  station counts as analyzed for all (a run on another station skips it);
  the other stations' Cores take the analyzed figures at their next library
  scan (boot, folder change, Rescan). The job itself stays per station. A
  file's analysis result clears the Analyze request on every station that
  asked for it.
- **Per-sync cost.** The next-song pick leaves unsupported files out
  without a lookup per song; the files owed analysis are read through new
  indexes (a long run stops after the first 20, the end of a run reads only
  the files not analyzed); a library upload marks only the files that left
  the folder as gone. Measured on 14 stations × 3,000 files: the
  once-a-second sync and a library upload cost what they did in 1.5.0; a
  station's first upload of files other stations already have takes about
  0.1 s longer (each new library row is checked against its file).

Redeploy the server, **after a database backup** (README, Installation
step 11: the `pg_dump` of the app schema): `git pull && sudo systemctl
restart qsysradio` (runs migration 0015; reload open panel pages). The
migration moves the per-file figures out of the `library` table into
`media_files`, merged per file: the quiet start / end of the latest analysis
(else of the station that saw the file most recently), the latest trims,
the levels averaged by the seconds each heard, the highest peak, the latest
analysis state (about 2 s for 14 stations × 3,000 files, during which the
Core keeps playing). Nothing is measured again. 1.5.0 does not run on the new
schema, so going back to 1.5.0 afterwards needs that backup (README, step
13). The Core only changed its version number: paste radio.lua when
convenient (until then the server logs a version mismatch).

## [1.5.0] - 2026-10-15

### Added
- **Settings by station.** Three layers, the most specific wins: radio.lua's
  fallback, then **All stations**, then **this station**. A station stores
  only what differs (new table `station_settings`, migration 0014); the
  All-stations values stay where they were (`app_settings`, `core.*`). Each
  station's Core gets its own merged settings and its own rev; a station
  with nothing of its own gets exactly the All-stations values, as before.
  The server's own settings (Station ID schedule, Station ID may be late by,
  Ad timing tolerance, Analyze new files daily at) are per station the same
  way: ad plans, IDs, the next-song pick, file analysis and Coming up use the
  station's values.
- **Settings page: All stations or one station.** A switch at the top of
  Station settings: **All stations** or the station chosen in the header
  (the default, once a station has connected). For a station, each setting
  says where its value comes from (this station, all stations or the
  default) and **use all stations (value)** takes a station's own value away
  again. Switching scope or the header's station asks first when there are
  unsaved changes. `GET /api/v1/settings?station=X[&scope=all]` says which
  view it is and adds `all`, `own`, `source` and `station_only` to each row;
  `POST /api/v1/settings` takes `scope: 'station'` (with `station`) or
  `'all'` (the default, as before). The SETTINGS log line names the scope
  (`station=Main` or `station=all`); a station reset reads
  `name=all-stations`.

### Changed
- **The folders are per station only.** Music, Imaging and Advertisements
  folder are set for a station, never for all stations (the server answers
  400), so a new station never inherits another's folders. A station without
  a music folder of its own plays radio.lua's fallback folder, and its
  Settings page says so.

Redeploy the server: `git pull && sudo systemctl restart qsysradio` (runs
migration 0014; reload open panel pages). Nothing else to do: the migration
moves the folders set today to every station already known, so the existing
station keeps its folders, and every other setting becomes its All-stations
value. The Core gets the same settings with the same rev (no rescan). The
Core only changed its version number: paste radio.lua when convenient
(until then the server logs a version mismatch).

## [1.4.1] - 2026-10-14

### Changed
- **Softer glow.** Every coloured halo on the panel (status pill, active
  tab, on-air deck, big numbers, primary buttons, lamps, the tiles' corner
  tint) is 30 % fainter. One knob sets them all: `--glow` at the top of
  `radio.css` (1 = the 1.4.0 look). Focus rings and borders are unchanged.
- **Titles scroll like a ticker.** A title too long for its card scrolls by
  continuously, round and round (about 35 px a second), instead of sliding
  to its end and back. A dot marks where the title starts again, and the
  card's edges fade softly while it moves.

Redeploy the server: `git pull && sudo systemctl restart qsysradio` (reload
open panel pages). The Core only changed its version number: paste
radio.lua when convenient (until then the server logs a version mismatch).

## [1.4.0] - 2026-10-14

### Added
- **Nav rail with what is on air.** The tabs are console keys with icons
  and each library's count; the rail stays at the top while a page scrolls
  and shows the song on air with its countdown on every page but the
  Dashboard, its progress as a red line along the rail's bottom edge.
- **Header.** A logo mark, the clock labelled as the server's, a chip with
  file analysis progress while a run is on (click: the Music page), and an
  account menu (your role, Sign out) instead of the name and link.
- **Pressed, sent and done looks on command buttons.** Transport keys go
  down when pressed; every button that sends a command (transport, Play
  next, Queue, Play now, Bumper now, Rescan) shows a sweep along its bottom
  edge until the Core answers, then flashes green (done) or red (failed).
- **Pages reload themselves.** Library pages also reload as each file
  analysis result comes in (`analysis.last` in the status poll: the latest
  result), when a play ends, and once a minute (other operators' changes,
  this hour's ad slots). The play log adds today's plays as they start and
  end.

### Changed
- **Deck meters glide.** The Core still reports once a second; between
  reports the bar follows like a damped spring (no stops or corners) and
  sways gently around the reading while there is sound, and the peak line
  holds a second, then falls. Smoothed for the eye; the numbers are the
  Core's.
- **Settings in boxes.** One box per group, in columns (three on a wide
  screen); each setting shows its unit, a green dot when set here, and its
  help under the field. The save bar counts unsaved changes and stays in
  view until they are saved or discarded.
- Sliding titles move about 40 % faster (35 px a second).
- The queue's empty message no longer breaks the Music link onto its own
  lines.

Redeploy the server: `git pull && sudo systemctl restart qsysradio` (reload
open panel pages). The Core only changed its version number: paste
radio.lua when convenient (until then the server logs a version mismatch).

## [1.3.1] - 2026-10-14

### Changed
- **Deck meters are vertical.** Each deck card on the dashboard has a meter
  column on its left: the rms now at the top, a vertical meter after the
  deck's gain (bar = rms, white line = peak, cyan tick = the Average level;
  the scale is widest around 0 dB, where songs sit), and the player gain at
  the bottom with its level-match part in cyan. It replaces the horizontal
  bar under the title. Deck cards are a little taller.

Redeploy the server: `git pull && sudo systemctl restart qsysradio` (reload
the dashboard). The Core only changed its version number: paste radio.lua
when convenient (until then the server logs a version mismatch).

## [1.3.0] - 2026-10-14

### Added
- **Level matching.** Every song, bumper and ad plays at the same average
  level. The Core measures each file's average (the player meter's `rms.N`
  at unity gain, while there is sound, never during a fade) on every play and
  in file analysis; the server keeps the average of all measurements,
  weighted by the seconds each heard, and the highest peak. At each play the
  deck's gain is the kind's level plus the trim to **Average level**
  (Settings → Levels; default 0 dB on the player meter), at most
  **Raise quiet files, at most** (6 dB) up and **Lower loud files, at most**
  (20 dB) down, and never raised past the **Peak ceiling** (+18 dB). A file
  not measured yet plays as it is; both limits at 0 turn it off.
- **Fades follow the trim.** Fade-ins rise to the trimmed gain and every
  fade-out (crossfade, skip, break, silence, stop) starts where the deck
  is, never at 0 dB. The new MockCore scenario `scenario_levels.lua` checks
  every kind of transition.
- **Deck readout on the dashboard.** Each deck card shows its player gain
  (live, once a second, so fades show) with the level-match trim, and its
  meter after that gain: rms bar, peak line, the Average level marked. The
  Core sends the readout with every sync (`meters`).
- **Level on the library pages.** A Level column (hover: peak and how long
  was measured) and Loudest / Quietest sorts.

### Changed
- **File analysis measures 20 s from the middle** of each file for its
  average level, between the quiet start and the quiet end (about 37 s per
  song instead of 17), and starts only while the song on air has more than
  50 s left. Migration 0013 starts a re-analysis of every file once, as
  Re-analyze all does, so each gets an analyzed level.
- The learned peak keeps the highest seen (it was the last play's).
- **Forget measured** forgets the average level and peak too; a replaced
  file (new size) is measured afresh.
- Music / Bumper / Advertisement level are added on top of level matching.

Redeploy both: paste radio.lua on the Core first (re-enter `SERVER_URL`,
`SERVER_TOKEN` and `STATION_ID`), then `git pull && sudo systemctl restart
qsysradio` (migration 0013 starts the re-analysis; a Core still on 1.2.1
would analyze without measuring levels).

## [1.2.1] - 2026-10-14

### Changed
- radio.lua's `CFG` defaults follow the station's design: players
  `Audio_Player23` / `Audio_Player24`, meter `PlayerMeter`, mixer `RadioOut`
  (inputs 1 and 2, whose `input.N.mute.1` take each player off air while it
  analyzes). README, the MockCore and the help texts use the same names.

Redeploy the Core: paste radio.lua and re-enter `SERVER_URL`,
`SERVER_TOKEN` and `STATION_ID` (the component names are already right). The
server only changed help text and its version: `git pull && sudo systemctl
restart qsysradio` when convenient.

## [1.2.0] - 2026-10-14

### Added
- **Analyze one file from its row.** Every row on the Music, Imaging & IDs
  and Advertisements pages has an Analyze button (the bars icon). It
  analyzes just that file, first in line, without starting a run; the file
  keeps its current figures until the new ones arrive. Rows show ✓
  (analyzed; hover for when), "queued" or "failed" (hover for why), and a
  **Not analyzed** filter lists the files still to do.

### Changed
- **A failed analysis is tried again.** It used to count as analyzed, so a
  one-off load error was never retried. Now it is skipped for the rest of
  that run and tried again by the next (the daily run, Analyze now or
  Re-analyze all); the library line says how many failed. Migration 0012
  also un-marks files that 1.1.0 marked analyzed after a failure.
- An analyzed file is still never analyzed again by itself: only when asked
  (its row, Re-analyze all, Forget measured) or when it is replaced.

### Fixed
- **Long Up next titles did not scroll** on computers that ask for reduced
  motion (Remote Desktop, Windows with animation effects off): the panel
  turned the slide off and cut the title. The slow slide now stays on there.

Redeploy the server (`git pull && sudo systemctl restart qsysradio`, runs
migration 0012). radio.lua only changed its version number: paste it when
convenient (until then the journal notes the version difference).

## [1.1.0] - 2026-10-13

### Added
- **File analysis on the off-air deck.** The Core measures each file's quiet
  start and end itself instead of learning them from plays: on the deck that
  is not on air, with that player's PlayerSum input muted (the meter is
  before the mixer, so it still reads full level). It plays the first 6 s,
  then the last 15 s, about 17 s a song. While the station plays it uses the
  idle deck between songs, and the station takes the deck back the moment it
  needs it (crossfade, skip, break, Play now). While stopped it uses both
  decks. Analyzed figures are final: plays no longer learn over them, which
  ends compounding from fades. A replaced file (new size) is analyzed again.
- **Daily run:** Settings → Silence detection → "Analyze new files daily at"
  (default 04:00 server time, or off) analyzes the files not analyzed yet.
- **Library pages:** progress ("Analyzing on the off-air deck: 37 files to
  go"), **Analyze now**, **Re-analyze all**, **Stop**; **Analyze again** in a
  file's quiet start / end editor, which also says whether its figures were
  analyzed or measured from plays. The dashboard shows the analyzing deck as
  "analyzing · off air".
- radio.lua `CFG`: `MIXER = "PlayerSum", MIX_A = 1, MIX_B = 2` (`""` = no
  analysis). Without that mixer the station plays as before and the Debug
  Output says analysis is off. Migration 0011.

Redeploy both: the Core (paste radio.lua and re-enter your `CFG` values,
plus `MIXER` if your mixer has another name; set PlayerSum's Script Access
to Script) and the server (`git pull && sudo systemctl restart qsysradio`,
runs migration 0011).

## [1.0.2] - 2026-10-13

### Fixed
- **With Fade in set above 0, songs lost more of their start on every
  play.** The Core doesn't read the meter while a fade-in moves the gain, so
  the first sound it saw came after the fade. It learned that as the song's
  quiet start, skipped it next time, and measured again after another
  fade-in, until the 3 s skip limit (a song starting at 0.5 s was learned at
  5 s). Plays the Core fades in no longer report a quiet start. Migration
  0010 forgets the measured quiet starts on stations that had Fade in above
  0; figures set by an operator stay. With Fade in at 0 (the default)
  nothing was affected. A file's own soft fade-in was never the problem: it
  counts as sound from −50 dBFS up.

Redeploy both: the Core (paste radio.lua) and the server (`git pull && sudo
systemctl restart qsysradio`, runs migration 0010).

## [1.0.1] - 2026-10-13

First stable version: the same code as 0.3.0, declared solid. From here on
every change ships as its own version (patch = fix, minor = feature).
Redeploy: the Core (paste radio.lua, so it reports 1.0.1) and the server
(`git pull && sudo systemctl restart qsysradio`).

## [0.3.0] - 2026-10-13

### Added
- **See and adjust each file's quiet start and end.** On the Music, Imaging &
  IDs and Advertisements pages, click the length without silence: the
  measured figures, your own (the Core never measures over them), "Use
  measured" and "Forget measured" (measured again on the next play). A
  "Forget measured" button does a whole page; an "Adjusted" filter lists the
  files you set. The server queues a rescan so the Core applies the change.
  Migration 0009.
- **Pause ads and bumpers**, for 30 min, 1 hour, until a time ("no more
  ads till 05:00") or until resumed: a pause button on the Next bumper /
  Next ad break cards, PAUSED and until when shown on the card. Server side
  (`station_holds`, `POST /api/v1/hold`): held ads leave the plan, held
  bumpers set `no_bumpers` (one check in radio.lua). Operator Bumper now /
  Ad break now still play.
- **Station IDs**: mark imaging files as IDs (Imaging & IDs page,
  `library.ident`, `POST /api/v1/library/ident`) and set **Station ID
  schedule** (off / hourly / half-hourly / quarter-hourly) and **Station ID
  may be late by**. The server names a random ID when one is owed (`ident`
  in the plan); the Core plays it first in that break instead of the regular
  bumper. Song picks and Coming up account for ID times; the bumper card
  shows the next ID time.
- **Library split into Music, Imaging & IDs and Ads tabs** with quick
  filters (`?only=never|unsupported|ident|hourly|mix|off`), search on `/`,
  view kept in the URL, and on Ads a "This hour" line of every per-hour slot
  in time order. `/library` now opens Music. Migration `0007_holds_and_idents`.
- **Fair rotation first, ad timing second.** The server keeps the fair
  pick (random among the least recently played quarter) unless it would
  start a per-hour ad more than **Ad timing tolerance** late (new setting,
  Rotation, default 3 min, server-only); only then is a song picked by
  length, from the same quarter, and from the oldest half only when that
  alone keeps the ad inside its window. Before, nearly every song near a
  slot was picked by length from the oldest half, so a song whose length
  never fitted could keep waiting. A pick that leaves the slot for a third
  song to reach no longer counts as late. Library totals show rotation
  health (never played, longest wait).
- **Per-hour ads never share a break.** All hourly slots are placed
  together: each ad's slots stay evenly spread and two ads' slots are at
  least 4 min apart (they could coincide when counts differed, e.g. 3/hour
  and 1/hour both at :52). Coming up now picks every song it lists the way
  the server will (by length for an ad slot within two songs), not only the
  first, so its ad times match what will air.
- **Up next knows about breaks, and Coming up.** The server forecasts the
  next 10 items (songs, ads, bumpers) with estimated start times from the
  Core's state, the ad schedule and the library, by the Core's rules (queue,
  server pick, per-hour slots and windows, ad / bumper counters, quiet-start
  skip, crossfade). Up next shows a break when an ad slot opens or a bumper
  is due before the current song ends (the ad card then says NEXT); the
  dashboard lists the forecast under the queue. `GET /api/v1/now?ahead=N`.
- **All panel times are server time, 24-hour**: recent plays, activity, the
  play log (its day is the server's day; "what was on air at" is read as
  server time) and the library's last-played tooltip.
- **Header clock is the server's time**, 24-hour and larger (it was the
  browser's local time): every status poll carries the server time and zone,
  the browser ticks between polls. Optional **NTP check** (Settings → Server
  settings, or `NTP_SERVER`): the clock turns green / red against an
  allowed error, with the measured offset in its tooltip; one warning is
  logged when it goes wrong.
- **Quiet starts skipped.** Songs start just before their first sound,
  measured on earlier plays: settings "Skip quiet start, at most" (3 s, 0 =
  off) and "Keep before the music" (0.3 s). The server sends each song's
  lead-in with the library reply; the Core seeks the player (`locate`)
  right after play. Lead-in is still measured from the file start; the ad
  timing allows for the skip. `track_start` carries `skip`.
- **Every song gets played; breaks land on time.** The server names the
  next song (`next` in the ad plan): a random one of the least recently
  played quarter of the music (never-played first, artist separation kept),
  or, when a per-hour ad slot is within two songs, the one whose length makes
  the song change fall just after the slot opens (Up next: FOR AD). The Core
  plays it after the operator queue and uses its own shuffle only without
  one. `library.last_played` (`0006_library_last_played`). An end-to-end test
  runs radio.lua against the server with a rescan every 75 s: all 24 songs
  play (before: one never did in 64 plays).
- **Library: length without silence and totals.** Music shows each song's
  length without the quiet start and end measured on earlier plays, and the
  list ends with the total length (and without silence) of everything that
  matches, plus how many have no known length yet.
- **Ad slots you can see, and no bunching.** Per-hour ads are staggered:
  ads with the same count fall due one after another (four at 4 an hour:
  every 3¾ min) instead of all at :07 / :22 / :37 / :52. Each slot has a
  window (10 min, or half the gap to the ad's next slot); a slot that cannot
  play in it (station stopped, long song) is skipped, not made up at the end
  of the hour. Library → Ads lists each ad's times this hour (played, owed,
  missed, to come); the dashboard's "Next ad break" card shows the next
  hourly ad and how long until it, instead of OFF.
- **Syslog on the Settings page**: a "Server settings" section (admins) for
  the syslog server's IP / host name and port, protocol, format, facility,
  lowest level and app name, with **Send test message**. Saved in
  `app_settings` (`syslog.*`); `.env`'s `SYSLOG_*` are now only the
  defaults. Each server worker re-reads them every 10 s and rebuilds the
  sender only when they changed, so no restart is needed.
  `GET/POST /api/v1/server-settings`, `POST /api/v1/server-settings/test`.
- **App server** set up like the other apps on the shared PostgreSQL server:
  own `qsysradio` schema (`PG_APP_SCHEMA`) with the cross-app `shared` schema
  second on the search path; config in `.env`; `install.sh` (venv,
  dependencies, `SECRET_KEY`, `RADIO_API_TOKEN`, schema + migrations, systemd
  unit running as the sudo user from the home-folder clone); `start.sh`
  (dependency sync when `requirements.txt` changes, migrations, gunicorn 4 × 8)
  so updates are `git pull && sudo systemctl restart qsysradio`. Numbered SQL
  migrations, one transaction each, advisory-locked.
- **Play log**: every event once in `core_events`, one row per airing in
  `play_log` (start/end, played, end reason); per-station queries; API
  (`/api/v1/now`, `/api/v1/plays`, `/api/v1/plays/at`); CSV download.
- **Web control panel** in the WebRetriever2 style (bundled fonts, no CDN):
  dashboard (on-air countdown, decks, up next, queue with reorder/remove,
  bumper countdown, activity with every command's result, recent plays),
  transport (Skip, Bumper now, Stop after current, Stop now / Start), library
  (search, sort, Play next, Queue, Play now, Bumper now, Rescan), play log,
  settings. Phone layout.
- **Shared sign-in** with 321Theater and the sister apps (SHARED_AUTH.md):
  sessions in `shared.app_sessions`, users read from `shared.users`; watch /
  operate / admin from `is_app_user`, `is_readonly`, `is_app_admin`, role.
- **Station settings on the server** (`server/core_settings.py`): every
  setting labelled, range-checked and documented, edited on the web page and
  pushed to the Core within a second; new **Bumpers & ads** settings
  (bumper/ad level, song fade under a bumper) and **Stop fade**.
- **Remote syslog** (`SYSLOG_*`): UDP or TCP, RFC 3164 or 5424, full header,
  sent from a background thread.
- `radio.lua`: one `/api/v1/core/sync` request a second carries queued
  events (boot id + seq, outbox persisted on the Core), state only when it
  changed (or every 10 s), and brings back commands and settings; library
  upload after every scan; operator queue, stop after current, play now,
  chosen bumper, queue move/remove by song.
- Tests: MockCore scenarios `scenario_server`, `scenario_control`,
  `scenario_regressions` (one case per review finding); `qsys/test/live_core.py`
  (radio.lua in real time against a real server); `tests/test_server.py`
  (pytest against real PostgreSQL: Core API, commands, sign-in, permissions,
  settings, play log, failure handling).
- **Ads**, a third kind of file next to music and imaging: own folder
  (`ads_dir`), level (`ad_gain_db`), `ad_every_n_tracks` and `ads_per_break`
  settings. Each ad is scheduled on the Library page: **Mix in** (rotated
  through ad breaks), **Per hour** with a count (spread over each clock hour,
  plays counted from the play log), or **Off**. The server works out which
  ads are owed and sends the Core an ad plan on its sync (`ads` / `ads_rev`);
  an owed ad starts a break at the next song change. A break plays ads back
  to back, then a bumper if one is due. `ad_start` / `ad_end` events, `ad`
  rows in the play log (Play log filter, CSV), "Ad break now" on the
  dashboard and Play now for an ad in the library (`ad_now` command), a "Next
  ad break" card, `POST /api/v1/ads`. Migration
  `0005_ads_and_learned_on_server` (`ad_rules` table).
- MockCore `scenario_ads.lua`; `live_core.py` has ads.
- README **Installation**: one numbered path from `git clone` to a linked
  Core: packages, PostgreSQL role and least-privilege grants (joining
  321Theater's database, or a new one), `.env`, `install.sh`, service and
  health checks, firewall, access in 321Theater, shared sign-in notes, the
  Q-SYS Designer side, backup, update, rollback, troubleshooting by error
  message, uninstall. `.env.example` documents `SESSION_COOKIE_SECURE` /
  `SESSION_COOKIE_DOMAIN` and what the shared schema needs.

### Changed
- **Fade-outs start the next item early.** When the end of a song, bumper or
  ad fades away (a ringing note, a dying beat) and stays 20 dB under the
  file's own peak for half a second, the next item starts up to 2 s before
  the regular crossfade point. Settings → Silence detection: "Start early
  over a fade-out, at most" (0 = off) and "Fade-out level".
- **Bumpers and ads get the silence handling songs have**: their quiet start
  is skipped and the next break item starts at the quiet end, not at the end
  of the file. The library shows "Without silence" for imaging too.
- **"Ads" is spelled out as "Advertisements"** wherever the panel has room:
  the tab and page title, the Settings group and labels, the pause menu, the
  play log filter. Short tags ("AD", "Next ad break") stay short.
- Dashboard: long titles in Up next slide back and forth instead of being
  cut off (still when the system asks for reduced motion); the Next ad
  break card shows the full time (17:32, not :32); the on-air row in Recent
  plays has one even red tint with a red edge (a fade per cell made stripes).
- radio.lua is ~1100 lines (was ~1220): the optional Q-SYS panel controls
  are gone (inputs `Skip`, `BumperNow`, `Automation`; outputs `Status`,
  `NowPlaying`, `NextUp`, `DeckA`, `DeckB`, `LastEvent`): the station is
  operated from the web panel only. Debug Output lines are short (event kind
  and file); the server keeps every field. Remove the Text Controller's
  extra controls from the design if you added them.
- radio.lua trimmed without changing behaviour: one `readJson`/`writeJson`
  pair, no `Store` class (the 30 s Chores timer already paced the outbox
  file), no unused `LOG` ring, one `Station.nextSong(take)` for peek and
  pick, one `timer()` helper for all four timers.
- **The Core keeps almost nothing on its media drive.** `learned.json` is
  gone: the server's `library` table keeps duration, lead-in, shortest
  trailing silence and peak per file (from each play's `track_end`) and
  "unsupported" (from `file_unsupported`, cleared when the file's size
  changes), and returns the trail and unsupported files to the Core in the
  reply to each library upload. `state.json` is gone: a Core that restarts
  asks for commands from 0 and the server fails any command it had picked up
  but not reported ("the Core restarted before reporting"). `outbox.json` is
  written only while the server is unreachable (and once more to clear it).
  Only `settings.json` and `outbox.json` remain. `media/_radio/learned.json`
  and `state.json` on an existing Core can be deleted.
- Queue and up-next lengths on the dashboard come from the server's library.
- `bumper_every_n_tracks` = N now means N songs between bumpers (it played
  N + 1). The Bumpers card is now "Next bumper": songs still to play after
  the current one, or NEXT, and the pool size says "bumpers", so "0 songs …
  5 in pool" no longer reads as an empty library.
- Settings: "Bumper / ad level" is now "Bumper level" (ads have their own);
  the imaging folder no longer holds ads.
- Install is now: fill in `.env`, run `sudo ./install.sh`. The database,
  the `qsysradio` role and schema are assumed to exist; the setup SQL moved to
  a "Database access" reference section and the steps are renumbered (1–13).
  The installer asks only for `PG_PASSWORD` (and host/port if remote).
- Install guide assumes 321Theater is already installed: the separate
  "new PostgreSQL / no sign-in" path is gone; `.env.example` now defaults to
  `PG_DBNAME=321theater` and `PG_USER=qsysradio`.
- From the fresh-install walkthroughs: HTTP requests are no longer written to
  the journal (the Core alone made one a second); `ACCESS_LOG=1` turns them
  back on. The systemd unit starts after a local `postgresql.service`.
  `migrate` warns when the database has migrations the code does not know
  (after a code rollback). The sign-in page says "Sign-in is not available"
  up front when 321Theater's tables are missing or not granted, instead of
  accepting a password and bouncing back.
- QSYSRadio never creates or alters anything in the `shared` schema
  (321Theater owns it). It only SELECTs `users` and reads/writes
  `app_sessions` rows. At startup `migrate` checks those tables and grants and
  logs the exact `GRANT` that is missing (`sign-in will not work: …`); sign-in
  then answers 503 "Sign-in unavailable" instead of failing.
- `radio.lua` made light: 250 ms station timer plus a 50 ms timer only while a
  deck fades or loads (crossfade armed with `Timer.CallAfter`); control objects
  looked up once; panel outputs once a second and only on change; learned.json
  saved at most every 5 min and on stop, the outbox at most every 30 s;
  RapidJSON preferred (Q-SYS guidance for large data). About 400 fast-timer
  runs per 5 min of play instead of 6000; garbage ~6 KB/s instead of ~34 KB/s.
- `radio.lua` header and settings slimmed: settings are fallbacks only (the
  server owns them), internals are constants, docs moved to the README.
- Meter reads only during clean playback (no fade), so learned lead-in, tail
  and peak are not skewed by moving gain. The dashboard no longer shows a
  live level (it cost a state report every second).
- Server: one pooled connection per request (shared by the session layer;
  up to `WEB_THREADS + 2` open and `PG_POOL_MAX_IDLE`, default 4, kept per worker);
  log lines written after commit; Core endpoints require `RADIO_API_TOKEN`
  (`RADIO_ALLOW_NO_TOKEN=1` for a lab).

### Fixed
- **Songs crossfaded about 4 s early from their second play.** The Core
  counted the crossfade itself as the song's silent tail (it measured after
  the fade had begun), so every learned tail was about the crossfade length.
  Tails now count only when the silence was heard before any fade; migration
  0008 forgets the wrong ones (real silent tails are learned again on the
  next play).
- radio.lua: a rescan restarted the shuffle bag, so with regular rescans
  some songs could go unplayed for a long time (the server now picks songs,
  and the bag keeps its cycle across rescans).
- migrate: a missing or unwritable `qsysradio` schema stops with the one SQL
  line to run (`CREATE SCHEMA … AUTHORIZATION …` / `ALTER SCHEMA … OWNER TO …`)
  instead of a psycopg2 traceback.
- radio.lua: a fade left on a deck could dip or stop the next track loaded on
  it (dead air) and record a bogus trail; `track_end` was lost when a track
  ended during a fade; skip / bumper requests could stick and cut a later
  track (also across a stop and start); the panel Automation toggle was out of
  sync at boot; a corrupt learned.json was overwritten (now kept as `.bad`);
  an empty music folder logged ~52,000 events a day (now once, with back-off,
  and music plays as soon as it is back); `uptime` was the Core clock's epoch;
  transient load failures blacklisted files forever; a bumper whose tail the
  next song started under never logged its end.
- Server: a session made by a sister app skipped QSYSRadio's access check (and
  a failed re-check signed the user out of every app); Core-supplied text
  could be stored and shown unescaped; two commands could commit out of order
  and one be skipped; a session cookie turned a database outage into 500s;
  one malformed event blocked the Core's queue forever; malformed Core state
  broke pages for everyone; a dead TCP syslog server could stall workers;
  large library uploads were slow (now bulk); library paging repeated/skipped
  rows; root-folder files showed no plays; "on air at" missed tracks with no
  known length; plays from other stations mixed in; session `last_seen` was
  local time; commands with no result stayed "sent" forever; several 500s
  that should be 400s; settings accepted `true` and non-string folders.
- Event identity is per station (`0004_events_unique_per_station`).
- Library page: it did not update after Rescan (or as things played) until
  reloaded; it now reloads the rows shown when the Core uploads a scan and
  when something new plays (never while an ad's schedule is being edited).
- radio.lua: Play now on a bumper while the station was stopped started the
  music but dropped the bumper.
- MockCore keeps the scenario seed and follows simulated time in `os.date`.

## [0.2.0] - 2026-10-12

### Added
- `qsys/radio/radio.lua` — standalone two-deck station: dir.get library
  scan, shuffle bag with artist separation, event-verified loader, linear-dB
  fades on a 50 ms tick, crossfade at `crossfade_s + learned trail`, silence
  detection at unity gain, bumper on the free deck every N tracks with the
  next track starting under its tail, operator Skip / BumperNow / Automation,
  status outputs, dead-air guard, EOF fallback, per-file learning persisted to
  `media/_radio/learned.json`, `EVENTS`/`LOG` rings for the 0.3 server.
- `qsys/test/mock_core.lua` — MockCore, an offline Q-SYS runtime stand-in
  (simulated clock, async loads, 1 Hz progress, post-gain meter, status-only-
  on-change) with a minimal JSON module; `run_mock.py` runner and
  `scenario_basic.lua` with nine assertions on the event log.
- README: deck controller design, MockCore testing, facts from probe 0.1.1
  (load latency, status/remaining semantics, meter-vs-gain, play/stop timing).

## [0.1.1] - 2026-10-12

### Added
- Probe: recursive `dir.get()` walk of `media/Audio` with file sizes and
  per-directory audio counts (full tree in the dump file).
- Probe: directory step — sets `PROBE_DIRECTORY` on the probe player if it
  has no folder selected and waits for it to settle.
- Probe: silent load sweep — sets `filename` to every file in the folder,
  records the player's `status`, duration (`remaining` at progress 0) and
  load latency, so we learn which formats the player accepts.
- README: "What the Core told us" table of facts from the first real run.

### Changed
- Probe: control metadata now read from `MinValue`/`MaxValue`/`MinString`/
  `MaxString`/`Choices` (the fields Q-SYS actually returns).
- Probe: file source is `dir.get()`; `all.files` is a Virtual control that is
  empty from script, so it is no longer relied on.
- Probe: playback plays the first file that loaded OK in the sweep.
- Probe: UTC timestamps; phase timing is relative to each phase start.
- Probe: restores `directory` as well as gain / loop / filename.

## [0.1.0] - 2026-10-12

### Added
- Project README: architecture, locked design decisions, repository layout,
  versioning policy, roadmap, and initial setting defaults.
- `qsys/probe/probe.lua` 0.1.0 — discovery script. Dumps design/platform
  info, network interfaces, every component in the design, every control on
  the two Audio Players and the level meter (type, direction, range, value),
  raw media strings with delimiters made visible, a parsed `all.files` list
  with extension census, `dir.get()` results for every plausible media path,
  and runs an instrumented play → fade → stop cycle with timing summary.
  Writes a full dump (plus JSON) to `media/_probe/` on the Core.
- Version tracking: `VERSION` file and this changelog.
