"""PostgreSQL access. PostgreSQL only, its own schema, pooled, fail loud.

QSYSRadio shares a database with the other apps and keeps everything in its
own schema (PG_APP_SCHEMA, default `qsysradio`). The cross-app `shared`
schema (PG_SHARED_SCHEMA) sits second on the search_path for the shared
users / sessions tables; this app never creates or alters anything there
except shared.app_sessions rows (sign-in). Unqualified table names resolve to
the app schema.

Web requests borrow ONE pooled connection each (app.py: dbconn()); CLI tools
and migrations use connect(). Both raise DatabaseUnavailable when PG is
unconfigured or unreachable. Never catch that and carry on with made-up data.
"""
import math
import os
import re
import threading
import time

import psycopg2
import psycopg2.errors
import psycopg2.extras

from . import config

DEFAULT_APP_SCHEMA = 'qsysradio'
DEFAULT_SHARED_SCHEMA = 'shared'

_IDENT = re.compile(r'^[a-z_][a-z0-9_]{0,62}$')
_TZ = re.compile(r'^[A-Za-z][A-Za-z0-9_+\-/]{0,63}$')


class DatabaseUnavailable(RuntimeError):
    pass


def _identifier(name, what):
    if not _IDENT.match(name or ''):
        raise ValueError(f'{what} {name!r} is not a valid schema name '
                         f'(lowercase letters, digits, underscore)')
    return name


def settings():
    return {
        'host': config.get('PG_HOST', 'localhost'),
        'port': config.get('PG_PORT', '5432'),
        'dbname': config.get('PG_DBNAME', ''),
        'user': config.get('PG_USER', ''),
        'password': config.get('PG_PASSWORD', ''),
    }


def schemas():
    """(app_schema, shared_schema), validated."""
    return (_identifier(config.get('PG_APP_SCHEMA', DEFAULT_APP_SCHEMA), 'PG_APP_SCHEMA'),
            _identifier(config.get('PG_SHARED_SCHEMA', DEFAULT_SHARED_SCHEMA), 'PG_SHARED_SCHEMA'))


def is_configured():
    s = settings()
    return bool(s['dbname'] and s['user'])


def _connect_kwargs(search_path=True, connect_timeout=10):
    if not is_configured():
        raise DatabaseUnavailable(
            f'PostgreSQL is not configured: set PG_HOST / PG_DBNAME / PG_USER / '
            f'PG_PASSWORD in {config.ENV_PATH} (see .env.example)')
    options = []
    if search_path:
        app_schema, shared_schema = schemas()
        options.append(f'-c search_path="{app_schema}","{shared_schema}"')
    tz = config.get('TIMEZONE')
    if tz:
        if not _TZ.match(tz):
            raise ValueError(f'TIMEZONE {tz!r} is not a time zone name (e.g. America/New_York)')
        options.append(f'-c TimeZone={tz}')
    kwargs = dict(settings(), connect_timeout=connect_timeout, application_name='qsysradio',
                  cursor_factory=psycopg2.extras.DictCursor)
    if options:
        kwargs['options'] = ' '.join(options)
    return kwargs


def _unavailable(exc):
    """DatabaseUnavailable for a failed connect, naming the fix when it is a missing grant."""
    msg = f'PostgreSQL unreachable: {exc}'.strip()
    if 'does not have CONNECT privilege' in str(exc):
        s = settings()
        msg += (f'\nAs the PostgreSQL superuser (on the primary): '
                f'GRANT CONNECT ON DATABASE "{s["dbname"]}" TO "{s["user"]}";')
    return DatabaseUnavailable(msg)


def connect(search_path=True, connect_timeout=10):
    """A new, unpooled connection (migrations, CLI tools)."""
    try:
        return psycopg2.connect(**_connect_kwargs(search_path, connect_timeout))
    except psycopg2.OperationalError as exc:
        raise _unavailable(exc) from exc


# ── Pool (one per worker process, created lazily after gunicorn forks) ───────
# Up to WEB_THREADS + 2 connections open per worker; up to PG_POOL_MAX_IDLE
# (default 4) kept for reuse. Mind PostgreSQL's max_connections across the
# workers and the sister apps sharing the server.
PING_AFTER_S = 10          # a connection idle longer than this is checked before use


class _Pool:
    def __init__(self):
        self.lock = threading.Lock()
        self.idle = []                 # [(conn, returned_at)]
        self.out = 0
        self.max_open = config.get_int('WEB_THREADS', 8) + 2
        self.max_idle = config.get_int('PG_POOL_MAX_IDLE', 4)
        self.kwargs = _connect_kwargs()

    def get(self):
        while True:
            with self.lock:
                if self.idle:
                    conn, at = self.idle.pop()
                    self.out += 1
                elif self.out >= self.max_open:
                    raise DatabaseUnavailable(f'all {self.max_open} database connections are busy')
                else:
                    self.out += 1
                    conn, at = None, None
            if conn is None:
                try:
                    return psycopg2.connect(**self.kwargs)
                except psycopg2.OperationalError as exc:
                    self.put(None)
                    raise _unavailable(exc) from exc
            if not conn.closed and (time.monotonic() - at < PING_AFTER_S or _alive(conn)):
                return conn
            self.put(conn, close=True)    # dead (e.g. PostgreSQL restarted): try another

    def put(self, conn, close=False):
        with self.lock:
            self.out -= 1
            if conn is not None and not close and not conn.closed and len(self.idle) < self.max_idle:
                self.idle.append((conn, time.monotonic()))
                return
        if conn is not None and not conn.closed:
            conn.close()


def _alive(conn):
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT 1')
        conn.rollback()
        return True
    except psycopg2.Error:
        return False


_pool, _pool_pid, _pool_lock = None, None, threading.Lock()


def _get_pool():
    global _pool, _pool_pid
    if _pool is None or _pool_pid != os.getpid():
        with _pool_lock:
            if _pool is None or _pool_pid != os.getpid():
                _pool, _pool_pid = _Pool(), os.getpid()
    return _pool


def borrow():
    """A pooled connection for this request; hand it back with give_back()."""
    return _get_pool().get()


def give_back(conn):
    """Roll back anything uncommitted and return the connection (or drop it if broken)."""
    broken = bool(conn.closed)
    if not broken:
        try:
            conn.rollback()
        except psycopg2.Error:
            broken = True
    _get_pool().put(conn, close=broken)


# ── Small shared helpers ─────────────────────────────────────────────────────

def text(value, limit=1000):
    """A clean string for the database: no NUL bytes, bounded; '' and None -> None."""
    if value is None or isinstance(value, (dict, list)):
        return None
    s = str(value).replace('\x00', '')[:limit]
    return s or None


def number(value):
    """A finite float or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def clean_json(value, depth=0):
    """Make Core-supplied JSON safe for JSONB: no NUL, no NaN/Infinity, bounded depth."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {text(k, 100) or '_': clean_json(v, depth + 1) for k, v in list(value.items())[:200]}
    if isinstance(value, list):
        return [clean_json(v, depth + 1) for v in value[:500]]
    if isinstance(value, str):
        return value.replace('\x00', '')[:2000]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53:
        return None
    return value


def like(q):
    """ILIKE pattern for a user's search text (wildcards escaped)."""
    q = q.replace('\x00', '')
    return '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def iso(value):
    return value.isoformat() if value is not None else None
