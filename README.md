# Q-SYS Radio Automation

Version **0.3.0-dev** · see [CHANGELOG.md](CHANGELOG.md)

A two-deck radio-station automation system built on a Q-SYS Core. Lua on the
Core does the real-time work (deck control, crossfades, silence detection,
never going dead) and is kept as light as possible. A Python/Flask app with a
PostgreSQL backend does everything else: the play log, the web control panel,
station settings, the library, syslog.

## Status

| Component | State |
|---|---|
| `qsys/probe` — discovery script | **0.1.1** — done |
| `qsys/radio` — deck controller | **0.3.0-dev** — standalone or server-linked; all features pass the MockCore suites; awaiting first run on the Core |
| `qsys/test` — MockCore simulator | **0.3.0-dev** — offline Core stand-in + a live simulated Core for demos |
| `server` — Flask + Postgres | **0.3.0-dev** — own schema, migrate-on-start, play log, control panel, shared sign-in, settings, syslog; pytest suite against real PostgreSQL |

## Architecture

```
 ┌────────────────────────── Q-SYS Core ──────────────────────────┐
 │  radio.lua                                                     │
 │   • both decks: load, play, fades, crossfade timing,           │
 │     silence detection, learning (lead-in / tail / peak)        │
 │   • keeps playing on its own if the server is gone             │
 │   • once a second: POST /api/v1/core/sync                      │
 │       → queued events, state (only when it changed)            │
 │       ← operator commands, settings                            │
 │  Audio_Player_01 ──► PlayerOutput01 meter.1                    │
 │  Audio_Player_02 ──► PlayerOutput01 meter.2                    │
 └──────────────────────────────┬─────────────────────────────────┘
                                │ HTTP (the Core pulls)
 ┌──────────────────────── App server ────────────────────────────┐
 │  Flask (gunicorn 4 × 8)                                        │
 │   • play log, library, station settings, commands              │
 │   • web control panel for many viewers (short polls)           │
 │   • shared sign-in with 321Theater & co., syslog               │
 └──────────────────────────────┬─────────────────────────────────┘
                                │ SQL (pooled)
 ┌──────────────────────── PostgreSQL ────────────────────────────┐
 │  schema qsysradio (this app) · schema shared (users, sessions) │
 └────────────────────────────────────────────────────────────────┘
```

## Design decisions (locked)

- **Lua owns real time, Python owns everything else.** The Core never waits
  on the server to keep audio flowing.
- **Never go dead.** If the server is unreachable, Lua keeps playing (local
  shuffle) and keeps events in an outbox to replay later.
- **Lua stays light.** One 250 ms timer; a 50 ms timer only while a deck is
  fading or loading; one HTTP request a second; controls looked up once;
  files written at most every 30 s / 5 min.
- **Settings live in Python.** Every station setting is defined, documented
  and validated in `server/core_settings.py`, edited on the web page and
  pushed to the Core. `radio.lua` only carries fallback values (a test keeps
  the two in step).
- **Per-deck metering.** Player 1 → `meter.1`, Player 2 → `meter.2`.
- **Two decks only.** A bumper plays on the free deck; the next track starts
  under it on the other deck.
- **One imaging pool.** Ads / bumpers / stingers / IDs are one category.
- **Learning.** Lua measures lead-in, trailing silence and peak while a track
  plays cleanly (never during a fade), starts crossfades early by the learned
  tail, and reports the figures to the server.

## What the Core told us (probe 0.1.0, 2026-10-12)

Facts from the first real run that the deck controller and server will build on.

