# QSYSRadio: notes for Claude

Radio automation, two decks per station (or one: a background-music zone,
songs only): ONE radio.lua on the Q-SYS Core
(`qsys/`) runs every station in `CFG.STATIONS` and does real time;
a Flask + PostgreSQL server (`server/`) does everything else (play log, web
control panel, settings, library, sign-in, syslog). README.md has the design.

## Every change
- Every change (each commit the user gets) is its own version: bump `VERSION`
  and `RADIO_VERSION` in radio.lua together (semver: patch = fix, minor =
  feature, major = breaking) and add a new top section
  `## [X.Y.Z] - YYYY-MM-DD` to CHANGELOG.md. No `[Unreleased]` section.
  `test_version_matches_everywhere` fails if they disagree.
- Document it in the same commit: README.md (design, settings, panel,
  deploy steps) wherever the change shows, plus `.env.example` for new keys.
- Tell the user what to redeploy: the Core (paste radio.lua), the server
  (`git pull && sudo systemctl restart qsysradio`), both, or neither.
- No duplicated code: reuse the helpers (Lua: `push`, `shuffled`, `set`/`trigger`,
  `readJson`/`writeJson`, `timer`, `post`, `emit(st, kind, data)`, `guard`,
  `Station:figures(key)`;
  Python: `db.text`, `db.number`, `db.clean_json`, `db.like`, `db.iso`,
  `db.lock_station`, `dbconn()`, `write_logs`, `core_settings.effective`, `media.file_of` /
  `media.supported` (per-file figures live in `media_files`; read them
  through the view `library_figures`), `ads.stale` (call it in the
  transaction of any change a station's ads plan is made from, which took
  `db.lock_station` first), `analysis.stale` (the same for a Core's list
  of files to analyze), `cores.seen`; JS:
  `Radio.api/post/command/setHTML/esc/num/status/meter/pick`).

## radio.lua: keep it slim and light
- Real time only. Anything that can live on the server does.
- Station settings are defined ONLY in `server/core_settings.py` (label,
  range, default, help). radio.lua's `SETTINGS` holds the same names with
  fallback values and nothing else; `tests/test_server.py` fails if the two
  drift. Internals (timer rates, timeouts, paths) are constants in `K` /
  `FILES`, not settings.
- No header essays; one-line comments only where the line needs one.
- Overhead budget, for every station together: one 250 ms timer (`Tick`
  loops over the prebuilt `Radio.stations`, then `Analyzer:tick` over the
  analysis players); the 50 ms `Fast` timer only while a deck fades or
  loads (`Engine.wake()`, loops over `Radio.decks`: every station's decks
  and the analysis players);
  one sync request per second for all stations (plus one library part at a
  time after a scan); control objects cached in `Deck.new`; panel writes
  only on change; files written only from `Chores` (every 30 s) or once per
  sync reply that changed any station's settings. Never add per-tick
  allocation (no closures, tables or string building in `Tick` / `Fast`),
  polling of the server, or whole-file writes on every event.
- Stations are objects (`Station.new(cfg)`): per-station state lives on the
  station, never in a global. Each station's tick, deck handlers, commands
  and reply part run under `guard` (pcall); a station failing 5 ticks in a
  row or failing to build is faulted (decks stopped, `station_fault`,
  rebuilt from its CFG entry after a back-off); the others never notice.
- A station has one or two decks (`st.single`: one player, songs only, no
  breaks). One code path with a one-deck case (`otherDeck` is the deck
  itself, the next song loads once the fade-out ends); never a copy of the
  transition logic, and loops over `st.decks`, never `decks[2]` blindly.
- Every list has a cap (`K.outbox_max`, `events_max`, `history_max`,
  `queue_max`, `analyze_max`); nothing may grow with uptime. The Core
  reports `mem_kb` every sync; `scenario_multi.lua` checks memory is flat.
