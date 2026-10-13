"""QSYSRadio Flask app (served by gunicorn via start.sh).

Pages and the /api/v1 endpoints they call need a shared sign-in (auth.py).
The Core's endpoints (/api/v1/core/*) need X-Radio-Token = RADIO_API_TOKEN.
/api/v1/health is open (install checks). Each request uses at most one pooled
database connection (dbconn()), returned when the request ends.
"""
import hmac
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

from . import auth, config, control, db, logs, plays
from .events import BadRequest
from .migrate import discover

VERSION = config.version()
log = logs.setup()

app = Flask(__name__)
app.config['SECRET_KEY'] = config.get('SECRET_KEY') or None


def dbconn():
    """This request's pooled connection (borrowed on first use)."""
    if 'db' not in g:
        g.db = db.borrow()
    return g.db


@app.teardown_appcontext
def _give_back(_exc):
    conn = g.pop('db', None)
    if conn is not None:
        db.give_back(conn)


auth.configure(app, dbconn)


def write_logs(lines):
    for item in lines or ():
        if item:
            log.log(*item)


# ── Errors ───────────────────────────────────────────────────────────────────

def _error(status, title, message):
    if request.path.startswith('/api/'):
        return jsonify(ok=False, error=message), status
    return render_template('error.html', title=title, message=message), status


@app.errorhandler(db.DatabaseUnavailable)
def _db_unavailable(exc):
    log.error('database unavailable: %s', exc)
    return _error(503, 'Database unavailable', 'The radio server cannot reach its database. Reload in a moment.')


@app.errorhandler(auth.SignInUnavailable)
def _signin_unavailable(exc):
    return _error(503, 'Sign-in unavailable', str(exc))


@app.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify(ok=False, error=str(exc)), 400


@app.errorhandler(403)
def _forbidden(_exc):
    return _error(403, 'Not allowed', g.get('deny_message') or 'Your account does not have permission for that.')


@app.before_request
def _session_unavailable():
    if getattr(session, 'unavailable', False):
        raise db.DatabaseUnavailable('could not load the session')
    if getattr(session, 'broken', None):
        raise auth.SignInUnavailable(session.broken)


@app.context_processor
def _inject():
    try:
        user = auth.current_user()
    except (db.DatabaseUnavailable, auth.SignInUnavailable):   # the error page itself must still render
        user = None
    return {'version': VERSION, 'user': user}


# ── Request helpers ──────────────────────────────────────────────────────────

def _arg(name, default=None):
    v = request.args.get(name)
    return v.replace('\x00', '').strip() if v else default


def _int_arg(name, default, lo, hi):
    try:
        return min(hi, max(lo, int(request.args.get(name, default))))
    except (TypeError, ValueError, OverflowError):
        return default


def _json_body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise BadRequest('body must be a JSON object')
    return payload


def _station(name=None):
    return control.resolve_station(dbconn(), name if isinstance(name, str) else _arg('station'))


_versions_seen = set()


def core_endpoint(f):
    """The Core's endpoints: X-Radio-Token must match RADIO_API_TOKEN. Running
    without a token needs an explicit RADIO_ALLOW_NO_TOKEN=1 (lab use)."""
    @wraps(f)
    def w(*a, **k):
        token = config.get('RADIO_API_TOKEN')
        if not token:
            if (config.get('RADIO_ALLOW_NO_TOKEN') or '') != '1':
                log.error('%s: refused, RADIO_API_TOKEN is not set in .env', request.path)
                return jsonify(ok=False, error='server has no RADIO_API_TOKEN configured'), 403
        elif not hmac.compare_digest(request.headers.get('X-Radio-Token', '').encode(), token.encode()):
            log.warning('%s: rejected request from %s (bad or missing X-Radio-Token)', request.path, request.remote_addr)
            return jsonify(ok=False, error='bad token'), 403
        return f(*a, **k)
    return w


# ── Pages ────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    nxt = request.values.get('next') or ''
    if not nxt.startswith('/') or nxt.startswith('//') or '\\' in nxt:
        nxt = url_for('dashboard')
    if auth.current_user() and not auth.current_user()['deny']:
        return redirect(nxt)
    error, username = None, ''
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:     # login CSRF
            return _error(403, 'Not allowed', 'Sign in from the QSYSRadio page.')
        username = request.form.get('username', '').strip()[:200]
        ok, error = auth.attempt_login(username, request.form.get('password', ''))
        if ok:
            return redirect(nxt)
    return render_template('login.html', error=error, username=username, next=nxt), (401 if error else 200)


@app.get('/logout')
def logout():
    if session.get('username'):
        log.info('logout user=%s', session.get('username'))
    session.clear()        # deletes the shared row: signs out of the sister apps too
    return redirect(url_for('login'))


def _page(template, name):
    return render_template(template, page=name)


@app.get('/')
@auth.login_required
def dashboard():
    return _page('dashboard.html', 'dashboard')


