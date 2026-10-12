# QSYSRadio: notes for Claude

Two-deck radio automation: Lua on the Q-SYS Core (`qsys/`) does real time;
a Flask + PostgreSQL server (`server/`) does library, rotation, logging and
the web UI. See README.md for the design.

## Every change: CHANGELOG + version
Record every change under `## [Unreleased]` in CHANGELOG.md in the same
commit. At release, roll it into a numbered section and bump `VERSION`
(semver). Lua scripts carry their own `Version:` header.

## PostgreSQL: its own schema, PostgreSQL only
- Shares a database with the other apps (321Theater, Leash, FetchLog, ...).
  Every QSYSRadio table lives in `PG_APP_SCHEMA` (default `qsysradio`).
  `PG_SHARED_SCHEMA` (default `shared`) is second on the search_path for the
  cross-app users table: read it, never create or alter anything there.
- Connect only through `server.db.connect()`; it sets the search_path and
  raises `DatabaseUnavailable` when PG is unconfigured or down. Never catch
  that and carry on with made-up data; requests get a 503.
- Write unqualified table names (the search_path resolves them). Placeholders
  are `%s`; new ids via `RETURNING id`; never `REAL` (use `DOUBLE PRECISION`);
  timestamps are `TIMESTAMPTZ`.

## Migrations (server/migrations/, applied by start.sh on every start)
- Schema changes go in a NEW file `NNNN_short_name.sql` (next number). Never
  edit, rename or renumber a migration that is on main: the runner records a
  checksum and only warns, it will not re-run it.
- Each file runs in one transaction with search_path = app schema only, so
  plain `CREATE TABLE x` lands in the app schema. No `BEGIN`/`COMMIT` in the
  file. Data backfills go in the same numbered files.
- Test against a real PostgreSQL (16 is installed in the cloud sandbox):
  fresh schema, then upgrade from the previous version.

## Web workers and background jobs
gunicorn runs several workers x threads (default 4 x 8) so many browsers can
watch at once. Never assume one process: anything that must run once
(scheduler, "what's next" decisions) takes a PostgreSQL advisory lock
(`pg_try_advisory_lock(hashtext('qsysradio:<job>'))`) and skips when another
process holds it. Pages that show live state poll short GETs (`/api/v1/now`);
no long-held connections or websockets.

## Core events and the play log
- radio.lua events carry `boot` + `seq` (unique); the server stores each
  once in `core_events` (ON CONFLICT DO NOTHING) and only NEW events touch
  `play_log` or syslog, so outbox replays are harmless. Keep it idempotent.
- Adding an event field: emit it in radio.lua, read it in server/events.py;
  raw JSON is always kept in `core_events.data`.
- Run both MockCore scenarios after any radio.lua change.

## Logging / syslog
Log through `server.logs.log` (logger `qsysradio`). It goes to stderr
(journald) and, when `SYSLOG_HOST` is set, to remote syslog with a full
RFC 3164/5424 header (app name `QSYSRadio`, English month names). A dead
syslog server must never break a request.

## Config
Per-machine settings are `.env` keys (document them in `.env.example`), read
via `server.config.get()`: process env first (systemd `EnvironmentFile=`),
then the file. Server-managed settings shared by every server go in the
`app_settings` table.

## Deployment
Installed as a git clone in the service user's home folder
(`~/QSYSRadio`), venv at `venv/`, systemd unit `qsysradio` written by
`install.sh` with `ExecStart=start.sh`. Updates must keep working as
`git pull && sudo systemctl restart qsysradio`: put launch logic in
`start.sh`, not the unit. If a change needs `sudo ./install.sh` re-run (unit
changes, OS packages), say so in the CHANGELOG and to the user. At the end of
every change, tell the user what to redeploy: the Core (paste a Lua script),
the server (pull + restart), both, or neither.

## Git
Develop on the assigned branch; push with `git push -u origin <branch>`. No
PR unless asked.