- Use the documented Q-SYS APIs (HttpClient.Upload, Timer, Component, dir,
  io on media/, rapidjson). Keep `Tick`, `Fast`, `Sync`, `Chores` timers
  global (a local timer can be garbage-collected).
- After any change run every MockCore scenario (`for s in
  qsys/test/scenario_*.lua; do python3 qsys/test/run_mock.py $s; done`); add a
  case to `scenario_regressions.lua` for every bug fixed.
- The Core's only files are `settings.json` (`{stations: {id: {rev,
  settings}}}`) and `outbox.json` (every station's events), both for when
  the server is unreachable (the outbox also while a station's part is
  answered `{error}`). Per-file data (learned figures, unsupported)
  and command state live on the server; don't add Core files.

## PostgreSQL: its own schema, PostgreSQL only
- Every QSYSRadio table lives in `PG_APP_SCHEMA` (default `qsysradio`).
  `PG_SHARED_SCHEMA` (default `shared`) is 321Theater's and second on the
  search_path: SELECT `users`, SELECT/INSERT/UPDATE/DELETE `app_sessions` rows,
  and nothing else. Never create, alter or own anything there (no DDL, not even
  "if missing"); `migrate.shared_problems()` only reports what is missing.
- Web requests use ONE pooled connection each: `dbconn()` in app.py (also
  used by the session layer); it goes back to the pool, rolled back, at
  teardown. Never borrow a second connection in a request. CLI/migrations use
  `db.connect()`. `DatabaseUnavailable` means 503; never carry on without data.
- Log after commit (`write_logs(lines)`), never inside a transaction.
- Placeholders `%s`; `RETURNING id`; no `REAL`; timestamps `TIMESTAMPTZ`
  (except `shared.app_sessions`, naive UTC via `auth.utcnow()`).
- Anything from the Core is untrusted: clean it (`db.text`, `db.number`,
  `db.clean_json`, `control._clean_state`) and escape it in the browser.

## Migrations (server/migrations/, applied by start.sh on every start)
- A NEW file `NNNN_short_name.sql` per change. Never edit or renumber one that
  has been pushed; one transaction each; no BEGIN/COMMIT inside.
- Test against a real PostgreSQL: `QSYSRADIO_ENV_FILE=... python -m pytest tests/`
  (own throw-away schemas). PG 16 is installed in the cloud sandbox.

## Core <-> server protocol
- `POST /api/v1/core/sync` (≈1/s, X-Radio-Token), ONE request for all the
  Core's stations: `{version, boot, core, mem_kb, analyze_rev?, analyzers?,
  stations: [{station, ack, settings_rev, ads_rev, meters, state?, events?,
  dropped?}]}`; reply `{stations: {id: {commands, settings, settings_rev,
  ads, ads_rev, ack_reset, accepted, duplicates, rejected} | {error}},
  analyze?, analyze_rev?}`. Events (station, boot, seq unique; stored once, each in a
  savepoint; at most `K.sync_batch` per request, shared round-robin);
  optional state (with `fault` when faulted); command ack; `meters` (each
  deck's gain, peak, rms; kept on `stations` for the panel). Server: one
  pooled connection, each station's part its own transaction (its advisory
  lock for its own work only, committed before the next, logs after each
  commit); a failing part is rolled back alone and answered `{error}` (the
  Core keeps that station's events); `DatabaseUnavailable` / connection
  loss = 503 for all. The 1.x body (no `stations`) is still accepted and
  answered as before (one version of overlap). `cores.seen` keeps each
  Core's boot / memory (lowest sample per 10-minute window) and logs the
  leak warning. One advisory lock per station (`db.lock_station`)
  serialises the sync and EVERY web write to that station's rows (commands,
  holds, IDs, ad rules, settings, analysis, figures): take it first, before
  touching any row (All stations: every station's, in name order; a Core's
  analysis run: every station of that Core, in name order, then its `cores`
  row). Lua sends `{}` for an empty table: accept `{}` where a list is expected.
- `POST /api/v1/core/library` after every scan, in parts of at most
  `K.library_chunk` (1000) items: `{station, boot, scan, part, parts,
  music_dir, imaging_dir, ads_dir, items}`, one upload in flight per Core.
  Each part is upserted (bulk `execute_values`) and marked with the scan id
  (`library.scan`, `stations.scan` / `scan_parts`); only the last part with
  every part in marks the rest gone (an upload never shrinks the library);
  a missing part = 409 and the Core sends the whole scan again. Reply:
  `{ok, scan, part, parts}`, the last `{ok, items, learned}`, `learned`
  {key: {trail, lead, level, peak} | {unsupported}} for the Core. A 1.x
  upload (no scan) is one complete scan.
- Level matching: `*_end` and `analysis` events carry `level` / `level_s` (rms
  average at unity, seconds heard) and `peak`; the server keeps their
  seconds-weighted energy average (`events.LEVEL_SET`) and the highest peak.
  The Core plays each item at its kind's gain + `levelTrim`; fades start from
  the deck's current gain, never from 0 dB.
- File analysis (`analysis.py`) is per Core, on its own analysis players (`CFG.ANALYZERS`: an
  Audio Player + a peak/rms meter channel, routed to no output; `Analyzer` in radio.lua, its decks
  are `Deck` objects of no station). On-air decks never analyze (no mixer, no take-over). The body
  carries top-level `analyze_rev` (only with an analysis player) and `analyzers` [{name, state,
  station?, key?}] (when changed or every 10 s; kept on `cores.analyzers`); the reply carries
  top-level `analyze` [{station, key}] (at most 20, in the order to measure) and `analyze_rev` when
  the rev differs. The list is cached on the `cores` row (`owed`, made again on a result, on
  `analysis.stale()`, or after 60 s), built after the stations' parts in its own transaction.
  Results are `analysis` events in the named station's part. The job (`cores.analyze_from`,
  `analyze_run_at`, `analyze_auto_at`) and the setting `analyze_at` ('right away' (default; a
  failure retried after 24 h) / 'HH:00' / 'off') are the Core's: All stations only. Analyzed
  figures are final (plays don't learn over them); a failure is retried by the next run, never
  the same one; one file (`analyze_wanted`) goes without a run. A pre-2.1 Core (per-station
  `analyze_rev`) gets `analyze: []` once, then nothing; a 1.x station's run stays on its
  `stations.analyze_*` columns until its first 2.x sync (`analysis.adopt`). A Core without an
  analysis player owes nothing (`analysis.run_of` gives `can` False).
- Ads: the server sends `ads` {due, mix, next, next_why, ident, no_bumpers} when `ads_rev` differs
  (`ads.py`); the Core only plays the plan (without one, every ad is a mix-in). The plan
  is cached per station (`ads.cached_plan`: made again when the station's events were
  accepted, when stale (`ads.stale`), at its own `plan_until` or after 30 s).
  `next` is the song to play next, picked on the server (`next_song`: least
  recently played, or by length to land the break on an ad slot); the Core
  takes it after the operator queue, once, and uses its own shuffle only when
  the server has named nothing. A one-player station (`stations.players` = 1,
  from its state) gets `next` only; its break commands, holds, ad rules and
  IDs are refused (`breaks.one_player` / `refuse_one_player`).

## Web panel
- Pages poll through `Radio.status()` (one loop per page, 8 s timeout);
  no websockets, no long-held connections. Escape every server value
  (`esc`, `num`); write lists with `setHTML` (only on change).
- Permissions are QSYSRadio's own verdict (`auth.current_user()`), re-read
  from `shared.users` every 5 min, even for sessions made by sister apps.

## Config
Per-machine settings are `.env` keys (documented in `.env.example`), read via
`server.config.get()`: process env first, then the file.

## Git
Develop on the assigned branch; push with `git push -u origin <branch>`. No
PR unless asked.