@app.get('/library')
@auth.login_required
def library_page():
    return _page('library.html', 'library')


@app.get('/log')
@auth.login_required
def log_page():
    return _page('log.html', 'log')


@app.get('/settings')
@auth.admin_required
def settings_page():
    return _page('settings.html', 'settings')


# ── Core API (token) ─────────────────────────────────────────────────────────

@app.get('/api/v1/health')
def health():
    """Version + schema state; 503 when PostgreSQL is down."""
    with dbconn().cursor() as cur:
        cur.execute('SELECT COALESCE(MAX(version), 0) FROM schema_migrations')
        schema_version = cur.fetchone()[0]
    expected = (discover() or [(0,)])[-1][0]
    return jsonify(ok=schema_version == expected, app='qsysradio', version=VERSION,
                   schema=db.schemas()[0], schema_version=schema_version, schema_expected=expected)


@app.post('/api/v1/core/sync')
@core_endpoint
def core_sync():
    """The Core's once-a-second request: events in, commands and settings out."""
    payload = _json_body()
    reply, lines = control.core_sync(dbconn(), payload, datetime.now(timezone.utc))
    write_logs(lines)
    seen = (payload.get('station'), payload.get('version'))
    if isinstance(seen[1], str) and seen not in _versions_seen:
        if len(_versions_seen) > 100:
            _versions_seen.clear()
        _versions_seen.add(seen)
        if seen[1] != VERSION:
            log.warning('station=%s runs radio.lua %s, server is %s', seen[0], seen[1], VERSION)
    return jsonify(reply)


@app.post('/api/v1/core/library')
@core_endpoint
def core_library():
    count, line = control.core_library(dbconn(), _json_body())
    write_logs([line])
    return jsonify(ok=True, items=count)


# ── Web API (signed in) ──────────────────────────────────────────────────────

@app.get('/api/v1/now')
@auth.login_required
def get_now():
    """Status poll for every page: the station's live state + recent commands,
    the station list, and (plays=N) on-air / recent plays."""
    conn, station = dbconn(), _station()
    out = {'ok': True, 'stations': control.stations(conn), 'station': None, 'on_air': [], 'recent': []}
    if station:
        out['station'] = control.status(conn, station)
        n = _int_arg('plays', 0, 0, 200)
        if n:
            out['on_air'], out['recent'] = plays.recent(conn, station, n)
    return jsonify(out)


@app.post('/api/v1/control')
@auth.operator_required
def post_control():
    body = _json_body()
    station = _station(body.get('station'))
    if not station:
        raise BadRequest('no station has connected yet')
    cid, line = control.add_command(dbconn(), station, str(body.get('cmd') or ''), body.get('args'),
                                    auth.current_user())
    write_logs([line])
    return jsonify(ok=True, id=cid)


@app.get('/api/v1/library')
@auth.login_required
def get_library():
    station = _station()
    if not station:
        return jsonify(ok=True, total=0, items=[])
    total, items = control.library(dbconn(), station, q=_arg('q'), kind=_arg('kind'), sort=_arg('sort', 'artist'),
                                   limit=_int_arg('limit', 200, 1, 1000), offset=_int_arg('offset', 0, 0, 10 ** 7))
    return jsonify(ok=True, station=station, total=total, items=items)


@app.get('/api/v1/settings')
@auth.login_required
def get_settings():
    return jsonify(ok=True, **control.settings_view(dbconn(), _station()))


@app.post('/api/v1/settings')
@auth.admin_required
def post_settings():
    changed, line = control.save_settings(dbconn(), _json_body().get('settings'), auth.current_user())
    write_logs([line])
    return jsonify(ok=True, changed=changed)


def _play_query(fn, *args):
    station = _station()
    if not station:
        return []
    try:
        return fn(dbconn(), station, *args)
    except db.psycopg2.DataError:
        raise BadRequest('times must look like 2026-10-12T14:00 (optionally with a zone)')


@app.get('/api/v1/plays')
@auth.login_required
def get_plays():
    """?from=&to= (ISO; default the last 24 h) &q= &kind=music|imaging &station= &limit="""
    now = datetime.now(timezone.utc)
    kind = _arg('kind')
    if kind not in (None, 'music', 'imaging'):
        raise BadRequest('kind must be music or imaging')
    rows = _play_query(plays.between, _arg('from', (now - timedelta(days=1)).isoformat()),
                       _arg('to', (now + timedelta(minutes=1)).isoformat()), _arg('q'), kind,
                       _int_arg('limit', 500, 1, 5000))
    return jsonify(ok=True, plays=rows)


@app.get('/api/v1/plays/at')
@auth.login_required
def get_plays_at():
    """?t=2026-10-12T14:03 -> what was on air then."""
    when = _arg('t')
    if not when:
        raise BadRequest('t is required')
    return jsonify(ok=True, t=when, plays=_play_query(plays.at, when))
