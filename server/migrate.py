"""Database migrations: numbered SQL files, applied once, in order.

    python -m server.migrate            apply pending migrations
    python -m server.migrate --status   list applied / pending, change nothing

start.sh runs this before every service start, so an update is just
`git pull && sudo systemctl restart qsysradio`.

Rules (see CLAUDE.md):
- One file per change: server/migrations/NNNN_short_name.sql. Never edit or
  renumber a file once it is on main; add a new one.
- Each file runs in ONE transaction with search_path set to the app schema
  only, so every object lands in PG_APP_SCHEMA. If any statement fails the
  whole file rolls back and the service does not start (fail loud).
- The file is sent to PostgreSQL as-is (no parameters, no splitting on ';').
- Applied versions are recorded in <app schema>.schema_migrations. A
  session advisory lock serialises concurrent starts (several servers can
  share one database).
"""
import hashlib
import re
import sys
from pathlib import Path

from . import db

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
_FILE = re.compile(r'^(\d{4})_([a-z0-9_]+)\.sql$')


def discover():
    """[(version, name, path, sha256)] sorted by version."""
    found = {}
    for path in sorted(MIGRATIONS_DIR.glob('*.sql')):
        m = _FILE.match(path.name)
        if not m:
            raise SystemExit(f'migrate: bad migration file name {path.name} '
                             f'(expected NNNN_name.sql)')
        version = int(m.group(1))
        if version in found:
            raise SystemExit(f'migrate: duplicate migration number {version:04d}')
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        found[version] = (version, m.group(2), path, sha)
    return [found[v] for v in sorted(found)]


def _schema_exists(cur, schema):
    cur.execute('SELECT 1 FROM pg_namespace WHERE nspname = %s', (schema,))
    return cur.fetchone() is not None


def _table_exists(cur, schema, table):
    cur.execute('SELECT to_regclass(%s) IS NOT NULL', (f'"{schema}"."{table}"',))
    return cur.fetchone()[0]


def _applied(cur, schema):
    if not _table_exists(cur, schema, 'schema_migrations'):
        return {}
    cur.execute(f'SELECT version, name, checksum FROM "{schema}".schema_migrations')
    return {r['version']: r for r in cur.fetchall()}


def status():
    app_schema, _ = db.schemas()
    conn = db.connect(search_path=False)
    try:
        with conn.cursor() as cur:
            applied = _applied(cur, app_schema) if _schema_exists(cur, app_schema) else {}
        conn.rollback()
    finally:
        conn.close()
    print(f'schema "{app_schema}"')
    for version, name, _path, sha in discover():
        row = applied.get(version)
        if row is None:
            state = 'pending'
        elif row['checksum'] != sha:
            state = 'applied (file changed since)'
        else:
            state = 'applied'
        print(f'  {version:04d}_{name}: {state}')


def run(log=print):
    """Apply every pending migration. Returns the number applied."""
    app_schema, _ = db.schemas()
    migrations = discover()
    conn = db.connect(search_path=False)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Create the app schema only when missing, so a PG user without
            # CREATE on the database still works once a DBA has made it.
            if not _schema_exists(cur, app_schema):
                cur.execute(f'CREATE SCHEMA "{app_schema}"')
                log(f'migrate: created schema "{app_schema}"')
            cur.execute('SELECT pg_advisory_lock(hashtext(%s))', (f'qsysradio:migrate:{app_schema}',))
            try:
                if not _table_exists(cur, app_schema, 'schema_migrations'):
                    cur.execute(f'''CREATE TABLE IF NOT EXISTS "{app_schema}".schema_migrations (
                        version    INTEGER PRIMARY KEY,
                        name       TEXT NOT NULL,
                        checksum   TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
                applied = _applied(cur, app_schema)
                count = 0
                for version, name, path, sha in migrations:
                    row = applied.get(version)
                    if row is not None:
                        if row['checksum'] != sha:
                            log(f'migrate: WARNING {path.name} changed after it was applied; '
                                f'not re-run. Put the change in a new migration.')
                        continue
                    sql = path.read_text(encoding='utf-8')
                    cur.execute('BEGIN')
                    try:
                        cur.execute(f'SET LOCAL search_path TO "{app_schema}"')
                        cur.execute(sql)
                        cur.execute(f'INSERT INTO "{app_schema}".schema_migrations '
                                    f'(version, name, checksum) VALUES (%s, %s, %s)',
                                    (version, name, sha))
                        cur.execute('COMMIT')
                    except Exception:
                        cur.execute('ROLLBACK')
                        log(f'migrate: FAILED {path.name}; rolled back')
                        raise
                    log(f'migrate: applied {path.name}')
                    count += 1
                latest = migrations[-1][0] if migrations else 0
                log(f'migrate: schema "{app_schema}" at version {latest:04d}'
                    + ('' if count else ' (nothing to do)'))
                return count
            finally:
                cur.execute('SELECT pg_advisory_unlock(hashtext(%s))', (f'qsysradio:migrate:{app_schema}',))
    finally:
        conn.close()


def main(argv):
    try:
        if '--status' in argv:
            status()
        else:
            run()
    except db.DatabaseUnavailable as exc:
        sys.exit(f'migrate: {exc}')


if __name__ == '__main__':
    main(sys.argv[1:])
