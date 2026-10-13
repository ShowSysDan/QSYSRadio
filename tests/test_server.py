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

from server import ads, config, control, core_settings, db, logs, migrate  # noqa: E402

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
    sql("""CREATE TABLE qsr_test_shared.app_sessions (sid TEXT PRIMARY KEY, user_id INTEGER REFERENCES
           qsr_test_shared.users(id) ON DELETE CASCADE, data TEXT NOT NULL DEFAULT '{}', created_at TIMESTAMP DEFAULT
           CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP NOT NULL)""")  # as 321Theater does
    migrate.run(log=lambda *_: None)
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


def test_core_restart_does_not_rerun_commands(app):
    """No command state on the Core: a restarted Core asks from ack 0."""
    sync(app, station='S8', boot='B1', state=STATE)
    op = signed_in(app, 'operator')
    first = op.post('/api/v1/control', json={'station': 'S8', 'cmd': 'skip'}, headers=WEB).json['id']
    assert [c['id'] for c in sync(app, station='S8', boot='B1', ack=0).json['commands']] == [first]   # delivered, no result
    second = op.post('/api/v1/control', json={'station': 'S8', 'cmd': 'rescan'}, headers=WEB).json['id']
    got = sync(app, station='S8', boot='B2', ack=0).json['commands']                                 # restarted
    assert [c['id'] for c in got] == [second]
    note = sql('SELECT ok, note FROM qsr_test.commands WHERE id = %s', (first,), fetch=True)[0]
    assert note[0] is False and 'restarted' in note[1]


# ── Library figures and ads ──────────────────────────────────────────────────

def library_post(app, station, items, **extra):
    return app.test_client().post('/api/v1/core/library', json=dict(station=station, items=items, **extra), headers=CORE)


def test_learned_figures_live_on_the_server(app):
    items = [{'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music', 'size': 10},
             {'key': 'M/bad.wma', 'dir': 'M/', 'file': 'bad.wma', 'kind': 'music', 'size': 20}]
    assert library_post(app, 'L1', items).json['learned'] == {}
    sync(app, station='L1', events=[
        ev(1, 'track_start', deck='A', file='a.mp3', dir='M/', duration=200.0),
        ev(2, 'track_end', deck='A', file='a.mp3', dir='M/', reason='end', lead_in=0.8, trail=3.0, peak=-4.5),
        ev(3, 'track_start', deck='B', file='a.mp3', dir='M/', duration=200.0),
        ev(4, 'track_end', deck='B', file='a.mp3', dir='M/', reason='cut', lead_in=0.6, peak=-5.0),
        ev(5, 'file_unsupported', file='bad.wma', dir='M/', status='Unsupported file format')])
    row = sql("SELECT duration_s, lead_in_s, trail_s, peak_dbfs FROM qsr_test.library WHERE station='L1' AND key='M/a.mp3'",
              fetch=True)[0]
    assert tuple(row) == (200.0, 0.6, 3.0, -5.0)             # a cut play keeps the trail it cannot measure
    assert library_post(app, 'L1', items).json['learned'] == {'M/a.mp3': {'trail': 3.0}, 'M/bad.wma': {'unsupported': True}}
    op = signed_in(app, 'operator')
    assert op.post('/api/v1/control', json={'station': 'L1', 'cmd': 'queue_add', 'args': {'key': 'M/bad.wma'}},
                   headers=WEB).status_code == 400
    assert op.get('/api/v1/library?station=L1&q=bad').json['items'][0]['unsupported'] is True
    items[1]['size'] = 21                                       # replaced with a new file: try it again
    assert 'M/bad.wma' not in library_post(app, 'L1', items).json['learned']


def test_queue_lengths_come_from_the_library(app):
    library_post(app, 'L2', [{'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music'}])
    sync(app, station='L2', events=[ev(1, 'track_start', deck='A', file='a.mp3', dir='M/', duration=123.0)],
         state=dict(STATE, next_up={'key': 'M/a.mp3', 'file': 'a.mp3'}, queue=[{'key': 'M/a.mp3', 'file': 'a.mp3'}]))
    st = signed_in(app, 'viewer').get('/api/v1/now?station=L2').json['station']['state']
    assert st['next_up']['duration'] == 123.0 and st['queue'][0]['duration'] == 123.0


