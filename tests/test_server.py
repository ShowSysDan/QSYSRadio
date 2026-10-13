"""Server tests against a real PostgreSQL (no SQLite stand-in).

    QSYSRADIO_ENV_FILE=/path/to/test.env venv/bin/python -m pytest tests/

The .env must point at a database where PG_USER may CREATE schemas. The
tests use their own schemas (qsr_test / qsr_test_shared), dropped afterwards,
so they never touch the real qsysradio or shared schemas. Skipped when no
database is configured.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ.update(PG_APP_SCHEMA='qsr_test', PG_SHARED_SCHEMA='qsr_test_shared', RADIO_API_TOKEN='tok',
                  WEB_THREADS='8', SYSLOG_HOST='')
os.environ.pop('RADIO_ALLOW_NO_TOKEN', None)

from server import config, control, core_settings, db, logs, migrate  # noqa: E402

if not db.is_configured():
    pytest.skip('no test database configured (QSYSRADIO_ENV_FILE)', allow_module_level=True)

from werkzeug.security import generate_password_hash  # noqa: E402

CORE = {'X-Radio-Token': 'tok'}
WEB = {'X-Requested-With': 'QSYSRadio'}
USERS = {  # name: (role, is_readonly, is_app_user, is_app_admin, must_change_password)
    'admin': ('admin', 0, 0, 0, 0), 'operator': ('user', 0, 1, 0, 0), 'viewer': ('user', 1, 1, 0, 0),
    'outsider': ('staff', 0, 0, 0, 0), 'newbie': ('user', 0, 1, 0, 1),
}


def sql(query, args=None, fetch=False):
    conn = db.connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


@pytest.fixture(scope='session', autouse=True)
def schemas():
    sql('DROP SCHEMA IF EXISTS qsr_test CASCADE; DROP SCHEMA IF EXISTS qsr_test_shared CASCADE; CREATE SCHEMA qsr_test_shared')
    sql("""CREATE TABLE qsr_test_shared.users (id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
           display_name TEXT, role TEXT DEFAULT 'user', must_change_password INTEGER DEFAULT 0, is_readonly INTEGER DEFAULT 0,
           is_app_user INTEGER DEFAULT 0, is_app_admin INTEGER DEFAULT 0)""")
    for name, (role, ro, au, aa, mc) in USERS.items():
        sql('INSERT INTO qsr_test_shared.users (username, password_hash, display_name, role, is_readonly, is_app_user, '
            'is_app_admin, must_change_password) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (name, generate_password_hash('pw'), name.title(), role, ro, au, aa, mc))
    migrate.run(log=lambda *_: None)      # also creates qsr_test_shared.app_sessions
    yield
    sql('DROP SCHEMA IF EXISTS qsr_test CASCADE; DROP SCHEMA IF EXISTS qsr_test_shared CASCADE')


@pytest.fixture(scope='session')
def app():
    from server.app import app as flask_app
    return flask_app


def signed_in(app, user):
    from server import auth
    auth._attempts.clear()             # the tests sign in far more often than the rate limit allows
    c = app.test_client()
    r = c.post('/login', data={'username': user, 'password': 'pw'})
    assert r.status_code == 302, r.get_data(as_text=True)
    return c


def sync(app, station='S1', **body):
    body.setdefault('version', config.version())
    body.setdefault('boot', 'B1')
    return app.test_client().post('/api/v1/core/sync', json=dict(station=station, **body), headers=CORE)


def ev(seq, kind, t=None, **data):
    t = t or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return dict(kind=kind, boot='B1', seq=seq, t=t, at=seq / 10, **data)


STATE = {'automation': True, 'state': 'playing', 'stop_after': False, 'queue': [], 'library': {'music': 2, 'imaging': 1},
         'decks': {'A': {'state': 'playing', 'file': 'a.mp3', 'live': True, 'elapsed': 3, 'duration': 200}},
         'settings': {'crossfade_s': 4}}


# ── Core endpoints ───────────────────────────────────────────────────────────

def test_core_token_required(app, monkeypatch):
    c = app.test_client()
    assert c.post('/api/v1/core/sync', json={'station': 'S1'}).status_code == 403
    assert c.post('/api/v1/core/sync', json={'station': 'S1'}, headers={'X-Radio-Token': 'nope'}).status_code == 403
    assert c.post('/api/v1/core/sync', json={'station': 'S1'}, headers={'X-Radio-Token': 'ünïcode'}).status_code == 403
    real_get = config.get
    monkeypatch.setattr(config, 'get', lambda k, d=None: None if k == 'RADIO_API_TOKEN' else real_get(k, d))
    assert c.post('/api/v1/core/sync', json={'station': 'S1'}).status_code == 403          # no token configured
    monkeypatch.setenv('RADIO_ALLOW_NO_TOKEN', '1')
    assert c.post('/api/v1/core/sync', json={'station': 'S1'}).status_code == 200          # explicit lab opt-out


def test_sync_events_play_log_and_dedupe(app):
    evs = [ev(1, 'boot'), ev(2, 'track_start', deck='A', file='Song.mp3', dir='', artist='Ar', title='Ti', duration=180.0),
           ev(3, 'track_end', deck='A', file='Song.mp3', dir='', reason='end', played=179.5)]
    r = sync(app, events=evs, state=STATE)
    assert r.status_code == 200 and r.json['accepted'] == 3 and r.json['rejected'] == 0
    assert sync(app, events=evs).json['duplicates'] == 3
    rows = sql("SELECT file, directory, ended_at IS NOT NULL, played_s FROM qsr_test.play_log WHERE station='S1'", fetch=True)
    assert [tuple(r) for r in rows] == [('Song.mp3', '', True, 179.5)]


def test_bad_events_are_rejected_alone(app):
    good = ev(10, 'transition', reason='end')
    bad = [ev(2 ** 31, 'x'), ev(11, 'track_start', file='bad\x00name'), ev(12, 'x', t='yesterday'), {'kind': 'x'}, 'junk',
           ev(13, 'track_start', deck='A', file='nan.mp3', duration=float('nan'))]
    r = sync(app, station='S2', events=[good] + bad)
    assert r.status_code == 200
    assert r.json['accepted'] == 3 and r.json['rejected'] == 4     # NUL stripped, NaN dropped -> stored
    names = sql("SELECT file FROM qsr_test.play_log WHERE station='S2' ORDER BY file", fetch=True)
    assert [n[0] for n in names] == ['badname', 'nan.mp3']


def test_core_state_cannot_break_pages(app):
    sync(app, station='S3', state={'automation': 'maybe', 'settings': [1], 'queue': {}, 'decks': 'x', 'library': None})
    c = signed_in(app, 'operator')
    for url in ('/api/v1/now?station=S3&plays=5', '/api/v1/settings?station=S3', '/api/v1/library?station=S3'):
        assert c.get(url).status_code == 200, url
    st = c.get('/api/v1/now?station=S3').json['station']['state']
    assert st['automation'] is False and st['queue'] == [] and st['decks'] == {}


def test_library_upload_bulk_and_paging(app):
    items = [{'key': f'Music/{i:05d}.mp3', 'dir': 'Music/', 'file': f'{i:05d}.mp3', 'kind': 'music',
              'artist': 'Same', 'title': 'Same'} for i in range(1500)]
    items += [{'key': 'root.mp3', 'dir': '', 'file': 'root.mp3', 'kind': 'music', 'artist': {'x': 1}, 'size': 2 ** 70}]
    t0 = time.monotonic()
    r = app.test_client().post('/api/v1/core/library', json={'station': 'S4', 'items': items}, headers=CORE)
    assert r.status_code == 200 and r.json['items'] == 1501 and time.monotonic() - t0 < 5
    c = signed_in(app, 'operator')
    for sort in ('artist', 'title', 'plays', 'last_played', 'least_recent'):
        seen = []
        for off in range(0, 1600, 200):
            seen += [i['key'] for i in c.get(f'/api/v1/library?station=S4&sort={sort}&limit=200&offset={off}').json['items']]
        assert len(seen) == 1501 and len(set(seen)) == 1501, sort
    # a root-folder file counts its plays ('' directory on both sides)
    sync(app, station='S4', events=[ev(1, 'track_start', deck='A', file='root.mp3', dir='', duration=60.0)])
    hit = c.get('/api/v1/library?station=S4&q=root').json['items'][0]
    assert hit['plays'] == 1 and hit['artist'] is None


# ── Commands ─────────────────────────────────────────────────────────────────

def test_commands_flow_ack_and_results(app):
    sync(app, station='S5', state=STATE)
    app.test_client().post('/api/v1/core/library', json={'station': 'S5', 'items': [
        {'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music'},
        {'key': 'I/b.wav', 'dir': 'I/', 'file': 'b.wav', 'kind': 'imaging'}]}, headers=CORE)
    op = signed_in(app, 'operator')
    post = lambda cmd, **args: op.post('/api/v1/control', json={'station': 'S5', 'cmd': cmd, 'args': args}, headers=WEB)
    assert post('queue_add', key='I/b.wav').status_code == 400              # imaging can't be queued
    assert post('bumper_now', key='M/a.mp3').status_code == 400
    assert post('queue_remove', index=float('inf')).status_code == 400
    assert post('nope').status_code == 400
    ids = [post(c, **a).json['id'] for c, a in (('queue_add', {'key': 'M/a.mp3', 'position': 'next'}), ('skip', {}),
                                                 ('bumper_now', {'key': 'I/b.wav'}), ('stop_after', {'on': False}))]
    got = sync(app, station='S5', ack=0).json['commands']
    assert [c['id'] for c in got] == ids
    assert sync(app, station='S5', ack=ids[-1]).json['commands'] == []
    sync(app, station='S5', events=[ev(50, 'command', id=ids[0], ok=True, note='queued at 1'),
                                    ev(51, 'command', id=ids[1], ok=False, note='nothing to skip')])
    states = {c['id']: c['state'] for c in op.get('/api/v1/now?station=S5').json['station']['commands']}
    assert states[ids[0]] == 'done' and states[ids[1]] == 'failed' and states[ids[2]] == 'sent'
    # delivered but never reported -> failed after RESULT_TTL
    sql("UPDATE qsr_test.commands SET delivered_at = NOW() - INTERVAL '10 minutes' WHERE id = %s", (ids[2],))
    sync(app, station='S5', ack=ids[-1])
    note = sql('SELECT ok, note FROM qsr_test.commands WHERE id = %s', (ids[2],), fetch=True)[0]
    assert note[0] is False and note[1] == 'no result from the Core'
    # a database that was reset: the Core's ack is ahead of every id
    assert sync(app, station='S5', ack=10 ** 9).json['ack_reset'] == max(ids)


def test_commands_never_skipped_by_commit_order(app):
    """A command whose INSERT commits after a later one must still be delivered."""
    sync(app, station='S6', state=STATE)
    slow = db.connect()
    with slow.cursor() as cur:                      # first command: id taken, commit held back
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('qsysradio:station:S6'))")
        cur.execute("INSERT INTO commands (station, cmd, args, label) VALUES ('S6', 'skip', '{}', 'held') RETURNING id")
        held = cur.fetchone()[0]
    op = signed_in(app, 'operator')
    out = {}
    t = threading.Thread(target=lambda: out.update(r=op.post('/api/v1/control', json={'station': 'S6', 'cmd': 'rescan'}, headers=WEB)))
    t.start()
    time.sleep(0.5)
    assert t.is_alive()                             # the second command waits for the station lock
    slow.commit()
    slow.close()
    t.join(5)
    later = out['r'].json['id']
    assert later > held
    assert [c['id'] for c in sync(app, station='S6', ack=0).json['commands']] == [held, later]


# ── Sign-in and permissions ──────────────────────────────────────────────────

def test_login_rules(app):
    c = app.test_client()
    assert c.post('/login', data={'username': 'admin', 'password': 'x'}).status_code == 401
    assert b'does not have access' in c.post('/login', data={'username': 'outsider', 'password': 'pw'}).data
    assert b'change your password' in c.post('/login', data={'username': 'newbie', 'password': 'pw'}).data
    r = c.post('/login', data={'username': 'admin', 'password': 'pw'}, headers={'Origin': 'http://evil.example'})
    assert r.status_code == 403
    assert c.post('/login?next=//evil.com', data={'username': 'admin', 'password': 'pw'}).headers['Location'] == '/'


def test_permissions(app):
    viewer, op, admin = signed_in(app, 'viewer'), signed_in(app, 'operator'), signed_in(app, 'admin')
    body = {'station': 'S5', 'cmd': 'rescan'}
    assert viewer.post('/api/v1/control', json=body, headers=WEB).status_code == 403
    assert op.post('/api/v1/control', json=body).status_code == 403                      # no X-Requested-With
    assert op.post('/api/v1/control', json=body, headers=WEB).status_code == 200
    assert op.get('/settings').status_code == 403
    assert op.post('/api/v1/settings', json={'settings': {}}, headers=WEB).status_code == 403
    assert admin.get('/settings').status_code == 200
    assert app.test_client().get('/api/v1/now').status_code == 401


def test_sister_app_session_is_checked_not_trusted(app):
    from server import auth
    """A session made by another app for a user without QSYSRadio access."""
    uid = sql("SELECT id FROM qsr_test_shared.users WHERE username = 'outsider'", fetch=True)[0][0]
    sid = 'S' * 43
    data = {'user_id': uid, 'username': 'outsider', 'role': 'staff', 'user_role': 'staff',
            '_role_checked_at': datetime.now(timezone.utc).timestamp()}
    sql('INSERT INTO qsr_test_shared.app_sessions (sid, user_id, data, expires_at) VALUES (%s, %s, %s, %s)',
        (sid, uid, json.dumps(data), auth.utcnow() + timedelta(hours=1)))
    c = app.test_client()
    c.set_cookie('session', sid)
    assert c.get('/api/v1/now').status_code == 403
    assert c.post('/api/v1/control', json={'station': 'S5', 'cmd': 'stop_now'}, headers=WEB).status_code == 403
    assert c.get('/').status_code == 403
    assert sql('SELECT 1 FROM qsr_test_shared.app_sessions WHERE sid = %s', (sid,), fetch=True)   # still signed in elsewhere


def test_session_times_are_utc(app):
    signed_in(app, 'operator')
    row = sql('SELECT last_seen, expires_at FROM qsr_test_shared.app_sessions ORDER BY last_seen DESC LIMIT 1', fetch=True)[0]
    assert abs((row[1] - row[0]) - timedelta(hours=12)) < timedelta(minutes=1)


def test_logout_is_global(app):
    c = signed_in(app, 'operator')
    n = len(sql('SELECT sid FROM qsr_test_shared.app_sessions', fetch=True))
    c.get('/logout')
    assert len(sql('SELECT sid FROM qsr_test_shared.app_sessions', fetch=True)) == n - 1
    assert c.get('/api/v1/now').status_code == 401


def test_database_down_gives_503_not_500(app, monkeypatch):
    c = signed_in(app, 'operator')

    def down():
        raise db.DatabaseUnavailable('test: database down')
    monkeypatch.setattr(db, 'borrow', down)
    r = c.get('/api/v1/now')
    assert r.status_code == 503 and r.is_json
    assert c.get('/').status_code == 503
    assert c.get('/api/v1/health').status_code == 503
    assert c.get('/static/css/radio.css').status_code == 200        # no session, no database needed


# ── Settings ─────────────────────────────────────────────────────────────────

def test_settings_validation_and_push(app):
    admin = signed_in(app, 'admin')
    save = lambda **s: admin.post('/api/v1/settings', json={'settings': s}, headers=WEB)
    assert save(crossfade_s=99).status_code == 400
    assert save(crossfade_s=True).status_code == 400
    assert save(music_dir='').status_code == 400
    assert save(music_dir={'x': 1}).status_code == 400
    assert save(music_dir='../etc').status_code == 400
    assert save(bumper_every_n_tracks='4.5').status_code == 400
    assert save(nope=1).status_code == 400
    assert save(bumper_every_n_tracks='3.0', bumper_fade_s=2.5, imaging_gain_db=-3, stop_fade_s=4,
                music_dir='My Music').json['changed'] == ['bumper_every_n_tracks', 'bumper_fade_s', 'imaging_gain_db',
                                                          'music_dir', 'stop_fade_s']
    first = sync(app, station='S7', settings_rev='').json
    assert first['settings'] == {'bumper_every_n_tracks': 3, 'bumper_fade_s': 2.5, 'imaging_gain_db': -3, 'stop_fade_s': 4,
                                 'music_dir': 'My Music/'}
    assert 'settings' not in sync(app, station='S7', settings_rev=first['settings_rev']).json
    assert save(music_dir=None).json['changed'] == ['music_dir']                          # back to the default
    assert 'music_dir' not in sync(app, station='S7', settings_rev=first['settings_rev']).json['settings']


def test_settings_spec_matches_radio_lua():
    """Every station setting is defined once on each side, with the same defaults."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), '..', 'qsys', 'radio', 'radio.lua')).read()
    block = re.search(r'^SETTINGS = \{(.*?)^\}', src, re.S | re.M).group(1)
    lua = {k: v.strip().strip('"') for k, v in re.findall(r'(\w+) = ("[^"]*"|-?[\d.]+)', block)}
    assert set(lua) == set(core_settings.BY_NAME)
    for name, spec in core_settings.BY_NAME.items():
        default = spec[7]
        assert (lua[name] == default) if isinstance(default, str) else float(lua[name]) == float(default), name


