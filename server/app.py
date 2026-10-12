"""QSYSRadio Flask app (served by gunicorn via start.sh)."""
import hmac
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

from . import config, db, events, logs, plays
from .migrate import discover

VERSION = config.version()
log = logs.setup()

app = Flask(__name__)
app.config['SECRET_KEY'] = config.get('SECRET_KEY') or None

_versions_seen = set()


@app.errorhandler(db.DatabaseUnavailable)
def _db_unavailable(exc):
    log.error('database unavailable: %s', exc)
    return jsonify(ok=False, error='database unavailable'), 503


@app.errorhandler(events.BadRequest)
def _bad_request(exc):
    return jsonify(ok=False, error=str(exc)), 400


def _query(sql_fn, *args, **kwargs):
    conn = db.connect()
    try:
        return sql_fn(conn, *args, **kwargs)
    finally:
        conn.close()


@app.get('/api/v1/health')
def health():
    """Version + schema state; 503 when PostgreSQL is down."""
    app_schema, _ = db.schemas()

    def read(conn):
        with conn.cursor() as cur:
            cur.execute('SELECT COALESCE(MAX(version), 0) FROM schema_migrations')
            return cur.fetchone()[0]
    schema_version = _query(read)
    migrations = discover()
    expected = migrations[-1][0] if migrations else 0
    return jsonify(ok=schema_version == expected, app='qsysradio', version=VERSION,
                   schema=app_schema, schema_version=schema_version,
                   schema_expected=expected)


@app.post('/api/v1/events')
def post_events():
    """Event batches from the Core (radio.lua Server module)."""
    token = config.get('RADIO_API_TOKEN')
    if token and not hmac.compare_digest(request.headers.get('X-Radio-Token', ''), token):
        log.warning('events: rejected POST from %s (bad or missing X-Radio-Token)', request.remote_addr)
        return jsonify(ok=False, error='bad token'), 403
    payload = request.get_json(silent=True)
    if payload is None:
        raise events.BadRequest('body must be JSON')
    station, radio_version, accepted, dupes = _query(
        events.ingest, payload, datetime.now(timezone.utc))
    if radio_version and (station, radio_version) not in _versions_seen:
        _versions_seen.add((station, radio_version))
        if radio_version != VERSION:
            log.warning('station=%s runs radio.lua %s, server is %s', station, radio_version, VERSION)
    return jsonify(ok=True, accepted=accepted, duplicates=dupes)


def _limit(default, cap):
    try:
        return max(1, min(cap, int(request.args.get('limit', default))))
    except ValueError:
        return default


@app.get('/api/v1/plays')
def get_plays():
    """?from=&to= (ISO; default the last 24 h) &q= &kind=music|imaging &station= &limit="""
    now = datetime.now(timezone.utc)
    start = request.args.get('from') or (now - timedelta(days=1)).isoformat()
    end = request.args.get('to') or (now + timedelta(minutes=1)).isoformat()
    kind = request.args.get('kind')
    if kind not in (None, '', 'music', 'imaging'):
        raise events.BadRequest('kind must be music or imaging')
    try:
        rows = _query(plays.between, start, end, search=request.args.get('q') or None,
                      kind=kind or None, station=request.args.get('station') or None,
                      limit=_limit(500, 5000))
    except db.psycopg2.DataError:
        raise events.BadRequest('from/to must be dates or timestamps, e.g. 2026-10-12T14:00')
    return jsonify(ok=True, plays=rows)


@app.get('/api/v1/plays/at')
def get_plays_at():
    """?t=2026-10-12T14:03 -> what was on air then."""
    when = request.args.get('t')
    if not when:
        raise events.BadRequest('t is required')
    try:
        rows = _query(plays.at, when)
    except db.psycopg2.DataError:
        raise events.BadRequest('t must be a timestamp, e.g. 2026-10-12T14:03')
    return jsonify(ok=True, t=when, plays=rows)


@app.get('/api/v1/now')
def get_now():
    """On air now + recent history (cheap; safe to poll from many browsers)."""
    on_air, recent = _query(plays.recent, limit=_limit(20, 200))
    return jsonify(ok=True, on_air=on_air, recent=recent)