def test_owed_spreads_plays_over_the_hour():
    assert [ads.owed(4, m / 60) for m in (0, 7, 8, 22, 23, 52, 53, 59)] == [0, 0, 1, 1, 2, 3, 4, 4]
    assert ads.owed(1, 0.49) == 0 and ads.owed(1, 0.5) == 1


def test_ads_plan_rules_and_play_log(app):
    items = [{'key': f'Ads/{n}.wav', 'dir': 'Ads/', 'file': f'{n}.wav', 'kind': 'ad'} for n in ('m1', 'm2', 'h', 'off')]
    assert library_post(app, 'A1', items, ads_dir='Ads/').status_code == 200
    op, viewer = signed_in(app, 'operator'), signed_in(app, 'viewer')
    rule = lambda c, **b: c.post('/api/v1/ads', json=dict(station='A1', **b), headers=WEB)
    assert rule(viewer, key='Ads/h.wav', mode='off').status_code == 403
    assert rule(op, key='Ads/h.wav', mode='often').status_code == 400
    assert rule(op, key='Ads/h.wav', mode='hourly', per_hour=0).status_code == 400
    assert rule(op, key='Ads/h.wav', mode='hourly', per_hour=True).status_code == 400
    assert rule(op, key='Ads/nope.wav', mode='off').status_code == 400
    assert rule(op, key='Ads/h.wav', mode='hourly', per_hour=30).status_code == 200
    assert rule(op, key='Ads/off.wav', mode='off').status_code == 200

    frac = float(sql("SELECT EXTRACT(EPOCH FROM NOW() - date_trunc('hour', NOW())) / 3600", fetch=True)[0][0])
    first = sync(app, station='A1', ads_rev='').json
    assert first['ads']['mix'] == ['Ads/m1.wav', 'Ads/m2.wav']
    assert first['ads']['due'] == (['Ads/h.wav'] if ads.owed(30, frac) > 0 else [])
    assert 'ads' not in sync(app, station='A1', ads_rev=first['ads_rev']).json             # unchanged: not resent

    # 30 plays this hour: nothing owed any more; the ad is logged as kind 'ad'
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    evs = [ev(10 + i, 'ad_start', t=(hour_start + timedelta(seconds=i)).strftime('%Y-%m-%dT%H:%M:%SZ'), deck='A',
              file='h.wav', dir='Ads/', duration=30.0) for i in range(30)]
    reply = sync(app, station='A1', events=evs, ads_rev=first['ads_rev']).json
    if first['ads']['due']:
        assert reply['ads']['due'] == []
    lib = op.get('/api/v1/library?station=A1&kind=ad').json['items']
    h = next(i for i in lib if i['key'] == 'Ads/h.wav')
    assert (h['mode'], h['per_hour'], h['plays_hour']) == ('hourly', 30, 30) and h['plays_today'] >= 30
    assert next(i for i in lib if i['key'] == 'Ads/m1.wav')['mode'] == 'mix'
    assert op.get('/api/v1/plays?station=A1&kind=ad').json['plays'][0]['kind'] == 'ad'
    # operator commands for ads
    post = lambda cmd, **a: op.post('/api/v1/control', json={'station': 'A1', 'cmd': cmd, 'args': a}, headers=WEB)
    assert post('ad_now', key='Ads/m1.wav').status_code == 200 and post('ad_now').status_code == 200
    assert post('bumper_now', key='Ads/m1.wav').status_code == 400 and post('queue_add', key='Ads/m1.wav').status_code == 400
    assert post('play_now', key='Ads/m1.wav').status_code == 200


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


def test_app_never_creates_anything_in_shared(monkeypatch):
    """Startup only checks the shared schema (321Theater owns it) and says what is missing."""
    sql('DROP SCHEMA IF EXISTS qsr_test_bare CASCADE; CREATE SCHEMA qsr_test_bare; '
        'CREATE TABLE qsr_test_bare.users (id SERIAL PRIMARY KEY, username TEXT)')
    monkeypatch.setenv('PG_SHARED_SCHEMA', 'qsr_test_bare')
    lines = []
    migrate.run(log=lines.append)
    tables = sql("SELECT tablename FROM pg_tables WHERE schemaname = 'qsr_test_bare'", fetch=True)
    sql('DROP SCHEMA qsr_test_bare CASCADE')
    assert [t[0] for t in tables] == ['users']
    assert any('app_sessions does not exist' in line for line in lines)


