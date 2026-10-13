"""Shared sign-on with the other apps (321Theater, Leash, FetchLog, ...).

Implements the family's SHARED_AUTH.md: sessions live in shared.app_sessions
(the `session` cookie holds only a random id), users are READ from
shared.users (321Theater owns writes), passwords are checked with Werkzeug.
Signing in here signs you in to the sister apps on the same host; signing out
signs you out of all of them.

QSYSRadio only reads shared.users and reads/writes shared.app_sessions rows;
it never creates or alters anything in the shared schema (321Theater owns it).
If those tables are missing or not granted, sign-in answers 503 with a clear
message and migrate.py logs the exact GRANT that is missing.

A session may have been created by a sister app, so QSYSRadio never trusts
the shared keys for its own permissions: it re-reads the user and keeps its
verdict under its own session key ('qsysradio'), refreshed every 5 minutes.
A user without QSYSRadio access gets 403 here and stays signed in elsewhere.

  sign in    is_app_user OR is_app_admin OR role 'admin' (and no pending password change)
  watch      everyone allowed in
  operate    allowed in and not is_readonly   (skip, queue, stop, start, ...)
  settings   is_app_admin OR role 'admin'
"""
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, g, jsonify, redirect, request, session, url_for
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict
from werkzeug.security import check_password_hash

from . import config, db
from .logs import log

_SID_RE = re.compile(r'^[A-Za-z0-9_-]{20,128}$')
_DUMMY_HASH = 'scrypt:32768:8:1$dummy$' + '0' * 64   # same work as a real check (no user enumeration)
RECHECK_S = 300
LOGIN_LIMIT, LOGIN_WINDOW_S = 15, 60                  # per worker process
NO_SESSION_PATHS = ('/static/', '/api/v1/core/', '/api/v1/health')
_USER_COLS = ('id, username, password_hash, display_name, role, must_change_password, '
              'is_readonly, is_app_user, is_app_admin')

_conn = None                                          # the request's DB connection (set by configure)
# the shared tables are missing or not granted to this app's role
_SHARED_ERRORS = (db.psycopg2.errors.UndefinedTable, db.psycopg2.errors.InsufficientPrivilege)


class SignInUnavailable(RuntimeError):
    pass


def _shared_failed(exc):
    _conn().rollback()
    log.error('sign-in unavailable: %s (see the "sign-in will not work" lines from migrate at startup)',
              str(exc).strip().splitlines()[0])
    return SignInUnavailable('Sign-in is not available: QSYSRadio cannot use the shared user tables. '
                             'An administrator should check the server log.')


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)   # app_sessions stores naive UTC


def configure(app, get_conn):
    global _conn
    _conn = get_conn
    app.config.update(
        SESSION_COOKIE_NAME='session', SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_SECURE=(config.get('SESSION_COOKIE_SECURE', '') or '').lower() in ('1', 'true', 'yes'),
        SESSION_COOKIE_DOMAIN=config.get('SESSION_COOKIE_DOMAIN') or None)
    app.session_interface = DBSessionInterface()


# ── Server-side sessions in shared.app_sessions ──────────────────────────────

class _DBSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None, new=False, skip=False, unavailable=False, broken=None):
        def _on_update(s):
            s.modified = True
        CallbackDict.__init__(self, initial, _on_update)
        self.sid, self.new, self.modified = sid, new, False
        self.skip, self.unavailable, self.broken = skip, unavailable, broken


class DBSessionInterface(SessionInterface):
    def open_session(self, app, request):
        if request.path.startswith(NO_SESSION_PATHS):
            return _DBSession(skip=True)
        sid = request.cookies.get(app.config['SESSION_COOKIE_NAME'])
        if not sid or not _SID_RE.match(sid):
            return _DBSession(sid=secrets.token_urlsafe(32), new=True)
        try:
            with _conn().cursor() as cur:
                cur.execute('SELECT data, expires_at FROM app_sessions WHERE sid = %s', (sid,))
                row = cur.fetchone()
                if row and row['expires_at'] < utcnow():
                    cur.execute('DELETE FROM app_sessions WHERE sid = %s', (sid,))
                    _conn().commit()
                    row = None
        except db.DatabaseUnavailable:
            return _DBSession(skip=True, unavailable=True)    # app.py answers 503
        except _SHARED_ERRORS as exc:
            return _DBSession(skip=True, broken=str(_shared_failed(exc)))
        if not row:   # never adopt an unknown client-supplied sid (session fixation)
            return _DBSession(sid=secrets.token_urlsafe(32), new=True)
        try:
            data = json.loads(row['data'] or '{}')
        except ValueError:
            data = {}
        return _DBSession(data if isinstance(data, dict) else {}, sid=sid)

    def save_session(self, app, session, response):
        if session is None or session.skip:
            return
        domain, path = self.get_cookie_domain(app), self.get_cookie_path(app)
        name = app.config['SESSION_COOKIE_NAME']
        try:
            if not session:
                if session.modified:      # cleared (logout): drop the shared row + cookie
                    with _conn().cursor() as cur:
                        cur.execute('DELETE FROM app_sessions WHERE sid = %s', (session.sid,))
                    _conn().commit()
                    response.delete_cookie(name, domain=domain, path=path)
                return
            if not session.modified and not session.new:
                return
            expires = utcnow() + app.permanent_session_lifetime
            with _conn().cursor() as cur:
                cur.execute("""INSERT INTO app_sessions (sid, user_id, data, last_seen, expires_at)
                               VALUES (%s, %s, %s, %s, %s)
                               ON CONFLICT (sid) DO UPDATE SET user_id = EXCLUDED.user_id, data = EXCLUDED.data,
                                   last_seen = EXCLUDED.last_seen, expires_at = EXCLUDED.expires_at""",
                            (session.sid, session.get('user_id'), json.dumps(dict(session), default=str),
                             utcnow(), expires))
            _conn().commit()
        except db.DatabaseUnavailable:
            log.error('session not saved: database unavailable')
            return
        except _SHARED_ERRORS as exc:
            _shared_failed(exc)
            return
        response.set_cookie(name, session.sid, expires=expires, httponly=self.get_cookie_httponly(app),
                            domain=domain, path=path, secure=self.get_cookie_secure(app),
                            samesite=self.get_cookie_samesite(app))


