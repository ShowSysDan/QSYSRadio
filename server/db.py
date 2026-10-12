"""PostgreSQL access. PostgreSQL only, its own schema, fail loud.

QSYSRadio shares a database with the other apps and keeps everything in its
own schema (PG_APP_SCHEMA, default `qsysradio`). The cross-app `shared`
schema (PG_SHARED_SCHEMA) sits second on the search_path so the shared users
table can be read once the web UI has logins; this app never creates tables
there. Unqualified table names therefore resolve to the app schema.

connect() raises DatabaseUnavailable when PG is unconfigured or unreachable.
Never catch it and carry on with made-up data.
"""
import re

import psycopg2
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


def connect(search_path=True, connect_timeout=10):
    """A psycopg2 connection with DictCursor rows. With search_path=True the
    session search_path is "<app>", "<shared>" from the first statement."""
    if not is_configured():
        raise DatabaseUnavailable(
            f'PostgreSQL is not configured: set PG_HOST / PG_DBNAME / PG_USER / '
            f'PG_PASSWORD in {config.ENV_PATH} (see .env.example)')
    kwargs = dict(settings(), connect_timeout=connect_timeout,
                  application_name='qsysradio',
                  cursor_factory=psycopg2.extras.DictCursor)
    options = []
    if search_path:
        app_schema, shared_schema = schemas()
        options.append(f'-c search_path="{app_schema}","{shared_schema}"')
    tz = config.get('TIMEZONE')
    if tz:
        if not _TZ.match(tz):
            raise ValueError(f'TIMEZONE {tz!r} is not a time zone name (e.g. America/New_York)')
        options.append(f'-c TimeZone={tz}')
    if options:
        kwargs['options'] = ' '.join(options)
    try:
        return psycopg2.connect(**kwargs)
    except psycopg2.OperationalError as exc:
        raise DatabaseUnavailable(f'PostgreSQL unreachable: {exc}'.strip()) from exc
