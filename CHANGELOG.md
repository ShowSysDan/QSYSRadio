# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- App server scaffold, set up like the other apps on the shared PostgreSQL
  server: own `qsysradio` schema (`PG_APP_SCHEMA`) with the cross-app
  `shared` schema second on the search path; config in `.env`
  (`.env.example`); `install.sh` (venv, dependencies, `SECRET_KEY`, schema +
  migrations, systemd unit running as the sudo user from the home-folder
  clone); `start.sh` (deps sync when `requirements.txt` changes, migrations,
  gunicorn) so updates are `git pull && sudo systemctl restart qsysradio`.
- Numbered SQL migrations (`server/migrations/`, tracked in
  `schema_migrations`, advisory-locked, one transaction per file);
  `0001_app_settings`.
- `GET /api/v1/health`: version, schema, applied vs expected migration.
- Play log. `radio.lua` gives every event a `boot` id + `seq`, keeps unsent
  events in an outbox persisted to `media/_radio/outbox.json` (cap 5000),
  and POSTs them to `/api/v1/events` every 2 s when `CFG.SERVER_URL` is set
  (`CFG.SERVER_TOKEN`, `CFG.STATION_ID`); emits a `boot` event; start/end
  events now carry `dir`, `artist`, `title`, `played`. Server migration
  `0002_core_events_play_log`: `core_events` (every event once, keyed by
  boot + seq) and `play_log` (one row per airing with start/end times). A
  new boot closes plays left open as `core_restart`.
- Play log API: `GET /api/v1/now`, `/api/v1/plays` (range, search, kind),
  `/api/v1/plays/at` (what was on air at a moment). `TIMEZONE` in `.env`.
- `RADIO_API_TOKEN` (`X-Radio-Token`) to restrict who may post events.
- Remote syslog (`SYSLOG_*` in `.env`): UDP or TCP, RFC 3164 or 5424, full
  header with app name `QSYSRadio`; plays, ends, warnings and errors.
- `scenario_server.lua`: server outage, outbox persistence and replay checks;
  MockCore gains a fake `HttpClient` and a simulated wall clock.

### Changed
- gunicorn defaults to 4 workers x 8 threads (`WEB_WORKERS` / `WEB_THREADS`)
  so many viewers and the Core are served at once.
- `VERSION` and `radio.lua` are `0.3.0-dev`.

### Fixed
- A bumper that the next track started under never logged `bumper_end`.
- MockCore now keeps the scenario seed (radio.lua reseeded from the clock,
  so runs differed); `scenario_basic` uses seed 1 and checks it actually
  covers the silent-tail track.

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