# ── Queries and robustness ───────────────────────────────────────────────────

def test_plays_queries_are_per_station_and_find_open_plays(app):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t = lambda s: (now - timedelta(seconds=s)).strftime('%Y-%m-%dT%H:%M:%SZ')
    sync(app, station='P1', events=[ev(1, 'track_start', t=t(30), deck='A', file='p1.mp3', duration=0)])
    sync(app, station='P2', events=[dict(ev(1, 'track_start', t=t(30), deck='A', file='p2.mp3', duration=100.0), boot='B2')])
    c = signed_in(app, 'operator')
    assert [p['file'] for p in c.get('/api/v1/now?station=P1&plays=10').json['recent']] == ['p1.mp3']
    at = c.get('/api/v1/plays/at?station=P1&t=' + t(10)).json['plays']
    assert [p['file'] for p in at] == ['p1.mp3']                                          # unknown length, still open
    assert c.get('/api/v1/plays?station=P1&from=nonsense').status_code == 400
    assert c.get('/api/v1/plays?station=P1&q=a%00b&from=x%00y').status_code == 400
    assert c.get('/api/v1/library?station=P1&offset=99999999999999999999').status_code == 200


def test_bad_bodies_are_400(app):
    op = signed_in(app, 'operator')
    assert op.post('/api/v1/control', json=[1], headers=WEB).status_code == 400
    assert op.post('/api/v1/control', json={'station': {'a': 1}, 'cmd': 'skip'}, headers=WEB).status_code in (200, 400)
    assert app.test_client().post('/api/v1/core/sync', json='x', headers=CORE).status_code == 400
    assert app.test_client().post('/api/v1/core/library', json={'station': 'S1', 'items': 'x'}, headers=CORE).status_code == 400