def test_signin_without_shared_tables_says_so(app, monkeypatch):
    from server import auth
    c = signed_in(app, 'operator')
    sql('ALTER TABLE qsr_test_shared.app_sessions RENAME TO app_sessions_hidden')
    auth._shared_check['at'] = -1e9
    try:
        r = c.get('/')
        assert r.status_code == 503 and b'Sign-in is not available' in r.data
        assert c.get('/api/v1/now').status_code == 503
        fresh = app.test_client()
        assert b'Sign-in is not available' in fresh.get('/login').data          # before typing a password
        login = fresh.post('/login', data={'username': 'operator', 'password': 'pw'})
        assert login.status_code == 401 and b'Sign-in is not available' in login.data
    finally:
        sql('ALTER TABLE qsr_test_shared.app_sessions_hidden RENAME TO app_sessions')
        auth._shared_check['at'] = -1e9


def test_database_newer_than_code_warns():
    sql("INSERT INTO qsr_test.schema_migrations (version, name, checksum) VALUES (9999, 'from_the_future', 'x')")
    try:
        lines = []
        migrate.run(log=lines.append)
        assert any('9999' in line and 'does not know' in line for line in lines)
    finally:
        sql('DELETE FROM qsr_test.schema_migrations WHERE version = 9999')


def test_premade_schema_without_create_says_how_to_fix(monkeypatch):
    """A DBA-made schema the role cannot write gets the fix, not a traceback."""
    sql('DROP SCHEMA IF EXISTS qsr_test_locked CASCADE; CREATE SCHEMA qsr_test_locked; '
        'REVOKE CREATE ON SCHEMA qsr_test_locked FROM CURRENT_USER')
    monkeypatch.setenv('PG_APP_SCHEMA', 'qsr_test_locked')
    try:
        with pytest.raises(SystemExit, match='ALTER SCHEMA "qsr_test_locked" OWNER TO'):
            migrate.run(log=lambda *_: None)
    finally:
        sql('DROP SCHEMA qsr_test_locked CASCADE')


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


def test_syslog_set_on_the_settings_page(app):
    import socket
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', 0))
    rx.settimeout(5)
    port = rx.getsockname()[1]
    admin, op = signed_in(app, 'admin'), signed_in(app, 'operator')
    save = lambda c, **s: c.post('/api/v1/server-settings', json={'settings': s}, headers=WEB)
    assert op.get('/api/v1/server-settings').status_code == 403
    assert save(op, host='127.0.0.1').status_code == 403
    for bad in ({'host': 'a b'}, {'host': 'x;rm'}, {'port': 0}, {'port': 70000}, {'protocol': 'http'},
                {'level': 'LOUD'}, {'app': 'two words'}, {'facility': 'nope'}, {'nope': 1}):
        assert save(admin, **bad).status_code == 400, bad
    assert admin.post('/api/v1/server-settings/test', json={}, headers=WEB).status_code == 400     # no host yet
    try:
        assert save(admin, host='127.0.0.1', port=port, format='rfc5424', app='RadioTest').json['changed'] == \
            ['app', 'format', 'host', 'port']
        view = {x['name']: x for x in admin.get('/api/v1/server-settings').json['settings']}
        assert view['host']['running'] == '127.0.0.1' and view['protocol']['choices'] == ['udp', 'tcp']
        assert admin.post('/api/v1/server-settings/test', json={}, headers=WEB).json['sent_to'] == f'127.0.0.1:{port}/udp'
        for _ in range(20):                      # the settings save itself is logged first
            line = rx.recv(4096).decode()
            if 'SYSLOG test message' in line:
                break
        assert line.startswith('<') and ' RadioTest ' in line and 'user=admin' in line
    finally:
        save(admin, host=None, port=None, format=None, app=None)
        rx.close()
    assert admin.get('/api/v1/server-settings').json['settings'][0]['running'] == ''


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
