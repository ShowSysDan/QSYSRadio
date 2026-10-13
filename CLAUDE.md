# QSYSRadio: notes for Claude

Two-deck radio automation: Lua on the Q-SYS Core (`qsys/`) does real time;
a Flask + PostgreSQL server (`server/`) does everything else (play log, web
control panel, settings, library, sign-in, syslog). README.md has the design.

## Every change
- Record it under `## [Unreleased]` in CHANGELOG.md in the same commit.
  At release, roll it into a numbered section and bump `VERSION` and
  `RADIO_VERSION` in radio.lua (semver).
- Tell the user what to redeploy: the Core (paste radio.lua), the server
  (`git pull && sudo systemctl restart qsysradio`), both, or neither.
- No duplicated code: reuse the helpers (Lua: `push`, `shuffled`, `set`/`trigger`,
  `readJson`/`writeJson`, `timer`, `post`, `emit`; Python: `db.text`,
  `db.number`, `db.clean_json`, `db.like`, `db.iso`, `dbconn()`,
  `write_logs`; JS: `Radio.api/post/command/setHTML/esc/num/status`).

## radio.lua: keep it slim and light
- Real time only. Anything that can live on the server does.
- Station settings are defined ONLY in `server/core_settings.py` (label,
  range, default, help). radio.lua's `SETTINGS` holds the same names with
  fallback values and nothing else; `tests/test_server.py` fails if the two
  drift. Internals (timer rates, timeouts, paths) are constants in `K` /
  `FILES`, not settings.
- No header essays; one-line comments only where the line needs one.
- Overhead budget: one 250 ms timer; the 50 ms `Fast` timer only while a deck
  fades or loads (`Engine.wake()`); one HTTP request per second; control
  objects cached in `Deck.new`; panel writes only on change; files written
  only from `Chores` (every 30 s) or when settings change. Never add per-tick allocation, polling of the server,
  or whole-file writes on every event.
- Use the documented Q-SYS APIs (HttpClient.Upload, Timer, Component, dir,
  io on media/, rapidjson). Keep `Tick`, `Fast`, `Sync`, `Chores` timers
  global (a local timer can be garbage-collected).
- After any change run every MockCore scenario (`for s in
  qsys/test/scenario_*.lua; do python3 qsys/test/run_mock.py $s; done`); add a
  case to `scenario_regressions.lua` for every bug fixed.
- The Core's only files are `settings.json` and `outbox.json` (both for when
  the server is unreachable). Per-file data (learned figures, unsupported)
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
- `POST /api/v1/core/sync` (≈1/s, X-Radio-Token): events (station, boot,
  seq unique; stored once, each in a savepoint), optional state, command ack;
  reply: commands with id > ack, settings when the rev differs, ack_reset.
  One advisory lock per station serialises sync and `add_command`.
- `POST /api/v1/core/library` after every scan (bulk `execute_values`);
  reply: `learned` {key: {trail, lead} | {unsupported}} for the Core.
- Ads: the server sends `ads` {due, mix, next, next_why, ident, no_bumpers} when `ads_rev` differs
  (`ads.py`); the Core only plays the plan (without one, every ad is a mix-in).
  `next` is the song to play next, picked on the server (`next_song`: least
  recently played, or by length to land the break on an ad slot); the Core
  takes it after the operator queue, once, and uses its own shuffle only when
  the server has named nothing.

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
