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
install.sh · start.sh · .env.example · requirements.txt · requirements-dev.txt · VERSION
```

## Installation

One path from a bare server to a Core on the air. You end up with the server
in `~/QSYSRadio`, running as the systemd service `qsysradio` under your login
user, its data in the schema `qsysradio` of the PostgreSQL database
321Theater uses, and radio.lua on the Core posting to it. Commands are for
Debian / Ubuntu with sudo.

The Core can also run on its own, without the server: do only
[step 11](#11-connect-the-q-sys-core) and leave `CFG.SERVER_URL = ""`.

### What you need

| Need | Details |
|---|---|
| App server | Debian 11+ or Ubuntu 22.04+ (Python 3.9 or later, systemd) and a login user with sudo |
| PostgreSQL | 13 or later (what 321Theater needs; QSYSRadio is tested on 16): normally the server 321Theater already uses (2A), or a new one on this machine (2B) |
| 321Theater | Needed for sign-in. It creates `shared.users`, the accounts everyone signs in with, and it is where you manage them: QSYSRadio has no account admin of its own. 2B shows how to install it |
| Q-SYS | Designer 9.x / 10.x and a Core that runs scripts (a Core Nano or Core 8 Flex needs the Scripting Engine licence; if the probe ran, you have it) |
| Network | Core → server TCP 5410, staff browsers → server TCP 5410, server → PostgreSQL TCP `<db-port>` (5432) |

Replace these placeholders with yours:

| Placeholder | What to put |
|---|---|
| `<db-host>` | The PostgreSQL server; `localhost` when it runs on this machine |
| `<db-port>` | Its port: `5432`, unless 321Theater's `.env` has another `PG_PORT` |
| `<dbname>` | `321theater`; `radio` only in 2B without sign-in |
| `<password>` | The password of the `qsysradio` role (step 2) |
| `<server-ip>` | This server's address on the Core's network |
| `<core-ip>`, `<staff-subnet>` | The Core's address; the staff PCs' subnet, e.g. `10.0.20.0/24` |

The commands use 321Theater's defaults: database `321theater`, role
`showadvance`, shared schema `shared`. If its `.env` (`PG_DBNAME`, `PG_USER`,
`PG_SHARED_SCHEMA`; usually `~/321theater/.env` on its server) says
otherwise, use its values. They also use QSYSRadio's port 5410: if you set
another `APP_PORT` in step 4, use yours in every command below.

### 1. Install the packages

install.sh needs Python 3.9+ with `venv`. curl and the PostgreSQL client are
for the checks and backups below.

```
sudo apt-get update
sudo apt-get install -y git python3 python3-venv curl postgresql-client
python3 --version
```

Expect `Python 3.9` or later. Ubuntu 20.04 has 3.8, which install.sh refuses.

- pip downloads from PyPI during the install, and again on any restart after
  `requirements.txt` changes. Behind a proxy, set it in `/etc/pip.conf` so the
  service sees it too (sudo drops `HTTPS_PROXY`):
  `printf '[global]\nproxy = http://<proxy>:<port>\n' | sudo tee /etc/pip.conf`
- `pg_dump` must be at least as new as the PostgreSQL server
  (`pg_dump --version`). If it is older, install the matching
  `postgresql-client-NN` from PostgreSQL's apt repository.

### 2. Prepare PostgreSQL

Do 2A when 321Theater already runs somewhere (the normal case), or 2B for a
new PostgreSQL on this machine.

#### 2A. Join 321Theater's database (normal case)

QSYSRadio gets its own login role, its own schema, and only the access to
`shared` that sign-in needs. 321Theater has already created `shared.users`
and `shared.app_sessions`.

Make a password of letters and digits only (`.env` is read by both systemd
and Python, which treat `\`, quotes and spaces differently):

```
python3 -c 'import secrets; print(secrets.token_hex(16))'
```

On the database server, as the PostgreSQL superuser:

```
sudo -u postgres psql -p <db-port> -d 321theater
```

```sql
CREATE ROLE qsysradio LOGIN PASSWORD '<password>';
GRANT CONNECT ON DATABASE "321theater" TO qsysradio;
CREATE SCHEMA qsysradio AUTHORIZATION qsysradio;
-- sign-in: read the accounts, read and write the shared sessions
GRANT USAGE ON SCHEMA shared TO qsysradio;
GRANT SELECT ON shared.users TO qsysradio;
GRANT SELECT, INSERT, UPDATE, DELETE ON shared.app_sessions TO qsysradio;
-- 321Theater's backups dump the whole database: let its role read this schema
GRANT USAGE ON SCHEMA qsysradio TO showadvance;
GRANT SELECT ON ALL TABLES IN SCHEMA qsysradio TO showadvance;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA qsysradio TO showadvance;
ALTER DEFAULT PRIVILEGES FOR ROLE qsysradio IN SCHEMA qsysradio GRANT SELECT ON TABLES TO showadvance;
ALTER DEFAULT PRIVILEGES FOR ROLE qsysradio IN SCHEMA qsysradio GRANT SELECT ON SEQUENCES TO showadvance;
\q
```

Each line answers `CREATE ROLE`, `GRANT`, `CREATE SCHEMA` or
`ALTER DEFAULT PRIVILEGES`, with no `ERROR`.

- Without the three `shared` grants the service still starts, but nobody
  can sign in. At every start the journal names what is missing, e.g.
  `migrate: WARNING sign-in will not work: GRANT SELECT ON "shared".users TO "qsysradio";`.
- Without the last five lines, 321Theater's own backups fail with
  `permission denied for schema qsysradio` once QSYSRadio has tables. With
  them, its backups include the radio data. Restore QSYSRadio from its own
  dump (step 12), not from 321Theater's.
- QSYSRadio needs no `CREATE` on `shared`: it never creates or changes
  anything there.
- Simpler, less isolated: use 321Theater's role as `PG_USER`. It owns the
  database, so none of the above is needed.

If PostgreSQL runs on another machine, the database server must also accept
connections from this app server. If 321Theater already connects from this
app server, only the `pg_hba.conf` line may be missing (none is needed if its
line has `all` in the user column).

1. Find the files: `sudo -u postgres psql -p <db-port> -c 'SHOW config_file;' -c 'SHOW hba_file;'`
2. In `postgresql.conf`, `listen_addresses` must include the LAN address
   (for example `listen_addresses = '*'`).
3. In `pg_hba.conf`, add (`md5` instead of `scram-sha-256` on PostgreSQL 13
   and older):
   ```
   host  321theater  qsysradio  <server-ip>/32  scram-sha-256
   ```
4. `sudo systemctl reload postgresql`. Use `restart` instead if you changed
   `listen_addresses`; that briefly disconnects 321Theater.
5. Allow TCP `<db-port>` from this server, e.g.
   `sudo ufw allow from <server-ip> to any port <db-port> proto tcp`.

Check from the app server. It asks for the password:

```
psql "host=<db-host> port=<db-port> dbname=321theater user=qsysradio" \
  -c 'SELECT count(*) FROM shared.users;' -c 'SELECT count(*) FROM shared.app_sessions;'