| Topic | Finding |
|---|---|
| Platform | Core 8 Flex, build 1-2603002, Lua 5.3, `Timer.Now()` and `json` available |
| Clock | `os.date` on the Core is **local time**; the debug log stamps UTC. All logging uses UTC (`os.date("!...")`) |
| File listing | `all.files` is a `Virtual` control and reads **empty from script** even with a folder selected. Lua enumerates with `dir.get()` instead, which returns `name`, `size`, `type` |
| Path recipe | `dir.get("media/" .. root .. directory)` — e.g. root `Audio/` + directory `Test/Tuning/` → `media/Audio/Test/Tuning/`. `dir.get("media")` and `dir.get("Audio/")` return nil |
| `directory` | Relative to `root`, trailing slash (`Test/Tuning/`) |
| `filename` | Bare file name inside `directory`; spaces, brackets, parentheses and accents are all present in the test folder |
| Duration | Known **as soon as a file loads**: with the player stopped at progress 0, `remaining.Value` is the full duration in seconds and `progress.MaxValue` updates to match. No playback needed |
| Load latency | 0.07–0.10 s for every format and size (3 MB MP3 to 59 MB WAV alike). 18 of 19 test files loaded; WMA fails with `"Unsupported file format"` |
| Load signals | `status` fires its EventHandler **only when the string changes** (two FLACs back to back fire nothing), and after a failed load `remaining` keeps the previous file's value. The controller therefore waits for `filename.ui` to echo the name and `remaining` to carry a fresh value, and only trusts duration when status starts with `OK` |
| Formats seen | FLAC, MP3 128/143/171/320 kbps, WAV 16-bit → `"OK: 44.1kHz[, 320kbps|, 16-bit], stereo"`; WMA → unsupported |
| `progress` / `remaining` | Type `Time`: seconds in `.Value`, `HH:MM:SS` in `.String`, read-only |
| `status` | `""` with nothing loaded, `"OK: …"` with a file loaded, **unchanged while playing** — use the `playing` LED for state |
| Play / stop | `play:Trigger()` → `playing=true` in 0.02 s; `stop` rewinds (`progress` 0, `remaining` = duration). `progress`/`remaining` update once per second, so sub-second timing uses `Timer.Now()` plus the duration read at load |
| Meter vs gain | The meter is **post-gain**: during a fade it tracks gain within one 250 ms sample. Silence is judged at unity (`meter − gain`). Meter floors at −120 about 0.75 s after gain hits −100; `rms` releases ~1 s slower, so `peak.N` is the detector input. `meter.N` equals `peak.N` when `select` = Peak |
| Audio onset | First meter movement 1.25 s after the play trigger on the test FLAC, then a slow rise (the song's quiet intro). Lead-in is per file, hence learned |
| `locate` | `Virtual`, **write-only** (seek target, not readable) |
| `gain` | −100 … +20 dB |
| Players | `Track Count = 1` (mono out), `Playlist Capable = True`; firmware adds `sync.playlist.*` / `sync.state` controls |
| Meter | `meter2`, 2 channels, −120 … +20 dBFS, floor reads −120.0000076; `response.time` 300 ms default (10 ms – 1 s); `peak.N` and `rms.N` both live regardless of `select` |
| Control metadata | `Component.GetControls()` exposes `MinValue`, `MaxValue`, `MinString`, `MaxString`, `Choices` |
| Dump file | `io.open` to `media/_probe/...` works; `dir.create` works |

## Deck controller

`qsys/radio/radio.lua` (Lua 5.3, Text Controller or Control Script). Edit only
the `CFG` block at the top: player / meter names, `SERVER_URL`,
`SERVER_TOKEN` (the server's `RADIO_API_TOKEN`) and `STATION_ID`.

- **Timing.** A 250 ms station timer reads each deck's `peak.N` (the meter
  samples every ~250 ms), watches for trailing silence, and checks the
  crossfade point; inside the last tick it arms `Timer.CallAfter` so the fade
  starts on time. Fades step gain every 50 ms on a timer that runs only while
  a deck is fading or loading (~3% of the time).
- **Transitions.** Normal end (crossfade early by the learned tail), skip,
  silence in the last `end_guard_s`, bumper on the free deck every N songs
  with the next song starting under its tail, stop after current, stop now.
- **Rotation.** Shuffle bag with artist separation; the operator queue comes
  first.
- **Learning** (`media/_radio/learned.json`, saved every 5 min and on stop):
  duration, lead-in, minimum trailing silence, peak at unity, play count.
  A file the player cannot play ("Unsupported…") is remembered; any other
  load failure only skips the file until the next scan. An unreadable
  learned.json is kept as `learned.json.bad`, never overwritten.
- **Never dead.** If neither deck is busy for `dead_air_s`, something starts;
  with an empty music folder it backs off and logs once.
- **Panel controls (optional).** Text Controller: named controls `Skip`,
  `BumperNow`, `Automation` (inputs) and `Status`, `NowPlaying`, `NextUp`,
  `DeckA`, `DeckB`, `LastEvent` (outputs). Control Script: Input Count = 3,
  Output Count = 6, same order. Outputs update once a second, only on change.

### Testing without a Core

`qsys/test/mock_core.lua` stands in for the Q-SYS runtime (simulated clock,
async loads, post-gain meter, fake HttpClient), modelled on the probe facts.
Needs `pip install lupa`.

```
python3 qsys/test/run_mock.py qsys/test/scenario_basic.lua        # rotation, bumpers, silence, learning
python3 qsys/test/run_mock.py qsys/test/scenario_server.lua       # server outage, outbox, replay
python3 qsys/test/run_mock.py qsys/test/scenario_control.lua      # every panel command, settings push
python3 qsys/test/run_mock.py qsys/test/scenario_regressions.lua  # one case per bug found in review
```

`qsys/test/live_core.py --server http://127.0.0.1:5410 --token T` runs
radio.lua in real time against a real server with a simulated library, for
demos and end-to-end testing of the control panel.

## Repository layout

```
qsys/
  probe/probe.lua     discovery script (run once per site / design)
  radio/radio.lua     deck controller
  test/               MockCore, scenarios, runner, live simulated Core
server/
  app.py              routes (pages, web API, Core API)
  auth.py             shared sign-in (shared.users / shared.app_sessions)
  control.py          Core sync + library, commands, status, settings
  core_settings.py    every station setting: label, range, default, help
  events.py           Core events -> core_events + play_log
  plays.py            play log queries
  db.py               pooled PostgreSQL access + small helpers
  logs.py             journald + remote syslog (background thread)
  migrate.py          applies server/migrations/NNNN_*.sql on every start
  templates/, static/ web control panel (bundled fonts, no CDN)
tests/                pytest suite (real PostgreSQL)
install.sh · start.sh · .env.example · requirements.txt · VERSION
```

## Getting started

### Run the station on the Core

1. Put a few bumpers in the imaging folder (any FLAC/MP3/WAV) or set
   "Bumper every N songs" to 0.
2. Paste `qsys/radio/radio.lua` into the Text Controller. Check `CFG`. To link
   it to the server, set `CFG.SERVER_URL` and `CFG.SERVER_TOKEN` (install.sh
   prints both).
3. Run it. It scans the folders and starts the first track on Player 1. Every
   event is one line in the Debug Output; a heartbeat prints every 30 s.

### Run the probe (once per site)

`qsys/probe/probe.lua` dumps the design, walks `media/Audio`, loads every
file in the player's folder silently, then plays one at −20 dB for ~20 s.
Set `RUN_PLAYBACK_PROBE = false` for a silent run.

## App server: install and update

The server installs into a clone in the service user's home folder and keeps
all its data in its **own PostgreSQL schema** (`qsysradio`) inside the
database the other apps use. Sign-in uses the family's `shared` schema
(users are read, never written; see *Sign-in* below).

### First install

```
cd ~
git clone https://github.com/ShowSysDan/QSYSRadio.git
cd QSYSRadio
cp .env.example .env          # fill in PG_HOST / PG_DBNAME / PG_USER / PG_PASSWORD
sudo ./install.sh
```

`install.sh` creates `venv/`, installs `requirements.txt`, generates
`SECRET_KEY` and `RADIO_API_TOKEN`, creates the schema and applies the
migrations, and writes `/etc/systemd/system/qsysradio.service` running as the
user who ran sudo, from this folder. It prints the `CFG.SERVER_URL` and
`CFG.SERVER_TOKEN` lines to paste into radio.lua. The panel is at
`http://<server>:5410/` (`APP_PORT`).

If the PG user may not create schemas, have a DBA run
`CREATE SCHEMA qsysradio AUTHORIZATION <pg user>;` once.

### Updates

```
cd ~/QSYSRadio && git pull && sudo systemctl restart qsysradio
```

Every start (`start.sh`) reinstalls Python dependencies if
`requirements.txt` changed, applies new `server/migrations/NNNN_*.sql` in
order (one transaction each, recorded in `qsysradio.schema_migrations`,
serialised by an advisory lock), then starts gunicorn. A failed migration
rolls back and the service does not start (`journalctl -u qsysradio -n 50`).
Re-run `sudo ./install.sh` only when the changelog says so.

### Tests

```
venv/bin/pip install pytest
QSYSRADIO_ENV_FILE=/path/to/test.env venv/bin/python -m pytest tests/
```

The suite uses its own throw-away schemas in the database the .env points at
(the PG user needs CREATE on it) and covers the Core API, commands, sign-in
and permissions, settings, the play log, and failure handling.

## Web control panel

- **Dashboard:** on-air countdown, up next (queue or rotation), bumper
  countdown, library size; transport (Skip, Bumper now, Stop after current,
  Stop now / Start); both decks live; the queue (reorder, remove, clear);
  activity (every command, who sent it, and the Core's answer); recent plays.
- **Library:** search, sort (artist, title, most / least recently played),
  Play next, Queue, Play now; imaging files: Bumper now. Rescan.
- **Play log:** a day (search, type), what was on air at a moment, CSV.
- **Settings** (admins): every station setting in groups (Library, Bumpers &
  ads, Fades, Levels, Silence detection, Rotation, Safety), with what the
  Core is running next to each. Saved settings reach the Core within a second.

Commands go to the server, the Core picks them up on its next sync (≤ 1 s)
and reports the result, which shows in Activity. Pages poll short requests,
so any number of people can watch at once.

### Sign-in

The same accounts and sessions as 321Theater and the other apps
(`shared.users`, `shared.app_sessions`; see FetchLog's SHARED_AUTH.md).

| Who | Can |
|---|---|
| `is_app_user`, `is_app_admin` or role `admin` | sign in and watch |
| … and not `is_readonly` | operate (transport, queue, rescan) |
| `is_app_admin` or role `admin` | change station settings |

QSYSRadio re-checks these against `shared.users` every 5 minutes, even for
sessions started in another app; someone without access gets "not allowed"
here and stays signed in elsewhere. Signing out signs out of every app.

## Play log: what played when

`qsysradio.play_log` holds one row per airing: `started_at` / `ended_at`
(timestamptz, the Core's clock), `kind` (`music` / `imaging`), `file`,
`directory`, `artist`, `title`, `deck`, `duration_s`, `played_s`,
`end_reason` (`end`, `silence`, `cut`, `eof`, `core_restart`). During a
crossfade two rows overlap. Every raw event is kept in
`qsysradio.core_events`.

Events carry a boot id + sequence number and wait in an outbox on the Core
(`media/_radio/outbox.json`, up to 5000) until the server has them, so an
outage or a script restart loses nothing; repeats are ignored.

```sql
SET TimeZone = 'America/New_York';
-- everything that aired on a day
SELECT started_at, ended_at, kind, artist, title, file, end_reason
FROM qsysradio.play_log
WHERE started_at >= '2026-10-12' AND started_at < '2026-10-13'
ORDER BY started_at;

-- what was on air at 2:03 pm
SELECT * FROM qsysradio.play_log
WHERE started_at <= '2026-10-12 14:03' AND ended_at > '2026-10-12 14:03';

-- most played this month
SELECT artist, title, count(*) FROM qsysradio.play_log
WHERE kind = 'music' AND started_at >= date_trunc('month', now())
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25;
```

## Syslog

Set `SYSLOG_HOST` in `.env` and restart to send the server's log to your
syslog server as well as journald: every play and end, commands and who sent
them, settings changes, sign-ins, unsupported files, dead-air guard, Core
restarts and errors. Options (all in `.env.example`): `SYSLOG_PORT` (514),
`SYSLOG_PROTOCOL` (`udp` / `tcp`), `SYSLOG_FORMAT` (`rfc3164` / `rfc5424`),
`SYSLOG_FACILITY` (`local0`), `SYSLOG_LEVEL` (`INFO`), `SYSLOG_APP_NAME`.

```
<134>Oct 12 19:40:21 radio-srv QSYSRadio[3066]: PLAY music "Bravo - Two" station=Main artist=Bravo deck=A dir=Music/ duration=28 file=Bravo - Two.mp3 title=Two at=2026-10-12T23:40:21Z
```

Sending runs on a background thread with a bounded queue, so a slow or dead
syslog server never delays the app; messages are dropped when the queue is
full and a lost TCP connection is retried every 30 s. The database is the
complete record.

## Versioning

- [Semantic Versioning](https://semver.org/). One project version in
  `VERSION`; git tags `vX.Y.Z`. `radio.lua` carries the same version and
  reports it; the server logs a mismatch.
- Changes are recorded in `CHANGELOG.md` under *Unreleased*.

## Roadmap

| Version | Milestone |
|---|---|
| 0.1 | Probe |
| 0.2 | Standalone deck controller |
| 0.3 | Server: play log, control panel, settings, sign-in, syslog *(in progress)* |
| 0.4 | Rotation rules on the server: no-repeat window, scheduled ad breaks, time-of-day rules |
| 0.5 | Learning v2: loudness normalisation from learned peak, lead-in aware starts |
| 1.0 | Stable: alerts, docs |

## Network requirement

The Core must reach the app server over HTTP (`host:APP_PORT`). Nothing needs
to reach the Core.
