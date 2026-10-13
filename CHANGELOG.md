# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
- README **Installation**: one numbered path from `git clone` to a linked
  Core: packages, PostgreSQL role and least-privilege grants (joining
  321Theater's database, or a new one), `.env`, `install.sh`, service and
  health checks, firewall, access in 321Theater, shared sign-in notes, the
  Q-SYS Designer side, backup, update, rollback, troubleshooting by error
  message, uninstall. `.env.example` documents `SESSION_COOKIE_SECURE` /
  `SESSION_COOKIE_DOMAIN` and what the shared schema needs.

### Changed
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