```

Expect two counts. `permission denied` means a grant is missing.
`no pg_hba.conf entry`, `Connection refused` or `password authentication
failed` point to the list just above.

#### 2B. A new PostgreSQL on this machine

Sign-in reads `shared.users`, which only 321Theater creates, and accounts
are added and enabled in 321Theater. So there are two options: install
321Theater here first, or run without sign-in until you do.

**With sign-in (recommended): 321Theater first, then 2A.** Its own README
(<https://github.com/ShowSysDan/321Theater>) has the details; this is the
short path.

1. Install PostgreSQL and the libraries 321Theater's PDF export needs:
   ```
   sudo apt-get update
   sudo apt-get install -y postgresql libpango-1.0-0 libpangoft2-1.0-0 libffi-dev libcairo2
   sudo -u postgres psql -c 'SELECT version();'
   ```
   Expect a `version` row starting `PostgreSQL 1`.
2. Make a password for 321Theater's role (letters and digits, as in 2A),
   then create the role and its database:
   ```
   python3 -c 'import secrets; print(secrets.token_hex(16))'
   sudo -u postgres psql -c "CREATE ROLE showadvance LOGIN PASSWORD '<theater-password>';"
   sudo -u postgres psql -c 'CREATE DATABASE "321theater" OWNER showadvance;'
   ```
   Expect `CREATE ROLE`, then `CREATE DATABASE`.
3. Install 321Theater:
   ```
   cd ~
   git clone https://github.com/ShowSysDan/321Theater.git 321theater
   cd 321theater
   cp .env.example .env
   chmod 600 .env
   nano .env
   sudo ./install.sh
   ```
   In its `.env`, set `PG_PASSWORD=<theater-password>`. Its other defaults
   fit (`PG_HOST=localhost`, `PG_DBNAME=321theater`, `PG_USER=showadvance`,
   `PG_SHARED_SCHEMA=shared`). With sudo, its installer also installs
   LibreOffice, a large download. Expect
   `✓ Admin account:   username=admin  password=admin123`, then
   `Service is running.` and `Open 321Theater at: http://<server-ip>:5400`.
4. Allow port 5400 for the staff PCs as in step 7. Open
   `http://<server-ip>:5400/`, sign in as `admin` / `admin123` and set a
   new password when asked: 8 or more characters, with an uppercase letter
   and a punctuation mark (otherwise it answers `Password must include …`).
   The installer's closing banner says `admin / admin`; the password is
   `admin123`.
5. Do 2A on this machine: `<db-host>` is `localhost`, `<db-port>` is
   `5432`, and skip the remote-access list.

