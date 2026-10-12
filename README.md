# Q-SYS Radio Automation

Version **0.3.0-dev** · see [CHANGELOG.md](CHANGELOG.md)

A two-deck radio-station automation system built on a Q-SYS Core. Lua on the
Core does the real-time work (deck control, crossfades, silence detection,
never going dead). A Python/Flask app with a PostgreSQL backend does the
thinking (library, rotation rules, bumper scheduling, repeat management,
learning, logging) and hosts the web UI.

## Status

| Component | State |
|---|---|
| `qsys/probe` — discovery script | **0.1.1** — done |
| `qsys/radio` — deck controller | **0.3.0-dev** — standalone; posts every event to the server through a persistent outbox when `CFG.SERVER_URL` is set; awaiting first run on the Core |
| `qsys/test` — MockCore simulator | **0.3.0-dev** — offline Core stand-in, with a fake HttpClient for server outages |
| `server` — Flask API + scheduler + Postgres | **0.3.0-dev** — own `qsysradio` schema, migrate-on-start, event ingest + timestamped play log, play log API, remote syslog |
| Web UI | not started (0.5) |

## Architecture

```
 ┌────────────────────────── Q-SYS Core ──────────────────────────┐
 │  Text Controller (Lua)                                         │
 │   • owns both decks: load, play, stop, gain fades,             │
 │     crossfade timing, silence / end-of-track detection         │
 │   • keeps playing from a local cache if the server is gone     │
 │   • reports every event upstream (HTTP POST)                   │
 │   • asks "what's next?" as a track nears its end               │
 │                                                                │
 │  Audio_Player_01 ──► PlayerOutput01 meter.1                    │
 │  Audio_Player_02 ──► PlayerOutput01 meter.2                    │
 └──────────────────────────────┬─────────────────────────────────┘
                                │ HTTP (Lua HttpClient, Core pulls)
                                ▼
 ┌──────────────────────── App server ────────────────────────────┐
 │  Flask (Python)                                                │
 │   • /api/v1/…  endpoints the Core talks to                     │
 │   • library + learned per-file metadata                        │
 │   • rotation rules, bumper / ad scheduling, repeat management  │
 │   • play log, now-playing, settings                            │
 │   • web UI                                                     │
 └──────────────────────────────┬─────────────────────────────────┘
                                │ SQL
                                ▼
 ┌──────────────────────── DB server ─────────────────────────────┐
 │  PostgreSQL                                                    │
 └────────────────────────────────────────────────────────────────┘
```

### Why plain HTTP from the Core

Lua's `HttpClient` gives request/response calls that can be replayed with
`curl`, show up cleanly in Flask's logs, and need no connection state. The
Core *pulls*: it asks for the next track when it needs one, posts events as
they happen, and polls a small commands endpoint (skip, reload settings) every
couple of seconds. `WebSocket` stays in reserve if sub-second push is ever
needed.

## Design decisions (locked)

- **Lua owns real time, Python owns choice.** The Core never waits on the
  server to keep audio flowing. Python only answers "what, on which deck,
  with what transition."
- **Never go dead.** If the server is unreachable, Lua keeps playing from its
  cached file list (local shuffle) and buffers events to replay later.
- **Per-deck metering.** Player 1 → `meter.1`, Player 2 → `meter.2`, so
  silence detection on one deck is unaffected by the other.
- **Gain.** Normal playing gain is 0 dB. Fades run linearly in dB to the
  −100 dB floor. Curves and durations are server-side settings.
- **Two decks only.** A bumper plays on the free deck; the next track starts
  under it on the other deck.
- **One imaging pool.** Ads / bumpers / stingers are one category; rules may
  vary by time of day.
- **Learning.** Python only knows what Lua tells it. The first time a file
  plays, Lua reports duration, lead-in silence, trailing silence and peak /
  RMS so the server can schedule tighter transitions next time.
- **Settings live in Python.** Silence threshold, hold time, end-of-track
  guard, crossfade length, poll interval, etc. are stored server-side and
  pushed to the Core. Lua ships with safe defaults so it can boot alone.

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