# ── Users and QSYSRadio's own permission check ───────────────────────────────

def _get_user(where, value):
    try:
        with _conn().cursor() as cur:
            cur.execute(f'SELECT {_USER_COLS} FROM users WHERE {where} = %s LIMIT 1', (value,))
            row = cur.fetchone()
    except _SHARED_ERRORS as exc:
        raise _shared_failed(exc)
    return dict(row) if row else None


def _verdict(u):
    allowed = bool(u.get('is_app_user') or u.get('is_app_admin') or u.get('role') == 'admin')
    why = None
    if not allowed:
        why = 'Your account does not have access to QSYSRadio. Ask an administrator to enable it.'
    elif u.get('must_change_password'):
        why = 'You must change your password in 321Theater before using QSYSRadio.'
    return {'uid': u['id'], 'at': time.time(), 'deny': why, 'name': u.get('display_name') or u['username'],
            'operate': allowed and not u.get('is_readonly', 1),
            'admin': bool(u.get('is_app_admin') or u.get('role') == 'admin')}


def current_user():
    """The signed-in user with QSYSRadio permissions, or None. Cached per request."""
    if 'qsysradio_user' in g:
        return g.qsysradio_user
    user = None
    if 'user_id' in session:
        v = session.get('qsysradio')
        if not isinstance(v, dict) or v.get('uid') != session['user_id'] or time.time() - v.get('at', 0) > RECHECK_S:
            u = _get_user('id', session['user_id'])
            if u is None:                 # account deleted: the shared session is void everywhere
                session.clear()
            else:
                v = session['qsysradio'] = _verdict(u)
        if 'user_id' in session:
            user = {'user_id': session['user_id'], 'username': session.get('username'), 'display_name': v['name'],
                    'deny': v['deny'], 'can_operate': bool(v['operate']), 'is_admin': bool(v['admin'])}
    g.qsysradio_user = user
    return user


_attempts, _attempts_lock = {}, threading.Lock()


def _rate_limited(ip):
    t = time.monotonic()
    with _attempts_lock:
        if len(_attempts) > 5000:
            _attempts.clear()
        recent = [x for x in _attempts.get(ip, []) if t - x < LOGIN_WINDOW_S] + [t]
        _attempts[ip] = recent
        return len(recent) > LOGIN_LIMIT


def attempt_login(username, password):
    """Returns (ok, message). On success the session is populated."""
    if _rate_limited(request.remote_addr or '-'):
        return False, 'Too many sign-in attempts. Wait a minute and try again.'
    try:
        user = _get_user('username', username)
    except SignInUnavailable as exc:
        return False, str(exc)
    ok = check_password_hash(user['password_hash'] if user else _DUMMY_HASH, password) and user is not None
    if not ok:
        log.warning('login failed user=%s ip=%s', username[:100], request.remote_addr)
        return False, 'Invalid username or password.'
    verdict = _verdict(user)
    if verdict['deny']:
        log.warning('login refused user=%s: %s', username, verdict['deny'])
        return False, verdict['deny']
    session.clear()                               # session-fixation defense + fresh sid
    session.sid, session.new = secrets.token_urlsafe(32), True
    session.update(user_id=user['id'], username=user['username'], display_name=verdict['name'],
                   user_role=user['role'], role=user['role'],       # both keys: the family reads either
                   is_readonly=bool(user.get('is_readonly')), is_app_user=bool(user.get('is_app_user')),
                   is_app_admin=bool(user.get('is_app_admin')), _role_checked_at=utcnow().timestamp(),
                   qsysradio=verdict)
    session.permanent = True
    log.info('login user=%s ip=%s', username, request.remote_addr)
    return True, ''


# ── Route guards ─────────────────────────────────────────────────────────────

def _is_api():
    return request.path.startswith('/api/')


def _deny(status, message):
    if _is_api():
        return jsonify(ok=False, error=message), status
    g.deny_message = message
    abort(status)


def _guard(level):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current_user()
            if u is None:
                if _is_api():
                    return jsonify(ok=False, error='sign in required'), 401
                return redirect(url_for('login', next=request.full_path.rstrip('?')))
            if u['deny']:
                return _deny(403, u['deny'])
            if level == 'operate' and not u['can_operate']:
                return _deny(403, 'Your account is read-only.')
            if level == 'admin' and not u['is_admin']:
                return _deny(403, 'Administrators only.')
            # state-changing calls must come from our own page (CSRF)
            if request.method == 'POST' and request.headers.get('X-Requested-With') != 'QSYSRadio':
                return _deny(403, 'bad request origin')
            return f(*a, **k)
        return w
    return deco


login_required = _guard('view')
operator_required = _guard('operate')
admin_required = _guard('admin')