**Without sign-in for now: QSYSRadio's own database.** The service, the
Core link and the play log all work (read the play log with SQL; see
[Play log](#play-log-what-played-when)). Nobody can sign in to the web
panel, though.

```
sudo apt-get update
sudo apt-get install -y postgresql
python3 -c 'import secrets; print(secrets.token_hex(16))'
sudo -u postgres psql -c "CREATE ROLE qsysradio LOGIN PASSWORD '<password>';"
sudo -u postgres psql -c "CREATE DATABASE radio OWNER qsysradio;"
psql "host=localhost dbname=radio user=qsysradio" -c 'SELECT version();'
```

The third line makes `<password>` (letters and digits only, see 2A). The
last one asks for it; expect a `version` row starting `PostgreSQL 1`. From
here on `<db-host>` is `localhost`, `<db-port>` is `5432` and `<dbname>` is
`radio`. Skip steps 8–10. The sign-in page says "Sign-in is not available:
QSYSRadio cannot use the shared user tables. An administrator should check
the server log."

To add sign-in later, install 321Theater into this same database:

1. Make a password for its role (as above), then create the role and let it
   create its schemas in `radio`:
   ```
   sudo -u postgres psql -c "CREATE ROLE showadvance LOGIN PASSWORD '<theater-password>';"
   sudo -u postgres psql -d radio -c 'GRANT CREATE ON DATABASE radio TO showadvance;'
   ```
2. Do steps 1, 3 and 4 of "With sign-in" above, with `PG_DBNAME=radio` as
   well as `PG_PASSWORD` in 321Theater's `.env`. Skip its step 2.
3. Open `sudo -u postgres psql -d radio`, paste the 2A SQL from
   `GRANT USAGE ON SCHEMA shared` to the end, and `\q`.
4. Check, with the `qsysradio` password. Expect two counts:
   ```
   psql "host=localhost dbname=radio user=qsysradio" \
     -c 'SELECT count(*) FROM shared.users;' -c 'SELECT count(*) FROM shared.app_sessions;'
   ```
5. Run `sudo systemctl restart qsysradio`, then do steps 8–10. `<dbname>`
   stays `radio`.

### 3. Clone the code

Clone as the user who will run the service: your own login, with sudo. The
service runs from this folder, so leave it where it is.

```
cd ~
git clone https://github.com/ShowSysDan/QSYSRadio.git
cd QSYSRadio
cat VERSION
```

Expect the version, e.g. `0.3.0-dev`.

- If `cat` says `No such file or directory`, the clone has no server: check
  that you cloned `ShowSysDan/QSYSRadio` and are on `main`
  (`git branch --show-current`).
- A private repository needs a GitHub token or SSH key.
- install.sh refuses a root shell ("Run as your own user via sudo (sudo
  ./install.sh), not as root directly.").
- To run the service as an account without sudo (e.g. `radio`), clone and
  fill in `.env` as that account, then run the installer for it from your
  own login (the first line creates the account; skip it if it exists):
  ```
  sudo adduser --disabled-password --gecos "" radio
  sudo -u radio -H git clone https://github.com/ShowSysDan/QSYSRadio.git /home/radio/QSYSRadio
  sudo -u radio cp /home/radio/QSYSRadio/.env.example /home/radio/QSYSRadio/.env
  sudo -u radio chmod 600 /home/radio/QSYSRadio/.env
  sudo -u radio nano /home/radio/QSYSRadio/.env
  sudo env SUDO_USER=radio /home/radio/QSYSRadio/install.sh
  ```
  Later commands that say `~/QSYSRadio` then mean `/home/radio/QSYSRadio`,
  run as that account, e.g. `sudo -u radio -H git -C /home/radio/QSYSRadio pull`.

### 4. Fill in .env

```
cp .env.example .env
chmod 600 .env
nano .env
```

| Key | What to put |
|---|---|
| `PG_HOST` | `<db-host>` (`localhost` when PostgreSQL is on this machine). Not the socket path `/var/run/postgresql`: Debian's default `peer` rule refuses it for this role |
| `PG_PORT` | `<db-port>` (`5432` unless 321Theater's `.env` says otherwise) |
| `PG_DBNAME` | `<dbname>`: `321theater`, or `radio` in 2B without sign-in |
| `PG_USER`, `PG_PASSWORD` | `qsysradio` and the password from step 2 |
| `PG_APP_SCHEMA` | leave `qsysradio` |
| `PG_SHARED_SCHEMA` | 321Theater's `PG_SHARED_SCHEMA` (`shared`) |
| `TIMEZONE` | uncomment and set the station's zone, e.g. `America/New_York` (empty = the PostgreSQL server's zone) |
| `APP_PORT` | `5410` unless something else uses it. If you change it, use your port wherever this guide says 5410 (health, firewall, `CFG.SERVER_URL`) |
| `SECRET_KEY`, `RADIO_API_TOKEN` | leave empty: install.sh generates them |
| `WEB_WORKERS`, `WEB_THREADS`, `PG_POOL_MAX_IDLE` | leave commented (4 × 8, 4). A server can open up to WEB_WORKERS × (WEB_THREADS + 2) = 40 PostgreSQL connections; check `SHOW max_connections;` (default 100) covers the sister apps too |
| `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_DOMAIN` | leave unset unless step 10 says otherwise |
| `SYSLOG_*` | optional, see [Syslog](#syslog) |
| `RADIO_ALLOW_NO_TOKEN` | never on a real station |
| `ACCESS_LOG` | leave commented. `1` logs every HTTP request to the journal (the Core alone makes one a second) |

One `KEY=value` per line, no spaces around `=`, no comment on the same line.
Check:

```
ls -l .env
grep -E '^(PG_|APP_PORT|TIMEZONE)' .env
```

Expect `-rw-------` and your values.

### 5. Run the installer

```
sudo ./install.sh
```

It creates `venv/` and installs `requirements.txt`. It writes `SECRET_KEY`
and `RADIO_API_TOKEN` into `.env`, then creates the schema if needed and
applies the migrations. Last, it writes `/etc/systemd/system/qsysradio.service`
and starts it. The service runs `start.sh` as you, from this folder, and
restarts 5 s after any failure. Expect (first run):

```
[INFO]  App directory: /home/<you>/QSYSRadio (service user: <you>)
==> Checking Python 3...
[INFO]  Found Python 3.x.y
==> Virtual environment...
[INFO]  Created /home/<you>/QSYSRadio/venv
[INFO]  Dependencies installed.
==> Configuration...
[INFO]  Generated SECRET_KEY
[INFO]  Generated RADIO_API_TOKEN (put it in radio.lua as CFG.SERVER_TOKEN)
==> Database...
migrate: applied 0001_app_settings.sql
migrate: applied 0002_core_events_play_log.sql
migrate: applied 0003_stations_commands_library.sql
migrate: applied 0004_events_unique_per_station.sql
migrate: schema "qsysradio" at version 0004
==> systemd service...
[INFO]  Wrote /etc/systemd/system/qsysradio.service
[INFO]  Service is running.

  Health:  http://<server-ip>:5410/api/v1/health
  Core:    in radio.lua set CFG.SERVER_URL = "http://<server-ip>:5410"
           and CFG.SERVER_TOKEN = "<token>"
  Update:  cd /home/<you>/QSYSRadio && git pull && sudo systemctl restart qsysradio
  Logs:    journalctl -u qsysradio -f
```

Keep the two `CFG` lines for step 11.

- In 2B without sign-in you also see `migrate: created schema "qsysradio"`
  before the `applied` lines, and before the last line
  `migrate: WARNING sign-in will not work: schema "shared" does not exist (321Theater creates it; check PG_SHARED_SCHEMA)`.
  Anywhere else, a `sign-in will not work` line names a missing grant (see
  [Troubleshooting](#troubleshooting)).
- The address shown is the first from `hostname -I`. On a server with several
  networks, use the one the Core can reach.
- Print the token again any time: `grep ^RADIO_API_TOKEN= ~/QSYSRadio/.env`.
- Re-running is safe: the secrets in `.env` are kept and only pending
  migrations run (`... at version 0004 (nothing to do)`).
- Without sudo (`./install.sh`) it sets up the venv, `.env` and the database,
  ends with `Done (no systemd service without sudo).` and prints no `CFG`
  lines. `./start.sh` then runs the server in the foreground.
- "Service is running." is checked only 2 seconds after the start, which is
  too early to catch every failure. Always do step 6.
- On a database error, the installer always ends with `[ERROR] Migration
  failed (see above). Check the PG_* values in /home/<you>/QSYSRadio/.env.`
  The real cause is in the line above that: see
  [Troubleshooting](#troubleshooting).

### 6. Check the service

The journal holds the service's own lines (sign-ins, commands, plays,
warnings); individual HTTP requests are not logged unless `ACCESS_LOG=1`.
The commands use sudo because a user outside the `adm` and
`systemd-journal` groups may not see every line of a system service.

```
systemctl status qsysradio --no-pager
sudo journalctl -u qsysradio --since "10 min ago" --no-pager
curl -s http://localhost:5410/api/v1/health; echo
```

Expect:

- `Active: active (running)`. `activating (auto-restart)` means it fails and
  retries every 5 s; the journal says why.
- In the journal: `migrate: schema "qsysradio" at version 0004 (nothing to do)`
  (install.sh already applied them), `[qsysradio] 0.3.0-dev starting on port
  5410 (4x8)` and gunicorn's `Listening at: http://0.0.0.0:5410`. No
  `sign-in will not work` line, except in 2B without sign-in.
- From curl:
  ```
  {"app":"qsysradio","ok":true,"schema":"qsysradio","schema_expected":4,"schema_version":4,"version":"0.3.0-dev"}
  ```
  An empty line instead means nothing is listening on 5410 (curl `-s` hides
  its own error): the service is not running, and the journal says why.

`"ok":false` (still HTTP 200) means the schema version differs from the
code's. HTTP 503 with "The radio server cannot reach its database" means
PostgreSQL is unreachable. The health check does not test the `shared`
grants; the `sign-in will not work` lines and signing in (step 9) do. To
list applied and pending migrations, run
`venv/bin/python -m server.migrate --status` from `~/QSYSRadio`.

### 7. Open the port

The Core and staff browsers connect on `APP_PORT` (5410). gunicorn listens on
every interface over plain HTTP, so allow only who needs it. With ufw:

```
sudo ufw allow from <core-ip> to any port 5410 proto tcp
sudo ufw allow from <staff-subnet> to any port 5410 proto tcp
sudo ufw status
```

Expect `5410/tcp  ALLOW  <core-ip>` and the same for `<staff-subnet>`
(`Status: inactive` means ufw is off). With firewalld:

```
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="<core-ip>/32" port port="5410" protocol="tcp" accept'
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="<staff-subnet>" port port="5410" protocol="tcp" accept'
sudo firewall-cmd --reload
sudo firewall-cmd --list-rich-rules
```

If neither is active, there is nothing to open here. Firewalls between VLANs
still need the same rule. Check from a staff PC:
`http://<server-ip>:5410/api/v1/health` shows the JSON from step 6.

The server itself connects out to PostgreSQL (`<db-port>`) and, if
`SYSLOG_HOST` is set, to syslog (514).

### 8. Give people access (in 321Theater)

In 2B without sign-in, skip steps 8–10 and go on with step 11.

QSYSRadio never writes accounts. Access comes from two flags on each
321Theater account, set by a 321Theater admin:

1. Sign in to 321Theater as an admin. On a fresh 321Theater, `admin` /
   `admin123` must set a new password first. Until then QSYSRadio refuses
   it with "You must change your password in 321Theater before using
   QSYSRadio."
2. Go to **Settings** → **Users** tab → **Edit** on the person's row.
3. Check **ROLE**. For anyone who should operate the radio (skip, queue,
   start, stop), it must not be "Read-only — view access only, no edits":
   pick "User — view & edit shows" or "Staff — …". ("Admin — full access"
   also makes them an admin of 321Theater.)
4. Under **OTHER APPLICATIONS**, tick **Is App User** (watch and operate),
   and **Is App Admin** for station settings. Click **Save Changes**.

Check: in the Users list the person's ROLE column shows `user` (or their
role) and an **App User** badge (and **App Admin** if ticked), not
**Read-only**.

What each flag allows is under [Sign-in](#sign-in). 321Theater's `admin`
role counts as Is App Admin here.

- Accounts made with **Add User** start with both flags off, and 321Theater
  currently saves every one of them as read-only, whatever role you picked
  (its Edit dialog then shows "Read-only — …"). Always edit them afterwards:
  steps 3 and 4.
- The role "Read-only — view access only, no edits" (with Is App User) makes
  someone watch-only here: a READ-ONLY tag shows next to their name and the
  buttons are greyed out. It also makes them read-only in 321Theater: there
  is no per-app read-only.
- Changes reach people already signed in within 5 minutes, or at once when
  they sign out and in again.
- Locking an account in 321Theater signs it out everywhere, but QSYSRadio
  does not check the lock yet. Untick the flags too.

### 9. First sign-in

Open `http://<server-ip>:5410/` and sign in with your 321Theater username and
password. Expect the dashboard. Until step 11, it says "No Core has connected
yet" under On air, and `· no Core connected (set CFG.SERVER_URL in
radio.lua)` next to Transport. If a READ-ONLY tag shows next to your name at
the top, see step 8, item 3.

| The sign-in page says | Fix |
|---|---|
| Invalid username or password. | Check them in 321Theater |
| Your account does not have access to QSYSRadio. Ask an administrator to enable it. | Step 8 |
| You must change your password in 321Theater before using QSYSRadio. | Change it in 321Theater |
| Too many sign-in attempts. Wait a minute and try again. | Wait a minute |
| Sign-in is not available: QSYSRadio cannot use the shared user tables. An administrator should check the server log. | 321Theater's tables are missing or not granted: see [Troubleshooting](#troubleshooting) |
| No message, but you are back on the sign-in page | The `shared.app_sessions` grant (step 2A), or a cookie setting (step 10): see [Troubleshooting](#troubleshooting) |

### 10. Shared sign-in with the other apps

You are signed in to 321Theater and QSYSRadio at once only when the browser
sends both the same `session` cookie:

- Use the same host name for all the apps. Cookies ignore the port, so
  `http://apps.example.org:5400/` (321Theater) and
  `http://apps.example.org:5410/` share a sign-in.
  `http://10.0.0.20:5410/` and `http://apps.example.org:5400/` do not. With
  different host names, people sign in to each app separately, with the
  same account.
- Leave `SESSION_COOKIE_DOMAIN` unset unless every app sets the same value.
  321Theater does not read it.
- The server speaks plain HTTP. Set `SESSION_COOKIE_SECURE=1` only when people
  reach it through an HTTPS reverse proxy. Over plain HTTP the browser drops
  a Secure cookie and sends you back to the sign-in page.
- A reverse proxy must pass the original Host header (nginx:
  `proxy_set_header Host $http_host;`). Otherwise sign-in is refused with
  "Sign in from the QSYSRadio page.". The Core can keep using port 5410
  directly.
- Keep the app servers' clocks in sync (`timedatectl` shows
  `System clock synchronized: yes`): sessions expire by the clock.

Restart after editing `.env`: `sudo systemctl restart qsysradio`. To check,
sign in to 321Theater, then open QSYSRadio on the same host name: with an
account that has access, the dashboard opens without asking.

### 11. Connect the Q-SYS Core

In Q-SYS Designer, with the station's design open:

1. **Components.** You need two Audio Players and a True Peak/RMS meter with
   2 channels. Wire Player 1 to meter input 1 and Player 2 to input 2, as
   well as into the air chain. Their Code Names must match `CFG`:
   `Audio_Player_01`, `Audio_Player_02`, `PlayerOutput01` (or change
   `CFG`).
2. **Script Access.** Select each of the three, then set **Properties** →
   **Script Access** to `Script` (or `All`). With the default `None`, the
   script cannot see them.
3. **Player root.** Keep both players' Root the same; the probe found
   `Audio/`. The script sets the folder and file itself. The probe also
   found `Track Count = 1` (mono out). QSC's training material says a
   1-track player plays only the left channel of a stereo file, so decide
   mono or stereo in the design.
4. **Audio files.** Open Core Manager (browse to `https://<core-ip>`, or use
   Tools → Show Core Manager in Designer), then **Files** → **Audio**.
   Create the music and imaging folders and upload. The defaults are
   `Test/Tuning/` and `Test/Imaging/` under `Audio`. Change them later on the
   Settings page (Music folder, Imaging folder). FLAC, MP3 and WAV are proven
   on the Core; WMA does not load. Put a few bumpers in the imaging folder,
   or set "Bumper every N songs" to 0.
5. **Script.** Add a Text Controller (or Control Script). Open its Lua
   editor, paste all of `qsys/radio/radio.lua`, and change only `CFG`:
   ```lua
   CFG = {
     PLAYER_A = "Audio_Player_01", METER_A = 1,
     PLAYER_B = "Audio_Player_02", METER_B = 2,
     METER = "PlayerOutput01",
     SERVER_URL = "http://<server-ip>:5410",   -- from install.sh
     SERVER_TOKEN = "<token>",                 -- RADIO_API_TOKEN from .env
     STATION_ID = "Main",                      -- unique per Core; "" = the design name
     AUTOSTART = true,
   }
   ```
   For the optional panel controls, see [Deck controller](#deck-controller).
6. **Run.** Use File → Save to Core & Run. The script scans the folders and
   starts the first track on Player 1. With `AUTOSTART = false` it waits for
   the Automation control or Start on the dashboard.
7. **Check the Core.** In the script's Debug Output (times are UTC):
   ```
   16:00:01 radio 0.3.0-dev starting on Core 8 Flex (<design name>)
   16:00:01 library: 19 music in media/Audio/Test/Tuning/, 3 imaging in media/Audio/Test/Imaging/
   ```
   There should be no `! server: sync failed` lines. Every event prints one
   line, and a heartbeat prints every 30 s.
8. **Check the server.** Within seconds, next to Transport on the
   dashboard, `· no Core connected (set CFG.SERVER_URL in radio.lua)`
   becomes `· Main · radio.lua 0.3.0-dev`, and under On air "No Core has
   connected yet" becomes the playing track (artist – title). The journal
   shows `INFO BOOT station=Main ...` and then `PLAY` lines:
   ```
   sudo journalctl -u qsysradio -f
   ```
   Ctrl+C stops it.

The Core only makes outbound HTTP requests. Its control LAN (usually LAN A)
must reach `<server-ip>` by one of three routes: the same subnet, its default
gateway, or a static route in Core Manager's network settings. Q-SYS does not
route between its LAN ports. Keep the Core's clock right (NTP), because the
play log uses it.

**Probe (once per site or design, optional).** `qsys/probe/probe.lua` needs
the same Script Access. It dumps the design and walks `media/Audio`. It
loads every file in the player's folder silently, then plays one at −20 dB
for about 20 s. Set `RUN_PLAYBACK_PROBE = false` for a silent run. Copy the
Debug Output from `===== PROBE START` to `===== PROBE END`.

### 12. Back up

Back up before every update, and on a schedule if you like. On the app
server (it asks for the `qsysradio` password):

```
pg_dump -h <db-host> -p <db-port> -U qsysradio -d <dbname> -n qsysradio -Fc -f ~/qsysradio-$(date +%F).dump
cp -p ~/QSYSRadio/.env ~/qsysradio-env-$(date +%F)
pg_restore -l ~/qsysradio-$(date +%F).dump | grep -c 'TABLE DATA'
```

The last line prints the number of tables (7 in 0.3.0-dev).

- `-n qsysradio` is required. The role may not read the rest of the database,
  and a whole-database dump fails with `permission denied`.
- To skip the password prompt (e.g. from cron), put
  `<db-host>:<db-port>:<dbname>:qsysradio:<password>` in `~/.pgpass`, then run
  `chmod 600 ~/.pgpass`.
- The accounts (`shared`) are 321Theater's to back up.
- `.env` holds the database password and the Core token. Keep the copy
  private.
- On the Core, `media/_radio/learned.json` holds what the script has learned
  about each file.

### 13. Update

Take a backup first (step 12). Then:

```
cd ~/QSYSRadio
git pull
sudo systemctl restart qsysradio
sudo journalctl -u qsysradio --since "5 min ago" --no-pager
curl -s http://localhost:5410/api/v1/health; echo
```

In the journal look for `migrate: applied NNNN_...` (when the update has
migrations) and `starting on port`, then `"ok":true` from curl.

Every start (`start.sh`) does three things in order:

1. It installs Python packages if `requirements.txt` changed
   (`requirements.txt changed, installing dependencies...`). This needs
   PyPI (step 1); without it the service restarts every 5 s.
2. It applies new `server/migrations/NNNN_*.sql` in order. Each runs in one
   transaction, is recorded in `qsysradio.schema_migrations`, and is
   serialised by an advisory lock. A failed one rolls back and the service
   does not start.
3. It starts gunicorn.

- When CHANGELOG.md says radio.lua changed, paste the new radio.lua on the
  Core and re-enter your `CFG` values. So does a journal line like
  `station=Main runs radio.lua 0.3.0-dev, server is 0.4.0`.
- Re-run `sudo ./install.sh` only when the changelog says so.
- `.env` is not tracked by git and is kept. If `git pull` refuses because
  files were edited in the clone, `git status` shows them.
- The Core keeps playing during the restart. Its Debug Output may show one
  `! server: sync failed`, then `server: reachable again`.

### 14. Roll back

Migrations only go forward, and there are no release tags yet, so roll back to
a commit:

```
cd ~/QSYSRadio
git log --oneline -n 10
git checkout <commit>
sudo systemctl restart qsysradio
curl -s http://localhost:5410/api/v1/health; echo
```

The database stays at the newer schema. The service still starts, the
journal says `migrate: WARNING the database has migration(s) 0005 that this
code does not know (code rolled back?)…`, and health shows `"ok":false` with
`schema_version` above `schema_expected`.

If the old code does not work with the newer schema, restore the backup
taken before the update. Stop the service first:

```
sudo systemctl stop qsysradio
```

When `<dbname>` is `321theater` (2A, 2B with sign-in), the PostgreSQL
superuser restores it, because QSYSRadio's role may not create schemas there.
If the database runs on another machine, copy the dump to your home folder
there first (add `user@` before `<db-host>` if your login there differs):

```
scp ~/qsysradio-YYYY-MM-DD.dump <db-host>:
```

Then, on the database server:

```
sudo -u postgres psql -p <db-port> -d 321theater -c 'DROP SCHEMA qsysradio CASCADE;'
sudo -u postgres pg_restore -p <db-port> -d 321theater < ~/qsysradio-YYYY-MM-DD.dump
```

When `<dbname>` is `radio` (2B without sign-in, also after adding 321Theater
later), the role owns the database and restores it alone, on this machine:

```
psql -h localhost -p <db-port> -U qsysradio -d radio -c 'DROP SCHEMA qsysradio CASCADE;'
pg_restore -h localhost -p <db-port> -U qsysradio -d radio < ~/qsysradio-YYYY-MM-DD.dump
```

Expect `DROP SCHEMA` (after a notice listing the tables) and no output from
`pg_restore`. This brings back the schema, its owner and its grants. Then, on the app server:

```
sudo systemctl start qsysradio
curl -s http://localhost:5410/api/v1/health; echo
```

Expect `"ok":true` again. To return to the current code later:

```
cd ~/QSYSRadio
git checkout main
git pull
sudo systemctl restart qsysradio
```

### Troubleshooting

Messages come from the installer, from the journal, or from the Core's
Debug Output. The journal's last hour:

```
sudo journalctl -u qsysradio --since "1 hour ago" --no-pager | tail -n 50
```

**Install and start**

| You see | Fix |
|---|---|
| `Python 3.9+ is required (found Python 3.8.x).` | Use Debian 11+ or Ubuntu 22.04+ |
| `venv failed. On Debian/Ubuntu: sudo apt-get install -y python3-venv`, or on a re-run `sudo: /home/<you>/QSYSRadio/venv/bin/pip: command not found` (`venv/bin/pip: No such file or directory` without sudo) | Install `python3-venv`, `rm -rf venv`, re-run install.sh |
| `Created …/.env from .env.example. Fill in PG_DBNAME / PG_USER / PG_PASSWORD, then re-run.` | Step 4 (for a remote database also `PG_HOST`, `PG_PORT`) |
| `migrate: PostgreSQL is not configured: set PG_HOST / PG_DBNAME / PG_USER / PG_PASSWORD in …` | `PG_DBNAME` or `PG_USER` is empty |
| `migrate: PostgreSQL unreachable: … Connection refused` | Wrong `PG_HOST` / `PG_PORT`, `listen_addresses`, or a firewall (step 2A) |
| `Connection refused` a few times right after a reboot, then the service runs | It started before PostgreSQL on the same machine and retried every 5 s. Nothing to fix |
| `… Peer authentication failed for user "qsysradio"` | `PG_HOST` is a socket path: set `PG_HOST=localhost` |
| `… no pg_hba.conf entry for host "…", user "…", database "…"` | Add the `pg_hba.conf` line and reload (step 2A) |
| `… password authentication failed for user "…"`, or `… fe_sendauth: no password supplied` | `PG_USER` / `PG_PASSWORD` do not match the role, or `PG_PASSWORD` is empty |
| `… invalid value for parameter "TimeZone": "…"`, or a traceback ending `ValueError: TIMEZONE '…' is not a time zone name` | `TIMEZONE` in `.env` must be a zone name such as `America/New_York`, or commented out |
| `… database "…" does not exist` | `PG_DBNAME` |
| `psycopg2.errors.InsufficientPrivilege: permission denied for database …` | The role may not create the schema: `CREATE SCHEMA qsysradio AUTHORIZATION qsysradio;` as superuser (step 2A) |
| `migrate: WARNING sign-in will not work: schema "shared" does not exist (…)` | 321Theater is not in this database (expected in 2B without sign-in), or `PG_SHARED_SCHEMA` differs from its value |
| `migrate: WARNING sign-in will not work: GRANT …` (or `… no USAGE on schema "shared": GRANT …`) | Run the `GRANT` it shows as the PostgreSQL superuser in this database (step 2A), then `sudo systemctl restart qsysradio`. The service runs meanwhile; only sign-in fails |
| `migrate: WARNING sign-in will not work: "shared".app_sessions does not exist (…)` | Start or update 321Theater once (it creates the table), then restart qsysradio |
| `migrate: WARNING the database has migration(s) NNNN that this code does not know (code rolled back?)…` | The code is older than the database (step 14). Update the code again, or restore the backup taken before the update |
| `migrate: FAILED NNNN_name.sql; rolled back` | The lines above say why. Nothing was changed; fix it and restart |
| `[qsysradio] No virtualenv at …/venv. Run: sudo ./install.sh` | As it says |
| `Connection in use: ('0.0.0.0', 5410)`, then `Can't connect to ('0.0.0.0', 5410)` | Another program has the port (`sudo ss -ltnp 'sport = :5410'`). Set `APP_PORT`, restart, then change `CFG.SERVER_URL` and the firewall to match |
| pip errors after `requirements.txt changed, installing dependencies...`, and the service restarts every 5 s | No access to PyPI: set the proxy in `/etc/pip.conf` (step 1) |

**Health and sign-in**

| You see | Fix |
|---|---|
| Health: HTTP 503, `The radio server cannot reach its database. Reload in a moment.` | PostgreSQL is down or unreachable; the journal shows `database unavailable: …` |
| Health: `"ok":false` (HTTP 200) | `schema_version` ≠ `schema_expected`: a migration failed (journal), or the code was rolled back (step 14) |
| "Sign-in is not available: QSYSRadio cannot use the shared user tables. …", or a "Sign-in unavailable" page (journal: `sign-in unavailable: relation "users" does not exist` or `permission denied for table …`) | 321Theater is not in this database, `PG_SHARED_SCHEMA` differs from its value, or a `shared` grant is missing. `sudo journalctl -u qsysradio -g 'will not work' --no-pager` shows the `GRANT` to run (step 2A) |
| Sign-in seems to work but lands on the sign-in page again; journal: `sign-in unavailable: permission denied for table app_sessions` | `GRANT SELECT, INSERT, UPDATE, DELETE ON shared.app_sessions TO qsysradio;` (step 2A) |
| Sign-in lands on the sign-in page again, nothing in the journal | `SESSION_COOKIE_SECURE=1` over plain HTTP, or a `SESSION_COOKIE_DOMAIN` the other apps do not use (step 10) |
| "Sign in from the QSYSRadio page." | The reverse proxy must pass the Host header (step 10) |
| "Your account is read-only.", or a READ-ONLY tag next to your name | In 321Theater, set ROLE to anything but "Read-only — …" (step 8, item 3; **Add User** saves new accounts read-only). Then sign out and in again |
| "Administrators only." | Tick **Is App Admin** (step 8, item 4), then sign out and in again |

**Core**

| You see | Fix |
|---|---|
| `meter component 'PlayerOutput01' not found`, `Deck A: component 'Audio_Player_01' not found` | The Code Name differs from `CFG` (step 11) |
| `Deck A: 'Audio_Player_01' has no control 'gain' (Script Access on?)` | Script Access is `None`, or the Code Name differs from `CFG` (step 11) |
| `meter 'PlayerOutput01' has no control peak.1` (or `peak.2`) | The meter's Script Access is `None`, or it has fewer than 2 channels (step 11) |
| `library: cannot list media/Audio/Test/Tuning/` | That folder is not on the Core: upload it, or change Music folder on the Settings page |
| `! server: sync failed (403 ); N events queued`; journal: `/api/v1/core/sync: rejected request from <core-ip> (bad or missing X-Radio-Token)` | `CFG.SERVER_TOKEN` differs from `RADIO_API_TOKEN` (`grep ^RADIO_API_TOKEN= ~/QSYSRadio/.env`): paste it and run again |
| Journal: `/api/v1/core/sync: refused, RADIO_API_TOKEN is not set in .env` | Re-run `sudo ./install.sh` (it generates one), then put it in `CFG.SERVER_TOKEN` |
| `! server: sync failed (…)` with no 403 and nothing in the journal | The Core cannot reach the server: check `SERVER_URL`, the Core's routing and the firewall (steps 7, 11). It keeps playing and queues up to 5000 events until it can |
| Journal: `station=Main runs radio.lua X, server is Y` | Paste radio.lua from this checkout onto the Core |

### Uninstall

```
sudo systemctl disable --now qsysradio
sudo rm /etc/systemd/system/qsysradio.service
sudo systemctl daemon-reload
```

Take a final backup (step 12) first if you want the data. Then, on the
database server, remove the schema and the role. In 2A, in 2B with sign-in,
and in 2B where 321Theater was added to `radio` later, the database also
holds 321Theater's data, so drop only QSYSRadio's part. Run
`sudo -u postgres psql -p <db-port> -d <dbname>` and first check who owns
the shared sessions table:

```sql
SELECT tableowner FROM pg_tables WHERE schemaname = 'shared' AND tablename = 'app_sessions';
```

If it says `qsysradio` (early 0.3.0-dev builds could create it), first run
`ALTER TABLE shared.app_sessions OWNER TO showadvance;`. Otherwise
`DROP OWNED` below would drop the table the other apps use. When
`<dbname>` is `radio`, also run `ALTER DATABASE radio OWNER TO showadvance;`
first: the role owns that database and could not be dropped. Then:

```sql
DROP SCHEMA qsysradio CASCADE;
DROP OWNED BY qsysradio;
DROP ROLE qsysradio;
\q
```

Only in 2B without sign-in, where `radio` holds nothing else, drop the
whole database instead:
`sudo -u postgres psql -c 'DROP DATABASE radio;' -c 'DROP ROLE qsysradio;'`.

Then remove the clone, which holds `.env` with the secrets:
`rm -rf ~/QSYSRadio`. On the Core, remove the script and, if you like,
`media/_radio/` in Core Manager.

Last, close what steps 2A and 7 opened. On the app server:
`sudo ufw delete allow from <core-ip> to any port 5410 proto tcp`, the same
for `<staff-subnet>` (with firewalld, `--remove-rich-rule` with the same
rules, then `--reload`). On a remote database server, delete the `qsysradio`
line from `pg_hba.conf` and the TCP `<db-port>` rule for this server, then
`sudo systemctl reload postgresql`.

## Tests

```
venv/bin/pip install -r requirements-dev.txt
QSYSRADIO_ENV_FILE=/path/to/test.env venv/bin/python -m pytest tests/
```

The suite uses its own throw-away schemas (`qsr_test`, `qsr_test_shared`) in
the database the test .env points at (the PG user needs CREATE on it). It
covers the Core API, commands, sign-in and permissions, settings, the play
log, and failure handling. `requirements-dev.txt` also brings `lupa` for the
MockCore scenarios and `live_core.py`.

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
(`shared.users`, `shared.app_sessions`; see FetchLog's
[SHARED_AUTH.md](https://github.com/ShowSysDan/FetchLog/blob/main/SHARED_AUTH.md)).

| Who | Can |
|---|---|
| `is_app_user`, `is_app_admin` or role `admin` | sign in and watch |
| … and not `is_readonly` | operate (transport, queue, rescan) |
| `is_app_admin` or role `admin` | change station settings |

The flags are set in 321Theater (Installation, step 8). QSYSRadio re-checks
them against `shared.users` every 5 minutes, even for sessions started in
another app. Someone without access gets "not allowed" here and stays signed
in elsewhere. Signing out signs out of every app that shares the cookie
(step 10).

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

The web panel's Play log page shows it. To query it with SQL, open a session
from the app server (it asks for the `qsysradio` password; the placeholders
are those of [Installation](#installation)), then paste a query below:

```
psql "host=<db-host> port=<db-port> dbname=<dbname> user=qsysradio"
```

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

| From | To | Port |
|---|---|---|
| Core (control LAN) | app server | TCP `APP_PORT` (5410), HTTP |
| Staff browsers | app server | TCP `APP_PORT` (5410), HTTP |
| App server | PostgreSQL | TCP 5432 (or its `PG_PORT`) |
| App server | syslog server (optional) | UDP or TCP 514 |

Nothing needs to reach the Core. See Installation steps 2A, 7 and 11.