## Deck controller (0.2)

`qsys/radio/radio.lua` runs the station on its own. One 50 ms tick drives
both decks; everything else hangs off it.

- **Library.** `dir.get()` on `music_dir` and `imaging_dir` (relative to the
  player's root, e.g. `Test/Tuning/`). `all.files` is not used. Files the
  player rejects are learned as unsupported and skipped from then on.
- **Deck.** `load` → `play` → `fadeOut` → `stop`, with a loader that waits for
  `filename.ui` to echo the name and `remaining` to carry a fresh duration.
  Every tick it reads `peak.N`, computes the level at unity (`meter − gain`),
  and tracks first audio, last audio, peak, and how long it has been silent.
- **Transitions.** When the live deck has `crossfade_s + learned_trail`
  seconds left (or operator skip, or ≥ `silence_hold_s` of silence inside
  `end_guard_s`), the other deck starts the next track and the live deck
  fades to −100 dB linearly in dB. Every `bumper_every_n_tracks`, the free
  deck plays a bumper instead, the outgoing track fades under it, and the
  next track starts on the just-freed deck when the bumper has
  `bumper_tail_s` left.
- **Rotation.** Shuffle bag over the whole music folder, no immediate repeat,
  `artist_separation` from `Artist - Title` file names.
- **Learning.** On every track end: duration, lead-in, trailing silence
  (minimum seen), peak at unity, play count → `media/_radio/learned.json`.
  Learned trail shortens the next crossfade so dead tails are never aired.
- **Never dead.** A tick that errors is caught and logged; if neither deck is
  busy for `dead_air_s`, something starts. EOF without a transition is
  handled. The whole thing runs with no server.
- **Operator controls** are optional: name controls `Skip`, `BumperNow`,
  `Automation`, `Status`, `NowPlaying`, `NextUp`, `DeckA`, `DeckB`,
  `LastEvent` on a Control Script, or set the Text Controller to 3 inputs /
  6 outputs and they map by index in that order.

Every event carries UTC `t`, Core-clock `at`, and a `boot` id + `seq`
number that identify it uniquely. `EVENTS` keeps the last 500 in memory; with
`CFG.SERVER_URL` set they also go to the server (see *Play log* below).

### Offline testing (MockCore)

`qsys/test/mock_core.lua` is a stand-in for the Q-SYS runtime with a
simulated clock, modelled on the probe measurements (async loads, 1 Hz
progress, post-gain meter with release, status-only-on-change). Scenarios are
plain Lua; the runner needs `pip install lupa`.

```
python3 qsys/test/run_mock.py qsys/test/scenario_basic.lua
python3 qsys/test/run_mock.py qsys/test/scenario_server.lua
```

`scenario_server.lua` points the station at a fake server that times out and
then returns 503 for 90 s, and checks that events queue, persist to
`outbox.json`, and all arrive once the server is back. With
`MOCK_EVENTS_OUT=/path.json` it also writes every POST body, which can be
replayed into a real server for an end-to-end test.

`scenario_basic.lua` plays 8 short tracks (one with a silent tail, one corrupt)
and 2 bumpers for 260 simulated seconds, presses Skip at t=65, and asserts
on the event log: bumpers on the free deck, silence cut, learned trail,
unsupported file excluded, no artist repeats, no dead air.

## Repository layout

```
qsys/
  probe/probe.lua     discovery script (run once per site / design)
  radio/radio.lua     deck controller (standalone station)
  test/               MockCore simulator, scenarios, runner
server/
  app.py              Flask app (gunicorn: server.app:app)
  config.py           .env reader (process env first, then .env)
  db.py               PostgreSQL connection, app + shared schema search_path
  migrate.py          applies server/migrations/NNNN_*.sql on every start
  migrations/         numbered SQL migrations (never edit one once merged)
docs/                 design notes, API contract
install.sh            first install (venv, .env, DB, systemd unit)
start.sh              service launcher: deps sync, migrate, gunicorn
.env.example          server config template (copy to .env)
requirements.txt
CHANGELOG.md
VERSION
```

## Getting started (0.2.0)

### Run the station on the Core

1. Put a few bumpers in `Audio/Test/Imaging/` (any FLAC/MP3/WAV), or set
   `SETTINGS.bumper_every_n_tracks = 0` to run music only.
2. Paste `qsys/radio/radio.lua` into the Text Controller. Check `CFG`
   (component names) and `SETTINGS` (`music_dir`, `imaging_dir`).
3. Run it. It stops both decks, scans the folders, and starts the first
   track on Player 1 at 0 dB. Watch the Debug Output: every event is one
   line (`track_start`, `transition`, `track_end`, `bumper_start`, …) and a
   heartbeat prints every 30 s.
4. Optional operator panel: see the Deck controller section above.

### Run the probe (once per site)

`qsys/probe/probe.lua` dumps the design, walks `media/Audio`, loads every
file in the player's folder silently (status, duration, load time), then plays
one at −20 dB for ~20 s, fades, stops and restores the player. Set
`RUN_PLAYBACK_PROBE = false` for a silent run.

## App server: install and update

The Flask server installs into a clone in the service user's home folder
and keeps all of its data in its **own PostgreSQL schema** (`qsysradio` by
default) inside the same database the other apps use. The cross-app `shared`
schema is on the search path for the shared user table (web UI logins, 0.5)
but QSYSRadio never creates anything there.

### First install

```
cd ~
git clone https://github.com/ShowSysDan/QSYSRadio.git
cd QSYSRadio
cp .env.example .env          # fill in PG_HOST / PG_DBNAME / PG_USER / PG_PASSWORD
sudo ./install.sh
```

`install.sh` creates `venv/`, installs `requirements.txt`, generates
`SECRET_KEY`, creates the `qsysradio` schema and applies the migrations, then
writes `/etc/systemd/system/qsysradio.service` running as the user who ran
sudo, from this folder, with `.env` as its `EnvironmentFile`. Check it at
`http://<server>:5410/api/v1/health` (port is `APP_PORT` in `.env`).

If the PG user may not create schemas in the database, have a DBA run
`CREATE SCHEMA qsysradio AUTHORIZATION <pg user>;` once; the app only creates
the schema when it is missing.

### Updates

```
cd ~/QSYSRadio && git pull && sudo systemctl restart qsysradio
```

Every start (`start.sh`) reinstalls Python dependencies if
`requirements.txt` changed, applies any new `server/migrations/NNNN_*.sql`
in order (each in one transaction, recorded in
`qsysradio.schema_migrations`, serialised by an advisory lock), then starts
gunicorn. A failed migration rolls back and the service does not start
(`journalctl -u qsysradio -n 50`); an unreachable database does the same and
systemd retries every 5 s. Re-run `sudo ./install.sh` only when the systemd
unit itself changes (the changelog will say so).

`python -m server.migrate --status` (from the venv) lists applied and pending
migrations without changing anything.

## Play log: what played when

Every time something goes to air the server records it in
`qsysradio.play_log`: one row per play with `started_at` / `ended_at`
(timestamptz, from the Core's clock), `kind` (`music` / `imaging`), `file`,
`directory`, `artist`, `title`, `deck`, `duration_s`, `played_s` and
`end_reason` (`end`, `silence`, `cut`, `eof`, `core_restart`). During a
crossfade two rows overlap, as on air. Every raw event (transitions,
unsupported files, dead-air guard, boots) is kept in `qsysradio.core_events`.

**How it gets there.** Set `CFG.SERVER_URL` (and `CFG.SERVER_TOKEN` if the
server has `RADIO_API_TOKEN`) at the top of `radio.lua`. The Core POSTs
events to `/api/v1/events` every 2 s. Unsent events wait in an outbox saved to
`media/_radio/outbox.json` (up to 5000), so a server outage, network drop or
script restart loses nothing; they are sent with their original times when
the server is back. Each event has a unique boot id + sequence number, so a
resend is ignored. Playback never waits on any of this.

**Ask the API** (times without an offset are read in `TIMEZONE` from `.env`):

```
GET /api/v1/now                                  on air now + last 20 plays
GET /api/v1/plays?from=2026-10-12&to=2026-10-13  plays in a range (default: last 24 h)
GET /api/v1/plays?q=beatles&kind=music           search file / artist / title
GET /api/v1/plays/at?t=2026-10-12T14:03          what was on air at that moment
```

**Or query the DB directly:**

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
syslog server as well as journald: every play and end, unsupported files,
dead-air guard, Core restarts, version mismatches, rejected POSTs and
database errors. Options (all in `.env.example`): `SYSLOG_PORT` (514),
`SYSLOG_PROTOCOL` (`udp` / `tcp`), `SYSLOG_FORMAT` (`rfc3164` / `rfc5424`),
`SYSLOG_FACILITY` (`local0`), `SYSLOG_LEVEL` (`INFO`), `SYSLOG_APP_NAME`
(`QSYSRadio`). The header carries the timestamp, hostname and app name so
the collector can file it; the event's own time is in the message as `at=`
(it differs when a backlog is replayed, which also adds `replayed=1`).

```
<134>Oct 12 19:40:21 radio-srv QSYSRadio[3066]: PLAY music "Bravo - Two" station=Main artist=Bravo deck=A dir=Test/Tuning/ duration=28 file=Bravo - Two.mp3 title=Two at=2026-10-12T23:40:21Z
```

Syslog is best effort (UDP can drop; a TCP collector going away loses the
message in flight, then the server reconnects every 30 s). The database is the
complete record.

## Versioning

- [Semantic Versioning](https://semver.org/). One project version in
  `VERSION`; git tags `vX.Y.Z`.
- Every Lua script carries `Version:` in its header and reports it to the
  server at startup; the server logs mismatches.
- The HTTP API is versioned by path (`/api/v1/…`).
- Changes are recorded in `CHANGELOG.md` under *Unreleased* and rolled into a
  numbered section at release time.

## Roadmap

| Version | Milestone |
|---|---|
| 0.1 | Probe: learn how the players, meter and media directory actually behave |
| 0.2 | Lua deck controller running standalone: local shuffle, crossfade, silence detection, bumper on the free deck, learning, MockCore tests |
| 0.3 | Flask API + Postgres schema, library sync from Lua, play log |
| 0.4 | Rotation rules: no-repeat window, artist separation, bumper cadence, time-of-day rules |
| 0.5 | Web UI: now playing, next up, history, skip, settings |
| 0.6 | Learning v2: loudness normalisation from learned peak, lead-in aware starts under bumpers, library-wide catalogue on boot |
| 1.0 | Stable: event replay after outages, alerts, docs |

## Settings (defaults in `radio.lua`, server-managed from 0.3)

| Setting | Default | Notes |
|---|---|---|
| `music_dir` / `imaging_dir` | `Test/Tuning/` / `Test/Imaging/` | relative to the player's root |
| `play_gain_db` / `fade_floor_db` | 0 / −100 | |
| `crossfade_s` | 4 | outgoing fade at a normal end |
| `fade_in_s` | 0 | incoming fade (0 = start at play gain) |
| `skip_fade_s` / `silence_fade_s` | 1.5 / 1.0 | outgoing fade on skip / on detected silence |
| `silence_threshold_dbfs` | −50 | at unity gain |
| `silence_hold_s` | 2.0 | |
| `end_guard_s` | 20 | silence only counts when remaining is under this |
| `bumper_every_n_tracks` | 4 | 0 = never |
| `bumper_tail_s` | 1.0 | next track starts when the bumper has this much left |
| `artist_separation` | 2 | |
| `dead_air_s` | 1.5 | |
| `tick_s` | 0.05 | fade resolution and detector rate |
| `load_timeout_s` | 1.5 | loads take ~0.1 s on the Core |

## Network requirement

The Core must be able to reach the app server over HTTP (`host:port` of the
Flask app). Nothing needs to reach the Core.