def test_pool_reuses_connections(app):
    c = signed_in(app, 'operator')
    opened = lambda: sql("SELECT sessions FROM pg_stat_database WHERE datname = current_database()", fetch=True)[0][0]
    before = opened()

    def hammer():
        for _ in range(25):
            assert c.get('/api/v1/now?plays=5').status_code == 200
    threads = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    new_sessions = opened() - before - 1          # minus this query's own session
    assert new_sessions <= int(os.environ['WEB_THREADS']) + 2, f'{new_sessions} connections for 200 requests'
    assert 1 <= len(db._get_pool().idle) <= 4      # idle ones kept for reuse (PG_POOL_MAX_IDLE)


def test_syslog_never_blocks(monkeypatch):
    """A TCP syslog server that never answers must not slow logging down."""
    import logging
    monkeypatch.setenv('SYSLOG_HOST', '10.255.255.1')        # unroutable: connect would hang
    monkeypatch.setenv('SYSLOG_PROTOCOL', 'tcp')
    test_log = logging.getLogger('qsysradio-test')
    saved = logs.log
    monkeypatch.setattr(logs, 'log', test_log)
    t0 = time.monotonic()
    logs.setup()
    for i in range(200):
        test_log.info('message %d', i)
    assert time.monotonic() - t0 < 1
    logs.log = saved
