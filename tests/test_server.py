"""Server tests against a real PostgreSQL (no SQLite stand-in).

    QSYSRADIO_ENV_FILE=/path/to/test.env venv/bin/python -m pytest tests/

The .env must point at a database where PG_USER may CREATE schemas. The
tests use their own schemas (qsr_test / qsr_test_shared), dropped afterwards,
so they never touch the real qsysradio or shared schemas. Skipped when no
database is configured.
"""
import json
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
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


# every web write to a station's rows: (who, path, body)
WRITERS = {
    'command': ('operator', '/api/v1/control', {'cmd': 'skip'}),
    'hold': ('operator', '/api/v1/hold', {'what': 'ads', 'until': None}),
    'ident': ('operator', '/api/v1/library/ident', {'key': 'I/id.wav', 'ident': True}),
    'ad_rule': ('operator', '/api/v1/ads', {'key': 'Ads/a.wav', 'mode': 'hourly', 'per_hour': 2}),
    'station_settings': ('admin', '/api/v1/settings', {'settings': {'crossfade_s': 6}, 'scope': 'station'}),
    'all_settings': ('admin', '/api/v1/settings', {'settings': {'crossfade_s': 6}}),
    'analysis_stop': ('operator', '/api/v1/analyze', {'scope': 'stop'}),
    'analyze_file': ('operator', '/api/v1/analyze', {'key': 'M/a.mp3'}),
    'trim': ('operator', '/api/v1/library/trim', {'key': 'M/a.mp3', 'lead': 1.0, 'trail': None}),
    'forget': ('operator', '/api/v1/library/forget', {'kind': 'music'}),
}


def lock_waits(thread, timeout=5):
    """What the other backends wait for (pg_stat_activity), as soon as one waits for a lock (or the thread is done)."""
    end = time.monotonic() + timeout
    while True:
        got = [r[0] for r in sql("""SELECT wait_event FROM pg_stat_activity WHERE wait_event_type = 'Lock'
                                    AND pid <> pg_backend_pid() AND datname = current_database()""", fetch=True)]
        if got or not thread.is_alive() or time.monotonic() > end:
            return got
        time.sleep(0.05)


@pytest.mark.parametrize('writer', list(WRITERS))
def test_web_writes_take_the_station_lock_first(app, writer):
    """Every web write to a station's rows takes the station's lock before it touches any row. The Core's sync
    holds the lock, then the station's stations row, then its library and figures rows (its events): a write
    that locked them the other way round (set_ident did: library, then stations) deadlocked with it."""
    st = f'LK{list(WRITERS).index(writer)}'
    items = [{'key': 'I/id.wav', 'dir': 'I/', 'file': 'id.wav', 'kind': 'imaging'},
             {'key': 'Ads/a.wav', 'dir': 'Ads/', 'file': 'a.wav', 'kind': 'ad'},
             {'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music'}]
    assert library_post(app, st, items, ads_dir='Ads/').status_code == 200
    batch(app, {'station': st, 'state': STATE}, core=f'{st}CORE', analyze_rev='')       # on a Core: a run is the Core's
    sql("""UPDATE qsr_test.media_files SET lead_in_s = 1.5 WHERE id IN (SELECT file_id FROM qsr_test.library WHERE station = %s);
           UPDATE qsr_test.library SET analyze_wanted = TRUE WHERE station = %s""", (st, st))
    who, path, body = WRITERS[writer]
    client, out = signed_in(app, who), {}
    core = db.connect()                       # the Core's sync of the station, held open
    try:
        with core.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (f'qsysradio:station:{st}',))
            cur.execute('UPDATE stations SET last_seen = NOW() WHERE station = %s', (st,))
        t = threading.Thread(target=lambda: out.update(r=client.post(path, json=dict(body, station=st), headers=WEB)))
        t.start()
        assert lock_waits(t) == ['advisory']   # the write waits for the station's lock ...
        with core.cursor() as cur:             # ... having locked none of its rows: the sync's own writes go on
            cur.execute("SET LOCAL lock_timeout = '3s'")
            cur.execute('UPDATE library SET last_played = NOW(), analyze_wanted = FALSE WHERE station = %s', (st,))
            cur.execute('UPDATE media_files SET duration_s = 200 WHERE id IN (SELECT file_id FROM library WHERE station = %s)', (st,))
            cur.execute('UPDATE stations SET plan_at = NOW() WHERE station = %s', (st,))
        core.commit()
        t.join(10)
        assert not t.is_alive() and out['r'].status_code == 200, out['r'].get_data(as_text=True)
    finally:
        core.close()
        if writer == 'all_settings':
            signed_in(app, 'admin').post('/api/v1/settings', json={'settings': {'crossfade_s': None}}, headers=WEB)


def test_a_change_to_shared_files_cancelled_by_a_deadlock_answers_409(app):
    """A file's figures are shared by every station that has it, which the station lock does not cover: Forget
    measured for a whole kind of station DA can meet station DB's sync writing the same files in the other order
    (its events, one file after the other). PostgreSQL cancels one; when it is the web change, it answers 409
    (nothing changed, try again), never a server error, and the next try goes through."""
    items = [{'key': f'M/dl{i}.mp3', 'dir': 'M/', 'file': f'dl{i}.mp3', 'kind': 'music', 'size': 7000 + i} for i in (1, 2)]
    for st in ('DA', 'DB'):
        assert library_post(app, st, items).status_code == 200
    ids = [r[0] for r in sql("SELECT file_id FROM qsr_test.library WHERE station = 'DA' ORDER BY key", fetch=True)]
    assert ids == [r[0] for r in sql("SELECT file_id FROM qsr_test.library WHERE station = 'DB' ORDER BY key", fetch=True)]
    op, out, codes = signed_in(app, 'operator'), {}, []
    forget = lambda: out.update(r=op.post('/api/v1/library/forget', json={'station': 'DA', 'kind': 'music'}, headers=WEB))
    for first, second in (ids, ids[::-1]) * 2:          # whichever file the Forget locks first, one round deadlocks
        sql('UPDATE qsr_test.media_files SET lead_in_s = 1.5, trail_s = NULL WHERE id = ANY(%s)', (ids,))
        core = db.connect()                             # DB's sync: its lock, then one file's figures ...
        try:
            with core.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('qsysradio:station:DB',))
                cur.execute('UPDATE media_files SET trail_s = 2 WHERE id = %s', (first,))
            t = threading.Thread(target=forget)
            t.start()
            assert lock_waits(t) == ['transactionid']   # the Forget waits for that file (holding the other, or not)
            with core.cursor() as cur:                  # ... then the other's
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute('UPDATE media_files SET trail_s = 2 WHERE id = %s', (second,))
            core.commit()
            t.join(10)
        finally:
            core.close()
        codes.append(out['r'].status_code)
        if codes[-1] != 200:
            break
    assert codes[-1] == 409 and set(codes[:-1]) <= {200}, codes
    assert out['r'].json['error'].startswith('Another station was saving the same files')
    rows = sql('SELECT lead_in_s, trail_s FROM qsr_test.media_files WHERE id = ANY(%s)', (ids,), fetch=True)
    assert sorted(tuple(r) for r in rows) == [(1.5, 2.0), (1.5, 2.0)]    # the sync's writes kept, nothing forgotten
    forget()
    assert out['r'].status_code == 200 and out['r'].json['files'] == 2     # tried again: done


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
    row = sql("SELECT duration_s, lead_in_s, trail_s, peak_dbfs FROM qsr_test.library_figures WHERE station='L1' AND key='M/a.mp3'",
              fetch=True)[0]
    assert tuple(row) == (200.0, 0.6, 3.0, -4.5)             # a cut play keeps the trail it cannot measure; the highest peak
    assert library_post(app, 'L1', items).json['learned'] == {'M/a.mp3': {'trail': 3.0, 'lead': 0.6},
                                                           'M/bad.wma': {'unsupported': True}}
    op = signed_in(app, 'operator')
    assert op.post('/api/v1/control', json={'station': 'L1', 'cmd': 'queue_add', 'args': {'key': 'M/bad.wma'}},
                   headers=WEB).status_code == 400
    assert op.get('/api/v1/library?station=L1&q=bad').json['items'][0]['unsupported'] is True
    lib = op.get('/api/v1/library?station=L1&kind=music').json
    a_row = next(i for i in lib['items'] if i['key'] == 'M/a.mp3')
    assert a_row['audible_s'] == pytest.approx(200 - 0.6 - 3.0)                  # without the quiet start and end
    t = lib['totals']
    assert (t['n'], t['duration_s'], t['audible_s'], t['unknown']) == (2, 200.0, pytest.approx(196.4), 1)
    assert t['never_played'] == 1 and t['oldest_played']                         # rotation health: bad.wma never aired
    items[1]['size'] = 21                                       # replaced with a new file: try it again
    assert 'M/bad.wma' not in library_post(app, 'L1', items).json['learned']


def test_queue_lengths_come_from_the_library(app):
    library_post(app, 'L2', [{'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music'}])
    sync(app, station='L2', events=[ev(1, 'track_start', deck='A', file='a.mp3', dir='M/', duration=123.0)],
         state=dict(STATE, next_up={'key': 'M/a.mp3', 'file': 'a.mp3'}, queue=[{'key': 'M/a.mp3', 'file': 'a.mp3'}]))
    st = signed_in(app, 'viewer').get('/api/v1/now?station=L2').json['station']['state']
    assert st['next_up']['duration'] == 123.0 and st['queue'][0]['duration'] == 123.0


def test_slots_have_windows_and_missed_ones_are_skipped():
    slots = ads.slot_minutes(4, 0.5)
    assert slots == [7.5, 22.5, 37.5, 52.5] and ads.window_minutes(4) == 7.5 and ads.window_minutes(1) == 10
    w = ads.window_minutes(4)
    assert ads.slot_states(slots, w, [], 5) == ['next'] * 4
    assert ads.slot_states(slots, w, [], 10) == ['due', 'next', 'next', 'next']
    # stopped from :05 to :40: the :07 and :22 slots are skipped, not played back to back
    assert ads.slot_states(slots, w, [], 40) == ['missed', 'missed', 'due', 'next']
    assert ads.slot_states(slots, w, [9.0, 41.0], 41) == ['done', 'missed', 'done', 'next']
    assert ads.slot_states(slots, w, [], 59.9) == ['missed'] * 3 + ['due']      # :52.5 is open until :60


def test_hourly_slots_never_share_a_break():
    """Ads with different counts interleave: at least 4 min apart, each ad still spread over the hour."""
    for counts in ([4, 4, 4, 4], [1, 4, 3, 1], [4, 3, 1], [30], [6, 6, 6, 6, 6]):
        slots = ads.spread(counts)
        every = sorted(m for x in slots for m in x)
        assert [len(x) for x in slots] == counts and 0 < every[0] and every[-1] < 60
        assert min((b - a for a, b in zip(every, every[1:])), default=60) >= min(4, 60 / len(every)) - 1e-9, counts
        for n, x in zip(counts, slots):                  # an ad's own slots: no closer than half its ideal gap
            assert all(b - a >= 30 / n for a, b in zip(x, x[1:])), (counts, x)
    assert ads.lateness([('a', 100), ('b', 155)], 0, 250) == 5                     # 100 + 155: 5 s late
    q, h = [('r', 300), ('q', 251)], [('r', 300), ('q', 251), ('o', 248)]
    assert ads.choose('r', q, h, 250, 180, 600) == ('r', 'rotation')                # 50 s late: within tolerance
    assert ads.choose('r', q, h, 250, 30, 600) == ('q', 'ad')                       # too late: the quarter's best
    assert ads.choose('r', [('r', 300)], h, 250, 30, 20) == ('q', 'ad')             # only the half saves the slot
    assert ads.choose('r', q, h, 1000, 0, 600) == ('r', 'rotation')                 # too far off to plan
    assert ads.choose('x', q, h, 250, 0, 600) == ('x', 'rotation')                  # pick of unknown length


def test_hourly_ads_are_staggered(app):
    keys = [f'Ads/s{i}.wav' for i in range(4)]
    library_post(app, 'A2', [{'key': k, 'dir': 'Ads/', 'file': k[4:], 'kind': 'ad'} for k in keys], ads_dir='Ads/')
    op = signed_in(app, 'operator')
    for k in keys:
        assert op.post('/api/v1/ads', json={'station': 'A2', 'key': k, 'mode': 'hourly', 'per_hour': 4},
                       headers=WEB).status_code == 200
    items = op.get('/api/v1/library?station=A2&kind=ad').json['items']
    minutes = sorted(datetime.fromisoformat(t).minute * 60 + datetime.fromisoformat(t).second
                     for i in items for t in (x['at'] for x in i['slots']))
    assert len(minutes) == 16 and all(len(i['slots']) == 4 for i in items)
    gaps = {b - a for a, b in zip(minutes, minutes[1:])}
    assert gaps <= {224, 225, 226}                  # one ad every 3.75 min, not four at once
    sync(app, station='A2', state=STATE)
    nxt = op.get('/api/v1/now?station=A2').json['station']['next_ad']
    assert nxt['at'] and nxt['key'] in keys and nxt['owed'] >= 0
    # nothing played: the plan owes exactly the ads with an open slot (at most 2 of 4 windows overlap)
    due = sync(app, station='A2', ads_rev='').json['ads']['due']
    states = {i['key']: [x['state'] for x in i['slots']] for i in op.get('/api/v1/library?station=A2&kind=ad').json['items']}
    assert len(due) <= 2 and all('due' in states[k] for k in due) and 'done' not in sum(states.values(), [])


def test_next_song_least_recently_played_and_ad_fit(app):
    """The server names the next song (the Core only plays it): an old one, or one that lands the break on the slot."""
    lengths = {'a': 100, 'b': 150, 'c': 200, 'd': 255, 'e': 300, 'f': 120, 'g': 180, 'h': 210}
    items = [{'key': f'M/{k}.mp3', 'dir': 'M/', 'file': f'{k}.mp3', 'kind': 'music', 'artist': k.upper()} for k in lengths]
    library_post(app, 'F1', items + [{'key': 'M/on.mp3', 'dir': 'M/', 'file': 'on.mp3', 'kind': 'music', 'artist': 'X'}])
    t0 = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime('%Y-%m-%dT%H:%M:%SZ')
    sync(app, station='F1', boot='old', events=[dict(ev(i + 1, 'track_start', t=t0, deck='B', file=f'{k}.mp3', dir='M/',
                                                        artist=k.upper(), duration=float(d)), boot='old')
                                               for i, (k, d) in enumerate(lengths.items())])
    # played hours ago, 'a' most recently, 'd' never (its length known from the library scan)
    for i, k in enumerate(lengths):
        sql("UPDATE qsr_test.play_log SET started_at = NOW() - make_interval(hours => 2 + %s), ended_at = NOW() - INTERVAL '1 hour' "
            "WHERE station = 'F1' AND file = %s", (i, f'{k}.mp3'))
        sql("UPDATE qsr_test.library SET last_played = " + ("NULL" if k == 'd' else "NOW() - make_interval(hours => 2 + %s)") +
            " WHERE station = 'F1' AND key = %s", ((f'M/{k}.mp3',) if k == 'd' else (i, f'M/{k}.mp3')))
    sync(app, station='F1', state=STATE, events=[ev(1, 'track_start', t=t0, deck='A', file='on.mp3', dir='M/',
                                                      artist='X', duration=200.0)])
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT started_at FROM play_log WHERE station = 'F1' AND ended_at IS NULL")
            next_start = cur.fetchone()[0] + timedelta(seconds=200 - 4)          # crossfade 4 s, no learned tail
            assert ads.next_song(cur, 'F1') == ('M/d.mp3', 'rotation')            # never played: first
            # the fair pick 'd' (251 s airtime) lands 1 s after the slot: it stays
            assert ads.next_song(cur, 'F1', (next_start + timedelta(seconds=250), 600)) == ('M/d.mp3', 'rotation')
            # 201 s late (more than the 180 s tolerance) and the window is 130 s: the oldest half's
            # best-timed song saves the slot ('g', 176 s airtime: 126 s late)
            assert ads.next_song(cur, 'F1', (next_start + timedelta(seconds=50), 130)) == ('M/g.mp3', 'ad')
            cur.execute("UPDATE library SET last_played = NOW() WHERE station = 'F1' AND key = 'M/d.mp3'")
            key, why = ads.next_song(cur, 'F1', (next_start + timedelta(seconds=600), 600))   # too far off: rotation
            assert why == 'rotation' and key in ('M/h.mp3', 'M/g.mp3')                   # the oldest quarter
            conn.rollback()
        sync(app, station='F1', state=dict(STATE, queue=[{'key': 'M/a.mp3'}]))
        with conn.cursor() as cur:
            assert ads.next_song(cur, 'F1') == (None, None)                                 # operator queue wins
    finally:
        conn.close()


def radio_lua_for(station):
    """radio.lua's source with its CFG pointed at this server (http://radio-server, token tok) and its
    (first) station named `station`: the Core's own setup, nothing of its internals."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), '..', 'qsys', 'radio', 'radio.lua'), encoding='utf-8').read()
    cfg = re.search(r'^CFG = \{.*?^\}', src, re.S | re.M)
    block = cfg.group(0).replace('SERVER_URL = ""', 'SERVER_URL = "http://radio-server"', 1)
    block = block.replace('SERVER_TOKEN = ""', 'SERVER_TOKEN = "tok"', 1)
    if 'STATIONS' in block:          # 2.x: CFG.STATIONS[1].id
        block = re.sub(r'\bid = "[^"]*"', f'id = "{station}"', block, count=1)
    else:                            # 1.x: CFG.STATION_ID
        block = block.replace('STATION_ID = ""', f'STATION_ID = "{station}"', 1)
    assert 'radio-server' in block and 'tok' in block and f'"{station}"' in block, block
    return src[:cfg.start()] + block + src[cfg.end():]


def test_every_song_gets_played_end_to_end(app, tmp_path, monkeypatch):
    """radio.lua (MockCore) against this server, with a rescan every few songs
    (each one used to reset the Core's shuffle bag): every song airs."""
    lupa = pytest.importorskip('lupa.lua53')
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    os.symlink(os.path.join(repo, 'qsys'), tmp_path / 'qsys')
    monkeypatch.chdir(tmp_path)
    client = app.test_client()
    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    L.execute('dofile("qsys/test/mock_core.lua")')
    L.execute("""local songs = {}
                 for i = 1, 24 do songs[i] = { name = string.format("Artist%d - Song%02d.mp3", i % 8, i),
                                              duration = 20 + (i * 7) % 15, level = -10 } end
                 MockCore.setup{ seed = 11, dirs = { ["Test/Tuning/"] = songs, ["Test/Imaging/"] = {} } }""")

    def handler(req):
        r = client.post(req.Url.split('radio-server', 1)[1], data=req.Data,
                        headers=dict(req.Headers.items()), content_type='application/json')
        return r.status_code, r.get_data(as_text=True)

    def rescan():                    # what the panel's Rescan does: a command the Core picks up on its sync
        conn = db.connect()
        try:
            control.add_command(conn, 'E2E', 'rescan', {}, {'username': 'e2e'})
        finally:
            conn.close()
    L.globals().MockCore.httpHandler = handler
    src = radio_lua_for('E2E')
    L.execute(src)
    for t in range(60, 1201, 75):
        L.eval('MockCore.at')(t, rescan)
    L.eval('MockCore.run')(1260)
    played = {r[0] for r in sql("SELECT DISTINCT file FROM qsr_test.play_log WHERE station = 'E2E'", fetch=True)}
    starts = sql("SELECT count(*) FROM qsr_test.play_log WHERE station = 'E2E'", fetch=True)[0][0]
    assert len(played) == 24, f'{24 - len(played)} of 24 songs never played in {starts} plays'
    done = sql("SELECT count(*) FILTER (WHERE ok) FROM qsr_test.commands WHERE station = 'E2E' AND cmd = 'rescan'", fetch=True)[0][0]
    assert done >= 10, f'only {done} of 16 rescans done'
    if 'STATIONS' in src:            # 2.x: the batched sync (the Core named) and the library in parts (scan ids)
        core, scan = sql("SELECT core, scan FROM qsr_test.stations WHERE station = 'E2E'", fetch=True)[0]
        assert core and scan and not scan.startswith('whole-'), (core, scan)
        assert sql('SELECT mem_kb > 0 FROM qsr_test.cores WHERE core = %s', (core,), fetch=True)[0][0]
    if 'ANALYZERS' in src:           # 2.1: its analysis player measured files (new files right away) while it played
        analyzed = sql("""SELECT count(*) FILTER (WHERE analyzed_at IS NOT NULL), count(*) FROM qsr_test.library_figures
                           WHERE station = 'E2E' AND present""", fetch=True)[0]
        assert analyzed[0] >= 5, f'{analyzed[0]} of {analyzed[1]} files analyzed'
        players = sql('SELECT analyzers FROM qsr_test.cores WHERE core = %s', (core,), fetch=True)[0][0]
        assert [p['name'] for p in players] == ['Analysis_Player'], players


def test_three_stations_on_one_core_end_to_end(app, tmp_path, monkeypatch):
    """radio.lua running three stations (MockCore) against this server: each plays its own folder with its own
    settings, the library of 1,100 files arrives in parts, and the station whose player is missing is faulted
    (the panel shows why) while the others play on."""
    lupa = pytest.importorskip('lupa.lua53')
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    os.symlink(os.path.join(repo, 'qsys'), tmp_path / 'qsys')
    monkeypatch.chdir(tmp_path)
    client = app.test_client()
    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    L.execute('dofile("qsys/test/mock_core.lua")')
    if L.eval('MockCore.stations') is None:
        pytest.skip('this MockCore has no multi-station wiring')
    L.execute("""local wiring, cfg = MockCore.stations(3, { prefix = "ME" })
                 cfg[3].players[2] = "Missing_Player"
                 local dirs = { ["Test/Tuning/"] = {}, ["Test/Imaging/"] = {} }
                 for s, n in ipairs({ 1100, 30, 30 }) do
                   local songs = {}
                   for i = 1, n do songs[i] = { name = string.format("ME%d Artist%d - Song%04d.mp3", s, i % 9, i),
                                                duration = 20 + (i * 7) % 15, level = -10 } end
                   dirs["Zone" .. s .. "/"] = songs
                 end
                 MockCore.setup{ seed = 5, players = wiring, dirs = dirs }
                 ME_CFG = cfg""")

    def handler(req):
        r = client.post(req.Url.split('radio-server', 1)[1], data=req.Data,
                        headers=dict(req.Headers.items()), content_type='application/json')
        return r.status_code, r.get_data(as_text=True)
    L.globals().MockCore.httpHandler = handler
    admin = signed_in(app, 'admin')
    for i in (1, 2, 3):                  # each station's folder and crossfade, set before its Core connects
        sync(app, station=f'ME{i}', boot='before')
        assert admin.post('/api/v1/settings', headers=WEB, json={'settings': {'music_dir': f'Zone{i}/', 'crossfade_s': 2 + i},
                                                                 'scope': 'station', 'station': f'ME{i}'}).status_code == 200
    L.execute("""MockCore.loadRadio(function(C) C.SERVER_URL, C.SERVER_TOKEN, C.CORE_ID, C.STATIONS = "http://radio-server", "tok",
                                                  "E2ECORE", ME_CFG end)""")
    L.eval('MockCore.run')(600)
    plays = {st: [r[0] for r in sql("SELECT file FROM qsr_test.play_log WHERE station = %s AND boot <> 'before'", (st,), fetch=True)]
             for st in ('ME1', 'ME2', 'ME3')}
    assert len(plays['ME1']) >= 10 and len(plays['ME2']) >= 10 and plays['ME3'] == [], {k: len(v) for k, v in plays.items()}
    assert all(f.startswith('ME1 ') for f in plays['ME1']) and all(f.startswith('ME2 ') for f in plays['ME2'])
    rows = {r[0]: tuple(r[1:]) for r in sql("""SELECT station, core, state->'settings'->>'crossfade_s', state->>'fault', scan_parts
                                        FROM qsr_test.stations WHERE station LIKE 'ME_'""", fetch=True)}
    assert rows['ME1'][:3] == ('E2ECORE', '3', None) and rows['ME1'][3] == [1, 2]          # 1,100 files: two parts
    assert rows['ME2'][:3] == ('E2ECORE', '4', None)
    assert 'Missing_Player' in rows['ME3'][2]
    assert sql("SELECT count(*) FROM qsr_test.library WHERE station = 'ME1' AND present", fetch=True)[0][0] == 1100
    now = signed_in(app, 'viewer').get('/api/v1/now?station=ME3').json
    assert 'Missing_Player' in now['station']['state']['fault'] and now['station']['online']
    assert isinstance(now['station']['analysis']['players'], list)      # the Core reports its analysis players (2.1)
    assert sql("SELECT mem_kb > 0 FROM qsr_test.cores WHERE core = 'E2ECORE'", fetch=True)[0][0]


def test_one_player_zone_end_to_end(app, tmp_path, monkeypatch):
    """radio.lua running a two-player station and a one-player zone (MockCore) against this server: the zone
    reports one player, uploads its music only, gets plans naming only the next song and never plays a break (a
    bumper every song asked for); the station beside it plays its bumpers."""
    lupa = pytest.importorskip('lupa.lua53')
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    os.symlink(os.path.join(repo, 'qsys'), tmp_path / 'qsys')
    monkeypatch.chdir(tmp_path)
    client = app.test_client()
    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    L.execute('dofile("qsys/test/mock_core.lua")')
    if L.eval('MockCore.zones') is None:
        pytest.skip('this MockCore has no one-player zones')
    L.execute("""local wiring, cfg = MockCore.stations(1, { ids = { "OPR" } })
                 local zw, zc = MockCore.zones(1, { ids = { "OPZ" } })
                 wiring[#wiring + 1], cfg[2] = zw[1], zc[1]
                 local function songs(p, n)
                   local o = {}
                   for i = 1, n do o[i] = { name = string.format("%s Artist%d - Song%02d.mp3", p, i % 5, i), duration = 25 + i % 9, level = -10 } end
                   return o
                 end
                 MockCore.setup{ seed = 3, players = wiring, dirs = { ["Radio/"] = songs("R", 12), ["Zone/"] = songs("Z", 12),
                   ["Imaging/"] = { { name = "id.wav", duration = 5, level = -6 } } } }
                 OP_CFG = cfg""")

    def handler(req):
        r = client.post(req.Url.split('radio-server', 1)[1], data=req.Data,
                        headers=dict(req.Headers.items()), content_type='application/json')
        return r.status_code, r.get_data(as_text=True)
    L.globals().MockCore.httpHandler = handler
    admin = signed_in(app, 'admin')
    for st, folder in (('OPR', 'Radio/'), ('OPZ', 'Zone/')):       # both with an imaging folder and a bumper every song
        sync(app, station=st, boot='before')
        assert admin.post('/api/v1/settings', headers=WEB, json={'settings': {'music_dir': folder, 'imaging_dir': 'Imaging/',
                          'bumper_every_n_tracks': 1}, 'scope': 'station', 'station': st}).status_code == 200
    L.execute("""MockCore.loadRadio(function(C) C.SERVER_URL, C.SERVER_TOKEN, C.CORE_ID, C.STATIONS, C.ANALYZERS =
                                                  "http://radio-server", "tok", "OPCORE", OP_CFG, {} end)""")
    L.eval('MockCore.run')(400)
    kinds = {st: dict(sql("SELECT kind, count(*) FROM qsr_test.play_log WHERE station = %s AND boot <> 'before' GROUP BY kind",
                          (st,), fetch=True)) for st in ('OPR', 'OPZ')}
    assert kinds['OPZ'].get('music', 0) >= 10 and set(kinds['OPZ']) == {'music'}, kinds
    assert kinds['OPR'].get('imaging', 0) >= 3, kinds
    players, plan = sql("SELECT players, plan FROM qsr_test.stations WHERE station = 'OPZ'", fetch=True)[0]
    assert players == 1 and set(plan) <= {'next', 'next_why'}, plan
    assert sql("SELECT players FROM qsr_test.stations WHERE station = 'OPR'", fetch=True)[0][0] == 2
    assert sql("SELECT DISTINCT kind FROM qsr_test.library WHERE station = 'OPZ' AND present", fetch=True) == [['music']]
    now = signed_in(app, 'viewer').get('/api/v1/now?station=OPZ&ahead=5').json['station']
    assert set(now['state']['decks']) == {'A'} and set(now['meters']) == {'A'} and all(x['kind'] == 'music' for x in now['ahead'])


def test_coming_up_forecast(app):
    songs = [{'key': f'M/{c}.mp3', 'dir': 'M/', 'file': f'{c}.mp3', 'kind': 'music', 'artist': c.upper()} for c in 'abcdefghij']
    spots = [{'key': 'Ads/h.wav', 'dir': 'Ads/', 'file': 'h.wav', 'kind': 'ad'}, {'key': 'I/b.wav', 'dir': 'I/', 'file': 'b.wav', 'kind': 'imaging'}]
    library_post(app, 'FC', songs + spots, ads_dir='Ads/')
    op = signed_in(app, 'operator')
    assert op.post('/api/v1/ads', json={'station': 'FC', 'key': 'Ads/h.wav', 'mode': 'hourly', 'per_hour': 4}, headers=WEB).status_code == 200
    now_m = float(sql("SELECT EXTRACT(EPOCH FROM NOW() - date_trunc('hour', NOW())) / 60", fetch=True)[0][0])
    slot = next(m for m in [7.5, 22.5, 37.5, 52.5, 67.5] if m > now_m + 1)       # the ad's next slot (one hourly ad: phase 0.5)
    remaining = (slot - now_m) * 60 + 30 + 4                                      # this song changes 30 s after it opens
    state = dict(STATE, queue=[{'key': 'M/c.mp3', 'file': 'c.mp3'}], tracks_since_bumper=2, tracks_since_ad=0,
                 decks={'A': {'state': 'playing', 'live': True, 'kind': 'music', 'key': 'M/a.mp3', 'file': 'a.mp3', 'artist': 'A',
                              'elapsed': 10, 'remaining': remaining, 'duration': 300}},
                 settings={'bumper_every_n_tracks': 3, 'ad_every_n_tracks': 0, 'ads_per_break': 2, 'crossfade_s': 4})
    sync(app, station='FC', state=state)
    a = op.get('/api/v1/now?station=FC&ahead=10').json['station']['ahead']
    kinds = [(x['kind'], x['why']) for x in a]
    assert kinds[0] == ('ad', 'hourly') and a[0]['key'] == 'Ads/h.wav'           # the slot opens before this song ends
    assert kinds[1] == ('music', 'queue') and a[1]['key'] == 'M/c.mp3'            # then the operator queue
    assert ('imaging', 'bumper') in kinds[2:4]                                     # bumper every 3: due after the next song
    assert len(a) == 10 and [x['at'] for x in a] == sorted(x['at'] for x in a)
    assert len({x['key'] for x in a if x['kind'] == 'music'}) == sum(1 for x in a if x['kind'] == 'music')   # no repeats
    assert 'ahead' not in op.get('/api/v1/now?station=FC').json['station']             # only when asked for


def test_forecast_fits_later_songs_to_ad_slots(app):
    """Not only the next song: every song the forecast lists is picked as the server will, by length near a slot."""
    names = ['c'] + [f's{i:02d}' for i in range(40)]
    songs = [{'key': f'M/{c}.mp3', 'dir': 'M/', 'file': f'{c}.mp3', 'kind': 'music', 'artist': c.upper()} for c in names]
    library_post(app, 'FF', songs + [{'key': 'Ads/h.wav', 'dir': 'Ads/', 'file': 'h.wav', 'kind': 'ad'}], ads_dir='Ads/')
    for i, c in enumerate(names):                   # lengths 2:30 .. 9:00, shuffled over the keys
        sql("UPDATE qsr_test.media_files SET duration_s = %s WHERE id = (SELECT file_id FROM qsr_test.library "
            "WHERE station = 'FF' AND key = %s)",
            (64 if c == 'c' else 150 + (i * 97) % 390, f'M/{c}.mp3'))
    op, admin = signed_in(app, 'operator'), signed_in(app, 'admin')
    op.post('/api/v1/ads', json={'station': 'FF', 'key': 'Ads/h.wav', 'mode': 'hourly', 'per_hour': 4}, headers=WEB)
    tolerance = lambda v: admin.post('/api/v1/settings', json={'settings': {'ad_fit_tolerance_s': v}}, headers=WEB)
    assert tolerance(0).status_code == 200                         # always pick by length
    state = dict(STATE, queue=[{'key': 'M/c.mp3', 'file': 'c.mp3'}],
                 decks={'A': {'state': 'playing', 'live': True, 'kind': 'music', 'key': 'M/x.mp3', 'file': 'x.mp3',
                              'remaining': 60, 'duration': 300}},
                 settings={'bumper_every_n_tracks': 0, 'ad_every_n_tracks': 0, 'crossfade_s': 4})
    sync(app, station='FF', state=state)
    whys = [(x['kind'], x['why']) for x in op.get('/api/v1/now?station=FF&ahead=10').json['station']['ahead']]
    assert whys[0] in (('music', 'queue'), ('ad', 'hourly'))
    assert ('music', 'for_ad') in whys, whys            # a later song picked by length for the next slot
    tolerance(600)                                      # fair rotation unless an ad would be 10 min late: no fitting
    whys = [(x['kind'], x['why']) for x in op.get('/api/v1/now?station=FF&ahead=10').json['station']['ahead']]
    tolerance(None)
    assert ('music', 'for_ad') not in whys, whys
    # the server-only setting is not sent to the Core
    assert 'ad_fit_tolerance_s' not in (sync(app, station='FF', settings_rev='').json.get('settings') or {})


def test_quiet_start_and_end_can_be_adjusted_and_forgotten(app):
    items = [{'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music'},
             {'key': 'I/b.wav', 'dir': 'I/', 'file': 'b.wav', 'kind': 'imaging'}]
    library_post(app, 'T1', items)
    sync(app, station='T1', events=[
        ev(1, 'track_start', deck='A', file='a.mp3', dir='M/', duration=200.0),
        ev(2, 'track_end', deck='A', file='a.mp3', dir='M/', reason='end', lead_in=0.8, trail=6.0),
        ev(3, 'bumper_start', deck='B', file='b.wav', dir='I/', duration=8.0),
        ev(4, 'bumper_end', deck='B', file='b.wav', dir='I/', reason='end', lead_in=0.2, trail=1.0)])
    assert library_post(app, 'T1', items).json['learned'] == {'M/a.mp3': {'trail': 6.0, 'lead': 0.8},
                                                           'I/b.wav': {'trail': 1.0, 'lead': 0.2}}   # every kind
    op, viewer = signed_in(app, 'operator'), signed_in(app, 'viewer')
    trim = lambda c, **b: c.post('/api/v1/library/trim', json=dict(station='T1', **b), headers=WEB)
    forget = lambda c, **b: c.post('/api/v1/library/forget', json=dict(station='T1', **b), headers=WEB)
    assert trim(viewer, key='M/a.mp3', lead=None, trail=0.5).status_code == 403
    assert trim(op, key='M/a.mp3', lead=None, trail=-1).status_code == 400
    assert trim(op, key='M/nope.mp3', lead=None, trail=1).status_code == 400
    assert trim(op, key='M/a.mp3', lead=None, trail=0.5).status_code == 200
    assert [c['cmd'] for c in sync(app, station='T1').json['commands']][-1] == 'rescan'      # the Core picks it up
    assert library_post(app, 'T1', items).json['learned']['M/a.mp3'] == {'trail': 0.5, 'lead': 0.8, 'fixed': True}
    row = next(i for i in op.get('/api/v1/library?station=T1&only=adjusted').json['items'])
    assert (row['key'], row['trail_s'], row['trail_set_s'], row['audible_s']) == ('M/a.mp3', 6.0, 0.5, pytest.approx(198.7))
    sync(app, station='T1', events=[                     # still measured underneath, never over the operator's figure
        ev(5, 'track_start', deck='A', file='a.mp3', dir='M/', duration=200.0),
        ev(6, 'track_end', deck='A', file='a.mp3', dir='M/', reason='end', lead_in=0.7, trail=5.0)])
    assert library_post(app, 'T1', items).json['learned']['M/a.mp3'] == {'trail': 0.5, 'lead': 0.7, 'fixed': True}
    assert forget(op).status_code == 400
    assert forget(op, kind='music').json['files'] == 1
    assert library_post(app, 'T1', items).json['learned']['M/a.mp3'] == {'trail': 0.5, 'fixed': True}   # the operator's stays
    assert trim(op, key='M/a.mp3', lead=None, trail=None).status_code == 200                       # back to measured
    assert forget(op, key='I/b.wav').json['files'] == 1
    assert library_post(app, 'T1', items).json['learned'] == {}


def test_average_level_and_deck_readout(app):
    """A file's average level is the energy average of its measurements (plays and analysis), weighted by
    the seconds each heard; its peak is the highest. The Core gets both; a replaced file starts again;
    Forget measured forgets them. The decks' gain and meters from each sync reach the panel."""
    items = [{'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music', 'size': 30},
             {'key': 'M/b.mp3', 'dir': 'M/', 'file': 'b.mp3', 'kind': 'music', 'size': 40}]
    library_post(app, 'LV1', items)
    sync(app, station='LV1', events=[
        ev(1, 'track_start', deck='A', file='a.mp3', dir='M/', duration=200.0),
        ev(2, 'track_end', deck='A', file='a.mp3', dir='M/', reason='end', level=4.0, level_s=150.0, peak=12.0),
        ev(3, 'analysis', file='a.mp3', dir='M/', ok=True, lead_in=0.4, trail=1.5, duration=200.0, level=10.0, level_s=50.0,
           peak=14.0),
        ev(4, 'track_start', deck='B', file='a.mp3', dir='M/', duration=200.0, trim=-6.4),
        ev(5, 'track_end', deck='B', file='a.mp3', dir='M/', reason='cut', level=99.0, level_s=10.0, peak=13.0),  # off the scale
        ev(6, 'track_start', deck='A', file='a.mp3', dir='M/', duration=200.0, trim=-6.4),
        ev(7, 'track_end', deck='A', file='a.mp3', dir='M/', reason='cut', level=-30.0),                        # no seconds
        ev(8, 'track_start', deck='B', file='b.mp3', dir='M/', duration=100.0),
        ev(9, 'track_end', deck='B', file='b.mp3', dir='M/', reason='end', level=-9.5, level_s=90.0, peak=2.0)])
    level = 10 * math.log10((10 ** 0.4 * 150 + 10 ** 1.0 * 50) / 200)
    row = sql("SELECT level_db, level_s, peak_dbfs FROM qsr_test.library_figures WHERE station='LV1' AND key='M/a.mp3'",
              fetch=True)[0]
    assert (row[0], row[1], row[2]) == (pytest.approx(level), 200.0, 14.0)
    learned = library_post(app, 'LV1', items).json['learned']
    assert learned['M/a.mp3'] == {'trail': 1.5, 'lead': 0.4, 'analyzed': True, 'level': round(level, 1), 'peak': 14.0}
    assert learned['M/b.mp3'] == {'level': -9.5, 'peak': 2.0}
    op = signed_in(app, 'operator')
    rows = op.get('/api/v1/library?station=LV1&sort=quietest').json['items']
    assert [r['key'] for r in rows] == ['M/b.mp3', 'M/a.mp3'] and rows[1]['level_db'] == pytest.approx(level)
    assert rows[1]['level_s'] == 200.0 and rows[1]['peak_dbfs'] == 14.0
    assert [r['key'] for r in op.get('/api/v1/library?station=LV1&sort=loudest').json['items']] == ['M/a.mp3', 'M/b.mp3']
    items[1]['size'] = 41                                  # replaced with a new file: measured afresh
    assert 'M/b.mp3' not in library_post(app, 'LV1', items).json['learned']
    assert op.post('/api/v1/library/forget', json={'station': 'LV1', 'key': 'M/a.mp3'}, headers=WEB).json['files'] == 1
    assert tuple(sql("SELECT level_db, level_s, peak_dbfs FROM qsr_test.library_figures WHERE station='LV1' AND key='M/a.mp3'",
                     fetch=True)[0]) == (None, None, None)
    # the decks' readout: cleaned, and gone when a Core does not send it
    sync(app, station='LV1', state=STATE, meters={'A': {'peak': 3.4, 'rms': -2.1, 'gain': -6.4},
                                                  'B': {'peak': -500, 'rms': 'loud', 'gain': -100}, 'C': {'peak': 1}})
    assert op.get('/api/v1/now?station=LV1').json['station']['meters'] == {
        'A': {'peak': 3.4, 'rms': -2.1, 'gain': -6.4}, 'B': {'peak': -120.0, 'gain': -100.0}}
    sync(app, station='LV1', meters='nope')
    assert op.get('/api/v1/now?station=LV1').json['station']['meters'] is None


def analyzer_sync(app, *parts, core='AC1', rev='', players=('Analysis_Player',), **body):
    """A 2.1 Core with analysis players: its stations' parts, its list's rev and its players' report."""
    body.setdefault('analyzers', [{'name': p, 'state': 'idle'} for p in players])
    return batch(app, *parts, core=core, analyze_rev=rev, **body)


def all_stations(app, **settings):
    """Save settings for All stations (None: back to the default)."""
    r = signed_in(app, 'admin').post('/api/v1/settings', json={'settings': settings}, headers=WEB)
    assert r.status_code == 200, r.get_data(as_text=True)


def test_file_analysis_job(app):
    """The job is the Core's (its analysis players measure files for every station on it): a run started on a
    library page, the results (final figures) in the reply to the sync that brings them, a failure waiting for
    the next run, one file again, a changed file, Stop, the daily run."""
    items = [{'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music', 'size': 1},
             {'key': 'M/b.mp3', 'dir': 'M/', 'file': 'b.mp3', 'kind': 'music', 'size': 2},
             {'key': 'I/c.wav', 'dir': 'I/', 'file': 'c.wav', 'kind': 'imaging', 'size': 3}]
    library_post(app, 'AN1', items)
    op, viewer = signed_in(app, 'operator'), signed_in(app, 'viewer')
    analyze = lambda c, **b: c.post('/api/v1/analyze', json=dict(station='AN1', **b), headers=WEB)
    job = lambda rev='', *evs: analyzer_sync(app, {'station': 'AN1', 'events': list(evs)}, core='ANC', rev=rev).json
    files = lambda *keys: [{'station': 'AN1', 'key': k} for k in keys]
    view = lambda: op.get('/api/v1/now?station=AN1').json['station']['analysis']
    all_stations(app, analyze_at='off')
    try:
        assert analyze(op, scope='new').status_code == 400                # no Core yet (1.x): nowhere to run it
        assert 'analyze' not in job()                                     # no run: nothing to do
        assert view() == {'active': False, 'left': 0, 'never': 3, 'failed': 0, 'run': False, 'can': True, 'gone': False,
                          'online': True, 'players': [{'name': 'Analysis_Player', 'state': 'idle'}], 'auto_at': None, 'at': 'off',
                          'last': None}
        assert analyze(viewer, scope='new').status_code == 403
        assert analyze(op, scope='bogus').status_code == 400
        assert analyze(op, scope='new').status_code == 200
        r = job()
        assert r['analyze'] == files('I/c.wav', 'M/a.mp3', 'M/b.mp3') and r['analyze_rev']      # short files first
        assert 'analyze' not in job(r['analyze_rev'])                                          # unchanged: not resent
        assert 'analyze' not in batch(app, {'station': 'AN1'}, core='ANC').json                 # no analysis player
        # results: figures replace what plays learned, and plays no longer learn over them; the list in the same reply
        r = job(r['analyze_rev'],
                ev(1, 'track_start', deck='A', file='a.mp3', dir='M/', duration=200.0),
                ev(2, 'track_end', deck='A', file='a.mp3', dir='M/', reason='end', lead_in=3.0, trail=9.0),
                ev(3, 'analysis', file='a.mp3', dir='M/', ok=True, lead_in=0.4, trail=1.5, duration=201.0),
                ev(4, 'analysis', file='c.wav', dir='I/', ok=False, status='Unsupported format'),
                ev(5, 'track_start', deck='B', file='a.mp3', dir='M/', duration=201.0),
                ev(6, 'track_end', deck='B', file='a.mp3', dir='M/', reason='end', lead_in=2.0, trail=0.2))
        assert r['analyze'] == files('M/b.mp3')                                # a failed file waits for the next run
        row = sql("SELECT lead_in_s, trail_s, duration_s, analyzed_at IS NOT NULL FROM qsr_test.library_figures "
                  "WHERE station='AN1' AND key='M/a.mp3'", fetch=True)[0]
        assert tuple(row) == (0.4, 1.5, 201.0, True)
        assert library_post(app, 'AN1', items).json['learned']['M/a.mp3'] == {'trail': 1.5, 'lead': 0.4, 'analyzed': True}
        now = view()
        assert (now['active'], now['left'], now['never'], now['failed'], now['can'], now['run']) == (True, 1, 2, 1, True, True)
        assert now['last']                                                     # the latest result: library pages reload on it
        rows = {i['key']: i for i in op.get('/api/v1/library?station=AN1&only=not_analyzed').json['items']}
        assert set(rows) == {'M/b.mp3', 'I/c.wav'}
        assert rows['M/b.mp3']['analyze_queued'] is True and rows['I/c.wav']['analyze_queued'] is False
        assert rows['I/c.wav']['analyze_error'] == 'Unsupported format' and rows['I/c.wav']['analyze_failed_at']
        with logged() as lines:
            r = job(r['analyze_rev'], ev(7, 'analysis', file='b.mp3', dir='M/', ok=True, lead_in=0.1, trail=0.3, duration=100.0))
        assert r['analyze'] == [] and r['analyze_rev'] == ''                   # done: the Core's list is cleared
        assert (20, 'ANALYSIS core=ANC done') in lines
        assert sql("SELECT analyze_from FROM qsr_test.cores WHERE core = 'ANC'", fetch=True)[0][0] is None
        assert view()['run'] is False
        # one file again; a changed file is owed again; re-analyze all; stop
        assert analyze(op, key='M/nope.mp3').status_code == 400
        assert analyze(op, key='M/b.mp3').status_code == 200               # just that file: no run, keeps its figures
        assert job()['analyze'] == files('M/b.mp3')
        assert sql("SELECT analyzed_at IS NOT NULL FROM qsr_test.library_figures WHERE station='AN1' AND key='M/b.mp3'",
                   fetch=True)[0][0]
        now = view()
        assert (now['active'], now['left'], now['run']) == (True, 1, True)
        rows = {i['key']: i for i in op.get('/api/v1/library?station=AN1').json['items']}
        assert rows['M/b.mp3']['analyze_queued'] is True and rows['M/a.mp3']['analyze_queued'] is False
        analyze(op, scope='stop')
        assert job('x')['analyze'] == [] and view()['run'] is False
        items[0]['size'] = 99
        library_post(app, 'AN1', items)
        assert analyze(op, scope='new').status_code == 200                 # a new run: the failed file is tried again
        assert job()['analyze'] == files('I/c.wav', 'M/a.mp3')
        analyze(op, scope='all')
        assert job()['analyze'] == files('I/c.wav', 'M/a.mp3', 'M/b.mp3')
        # the daily run starts by itself once its time has passed today, once a day
        analyze(op, scope='stop')
        all_stations(app, analyze_at='00:00')
        with logged() as lines:
            assert job()['analyze'] == files('I/c.wav', 'M/a.mp3')
        assert (20, 'ANALYSIS core=ANC daily run at 00:00: files not analyzed yet') in lines
        assert view()['auto_at'] and view()['at'] == '00:00'
        analyze(op, scope='stop')
        assert job('x')['analyze'] == []                                       # not again today
    finally:
        all_stations(app, analyze_at=None)


def test_analysis_list_is_per_core(app):
    """A Core's list: the files of every station on it, each file once, nothing of another Core's stations nor of a
    station gone from it; Re-analyze all and Stop on a station's page act on its Core; the players' report is cleaned,
    kept and shown. A Core before 2.1.0 (a 2.0.0 part's analyze_rev, or the 1.x body) gets no work: its list is cleared."""
    item = lambda d, f, size, kind='music': {'key': d + f, 'dir': d, 'file': f, 'kind': kind, 'size': size}
    library_post(app, 'PA1', [item('M/', 's.mp3', 8001), item('M/', 'p1.mp3', 8002), item('I/', 'i1.wav', 8003, 'imaging')])
    library_post(app, 'PA2', [item('Zone/', 's.mp3', 8001), item('Zone/', 'p2.mp3', 8004)])
    library_post(app, 'PB1', [item('X/', 's.mp3', 8001), item('X/', 'p3.mp3', 8005)])
    f = lambda st, key: {'station': st, 'key': key}
    op = signed_in(app, 'operator')
    view = lambda st: op.get(f'/api/v1/now?station={st}').json['station']['analysis']
    a = analyzer_sync(app, {'station': 'PA1'}, {'station': 'PA2'}, core='PCA').json           # 'right away' (the default)
    assert a['analyze'] == [f('PA1', 'I/i1.wav'), f('PA1', 'M/p1.mp3'), f('PA1', 'M/s.mp3'), f('PA2', 'Zone/p2.mp3')]
    b = analyzer_sync(app, {'station': 'PB1'}, core='PCB').json
    assert b['analyze'] == [f('PB1', 'X/p3.mp3'), f('PB1', 'X/s.mp3')]
    # the shared file measured for PA2: the file's, so PA1's row is done too (PCB's list drops it when made again)
    a = analyzer_sync(app, {'station': 'PA2', 'events': [ev(1, 'analysis', file='s.mp3', dir='Zone/', ok=True, lead_in=0.1,
                                                            trail=0.2, duration=60.0)]},
                      {'station': 'PA1'}, core='PCA', rev=a['analyze_rev']).json
    assert a['analyze'] == [f('PA1', 'I/i1.wav'), f('PA1', 'M/p1.mp3'), f('PA2', 'Zone/p2.mp3')]
    sql("UPDATE qsr_test.cores SET owed_at = NOW() - INTERVAL '61 seconds' WHERE core = 'PCB'")
    b = analyzer_sync(app, {'station': 'PB1'}, core='PCB', rev=b['analyze_rev']).json
    assert b['analyze'] == [f('PB1', 'X/p3.mp3')]
    # Re-analyze all on PA2's page: the files of PA1 and PA2 (the Core's run), not PB1's
    assert op.post('/api/v1/analyze', json={'station': 'PB9', 'scope': 'all'}, headers=WEB).status_code == 400
    with logged() as lines:
        assert op.post('/api/v1/analyze', json={'station': 'PA2', 'scope': 'all'}, headers=WEB).status_code == 200
    assert (20, 'ANALYSIS station=PA2 core=PCA every file by=operator') in lines
    a = analyzer_sync(app, {'station': 'PA1'}, {'station': 'PA2'}, core='PCA', rev=a['analyze_rev']).json
    assert a['analyze'] == [f('PA1', 'I/i1.wav'), f('PA1', 'M/p1.mp3'), f('PA1', 'M/s.mp3'), f('PA2', 'Zone/p2.mp3')]
    assert 'analyze' not in analyzer_sync(app, {'station': 'PB1'}, core='PCB', rev=b['analyze_rev']).json
    assert [(view(st)['run'], view(st)['left']) for st in ('PA1', 'PA2', 'PB1')] == [(True, 3), (True, 2), (False, 1)]
    # Stop on PA1's page: the Core's run and every request of its stations end; new files still go right away
    assert op.post('/api/v1/analyze', json={'station': 'PA2', 'key': 'Zone/s.mp3'}, headers=WEB).status_code == 200
    assert op.post('/api/v1/analyze', json={'station': 'PB1', 'key': 'X/s.mp3'}, headers=WEB).status_code == 200
    assert op.post('/api/v1/analyze', json={'station': 'PA1', 'scope': 'stop'}, headers=WEB).status_code == 200
    wanted = sql("SELECT station FROM qsr_test.library WHERE analyze_wanted AND station IN ('PA1', 'PA2', 'PB1')", fetch=True)
    assert [r[0] for r in wanted] == ['PB1']                                     # another Core's request stays
    a = analyzer_sync(app, {'station': 'PA1'}, {'station': 'PA2'}, core='PCA', rev=a['analyze_rev']).json
    assert a['analyze'] == [f('PA1', 'I/i1.wav'), f('PA1', 'M/p1.mp3'), f('PA2', 'Zone/p2.mp3')] and not view('PA2')['run']
    # a station its Core no longer syncs (taken out of CFG.STATIONS) is left out
    sql("UPDATE qsr_test.stations SET last_seen = NOW() - INTERVAL '11 minutes' WHERE station = 'PA2'")
    sql("UPDATE qsr_test.cores SET owed_at = NULL WHERE core = 'PCA'")
    a = analyzer_sync(app, {'station': 'PA1'}, core='PCA', rev=a['analyze_rev']).json
    assert a['analyze'] == [f('PA1', 'I/i1.wav'), f('PA1', 'M/p1.mp3')]
    # ... and its page says so: not Analyzing, no row queued (its files are never listed), not even one asked for
    assert op.post('/api/v1/analyze', json={'station': 'PA2', 'key': 'Zone/p2.mp3'}, headers=WEB).status_code == 200
    assert (view('PA2')['gone'], view('PA2')['can'], view('PA2')['active'], view('PA2')['left']) == (True, False, False, 1)
    assert not any(i['analyze_queued'] for i in op.get('/api/v1/library?station=PA2').json['items'])
    assert (view('PA1')['gone'], view('PA1')['can']) == (False, True)
    # the players' report: cleaned, kept while the Core sends none, shown on the library page; a new boot forgets it
    report = [{'name': 'Analysis_Player', 'state': 'measuring', 'station': 'PA1', 'key': 'M/p1.mp3', 'extra': {'x': 1}},
              {'name': 'Analysis_Player2', 'state': 'idle', 'station': None}, {'state': 'idle'}, 'junk', {'name': '<b>' * 50}]
    players = [{'name': 'Analysis_Player', 'state': 'measuring', 'station': 'PA1', 'key': 'M/p1.mp3'},
               {'name': 'Analysis_Player2', 'state': 'idle'}, {'name': ('<b>' * 50)[:100], 'state': 'idle'}]
    analyzer_sync(app, {'station': 'PA1'}, core='PCA', rev=a['analyze_rev'], analyzers=report)
    assert view('PA1')['players'] == players and view('PA1')['can'] is True and view('PA1')['active'] is True
    # the Core stops syncing (off, or the server unreachable): its last report is not what its players do, so not
    # 'measuring', Analyzing (the header) nor queued rows, until it syncs again
    queued = lambda: {i['key']: i['analyze_queued'] for i in op.get('/api/v1/library?station=PA1').json['items']}
    assert queued()['M/p1.mp3'] is True
    sql("UPDATE qsr_test.cores SET last_seen = NOW() - INTERVAL '11 seconds' WHERE core = 'PCA'")
    assert (view('PA1')['online'], view('PA1')['active'], view('PA1')['can']) == (False, False, True)
    assert not any(queued().values())
    batch(app, {'station': 'PA1'}, core='PCA', analyze_rev=a['analyze_rev'])
    assert view('PA1')['players'] == players and (view('PA1')['online'], view('PA1')['active']) == (True, True)
    assert queued()['M/p1.mp3'] is True
    batch(app, {'station': 'PA1'}, core='PCA', boot='B2', analyzers={})              # restarted with none (Lua's empty list)
    assert (view('PA1')['players'], view('PA1')['can'], view('PA1')['active']) == ([], False, False)
    # a Core before 2.1.0 gets no work: its list is cleared once, then nothing; a page cannot start a run for it
    r = batch(app, {'station': 'PO1', 'analyze_rev': 'abc123'}, core='OLDCORE').json
    assert (r['stations']['PO1']['analyze'], r['stations']['PO1']['analyze_rev']) == ([], '') and 'analyze' not in r
    assert 'analyze' not in batch(app, {'station': 'PO1', 'analyze_rev': ''}, core='OLDCORE').json['stations']['PO1']
    assert (view('PO1')['can'], view('PO1')['players']) == (False, None)
    old = sync(app, station='PO2', analyze_rev='abc123').json                         # 1.x
    assert (old['analyze'], old['analyze_rev']) == ([], '') and 'analyze' not in sync(app, station='PO2', analyze_rev='').json
    assert op.post('/api/v1/analyze', json={'station': 'PO2', 'scope': 'new'}, headers=WEB).status_code == 400


def test_a_1x_stations_run_goes_on_from_its_core(app):
    """A run a station had on a 1.x Core (kept on its row by migration 0017: such as 1.3.0's level re-analysis of
    every file) waits while the 1.x Core syncs, then goes on as its Core's at its first 2.x sync: the files analyzed
    before the run started are owed again, not only those never analyzed."""
    item = lambda f, size: {'key': 'M/' + f, 'dir': 'M/', 'file': f, 'kind': 'music', 'size': size}
    library_post(app, 'OX1', [item('u1.mp3', 8301), item('u2.mp3', 8302)])
    sync(app, station='OX1', analyze_rev='abc')                                        # the 1.x Core
    sql("UPDATE qsr_test.media_files SET analyzed_at = NOW() - INTERVAL '2 days' WHERE file = 'u1.mp3' AND size = 8301")
    sql("""UPDATE qsr_test.stations SET analyze_from = NOW() - INTERVAL '1 day', analyze_run_at = NOW() - INTERVAL '1 day'
           WHERE station = 'OX1'""")
    old = sync(app, station='OX1', analyze_rev='').json
    assert 'analyze' not in old                                                        # still no work for the 1.x Core
    job = lambda: tuple(sql("SELECT analyze_from IS NOT NULL, analyze_run_at IS NOT NULL FROM qsr_test.stations "
                            "WHERE station = 'OX1'", fetch=True)[0])
    assert job() == (True, True)
    with logged() as lines:
        r = analyzer_sync(app, {'station': 'OX1'}, core='OXC').json                    # radio.lua 2.1.0 pasted
    assert (20, "ANALYSIS station=OX1 core=OXC its run goes on as the Core's") in lines
    assert job() == (False, False)
    assert sql("SELECT analyze_from < NOW() - INTERVAL '23 hours' FROM qsr_test.cores WHERE core = 'OXC'", fetch=True)[0][0]
    assert r['analyze'] == [{'station': 'OX1', 'key': 'M/u1.mp3'}, {'station': 'OX1', 'key': 'M/u2.mp3'}]
    view = signed_in(app, 'viewer').get('/api/v1/now?station=OX1').json['station']['analysis']
    assert (view['run'], view['left']) == (True, 2)
    with logged() as lines:
        analyzer_sync(app, {'station': 'OX1'}, core='OXC', rev=r['analyze_rev'])
    assert not [m for _, m in lines if 'goes on' in m]                                   # once


def test_nothing_queued_on_a_core_without_an_analysis_player(app):
    """A Core that cannot measure files (radio.lua 2.0.0, until 2.1.0 is pasted; or CFG.ANALYZERS = {}) owes nothing:
    no row of its stations is shown queued, even in 'right away' mode (the default) or asked for on its own; once it
    reports an analysis player, they are."""
    item = lambda f, size: {'key': 'M/' + f, 'dir': 'M/', 'file': f, 'kind': 'music', 'size': size}
    library_post(app, 'NQ1', [item('n1.mp3', 8401), item('n2.mp3', 8402)])
    op = signed_in(app, 'operator')
    queued = lambda: {i['key']: i['analyze_queued'] for i in op.get('/api/v1/library?station=NQ1').json['items']}
    batch(app, {'station': 'NQ1'}, core='NQC')                                         # 2.0.0: no analysis player report
    assert queued() == {'M/n1.mp3': False, 'M/n2.mp3': False}
    assert op.post('/api/v1/analyze', json={'station': 'NQ1', 'key': 'M/n1.mp3'}, headers=WEB).status_code == 200
    assert queued() == {'M/n1.mp3': False, 'M/n2.mp3': False}
    analyzer_sync(app, {'station': 'NQ1'}, core='NQC', boot='B2', players=())        # 2.1.0 with CFG.ANALYZERS = {}
    assert queued() == {'M/n1.mp3': False, 'M/n2.mp3': False}
    assert op.get('/api/v1/now?station=NQ1').json['station']['analysis']['can'] is False
    r = analyzer_sync(app, {'station': 'NQ1'}, core='NQC', boot='B3').json            # an analysis player added
    assert queued() == {'M/n1.mp3': True, 'M/n2.mp3': True}
    assert r['analyze'] == [{'station': 'NQ1', 'key': 'M/n1.mp3'}, {'station': 'NQ1', 'key': 'M/n2.mp3'}]


def test_analyze_new_files_right_away_or_off(app):
    """'right away' (the default): a new file is listed at once, no run needed; one that fails is not tried again in a
    loop but RETRY_H hours later; off: only what a library page starts (the daily run: test_file_analysis_job)."""
    item = lambda f, size: {'key': 'M/' + f, 'dir': 'M/', 'file': f, 'kind': 'music', 'size': size}
    files = lambda *keys: [{'station': 'RA1', 'key': k} for k in keys]
    view = lambda: signed_in(app, 'viewer').get('/api/v1/now?station=RA1').json['station']['analysis']
    library_post(app, 'RA1', [item('r1.mp3', 8101)])
    r = analyzer_sync(app, {'station': 'RA1'}, core='RAC').json
    assert r['analyze'] == files('M/r1.mp3')
    assert (view()['active'], view()['left'], view()['run'], view()['at']) == (True, 1, False, 'right away')
    library_post(app, 'RA1', [item('r1.mp3', 8101), item('r2.mp3', 8102)])             # a new file: listed at the next sync
    r = analyzer_sync(app, {'station': 'RA1'}, core='RAC', rev=r['analyze_rev']).json
    assert r['analyze'] == files('M/r1.mp3', 'M/r2.mp3')
    r = analyzer_sync(app, {'station': 'RA1', 'events': [ev(1, 'analysis', file='r1.mp3', dir='M/', ok=False, status='timed out'),
                                                          ev(2, 'analysis', file='r2.mp3', dir='M/', ok=True, lead_in=0.0,
                                                             trail=0.0, duration=30.0)]}, core='RAC', rev=r['analyze_rev']).json
    assert r['analyze'] == [] and r['analyze_rev'] == ''                                 # the failed one waits
    assert (view()['active'], view()['never'], view()['failed']) == (False, 1, 1)
    sql("UPDATE qsr_test.cores SET owed_at = NULL WHERE core = 'RAC'")
    assert 'analyze' not in analyzer_sync(app, {'station': 'RA1'}, core='RAC').json      # still waiting: no tight loop
    # a day later it is tried again (the list made again by the clock)
    sql("UPDATE qsr_test.media_files SET analyze_failed_at = NOW() - INTERVAL '25 hours' WHERE file = 'r1.mp3' AND size = 8101")
    sql("UPDATE qsr_test.cores SET owed_at = NOW() - INTERVAL '61 seconds' WHERE core = 'RAC'")
    r = analyzer_sync(app, {'station': 'RA1'}, core='RAC').json
    assert r['analyze'] == files('M/r1.mp3')
    # off: nothing until a run is started; right away again: at once
    all_stations(app, analyze_at='off')
    try:
        assert analyzer_sync(app, {'station': 'RA1'}, core='RAC', rev=r['analyze_rev']).json['analyze'] == []
        assert (view()['active'], view()['left'], view()['at']) == (False, 0, 'off')
    finally:
        all_stations(app, analyze_at=None)
    assert analyzer_sync(app, {'station': 'RA1'}, core='RAC').json['analyze'] == files('M/r1.mp3')


def test_analysis_list_is_cached(app, monkeypatch):
    """The Core's list is made again when a result comes in, when something it is made from changes (a library upload,
    a file asked for, a run started or stopped, the setting, Forget measured) or once a minute; not on every sync."""
    items = [{'key': f'M/c{i}.mp3', 'dir': 'M/', 'file': f'c{i}.mp3', 'kind': 'music', 'size': 8200 + i} for i in (1, 2)]
    library_post(app, 'CA1', items)
    made, real = [], control.analysis.owed
    monkeypatch.setattr(control.analysis, 'owed', lambda cur, core, *a, **k: made.append(core) or real(cur, core, *a, **k))
    op = signed_in(app, 'operator')
    send = lambda rev, *evs: analyzer_sync(app, {'station': 'CA1', 'events': list(evs)}, core='CAC', rev=rev).json
    rev = send('')['analyze_rev']
    assert made == ['CAC']
    for seq in (1, 2, 3):              # plays and the usual second: the cached list
        assert 'analyze' not in send(rev, ev(seq, 'track_start', deck='A', file='c1.mp3', dir='M/', duration=60.0))
    assert made == ['CAC'] and analyzer_sync(app, {'station': 'CA1'}, core='CAC', rev='').json['analyze_rev'] == rev
    changes = [
        lambda: send(rev, ev(10, 'analysis', file='c2.mp3', dir='M/', ok=True, lead_in=0.2, trail=1.0, duration=60.0)),
        lambda: library_post(app, 'CA1', items),
        lambda: op.post('/api/v1/analyze', json={'station': 'CA1', 'key': 'M/c2.mp3'}, headers=WEB),
        lambda: op.post('/api/v1/analyze', json={'station': 'CA1', 'scope': 'stop'}, headers=WEB),
        lambda: op.post('/api/v1/analyze', json={'station': 'CA1', 'scope': 'all'}, headers=WEB),
        lambda: op.post('/api/v1/library/forget', json={'station': 'CA1', 'kind': 'music'}, headers=WEB),
        lambda: all_stations(app, analyze_at='off'),
        lambda: all_stations(app, analyze_at=None),
        lambda: sql("UPDATE qsr_test.cores SET owed_at = NOW() - INTERVAL '61 seconds' WHERE core = 'CAC'"),
    ]
    try:
        for i, change in enumerate(changes):
            n = len(made)
            change()
            send(rev)
            send(rev)
            assert made.count('CAC') == n + 1, i
    finally:
        all_stations(app, analyze_at=None)


# ── Per-file figures, shared by every station ────────────────────────────────

FIGS = ('duration_s', 'lead_in_s', 'trail_s', 'lead_set_s', 'trail_set_s', 'level_db', 'level_s', 'peak_dbfs', 'unsupported',
        'analyzed_at')


def figures(station, key):
    """The figures a station sees for one of its files (library_figures), as a dict."""
    rows = sql(f"SELECT {', '.join(FIGS)}, file_id FROM qsr_test.library_figures WHERE station = %s AND key = %s",
               (station, key), fetch=True)
    return dict(zip(FIGS + ('file_id',), rows[0])) if rows else None


def test_figures_belong_to_the_file_not_the_station(app):
    """Name + size is the file: another station (another folder) gets its figures, a move keeps them, an edited
    file (new size) starts afresh, a file of unknown size is never shared."""
    a = [{'key': 'Music/x.mp3', 'dir': 'Music/', 'file': 'x.mp3', 'kind': 'music', 'size': 7001},
         {'key': 'Music/y.mp3', 'dir': 'Music/', 'file': 'y.mp3', 'kind': 'music'},                  # size unknown
         {'key': 'Music/z.mp3', 'dir': 'Music/', 'file': 'z.mp3', 'kind': 'music', 'size': 0}]       # the Core could not tell
    assert library_post(app, 'SH1', a).json['learned'] == {}
    sync(app, station='SH1', events=[
        ev(1, 'track_start', deck='A', file='x.mp3', dir='Music/', duration=200.0),
        ev(2, 'track_end', deck='A', file='x.mp3', dir='Music/', reason='end', lead_in=0.9, trail=4.0, level=-6.0, level_s=100.0,
           peak=3.0),
        ev(3, 'analysis', file='x.mp3', dir='Music/', ok=True, lead_in=0.5, trail=2.5, duration=201.0, level=-4.0, level_s=20.0,
           peak=5.0),
        ev(4, 'track_start', deck='B', file='y.mp3', dir='Music/', duration=100.0),
        ev(5, 'track_end', deck='B', file='y.mp3', dir='Music/', reason='end', lead_in=0.2, trail=1.0),
        ev(6, 'track_start', deck='A', file='z.mp3', dir='Music/', duration=90.0),
        ev(7, 'track_end', deck='A', file='z.mp3', dir='Music/', reason='end', lead_in=0.3, trail=1.1)])
    level = round(10 * math.log10((10 ** -0.6 * 100 + 10 ** -0.4 * 20) / 120), 1)
    x = {'trail': 2.5, 'lead': 0.5, 'analyzed': True, 'level': level, 'peak': 5.0}
    assert library_post(app, 'SH1', a).json['learned'] == {'Music/x.mp3': x, 'Music/y.mp3': {'trail': 1.0, 'lead': 0.2},
                                                           'Music/z.mp3': {'trail': 1.1, 'lead': 0.3}}
    # another station, another folder, the same file: its figures straight away (reply and library view)
    b = [{'key': 'Zone/Sub/x.mp3', 'dir': 'Zone/Sub/', 'file': 'x.mp3', 'kind': 'music', 'size': 7001},
         {'key': 'Zone/y.mp3', 'dir': 'Zone/', 'file': 'y.mp3', 'kind': 'music'},
         {'key': 'Zone/z.mp3', 'dir': 'Zone/', 'file': 'z.mp3', 'kind': 'music', 'size': 0}]
    assert library_post(app, 'SH2', b).json['learned'] == {'Zone/Sub/x.mp3': x}           # unknown sizes: not shared
    row = next(i for i in signed_in(app, 'viewer').get('/api/v1/library?station=SH2').json['items'] if i['key'] == 'Zone/Sub/x.mp3')
    assert (row['duration_s'], row['lead_in_s'], row['trail_s'], row['level_s'], row['peak_dbfs']) == (201.0, 0.5, 2.5, 120.0, 5.0)
    assert row['analyzed_at'] and row['level_db'] == pytest.approx(level, abs=0.05)
    assert figures('SH1', 'Music/x.mp3')['file_id'] == figures('SH2', 'Zone/Sub/x.mp3')['file_id']
    assert sql("SELECT count(*) FROM qsr_test.media_files WHERE file = 'x.mp3' AND size = 7001", fetch=True)[0][0] == 1
    # a play on SH2 counts for SH1 too (the level joins the file's average)
    sync(app, station='SH2', events=[ev(1, 'track_start', deck='A', file='x.mp3', dir='Zone/Sub/', duration=201.0),
                                     ev(2, 'track_end', deck='A', file='x.mp3', dir='Zone/Sub/', reason='end', level=-4.0,
                                        level_s=30.0, peak=6.0)])
    assert (figures('SH1', 'Music/x.mp3')['level_s'], figures('SH1', 'Music/x.mp3')['peak_dbfs']) == (150.0, 6.0)
    # moved to another folder: the same figures; the files of unknown size keep theirs on their own station
    a[0].update(key='Archive/x.mp3', dir='Archive/')
    learned = library_post(app, 'SH1', a).json['learned']
    assert learned['Archive/x.mp3']['analyzed'] is True and learned['Archive/x.mp3']['trail'] == 2.5
    assert learned['Music/y.mp3'] == {'trail': 1.0, 'lead': 0.2} and learned['Music/z.mp3'] == {'trail': 1.1, 'lead': 0.3}
    # edited (a new size): measured afresh; the other station's file is still the old one
    b[0]['size'] = 7002
    assert library_post(app, 'SH2', b).json['learned'] == {}
    assert figures('SH2', 'Zone/Sub/x.mp3')['analyzed_at'] is None and figures('SH1', 'Archive/x.mp3')['analyzed_at']
    # a file whose size becomes known moves to the shared row and leaves nothing behind
    a[1]['size'] = 7003
    library_post(app, 'SH1', a)
    assert sql("SELECT count(*) FROM qsr_test.media_files WHERE file = 'y.mp3' AND size IS NULL", fetch=True)[0][0] == 1  # SH2's
    assert figures('SH1', 'Music/y.mp3')['lead_in_s'] is None
    # an event about a file a station does not have changes nothing and fails nothing
    before = figures('SH1', 'Archive/x.mp3')
    r = sync(app, station='SH3', events=[ev(1, 'track_end', deck='A', file='x.mp3', dir='Archive/', reason='end', lead_in=9.0,
                                            trail=9.0, level=10.0, level_s=50.0, peak=9.0),
                                         ev(2, 'analysis', file='x.mp3', dir='Archive/', ok=False, status='nope'),
                                         ev(3, 'file_unsupported', file='x.mp3', dir='Archive/')]).json
    assert (r['accepted'], r['rejected']) == (3, 0) and figures('SH1', 'Archive/x.mp3') == before
    # an old Core's figures in its list still land on the file
    old = [{'key': 'Old/q.mp3', 'dir': 'Old/', 'file': 'q.mp3', 'kind': 'music', 'size': 7004, 'duration': 99.0, 'lead_in': 0.4,
            'trail': 1.5, 'peak': -3.0}]
    assert library_post(app, 'SH4', old).json['learned'] == {'Old/q.mp3': {'trail': 1.5, 'lead': 0.4}}
    assert figures('SH4', 'Old/q.mp3')['duration_s'] == 99.0 and figures('SH4', 'Old/q.mp3')['peak_dbfs'] == -3.0


def test_trims_forget_and_analysis_apply_to_every_station(app):
    a = [{'key': 'M/t.mp3', 'dir': 'M/', 'file': 't.mp3', 'kind': 'music', 'size': 7101},
         {'key': 'M/only_a.mp3', 'dir': 'M/', 'file': 'only_a.mp3', 'kind': 'music', 'size': 7102}]
    b = [{'key': 'Elsewhere/t.mp3', 'dir': 'Elsewhere/', 'file': 't.mp3', 'kind': 'music', 'size': 7101},
         {'key': 'Elsewhere/w.mp3', 'dir': 'Elsewhere/', 'file': 'w.mp3', 'kind': 'music', 'size': 7103}]
    library_post(app, 'SA', a)
    library_post(app, 'SB', b)
    library_post(app, 'SC', [dict(b[0], size=7999)])                        # same name, another size: another file
    library_post(app, 'SD', [dict(b[0], key='D/t.mp3', dir='D/')])
    library_post(app, 'SD', [dict(b[1], key='D/w.mp3', dir='D/')])          # no longer has t.mp3
    mark = lambda: sql('SELECT COALESCE(max(id), 0) FROM qsr_test.commands', fetch=True)[0][0]
    rescanned = lambda since: sorted(r[0] for r in sql("SELECT station FROM qsr_test.commands WHERE cmd = 'rescan' AND id > %s",
                                                       (since,), fetch=True))
    sync(app, station='SA', events=[
        ev(1, 'track_start', deck='A', file='t.mp3', dir='M/', duration=180.0),
        ev(2, 'track_end', deck='A', file='t.mp3', dir='M/', reason='end', lead_in=0.6, trail=3.0, level=-5.0, level_s=60.0, peak=1.0),
        ev(3, 'track_start', deck='B', file='only_a.mp3', dir='M/', duration=170.0),
        ev(4, 'track_end', deck='B', file='only_a.mp3', dir='M/', reason='end', lead_in=0.4, trail=2.0)])
    op = signed_in(app, 'operator')
    post = lambda url, **body: op.post(url, json=body, headers=WEB)
    # a trim on SA is the file's: SB's Core is told at once (a rescan; its upload reply has it); not SC or SD
    m = mark()
    assert post('/api/v1/library/trim', station='SA', key='M/t.mp3', lead=None, trail=0.8).status_code == 200
    assert rescanned(m) == ['SA', 'SB']
    assert 'rescan' in [c['cmd'] for c in sync(app, station='SB').json['commands']]
    assert library_post(app, 'SB', b).json['learned']['Elsewhere/t.mp3'] == {'trail': 0.8, 'lead': 0.6, 'level': -5.0, 'peak': 1.0,
                                                                             'fixed': True}
    assert [i['key'] for i in op.get('/api/v1/library?station=SB&only=adjusted').json['items']] == ['Elsewhere/t.mp3']
    # Forget measured on SB (its music): forgotten on SA too (told at once), the trim stays; SA's own other file
    # is not SB's to forget
    m = mark()
    assert post('/api/v1/library/forget', station='SB', kind='music').json['files'] == 1
    assert rescanned(m) == ['SA', 'SB']
    learned = library_post(app, 'SA', a).json['learned']
    assert learned == {'M/t.mp3': {'trail': 0.8, 'fixed': True}, 'M/only_a.mp3': {'trail': 2.0, 'lead': 0.4}}
    m = mark()
    assert post('/api/v1/library/forget', station='SB', key='Elsewhere/t.mp3').json['files'] == 0     # nothing left to forget
    assert rescanned(m) == ['SB']                                                                     # nothing to tell SA
    # analysis (SA and SB on two Cores): SA's result is the file's; SB's run owes only what nobody has analyzed
    all_stations(app, analyze_at='off')           # no run but those started here
    try:
        assert post('/api/v1/analyze', station='SA', key='M/t.mp3').status_code == 200
        assert post('/api/v1/analyze', station='SB', key='Elsewhere/t.mp3').status_code == 200
        assert analyzer_sync(app, {'station': 'SB'}, core='TFB').json['analyze'] == [{'station': 'SB', 'key': 'Elsewhere/t.mp3'}]
        analyzer_sync(app, {'station': 'SA', 'events': [ev(5, 'analysis', file='t.mp3', dir='M/', ok=True, lead_in=0.1, trail=0.2,
                                                           duration=180.0)]}, core='TFA')
        assert sql("SELECT count(*) FROM qsr_test.library WHERE analyze_wanted AND station IN ('SA', 'SB')", fetch=True)[0][0] == 0
        assert post('/api/v1/analyze', station='SB', scope='new').status_code == 200
        assert analyzer_sync(app, {'station': 'SB'}, core='TFB').json['analyze'] == [{'station': 'SB', 'key': 'Elsewhere/w.mp3'}]
        rows = {i['key']: i for i in op.get('/api/v1/library?station=SB').json['items']}
        assert rows['Elsewhere/t.mp3']['analyzed_at'] and not rows['Elsewhere/t.mp3']['analyze_queued']
        assert rows['Elsewhere/w.mp3']['analyze_queued'] is True
        assert op.get('/api/v1/now?station=SB').json['station']['analysis']['left'] == 1
        assert op.get('/api/v1/now?station=SA').json['station']['analysis']['run'] is False      # another Core's run
        post('/api/v1/analyze', station='SB', scope='stop')
    finally:
        all_stations(app, analyze_at=None)
    # unsupported on one station: no station plays it
    sync(app, station='SA', events=[ev(6, 'file_unsupported', file='t.mp3', dir='M/', status='Unsupported file format')])
    assert library_post(app, 'SB', b).json['learned']['Elsewhere/t.mp3'] == {'unsupported': True}
    assert post('/api/v1/control', station='SB', cmd='queue_add', args={'key': 'Elsewhere/t.mp3'}).status_code == 400
    sync(app, station='SB', state=STATE, events=[ev(1, 'track_start', deck='A', file='w.mp3', dir='Elsewhere/', duration=100.0)])
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            assert ads.next_song(cur, 'SB') == (None, None)          # w.mp3 on air and t.mp3 unsupported: nothing to name
    finally:
        conn.close()


def test_stations_uploading_the_same_new_files_at_once(app):
    """Two stations adding the same new files at the same moment (in opposite orders): one row per file, no deadlock."""
    errors, rounds = [], 3

    def upload(station, names):
        conn = db.connect()
        try:
            control.core_library(conn, {'station': station, 'items': [
                {'key': f'C/{n}', 'dir': 'C/', 'file': n, 'kind': 'music', 'size': 7200} for n in names]})
        except Exception as exc:              # noqa: BLE001 (reported below)
            errors.append(exc)
        finally:
            conn.close()
    for r in range(rounds):
        names = [f'r{r}-{i:04d}.mp3' for i in range(1500)]
        threads = [threading.Thread(target=upload, args=('CC1', names)), threading.Thread(target=upload, args=('CC2', names[::-1]))]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert errors == []
    assert tuple(sql("SELECT count(*), count(DISTINCT file) FROM qsr_test.media_files WHERE size = 7200", fetch=True)[0]) == (
        1500 * rounds,) * 2
    assert sql("""SELECT count(*) FROM qsr_test.library a JOIN qsr_test.library b ON b.station = 'CC2' AND b.key = a.key
                  WHERE a.station = 'CC1' AND a.file_id = b.file_id""", fetch=True)[0][0] == 1500 * rounds


def test_holds_and_station_ids(app):
    items = [{'key': 'Ads/m.wav', 'dir': 'Ads/', 'file': 'm.wav', 'kind': 'ad'},
             {'key': 'I/id1.wav', 'dir': 'I/', 'file': 'id1.wav', 'kind': 'imaging'},
             {'key': 'I/id2.wav', 'dir': 'I/', 'file': 'id2.wav', 'kind': 'imaging'},
             {'key': 'I/sweep.wav', 'dir': 'I/', 'file': 'sweep.wav', 'kind': 'imaging'},
             {'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music'}]
    library_post(app, 'H1', items, ads_dir='Ads/')
    op, viewer, admin = signed_in(app, 'operator'), signed_in(app, 'viewer'), signed_in(app, 'admin')
    hold = lambda c, **b: c.post('/api/v1/hold', json=dict(station='H1', **b), headers=WEB)
    plan = lambda: sync(app, station='H1', ads_rev='').json['ads']
    assert plan()['mix'] == ['Ads/m.wav'] and 'no_bumpers' not in plan()
    assert hold(viewer, what='ads', until=None).status_code == 403
    assert hold(op, what='music', until=None).status_code == 400
    assert hold(op, what='ads', until='2001-01-01T00:00:00Z').status_code == 400          # in the past
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert hold(op, what='ads', until=later).status_code == 200
    assert hold(op, what='bumpers', until=None).status_code == 200                       # until resumed
    p = plan()
    assert p['mix'] == [] and p['due'] == [] and p['no_bumpers'] is True
    holds = op.get('/api/v1/now?station=H1').json['station']['holds']
    assert holds['ads']['until'] and holds['bumpers']['until'] is None and holds['ads']['by'] == 'operator'
    assert hold(op, what='ads', until='off').status_code == 200 and hold(op, what='bumpers', until='off').status_code == 200
    assert plan()['mix'] == ['Ads/m.wav'] and 'no_bumpers' not in plan()
    sql("INSERT INTO qsr_test.station_holds (station, what, until) VALUES ('H1', 'ads', NOW() - INTERVAL '1 minute')")
    assert plan()['mix'] == ['Ads/m.wav']                                                  # an expired hold is no hold

    # station IDs: two marked, every quarter hour with a 15 min window (so one is always owed until played)
    mark = lambda key, on=True: op.post('/api/v1/library/ident', json={'station': 'H1', 'key': key, 'ident': on}, headers=WEB)
    assert mark('M/a.mp3').status_code == 400 and viewer.post('/api/v1/library/ident', json={'station': 'H1', 'key': 'I/id1.wav',
                                                                                         'ident': True}, headers=WEB).status_code == 403
    assert mark('I/id1.wav').status_code == 200 and mark('I/id2.wav').status_code == 200
    assert 'ident' not in plan()                                                           # schedule off
    settings = lambda **v: admin.post('/api/v1/settings', json={'settings': v}, headers=WEB)
    assert settings(ident_schedule='often').status_code == 400
    assert settings(ident_schedule='quarter-hourly', ident_window_min=15).status_code == 200
    try:
        owed = plan().get('ident')
        assert owed in ('I/id1.wav', 'I/id2.wav')
        assert 'ident_schedule' not in (sync(app, station='H1', settings_rev='').json.get('settings') or {})  # server-only
        hold(op, what='ads', until=None)
        sync(app, station='H1', state=dict(STATE, decks={'A': {'state': 'playing', 'live': True, 'kind': 'music', 'key': 'M/x.mp3',
                                                              'file': 'x.mp3', 'remaining': 60, 'duration': 200}},
                                           settings={'ad_every_n_tracks': 1, 'bumper_every_n_tracks': 1}))
        ahead = op.get('/api/v1/now?station=H1&ahead=6').json['station']['ahead']
        assert (ahead[0]['kind'], ahead[0]['why'], ahead[0]['key']) == ('imaging', 'ident', owed)   # the ID first
        assert not any(x['kind'] == 'ad' for x in ahead)                                         # ads held
        hold(op, what='ads', until='off')
        sync(app, station='H1', events=[ev(1, 'bumper_start', deck='B', file=owed.split('/')[1], dir='I/', duration=5.0)])
        assert 'ident' not in plan()                                                       # played: nothing owed
        lib = op.get('/api/v1/library?station=H1&kind=imaging&only=ident').json['items']
        assert sorted(i['key'] for i in lib) == ['I/id1.wav', 'I/id2.wav'] and all(i['ident'] for i in lib)
        never = op.get('/api/v1/library?station=H1&kind=imaging&only=never').json['items']
        assert owed not in [i['key'] for i in never] and len(never) == 2
        nxt = op.get('/api/v1/now?station=H1').json['station']['ident']
        assert nxt['count'] == 2 and nxt['next_at'] and nxt['due'] is None
    finally:
        settings(ident_schedule=None, ident_window_min=None)


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

    now_m = float(sql("SELECT EXTRACT(EPOCH FROM NOW() - date_trunc('hour', NOW())) / 60", fetch=True)[0][0])
    first = sync(app, station='A1', ads_rev='').json
    assert first['ads']['mix'] == ['Ads/m1.wav', 'Ads/m2.wav']
    open_now = 'due' in ads.slot_states(ads.slot_minutes(30, 0.5), ads.window_minutes(30), [], now_m)
    assert first['ads']['due'] in ([], ['Ads/h.wav']) and (first['ads']['due'] == ['Ads/h.wav']) == open_now
    assert 'ads' not in sync(app, station='A1', ads_rev=first['ads_rev']).json             # unchanged: not resent

    # a play in every slot's window this hour: nothing owed; the ad is logged as kind 'ad'
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    evs = [ev(10 + i, 'ad_start', t=(hour_start + timedelta(minutes=m, seconds=5)).strftime('%Y-%m-%dT%H:%M:%SZ'),
              deck='A', file='h.wav', dir='Ads/', duration=30.0) for i, m in enumerate(ads.slot_minutes(30, 0.5))]
    reply = sync(app, station='A1', events=evs, ads_rev=first['ads_rev']).json
    assert reply.get('ads', first['ads'])['due'] == []
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


# ── One Core, many stations (2.0): batched sync, library in parts ────────────

def batch(app, *parts, core='CORE1', boot='B1', **body):
    """A 2.x Core's sync: every station of the Core in one body."""
    body.setdefault('version', config.version())
    return app.test_client().post('/api/v1/core/sync', json=dict(core=core, boot=boot, stations=list(parts), **body),
                                  headers=CORE)


@contextmanager
def logged():
    """The app log lines ((level, message)) written meanwhile."""
    class Catch(logging.Handler):
        def emit(self, record):
            lines.append((record.levelno, record.getMessage()))
    lines, h, lg = [], Catch(logging.DEBUG), logging.getLogger('qsysradio')
    lg.addHandler(h)
    try:
        yield lines
    finally:
        lg.removeHandler(h)


def test_batched_sync_many_stations_in_one_body(app):
    """One request for all the Core's stations: each gets its own reply (commands, acks, settings, events)."""
    first = batch(app, {'station': 'BA1', 'state': STATE}, {'station': 'BA2'}, {'station': 'BA3', 'settings_rev': ''})
    assert first.status_code == 200 and set(first.json) == {'stations'}
    assert set(first.json['stations']) == {'BA1', 'BA2', 'BA3'} and 'settings' in first.json['stations']['BA3']
    op, admin = signed_in(app, 'operator'), signed_in(app, 'admin')
    cmd = lambda st, c: op.post('/api/v1/control', json={'station': st, 'cmd': c}, headers=WEB).json['id']
    c1, c3 = cmd('BA1', 'skip'), cmd('BA3', 'rescan')
    assert admin.post('/api/v1/settings', json={'settings': {'crossfade_s': 7}, 'scope': 'station', 'station': 'BA2'},
                      headers=WEB).status_code == 200
    revs = {st: first.json['stations']['BA3']['settings_rev'] for st in ('BA1', 'BA2', 'BA3')}
    with logged() as lines:          # each event names its station, as the 2.x Core sends it
        r = batch(app, {'station': 'BA1', 'ack': 0, 'settings_rev': revs['BA1'],
                        'events': [ev(1, 'track_start', deck='A', file='one.mp3', dir='M/', duration=100.0, station='BA1')]},
                  {'station': 'BA2', 'ack': 0, 'settings_rev': revs['BA2'],
                   'events': [ev(2, 'track_start', deck='B', file='two.mp3', dir='M/', duration=90.0, station='BA2')], 'dropped': 3},
                  {'station': 'BA3', 'ack': 0, 'settings_rev': revs['BA3'], 'events': {},
                   'meters': {'A': {'peak': 1.0, 'rms': -3.0, 'gain': -2.0}}}, mem_kb=900.5).json['stations']
    play = [msg for _, msg in lines if msg.startswith('PLAY music "one.mp3"')]
    assert len(play) == 1 and play[0].count('station=') == 1, play                  # the part's station, once
    assert not sql("SELECT bool_or(data ? 'station') FROM qsr_test.core_events WHERE station LIKE 'BA%%'", fetch=True)[0][0]
    assert [c['id'] for c in r['BA1']['commands']] == [c1] and [c['id'] for c in r['BA3']['commands']] == [c3]
    assert r['BA2']['commands'] == [] and r['BA2']['settings']['crossfade_s'] == 7 and r['BA2']['settings_rev'] != revs['BA2']
    assert 'settings' not in r['BA1'] and 'settings' not in r['BA3']
    assert (r['BA1']['accepted'], r['BA2']['accepted'], r['BA3']['accepted']) == (1, 1, 0)
    assert 'ads' in r['BA1'] and r['BA1']['ads_rev']
    plays = sql("SELECT station, file FROM qsr_test.play_log WHERE station LIKE 'BA%%' ORDER BY station", fetch=True)
    assert [tuple(p) for p in plays] == [('BA1', 'one.mp3'), ('BA2', 'two.mp3')]
    rows = sql("SELECT station, core, radio_version, meters IS NOT NULL FROM qsr_test.stations WHERE station LIKE 'BA%%' ORDER BY 1",
               fetch=True)
    assert [tuple(x) for x in rows] == [('BA1', 'CORE1', config.version(), False), ('BA2', 'CORE1', config.version(), False),
                                        ('BA3', 'CORE1', config.version(), True)]
    assert tuple(sql("SELECT boot, version, mem_kb FROM qsr_test.cores WHERE core = 'CORE1'", fetch=True)[0]) == (
        'B1', config.version(), 900.5)
    # acked: not sent again; a reset database: each station is told on its own
    r = batch(app, {'station': 'BA1', 'ack': c1}, {'station': 'BA3', 'ack': 10 ** 9}).json['stations']
    assert r['BA1']['commands'] == [] and 'ack_reset' not in r['BA1'] and r['BA3']['ack_reset'] == c3
    assert op.get('/api/v1/now?station=BA1').json['station']['core'] == 'CORE1'
    assert {s['station']: s['core'] for s in op.get('/api/v1/now').json['stations']}['BA2'] == 'CORE1'
    # empty and odd bodies
    assert batch(app).json == {'stations': {}}
    assert app.test_client().post('/api/v1/core/sync', json={'stations': {}, 'core': 'CORE1'}, headers=CORE).json == {'stations': {}}
    assert app.test_client().post('/api/v1/core/sync', json={'stations': 'x'}, headers=CORE).status_code == 400
    r = batch(app, 'junk', {'station': None}, {'events': []}, {'station': 'BA1'}, {'station': 'BA1', 'ack': 'x'}).json['stations']
    assert set(r) == {'BA1'}                                                       # no name: no part; a repeat: once


def test_one_stations_bad_part_does_not_fail_the_others(app, monkeypatch):
    """A station whose part fails is rolled back on its own (its events come again); the others are committed."""
    evs = lambda st, seq: [ev(seq, 'track_start', deck='A', file=f'{st}.mp3', dir='M/', duration=60.0)]
    good = lambda: ({'station': 'BB1', 'events': evs('BB1', 1)}, {'station': 'BB3', 'events': evs('BB3', 3)})
    with logged() as lines:
        r = batch(app, good()[0], {'station': 'BB2', 'events': 'junk'}, good()[1]).json['stations']
    assert r['BB2'] == {'error': 'events must be a list'}
    assert r['BB1']['accepted'] == 1 and r['BB3']['accepted'] == 1 and 'commands' in r['BB3']
    assert any(lvl == 30 and 'station=BB2 sync failed' in msg and 'events must be a list' in msg for lvl, msg in lines)
    # a database error in the middle of one station's work (its transaction aborted): the others go on, on the same connection
    real = control.ads.cached_plan

    def failing(cur, station, fresh=False):
        if station == 'BB2':
            cur.execute('SELECT 1 / 0')
        return real(cur, station, fresh)
    monkeypatch.setattr(control.ads, 'cached_plan', failing)
    with logged() as lines:
        r = batch(app, {'station': 'BB1', 'events': evs('BB1', 11)}, {'station': 'BB2', 'events': evs('BB2', 12), 'state': STATE},
                  {'station': 'BB3', 'events': evs('BB3', 13)}).json['stations']
    assert 'division by zero' in r['BB2']['error'] and set(r['BB2']) == {'error'}
    assert r['BB1']['accepted'] == 1 and r['BB3']['accepted'] == 1
    assert not any('station=BB2' in msg for _, msg in lines)              # logged at most once a minute
    played = lambda st: sql('SELECT count(*) FROM qsr_test.play_log WHERE station = %s', (st,), fetch=True)[0][0]
    assert (played('BB1'), played('BB2'), played('BB3')) == (2, 0, 2)        # BB2's events rolled back
    assert sql("SELECT count(*) FROM qsr_test.stations WHERE station = 'BB2'", fetch=True)[0][0] == 0
    monkeypatch.setattr(control.ads, 'cached_plan', real)
    r = batch(app, {'station': 'BB2', 'events': evs('BB2', 12)}).json['stations']
    assert r['BB2']['accepted'] == 1 and played('BB2') == 1                 # sent again: stored now, once
    # the database itself gone: the whole request is 503 and the Core backs off
    def lost(cur, station, fresh=False):
        raise db.psycopg2.OperationalError('server closed the connection unexpectedly')
    monkeypatch.setattr(control.ads, 'cached_plan', lost)
    assert batch(app, {'station': 'BB1'}, {'station': 'BB3'}).status_code == 503


def test_legacy_core_body_and_the_upgrade(app):
    """A 1.x Core (one station, no 'stations') gets today's reply; the same station then synced by a 2.x Core
    (a new boot) carries on: its unreported command does not run twice, and it is now on that Core."""
    old = sync(app, station='UP1', boot='old', state=STATE, events=[ev(1, 'boot')])
    assert old.status_code == 200 and 'stations' not in old.json and 'commands' in old.json and old.json['accepted'] == 1
    op = signed_in(app, 'operator')
    cid = op.post('/api/v1/control', json={'station': 'UP1', 'cmd': 'skip'}, headers=WEB).json['id']
    assert [c['id'] for c in sync(app, station='UP1', boot='old').json['commands']] == [cid]           # delivered, no result
    assert sql("SELECT core FROM qsr_test.stations WHERE station = 'UP1'", fetch=True)[0][0] is None
    r = batch(app, {'station': 'UP1', 'ack': 0, 'state': STATE}, core='NEWCORE', boot='new').json['stations']['UP1']
    assert r['commands'] == []
    assert sql('SELECT ok, note FROM qsr_test.commands WHERE id = %s', (cid,), fetch=True)[0][1] == 'the Core restarted before reporting'
    assert sql("SELECT core FROM qsr_test.stations WHERE station = 'UP1'", fetch=True)[0][0] == 'NEWCORE'
    assert 'commands' in sync(app, station='UP1', boot='new').json                  # and back (a rollback): still fine


def test_library_upload_in_parts(app):
    """A scan in parts is staged: only its last part, with every part in, marks the files not in it gone."""
    item = lambda n: {'key': f'M/{n}.mp3', 'dir': 'M/', 'file': f'{n}.mp3', 'kind': 'music', 'size': 9000 + ord(n)}
    send = lambda scan, part, parts, names, **kw: app.test_client().post('/api/v1/core/library', headers=CORE, json=dict(
        station='LP1', boot='B1', scan=scan, part=part, parts=parts, music_dir='M/', items=[item(n) for n in names], **kw))
    present = lambda: sorted(r[0][2] for r in sql("SELECT key FROM qsr_test.library WHERE station = 'LP1' AND present",
                                                   fetch=True))
    assert library_post(app, 'LP1', [item(n) for n in 'abc']).json['items'] == 3                   # 1.x: whole
    assert present() == ['a', 'b', 'c']
    assert send('s1', 1, 3, 'ad').json == {'ok': True, 'scan': 's1', 'part': 1, 'parts': 3}
    assert send('s1', 2, 3, 'b').json == {'ok': True, 'scan': 's1', 'part': 2, 'parts': 3}
    assert present() == ['a', 'b', 'c', 'd']                       # staged: nothing gone before the scan is complete
    last = send('s1', 3, 3, 'e')
    assert last.status_code == 200 and last.json['ok'] is True and last.json['items'] == 4 and last.json['learned'] == {}
    assert present() == ['a', 'b', 'd', 'e']
    assert send('s1', 3, 3, 'e').status_code == 200 and present() == ['a', 'b', 'd', 'e']          # the last part again: the same
    # a part lost on the way: the last part is refused and nothing shrinks
    assert send('s2', 1, 3, 'a').status_code == 200
    with logged() as lines:
        r = send('s2', 3, 3, 'b')
    assert r.status_code == 409 and r.json['ok'] is False and 'part(s) 2 of 3' in r.json['error']
    assert any(lvl == 30 and 'LIBRARY' in msg and 's2' in msg for lvl, msg in lines)
    assert present() == ['a', 'b', 'd', 'e']
    # sent again (a new scan id): complete
    for i, names in enumerate(('a', 'bf'), 1):
        assert send('s3', i, 2, names).status_code == 200
    assert present() == ['a', 'b', 'f']
    # a newer scan supersedes an unfinished one: the old one's last part is refused
    assert send('s4', 1, 2, 'a').status_code == 200
    assert send('s5', 1, 1, 'ag').status_code == 200 and present() == ['a', 'g']
    assert send('s4', 2, 2, 'b').status_code == 409 and present() == ['a', 'g']
    # figures in the last part's reply; the folders and the scan recorded on the station
    sync(app, station='LP1', events=[ev(1, 'track_end', deck='A', file='g.mp3', dir='M/', reason='end', lead_in=0.5, trail=1.5)])
    assert send('s6', 1, 2, 'a').json.get('learned') is None
    assert send('s6', 2, 2, 'g').json['learned'] == {'M/g.mp3': {'trail': 1.5, 'lead': 0.5}}
    assert tuple(sql("SELECT music_dir, scan, scan_parts FROM qsr_test.stations WHERE station = 'LP1'", fetch=True)[0]) == (
        'M/', 's6', [1, 2])
    for bad in ({'part': 0}, {'part': 3}, {'parts': 'two'}, {'part': None}):
        body = dict({'station': 'LP1', 'scan': 's7', 'part': 1, 'parts': 2, 'items': []}, **bad)
        assert app.test_client().post('/api/v1/core/library', json=body, headers=CORE).status_code == 400, bad


def test_ads_plan_is_cached(app, monkeypatch):
    """The plan is made again when the station's events come in, when anything it is made from changes, or
    every 30 s; not on every sync."""
    items = [{'key': 'Ads/x.wav', 'dir': 'Ads/', 'file': 'x.wav', 'kind': 'ad'},
             {'key': 'I/id.wav', 'dir': 'I/', 'file': 'id.wav', 'kind': 'imaging'},
             {'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music'}]
    for st in ('PC1', 'PC2'):
        library_post(app, st, items, ads_dir='Ads/')
    made = []
    real = ads.plan
    monkeypatch.setattr(ads, 'plan', lambda cur, station: made.append(station) or real(cur, station))
    count = lambda: made.count('PC1')
    rev = batch(app, {'station': 'PC1', 'ads_rev': '', 'state': STATE}).json['stations']['PC1']['ads_rev']
    assert count() == 1
    for _ in range(3):
        assert 'ads' not in batch(app, {'station': 'PC1', 'ads_rev': rev}).json['stations']['PC1']
    assert count() == 1
    again = batch(app, {'station': 'PC1', 'ads_rev': ''}).json['stations']['PC1']          # the Core asks: the cached plan
    assert again['ads_rev'] == rev and again['ads']['mix'] == ['Ads/x.wav'] and count() == 1
    e = [ev(1, 'transition', reason='end')]
    batch(app, {'station': 'PC1', 'ads_rev': rev, 'events': e})                            # events accepted: made again
    batch(app, {'station': 'PC1', 'ads_rev': rev, 'events': e})                            # only duplicates: not
    assert count() == 2
    op, admin = signed_in(app, 'operator'), signed_in(app, 'admin')
    changes = [
        lambda: op.post('/api/v1/hold', json={'station': 'PC1', 'what': 'bumpers', 'until': None}, headers=WEB),
        lambda: op.post('/api/v1/ads', json={'station': 'PC1', 'key': 'Ads/x.wav', 'mode': 'off'}, headers=WEB),
        lambda: op.post('/api/v1/library/ident', json={'station': 'PC1', 'key': 'I/id.wav', 'ident': True}, headers=WEB),
        lambda: admin.post('/api/v1/settings', json={'settings': {'artist_separation': 3}, 'scope': 'station', 'station': 'PC1'},
                           headers=WEB),
        lambda: admin.post('/api/v1/settings', json={'settings': {'ad_fit_tolerance_s': 120}}, headers=WEB),   # All stations
        lambda: library_post(app, 'PC1', items),
        lambda: op.post('/api/v1/control', json={'station': 'PC1', 'cmd': 'skip'}, headers=WEB),
        lambda: sql("UPDATE qsr_test.stations SET plan_at = NOW() - INTERVAL '31 seconds' WHERE station = 'PC1'"),
        lambda: sql("UPDATE qsr_test.stations SET plan_until = NOW() - INTERVAL '1 second' WHERE station = 'PC1'"),
    ]
    try:
        for i, change in enumerate(changes):
            n = count()
            r = change()
            assert r is None or r.status_code == 200, i
            batch(app, {'station': 'PC1', 'ads_rev': rev})
            batch(app, {'station': 'PC1', 'ads_rev': rev})
            assert count() == n + 1, i
        assert made.count('PC2') == 0
        rev2 = sync(app, station='PC2', ads_rev='').json['ads_rev']            # a 1.x Core's sync is cached the same
        sync(app, station='PC2', ads_rev=rev2)
        assert made.count('PC2') == 1
        op.post('/api/v1/hold', json={'station': 'PC1', 'what': 'bumpers', 'until': 'off'}, headers=WEB)
        sync(app, station='PC2', ads_rev=rev2)
        assert made.count('PC2') == 1                                          # another station's change: not this one
        plan = batch(app, {'station': 'PC1', 'ads_rev': ''}).json['stations']['PC1']['ads']
        assert plan['mix'] == [] and 'no_bumpers' not in plan                  # the rule (off) and the resume are in it
        # it also changes by the clock: cached until the next slot opens (or a due one's window closes, or the hour turns)
        op.post('/api/v1/ads', json={'station': 'PC1', 'key': 'Ads/x.wav', 'mode': 'hourly', 'per_hour': 4}, headers=WEB)
        batch(app, {'station': 'PC1', 'ads_rev': rev})
        until, at = sql("SELECT plan_until, plan_at FROM qsr_test.stations WHERE station = 'PC1'", fetch=True)[0]
        hour = at.replace(minute=0, second=0, microsecond=0)
        times = [hour + timedelta(minutes=m + w) for m in ads.slot_minutes(4, 0.5) for w in (0, ads.window_minutes(4))]
        assert until == min(t for t in times + [hour + timedelta(hours=1)] if t > at)
    finally:
        admin.post('/api/v1/settings', json={'settings': {'ad_fit_tolerance_s': None}}, headers=WEB)


def test_one_player_station(app):
    """A one-player station (radio.lua 2.2.0: a background-music zone) plays songs only. stations.players comes from
    its state; its plan names only the next song; Bumper now, Ad break now, holds, ad rules and IDs are refused;
    the dashboard, Settings and Coming up leave its breaks out. Back to two players, breaks again."""
    songs = [{'key': f'M/{c}.mp3', 'dir': 'M/', 'file': f'{c}.mp3', 'kind': 'music', 'artist': c.upper()} for c in 'abcdef']
    spots = [{'key': 'I/id.wav', 'dir': 'I/', 'file': 'id.wav', 'kind': 'imaging'},
             {'key': 'Ads/x.wav', 'dir': 'Ads/', 'file': 'x.wav', 'kind': 'ad'}]
    library_post(app, 'OP1', songs + spots, ads_dir='Ads/')          # its library from when it had two players
    op, admin = signed_in(app, 'operator'), signed_in(app, 'admin')
    assert op.post('/api/v1/ads', json={'station': 'OP1', 'key': 'Ads/x.wav', 'mode': 'hourly', 'per_hour': 4}, headers=WEB).status_code == 200
    assert op.post('/api/v1/hold', json={'station': 'OP1', 'what': 'bumpers', 'until': None}, headers=WEB).status_code == 200
    two = dict(STATE, players=2)
    first = batch(app, {'station': 'OP1', 'ads_rev': '', 'state': two}).json['stations']['OP1']
    assert first['ads']['mix'] == [] and first['ads']['no_bumpers'] is True and 'next' in first['ads']
    assert sql("SELECT players FROM qsr_test.stations WHERE station = 'OP1'", fetch=True)[0][0] == 2

    one = dict(STATE, players=1, decks={'A': {'state': 'playing', 'live': True, 'kind': 'music', 'key': 'M/a.mp3',
                                              'file': 'a.mp3', 'artist': 'A', 'remaining': 60, 'duration': 200}},
               settings={'bumper_every_n_tracks': 1, 'ad_every_n_tracks': 1, 'crossfade_s': 4})
    r = batch(app, {'station': 'OP1', 'ads_rev': first['ads_rev'], 'state': one}).json['stations']['OP1']
    assert set(r['ads']) == {'next', 'next_why'} and r['ads']['next'].startswith('M/'), r['ads']   # the cached plan made again
    assert sql("SELECT players FROM qsr_test.stations WHERE station = 'OP1'", fetch=True)[0][0] == 1
    batch(app, {'station': 'OP1', 'state': {'automation': False, 'state': 'fault', 'fault': 'x'}})    # a fault state has no count
    assert sql("SELECT players FROM qsr_test.stations WHERE station = 'OP1'", fetch=True)[0][0] == 1
    for bad in (True, 3, '1'):                                       # anything but 1 or 2 is not a count
        batch(app, {'station': 'OP1', 'state': dict(one, players=bad)})
        assert sql("SELECT players FROM qsr_test.stations WHERE station = 'OP1'", fetch=True)[0][0] == 1, bad
    batch(app, {'station': 'OP1', 'state': one})

    # what is about breaks is refused, with a clear message; songs are not
    cmd = lambda c, **a: op.post('/api/v1/control', json={'station': 'OP1', 'cmd': c, 'args': a}, headers=WEB)
    for refused in (cmd('bumper_now'), cmd('ad_now'), cmd('bumper_now', key='I/id.wav'), cmd('play_now', key='Ads/x.wav'),
                    op.post('/api/v1/hold', json={'station': 'OP1', 'what': 'ads', 'until': None}, headers=WEB),
                    op.post('/api/v1/ads', json={'station': 'OP1', 'key': 'Ads/x.wav', 'mode': 'off'}, headers=WEB),
                    op.post('/api/v1/library/ident', json={'station': 'OP1', 'key': 'I/id.wav', 'ident': True}, headers=WEB)):
        assert refused.status_code == 400 and 'one player' in refused.json['error'], refused.json
    assert cmd('play_now', key='M/b.mp3').status_code == 200 and cmd('skip').status_code == 200
    assert op.post('/api/v1/hold', json={'station': 'OP1', 'what': 'bumpers', 'until': 'off'}, headers=WEB).status_code == 200

    # the panel: one player, no holds / ads / IDs; Settings without the break settings and folders; Coming up: songs only
    sync_now = op.get('/api/v1/now?station=OP1&ahead=8').json
    st = sync_now['station']
    assert st['players'] == 1 and st['holds'] == {} and st['next_ad'] == {'owed': 0, 'at': None} and st['ident']['count'] == 0
    assert {s['station']: s['players'] for s in sync_now['stations']}['OP1'] == 1
    assert len(st['ahead']) == 5 and all(x['kind'] == 'music' for x in st['ahead']), st['ahead']   # every other song, no breaks
    gaps = [(datetime.fromisoformat(b['at']) - datetime.fromisoformat(a['at'])).total_seconds() for a, b in zip(st['ahead'], st['ahead'][1:])]
    assert all(g == pytest.approx(210, abs=1) for g in gaps), gaps   # songs never played: 3:30 each, no crossfade overlap
    # its song fading out (its only deck, still on air): Coming up goes on, the next song from the fade's end
    batch(app, {'station': 'OP1', 'state': dict(one, decks={'A': dict(one['decks']['A'], state='fading', fade_left=3.0)})})
    ahead = op.get('/api/v1/now?station=OP1&ahead=3').json['station']['ahead']
    first = (datetime.fromisoformat(ahead[0]['at']) - datetime.now(timezone.utc)).total_seconds() if ahead else None
    assert len(ahead) == 3 and all(x['kind'] == 'music' for x in ahead) and 0 < first <= 3.5, (first, ahead)
    batch(app, {'station': 'OP1', 'state': one})
    view = admin.get('/api/v1/settings?station=OP1').json
    names = {x['name'] for x in view['settings']}
    assert view['players'] == 1 and 'music_dir' in names and 'crossfade_s' in names and not names & core_settings.BREAK_ONLY
    batch(app, {'station': 'OP2', 'state': two})
    view = admin.get('/api/v1/settings?station=OP2').json                  # a two-player station's: all of them
    assert view['players'] == 2 and {'bumper_every_n_tracks', 'imaging_dir', 'ads_dir'} <= {x['name'] for x in view['settings']}

    # two players again: breaks again
    r = batch(app, {'station': 'OP1', 'ads_rev': r['ads_rev'], 'state': two}).json['stations']['OP1']
    assert 'mix' in r['ads'] and op.post('/api/v1/ads', json={'station': 'OP1', 'key': 'Ads/x.wav', 'mode': 'off'},
                                         headers=WEB).status_code == 200


def test_core_memory_warning(app):
    """Per Core, on the lowest Lua memory of each 10-minute window (one sample includes garbage not yet
    collected, so it saws up and down): the settled low is the lowest window low after the first 10 minutes
    of a boot; one warning (at most once an hour) when a whole window's low is more than twice that and over
    32 MB. A new boot starts over."""
    def mem(*kbs, boot='M1'):
        for kb in kbs:
            assert batch(app, {'station': 'MW1'}, core='MEMCORE', boot=boot, mem_kb=kb).status_code == 200

    def window(*kbs, boot='M1', minutes=10):
        """The clock moves on (every time on the row moves back), then a window's samples: the first closes
        the window before."""
        sql(f"""UPDATE qsr_test.cores SET booted_at = booted_at - INTERVAL '{minutes} minutes',
                       win_at = win_at - INTERVAL '{minutes} minutes', mem_low_at = mem_low_at - INTERVAL '{minutes} minutes',
                       mem_warned_at = mem_warned_at - INTERVAL '{minutes} minutes' WHERE core = 'MEMCORE'""")
        mem(*kbs, boot=boot)
    row = lambda: tuple(sql("SELECT mem_kb, win_low_kb, mem_low_kb FROM qsr_test.cores WHERE core = 'MEMCORE'", fetch=True)[0])
    warned = lambda lines: [msg for lvl, msg in lines if lvl == 30 and 'MEMCORE' in msg]
    with logged() as lines:
        mem(20000, 5000, 9000)
        assert row() == (9000.0, 5000.0, None)                   # starting up: no low kept yet
        # healthy: garbage piles up to 4.5 x the live memory between collections, and never grows
        for _ in range(12):                                      # two hours
            window(20000, 90000, 60000, 'lots', 20500, 75000)
        assert row() == (75000.0, 20000.0, 20000.0)               # 'lots': no sample, the window goes on
        assert not warned(lines)
        # a leak: each window's lowest sample climbs; one warning when one is over twice the settled low
        window(30000, 80000)
        window(41000, 90000, 45000)
        assert not warned(lines)                                 # the window is not over yet
        window(50000)
        assert warned(lines) == ['core=MEMCORE Lua memory 40.0 MB at its lowest in 10 min, was 19.5 MB after start: possible leak']
        for kb in (60000, 70000, 80000, 90000):                  # within the hour: no more
            window(kb)
        assert len(warned(lines)) == 1
        window(100000, minutes=30)                               # an hour after the warning: again
        assert len(warned(lines)) == 2 and warned(lines)[-1].startswith('core=MEMCORE Lua memory 87.9 MB')
        assert tuple(sql("SELECT mem_low_kb, mem_low_at < NOW() - INTERVAL '2 hours' FROM qsr_test.cores WHERE core = 'MEMCORE'",
                         fetch=True)[0]) == (20000.0, True)
        # restarted: starts over, the last boot's low forgotten
        mem(60000, boot='M2')
        assert row() == (60000.0, 60000.0, None)
        assert sql("SELECT booted_at > NOW() - INTERVAL '1 minute' FROM qsr_test.cores WHERE core = 'MEMCORE'", fetch=True)[0][0]
        window(61000, 150000, boot='M2')
        window(62000, 150000, boot='M2')
        window(63000, boot='M2')
        assert row()[2] == 61000.0 and len(warned(lines)) == 2
    # under 32 MB never warns, however it grows
    with logged() as lines:
        mem(2000, boot='M3')
        window(2000, boot='M3')
        window(30000, boot='M3')
        window(31000, boot='M3')
        window(32000, boot='M3')
    assert row()[2] == 2000.0 and not warned(lines)


def test_faulted_station_shows_on_the_panel(app):
    """A station the Core could not build still syncs: its fault reaches the dashboard and the station list,
    and its station_fault event the log as a warning."""
    with logged() as lines:
        batch(app, {'station': 'FT1', 'state': {'automation': False, 'state': 'off', 'fault': 'player "Audio_Player99" not found'},
                    'events': [ev(1, 'station_fault', error='player "Audio_Player99" not found', retry_s=30)]},
              {'station': 'FT2', 'state': dict(STATE, fault={'x': 1})})
    assert any(lvl == 30 and msg.startswith('STATION_FAULT station=FT1 error=player') for lvl, msg in lines), lines
    v = signed_in(app, 'viewer')
    st = v.get('/api/v1/now?station=FT1').json
    assert st['station']['state']['fault'] == 'player "Audio_Player99" not found' and st['station']['online']
    assert {s['station']: s['fault'] for s in st['stations']}['FT1'] == 'player "Audio_Player99" not found'
    assert v.get('/api/v1/now?station=FT2').json['station']['state']['fault'] is None
    assert b'id="fault-banner"' in v.get('/').data
    # the state exactly as radio.lua sends it for a faulted station (Lua's empty tables arrive as {}): the dashboard's poll
    core_state = {'fault': "player 'X' not found", 'automation': False, 'state': 'fault', 'retry_s': 30, 'decks': {}, 'queue': {},
                  'library': {'music': 0, 'imaging': 0, 'ad': 0}, 'settings': {'crossfade_s': 4},
                  'analyze': {'can': True}, 'uptime': 12, 'outbox': 0}
    analyzer_sync(app, {'station': 'FT3', 'ack': 0, 'settings_rev': '', 'ads_rev': '', 'state': core_state}, core='FTCORE')
    r = v.get('/api/v1/now?station=FT3&plays=10&ahead=10')
    assert r.status_code == 200 and r.json['station']['state']['queue'] == [] and r.json['station']['state']['retry_s'] == 30
    # its Core has an analysis player: the Music page names the fault, and the players, not the CFG
    assert r.json['station']['analysis']['can'] is True


def test_batched_sync_of_14_stations_is_quick(app):
    """14 stations of 3,000 files each (uploaded in parts) synced in one request: well within the second, the Core's
    list of files to analyze included (cached: made again only when something changed)."""
    n_st, n_files = 14, 3000
    names = [f'Q{i:02d}' for i in range(n_st)]
    client = app.test_client()
    for i, st in enumerate(names):
        items = [{'key': f'M/{st}-{j:04d}.mp3', 'dir': 'M/', 'file': f'{st}-{j:04d}.mp3', 'kind': 'music',
                  'artist': f'Artist {j % 300}', 'size': 10 ** 6 + j} for j in range(n_files - 50)]
        items += [{'key': f'I/{st}-{j}.wav', 'dir': 'I/', 'file': f'{st}-{j}.wav', 'kind': 'imaging', 'size': 5000 + j} for j in range(30)]
        items += [{'key': f'Ads/{st}-{j}.wav', 'dir': 'Ads/', 'file': f'{st}-{j}.wav', 'kind': 'ad', 'size': 7000 + j} for j in range(20)]
        for p in range(3):
            r = client.post('/api/v1/core/library', headers=CORE, json={
                'station': st, 'boot': 'B1', 'scan': f'q{i}', 'part': p + 1, 'parts': 3, 'music_dir': 'M/', 'ads_dir': 'Ads/',
                'imaging_dir': 'I/', 'items': items[p * 1000:(p + 1) * 1000]})
            assert r.status_code == 200, r.get_data(as_text=True)
        sql("INSERT INTO qsr_test.ad_rules (station, key, mode, per_hour) VALUES (%s, %s, 'hourly', 4)", (st, f'Ads/{st}-0.wav'))
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    part = lambda i, st, seq: {'station': st, 'ack': 0, 'settings_rev': '', 'ads_rev': '',
                               'meters': {'A': {'peak': -3, 'rms': -12, 'gain': 0}, 'B': {'peak': -120, 'rms': -120, 'gain': -100}},
                               'state': STATE, 'events': [ev(seq + i * 10, 'track_start', t=t0.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                                             deck='A', file=f'{st}-0001.mp3', dir='M/', duration=200.0)]}
    timings = []
    for seq in (1, 2, 3):
        start = time.monotonic()
        r = analyzer_sync(app, *[part(i, st, seq) for i, st in enumerate(names)], core='BIGCORE', mem_kb=4000)
        timings.append(time.monotonic() - start)
        assert r.status_code == 200 and all('error' not in x for x in r.json['stations'].values())
        assert len(r.json['stations']) == n_st and all(x['ads']['next'] for x in r.json['stations'].values())
        listed = r.json['analyze']           # every file new ('right away'): the short ones first (imaging, ads; by key)
        assert len(listed) == 20 and all(f['key'].startswith('Ads/') for f in listed) and {f['station'] for f in listed} <= set(names)
    usual = lambda: analyzer_sync(app, *[{'station': st, 'ack': 0, 'settings_rev': x['settings_rev'], 'ads_rev': x['ads_rev'],
                                          'meters': {}} for st, x in r.json['stations'].items()],
                                  core='BIGCORE', rev=r.json['analyze_rev'], mem_kb=4000)
    start = time.monotonic()
    again = usual()
    timings.append(time.monotonic() - start)                 # the usual second: nothing new, plans and list cached
    assert again.status_code == 200 and not any('ads' in x or 'settings' in x for x in again.json['stations'].values())
    assert 'analyze' not in again.json
    # every file analyzed: the list made again reads every library of the Core (the most it costs), once
    sql('UPDATE qsr_test.media_files SET analyzed_at = NOW() WHERE id IN (SELECT file_id FROM qsr_test.library WHERE station = ANY(%s))',
        (names,))
    sql("UPDATE qsr_test.cores SET owed_at = NULL WHERE core = 'BIGCORE'")
    start = time.monotonic()
    done = usual()
    timings.append(time.monotonic() - start)
    assert done.json['analyze'] == [] and done.json['analyze_rev'] == ''
    assert max(timings[:3]) < 1.5 and timings[3] < 0.5 and timings[4] < 1.0, timings   # measured: about 0.2, 0.05 and 0.12 s


# ── Sign-in and permissions ──────────────────────────────────────────────────

def test_login_rules(app):
    c = app.test_client()
    assert c.post('/login', data={'username': 'admin', 'password': 'x'}).status_code == 401
    assert b'does not have access' in c.post('/login', data={'username': 'outsider', 'password': 'pw'}).data
    assert b'change your password' in c.post('/login', data={'username': 'newbie', 'password': 'pw'}).data
    r = c.post('/login', data={'username': 'admin', 'password': 'pw'}, headers={'Origin': 'http://evil.example'})
    assert r.status_code == 403
    assert c.post('/login?next=//evil.com', data={'username': 'admin', 'password': 'pw'}).headers['Location'] == '/'


def test_library_pages(app):
    c = signed_in(app, 'viewer')
    assert c.get('/library').headers['Location'].endswith('/library/music')
    assert c.get('/library/nope').headers['Location'].endswith('/library/music')
    for which, title in (('music', b'Music'), ('imaging', b'Imaging &amp; IDs'), ('ads', b'Advertisements')):
        r = c.get('/library/' + which)
        assert r.status_code == 200 and b'<title>' + title in r.data, which
    assert app.test_client().get('/library/music').status_code == 302                # signed out


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
    here = lambda **s: admin.post('/api/v1/settings', json={'settings': s, 'scope': 'station', 'station': 'S7'}, headers=WEB)
    sync(app, station='S7')
    assert save(crossfade_s=99).status_code == 400
    assert save(crossfade_s=True).status_code == 400
    assert here(music_dir='').status_code == 400
    assert here(music_dir={'x': 1}).status_code == 400
    assert here(music_dir='../etc').status_code == 400
    assert save(bumper_every_n_tracks='4.5').status_code == 400
    assert save(nope=1).status_code == 400
    assert save(bumper_every_n_tracks='3.0', bumper_fade_s=2.5, imaging_gain_db=-3, stop_fade_s=4).json['changed'] == [
        'bumper_every_n_tracks', 'bumper_fade_s', 'imaging_gain_db', 'stop_fade_s']
    assert here(music_dir='My Music').json['changed'] == ['music_dir']                   # folders: per station
    first = sync(app, station='S7', settings_rev='').json
    assert first['settings'] == {'bumper_every_n_tracks': 3, 'bumper_fade_s': 2.5, 'imaging_gain_db': -3, 'stop_fade_s': 4,
                                 'music_dir': 'My Music/'}
    assert 'settings' not in sync(app, station='S7', settings_rev=first['settings_rev']).json
    assert here(music_dir=None).json['changed'] == ['music_dir']                          # back to the default
    assert 'music_dir' not in sync(app, station='S7', settings_rev=first['settings_rev']).json['settings']


def settings_for(app, station):
    """The settings and rev a Core with nothing yet gets on its next sync."""
    r = sync(app, station=station, settings_rev='').json
    return r['settings'], r['settings_rev']


def test_settings_by_station_layers(app):
    """radio.lua's fallback <- All stations <- this station: each station gets its own merged values and rev."""
    sync(app, station='SL1')
    sync(app, station='SL2')
    admin = signed_in(app, 'admin')
    save = lambda station=None, **s: admin.post('/api/v1/settings', headers=WEB, json=dict(
        {'settings': s}, **({'scope': 'station', 'station': station} if station else {'scope': 'all'})))
    try:
        assert save(crossfade_s=5).json == {'ok': True, 'changed': ['crossfade_s'], 'scope': 'all', 'station': None}
        (s1, rev1), (s2, rev2) = settings_for(app, 'SL1'), settings_for(app, 'SL2')
        assert s1['crossfade_s'] == s2['crossfade_s'] == 5 and s1 == s2 and rev1 == rev2   # nothing of their own: All stations
        assert save('SL1', crossfade_s=8).json == {'ok': True, 'changed': ['crossfade_s'], 'scope': 'station', 'station': 'SL1'}
        (s1, rev1), (s2, rev2) = settings_for(app, 'SL1'), settings_for(app, 'SL2')
        assert s1['crossfade_s'] == 8 and s2['crossfade_s'] == 5 and rev1 != rev2
        # an All-stations change reaches a station without its own value, not one with
        assert save(crossfade_s=6, fade_in_s=1).status_code == 200
        (s1, _), (s2, _) = settings_for(app, 'SL1'), settings_for(app, 'SL2')
        assert (s1['crossfade_s'], s1['fade_in_s']) == (8, 1) and (s2['crossfade_s'], s2['fade_in_s']) == (6, 1)
        # a station value equal to the All-stations one still counts as its own; reset = back to All stations
        assert save('SL2', fade_in_s=1).json['changed'] == ['fade_in_s']
        assert save(fade_in_s=2).status_code == 200
        assert settings_for(app, 'SL2')[0]['fade_in_s'] == 1 and settings_for(app, 'SL1')[0]['fade_in_s'] == 2
        assert save('SL1', crossfade_s=None).json['changed'] == ['crossfade_s']
        assert settings_for(app, 'SL1')[0]['crossfade_s'] == 6
        assert save('SL2', fade_in_s=None).status_code == 200
        assert settings_for(app, 'SL1') == settings_for(app, 'SL2')                       # the same again: the same rev
        # the log line names the scope; a station reset reads as back to All stations
        conn = db.connect()
        try:
            user = {'username': 'admin'}
            assert control.save_settings(conn, {'skip_fade_s': 3}, user, 'SL1')[1] == (
                20, 'SETTINGS station=SL1 changed by=admin skip_fade_s=3.0')
            assert control.save_settings(conn, {'skip_fade_s': None, 'music_dir': 'M'}, user, 'SL1')[1] == (
                20, 'SETTINGS station=SL1 changed by=admin music_dir=M/ skip_fade_s=all-stations')
            assert control.save_settings(conn, {'music_dir': None}, user, 'SL1')[1][1].endswith(' music_dir=default')
            assert control.save_settings(conn, {'skip_fade_s': 2}, user)[1] == (
                20, 'SETTINGS station=all changed by=admin skip_fade_s=2.0')
            assert control.save_settings(conn, {'skip_fade_s': None}, user)[1][1].endswith(' skip_fade_s=default')
        finally:
            conn.close()
        assert save('NOPE', crossfade_s=3).status_code == 400                              # no such station
        assert signed_in(app, 'operator').post('/api/v1/settings', json={'settings': {'crossfade_s': 3}, 'scope': 'station',
                                                                          'station': 'SL1'}, headers=WEB).status_code == 403
    finally:
        save(crossfade_s=None, fade_in_s=None)


def test_folders_are_set_per_station_only(app):
    sync(app, station='FD1')
    sync(app, station='FD2')
    admin = signed_in(app, 'admin')
    post = lambda body: admin.post('/api/v1/settings', json=body, headers=WEB)
    for name in ('music_dir', 'imaging_dir', 'ads_dir'):
        r = post({'settings': {name: 'X'}})
        assert r.status_code == 400 and 'set per station' in r.json['error'], name
        assert post({'settings': {name: 'X'}, 'scope': 'all'}).status_code == 400
    assert post({'settings': {'music_dir': 'Zone 1', 'ads_dir': 'Spots'}, 'scope': 'station', 'station': 'FD1'}).json['changed'] == [
        'ads_dir', 'music_dir']
    s1, s2 = settings_for(app, 'FD1')[0], settings_for(app, 'FD2')[0]
    assert (s1['music_dir'], s1['ads_dir']) == ('Zone 1/', 'Spots/') and 'music_dir' not in s2 and 'ads_dir' not in s2
    # a folder left in the All-stations rows (never written by the server) is never read
    sql("""INSERT INTO qsr_test.app_settings (key, value) VALUES ('core.music_dir', '"Old/"')""")
    try:
        assert 'music_dir' not in settings_for(app, 'FD2')[0] and settings_for(app, 'FD1')[0]['music_dir'] == 'Zone 1/'
        assert 'music_dir' not in {x['name'] for x in admin.get('/api/v1/settings?scope=all').json['settings']}
    finally:
        sql("DELETE FROM qsr_test.app_settings WHERE key = 'core.music_dir'")


def test_settings_views_all_stations_and_one_station(app):
    sync(app, station='SV1', state=dict(STATE, settings={'crossfade_s': 4, 'fade_in_s': 0}))
    admin, viewer = signed_in(app, 'admin'), signed_in(app, 'viewer')
    save = lambda body: admin.post('/api/v1/settings', json=body, headers=WEB)
    try:
        assert save({'settings': {'crossfade_s': 5, 'ad_fit_tolerance_s': 60}}).status_code == 200
        assert save({'settings': {'fade_in_s': 2, 'music_dir': 'Zone/', 'ad_fit_tolerance_s': 30},
                     'scope': 'station', 'station': 'SV1'}).status_code == 200
        every = viewer.get('/api/v1/settings?station=SV1&scope=all').json
        assert (every['scope'], every['station']) == ('all', None) and 'rev' not in every
        rows = {x['name']: x for x in every['settings']}
        assert not set(rows) & core_settings.STATION_ONLY and not any(x['station_only'] for x in rows.values())
        assert set(rows) == set(core_settings.BY_NAME) - core_settings.STATION_ONLY
        assert {k: rows['crossfade_s'][k] for k in ('override', 'all', 'source', 'default')} == {
            'override': 5, 'all': 5, 'source': 'all', 'default': 4}
        assert (rows['fade_in_s']['override'], rows['fade_in_s']['source']) == (None, 'default') and 'own' not in rows['fade_in_s']
        one = viewer.get('/api/v1/settings?station=SV1').json
        assert (one['scope'], one['station']) == ('station', 'SV1') and one['rev'] and 'in_sync' in one
        rows = {x['name']: x for x in one['settings']}
        assert set(rows) == set(core_settings.BY_NAME) - core_settings.ALL_ONLY and rows['music_dir']['station_only'] is True
        pick = lambda n: {k: rows[n][k] for k in ('override', 'own', 'all', 'source', 'running')}
        assert pick('crossfade_s') == {'override': None, 'own': None, 'all': 5, 'source': 'all', 'running': 4}
        assert pick('fade_in_s') == {'override': 2, 'own': 2, 'all': None, 'source': 'station', 'running': 0}
        assert pick('music_dir') == {'override': 'Zone/', 'own': 'Zone/', 'all': None, 'source': 'station', 'running': None}
        assert pick('dead_air_s')['source'] == 'default'
        assert pick('ad_fit_tolerance_s') == {'override': 30, 'own': 30, 'all': 60, 'source': 'station', 'running': 30}
        assert rows['ident_schedule']['running'] == 'off'                     # the server's own: what it uses for SV1
        assert one['in_sync'] is False                                         # the Core has not taken them yet
        sync(app, station='SV1', settings_rev=one['rev'])
        assert viewer.get('/api/v1/settings?station=SV1').json['in_sync'] is True
        assert viewer.get('/api/v1/settings?station=nope').json['scope'] == 'all'          # no such station: All stations
        assert viewer.get('/api/v1/settings').json['scope'] == 'station'                   # the most recent station
        conn = db.connect()
        try:
            assert control.settings_view(conn, None)['scope'] == 'all'                     # no station has connected
        finally:
            conn.close()
    finally:
        save({'settings': {'crossfade_s': None, 'ad_fit_tolerance_s': None}})


def test_server_only_settings_per_station(app):
    """ident_schedule, ad_fit_tolerance_s, ident_window_min: per station, like the rest. analyze_at is the whole
    Core's (its analysis players serve every station on it): set for All stations only."""
    items = [{'key': 'I/id.wav', 'dir': 'I/', 'file': 'id.wav', 'kind': 'imaging'},
             {'key': 'M/a.mp3', 'dir': 'M/', 'file': 'a.mp3', 'kind': 'music', 'size': 50}]
    op, admin = signed_in(app, 'operator'), signed_in(app, 'admin')
    for st in ('SO1', 'SO2'):
        library_post(app, st, items)
        assert op.post('/api/v1/library/ident', json={'station': st, 'key': 'I/id.wav', 'ident': True}, headers=WEB).status_code == 200
    save = lambda station=None, **s: admin.post('/api/v1/settings', headers=WEB, json=dict(
        {'settings': s}, **({'scope': 'station', 'station': station} if station else {})))
    plan = lambda st: sync(app, station=st, ads_rev='').json['ads']
    try:
        assert save(ident_schedule='off', analyze_at='off').status_code == 200
        assert 'ident' not in plan('SO1') and 'ident' not in plan('SO2')
        assert save('SO1', ident_schedule='quarter-hourly', ident_window_min=15).status_code == 200
        assert plan('SO1')['ident'] == 'I/id.wav' and 'ident' not in plan('SO2')
        assert op.get('/api/v1/now?station=SO1').json['station']['ident']['next_at']
        assert op.get('/api/v1/now?station=SO2').json['station']['ident']['next_at'] is None
        assert 'ident_schedule' not in settings_for(app, 'SO1')[0]                     # still not sent to the Core
        # analyze_at: All stations only; not in a station's view; a station's own value (left by 2.0) never read
        r = save('SO1', analyze_at='00:00')
        assert r.status_code == 400 and r.json['error'] == ('Analyze new files applies to every station of the Core: '
                                                             'set it for All stations')
        assert save('SO1', analyze_at=None, crossfade_s=5).status_code == 400              # nothing of it is saved
        assert 'analyze_at' not in {x['name'] for x in admin.get('/api/v1/settings?station=SO1').json['settings']}
        row = {x['name']: x for x in admin.get('/api/v1/settings?scope=all').json['settings']}['analyze_at']
        assert (row['label'], row['default'], row['override']) == ('Analyze new files', 'right away', 'off')
        assert row['choices'] == ['right away', 'off'] + [f'{h:02d}:00' for h in range(24)]
        sql("""INSERT INTO qsr_test.station_settings (station, name, value) VALUES ('SO1', 'analyze_at', '"00:00"')""")
        try:
            assert op.get('/api/v1/now?station=SO1').json['station']['analysis']['at'] == 'off'
        finally:
            sql("DELETE FROM qsr_test.station_settings WHERE station = 'SO1'")
        assert save(analyze_at='04:00').status_code == 200
        assert op.get('/api/v1/now?station=SO2').json['station']['analysis']['at'] == '04:00'
        assert 'analyze_at' not in settings_for(app, 'SO1')[0]                          # the server's own
    finally:
        save(ident_schedule=None, analyze_at=None)
    assert op.get('/api/v1/now?station=SO2').json['station']['analysis']['at'] == 'right away'      # the default


def test_migration_0014_moves_the_folders_to_every_station():
    """The folders set for everyone become each existing station's own; nothing else moves."""
    path = next(p for v, _, p, _ in migrate.discover() if v == 14)
    sql('DROP SCHEMA IF EXISTS qsr_test_m14 CASCADE; CREATE SCHEMA qsr_test_m14; '
        'CREATE TABLE qsr_test_m14.stations (station TEXT PRIMARY KEY); '
        'CREATE TABLE qsr_test_m14.app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, '
        'updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')
    try:
        sql("INSERT INTO qsr_test_m14.stations VALUES ('Main'), ('Zone')")
        sql("""INSERT INTO qsr_test_m14.app_settings (key, value) VALUES ('core.music_dir', '"Music/"'),
               ('core.imaging_dir', '"Imaging/"'), ('core.crossfade_s', '5'), ('syslog.host', '"h"')""")
        conn = db.connect(search_path=False)
        try:
            with conn.cursor() as cur:
                cur.execute('SET LOCAL search_path TO qsr_test_m14')
                cur.execute(path.read_text(encoding='utf-8'))
            conn.commit()
        finally:
            conn.close()
        moved = sql('SELECT station, name, value FROM qsr_test_m14.station_settings ORDER BY 1, 2', fetch=True)
        assert [tuple(r) for r in moved] == [('Main', 'imaging_dir', '"Imaging/"'), ('Main', 'music_dir', '"Music/"'),
                                             ('Zone', 'imaging_dir', '"Imaging/"'), ('Zone', 'music_dir', '"Music/"')]
        left = sql('SELECT key FROM qsr_test_m14.app_settings ORDER BY 1', fetch=True)
        assert [r[0] for r in left] == ['core.crossfade_s', 'syslog.host']
    finally:
        sql('DROP SCHEMA IF EXISTS qsr_test_m14 CASCADE')


def test_migration_0015_merges_each_files_figures():
    """Up to 1.5.0 each station kept its own figures per file: 0015 merges them per name + size (the latest
    analysis or the most recently seen station's, trims, levels weighted by seconds, highest peak), gives
    each file of unknown size its own row, and points every library row at its file."""
    sql('DROP SCHEMA IF EXISTS qsr_test_m15 CASCADE; CREATE SCHEMA qsr_test_m15')
    conn = db.connect(search_path=False)
    try:
        for version, _, path, _ in migrate.discover():
            if version > 15:
                break
            if version == 15:
                with conn.cursor() as cur:
                    cur.execute("""SET LOCAL search_path TO qsr_test_m15;
                        INSERT INTO library (station, key, kind, directory, file, size, duration_s, lead_in_s, trail_s, lead_set_s,
                                             trail_set_s, peak_dbfs, level_db, level_s, unsupported, analyzed_at, analyze_failed_at,
                                             analyze_error, analyze_wanted, ident, first_seen, last_seen) VALUES
                        ('A', 'M/a.mp3', 'music', 'M/', 'a.mp3', 100, 200, 0.5, 2.0, 1.0, NULL, -2, -10, 100, FALSE,
                         '2026-01-02Z', '2026-01-05Z', 'e-new', TRUE, FALSE, '2025-01-01Z', '2026-03-01Z'),
                        ('B', 'Other/a.mp3', 'music', 'Other/', 'a.mp3', 100, 201, 0.7, 3.0, NULL, 4.0, -1, -4, 50, TRUE,
                         '2026-02-01Z', '2026-01-03Z', 'e-old', FALSE, FALSE, '2025-06-01Z', '2026-02-01Z'),
                        ('A', 'M/c.mp3', 'music', 'M/', 'c.mp3', 5, NULL, 0.2, 1.0, NULL, NULL, NULL, NULL, NULL, FALSE,
                         NULL, NULL, NULL, FALSE, FALSE, '2025-01-01Z', '2026-01-01Z'),
                        ('B', 'X/c.mp3', 'music', 'X/', 'c.mp3', 5, 60, 0.3, NULL, 2.0, NULL, NULL, -8, 0, FALSE,
                         NULL, NULL, NULL, FALSE, FALSE, '2025-01-01Z', '2026-02-01Z'),
                        ('A', 'I/b.wav', 'imaging', 'I/', 'b.wav', NULL, 5, 0.1, 0.2, NULL, NULL, -3, NULL, NULL, FALSE,
                         NULL, NULL, NULL, FALSE, TRUE, '2025-01-01Z', '2026-01-01Z'),
                        ('B', 'I/b.wav', 'imaging', 'I/', 'b.wav', NULL, 6, 0.4, 0.5, NULL, NULL, -6, NULL, NULL, TRUE,
                         NULL, NULL, NULL, FALSE, FALSE, '2025-01-01Z', '2026-01-01Z')""")
            with conn.cursor() as cur:
                cur.execute('SET LOCAL search_path TO qsr_test_m15')
                cur.execute(path.read_text(encoding='utf-8'))
            conn.commit()
    finally:
        conn.close()
    try:
        cols = ('duration_s', 'lead_in_s', 'trail_s', 'lead_set_s', 'trail_set_s', 'peak_dbfs', 'level_db', 'level_s',
                'unsupported', 'analyzed_at', 'analyze_failed_at', 'analyze_error')
        rows = {(r[0], r[1]): r for r in sql(f"""SELECT station, key, file_id, ident, analyze_wanted, {', '.join(cols)}
                                                 FROM qsr_test_m15.library_figures""", fetch=True)}
        a, b = rows['A', 'M/a.mp3'], rows['B', 'Other/a.mp3']
        assert a['file_id'] == b['file_id'] and tuple(a[c] for c in cols) == tuple(b[c] for c in cols)
        level = 10 * math.log10((10 ** -1.0 * 100 + 10 ** -0.4 * 50) / 150)
        assert {c: a[c] for c in cols} == {
            'duration_s': 200.0, 'lead_in_s': 0.7, 'trail_s': 3.0,                   # length: most recently seen; quiet ends: the latest analysis
            'lead_set_s': 1.0, 'trail_set_s': 4.0, 'peak_dbfs': -1.0, 'level_db': pytest.approx(level), 'level_s': 150.0,
            'unsupported': True, 'analyzed_at': datetime(2026, 2, 1, tzinfo=timezone.utc),
            'analyze_failed_at': datetime(2026, 1, 5, tzinfo=timezone.utc), 'analyze_error': 'e-new'}
        assert (a['analyze_wanted'], b['analyze_wanted']) == (True, False)          # per station, as before
        c = rows['A', 'M/c.mp3']                                                    # nothing analyzed: the most recently seen
        assert c['file_id'] == rows['B', 'X/c.mp3']['file_id']
        assert (c['lead_in_s'], c['trail_s'], c['lead_set_s'], c['duration_s'], c['level_db'], c['level_s']) == (0.3, 1.0, 2.0, 60.0, None, None)
        ia, ib = rows['A', 'I/b.wav'], rows['B', 'I/b.wav']                         # size unknown: never merged
        assert ia['file_id'] != ib['file_id'] and ia['ident'] is True
        assert (ia['duration_s'], ia['lead_in_s'], ia['peak_dbfs'], ia['unsupported']) == (5.0, 0.1, -3.0, False)
        assert (ib['duration_s'], ib['lead_in_s'], ib['peak_dbfs'], ib['unsupported']) == (6.0, 0.4, -6.0, True)
        assert sql('SELECT count(*) FROM qsr_test_m15.media_files', fetch=True)[0][0] == 4
        left = {r[0] for r in sql("""SELECT column_name FROM information_schema.columns
                                     WHERE table_schema = 'qsr_test_m15' AND table_name = 'library'""", fetch=True)}
        assert not left & set(cols) and 'file_id' in left
        assert sql("""SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'qsr_test_m15'
                      AND table_name = 'library' AND column_name = 'file_id'""", fetch=True)[0][0] == 'NO'
    finally:
        sql('DROP SCHEMA IF EXISTS qsr_test_m15 CASCADE')


def test_migration_0017_moves_the_analysis_job_to_the_core():
    """A run in progress on a Core's stations goes on as the Core's (the widest: the latest start), a daily run
    started on one of them counts for the Core, and their own columns are emptied; a station with no Core yet (a
    1.x Core's) keeps its job until it syncs from a 2.x Core (test_a_1x_stations_run_goes_on_from_its_core);
    any station's own analyze_at goes."""
    sql('DROP SCHEMA IF EXISTS qsr_test_m17 CASCADE; CREATE SCHEMA qsr_test_m17')
    conn = db.connect(search_path=False)
    try:
        for version, _, path, _ in migrate.discover():
            if version > 17:
                break
            if version == 17:
                with conn.cursor() as cur:
                    cur.execute("""SET LOCAL search_path TO qsr_test_m17;
                        INSERT INTO cores (core) VALUES ('C1'), ('C2'), ('C3');
                        INSERT INTO stations (station, core, analyze_from, analyze_run_at, analyze_auto_at) VALUES
                        ('A', 'C1', '-infinity', '2026-10-01Z', '2026-10-14T04:00Z'),
                        ('B', 'C1', '2026-10-02Z', '2026-10-02Z', NULL),
                        ('C', 'C2', NULL, NULL, '2026-10-13T04:00Z'),
                        ('D', NULL, '2026-10-03Z', '2026-10-03Z', '2026-10-15T04:00Z');
                        INSERT INTO station_settings (station, name, value) VALUES ('A', 'analyze_at', '"off"'),
                        ('A', 'crossfade_s', '5'), ('C', 'analyze_at', '"03:00"')""")
            with conn.cursor() as cur:
                cur.execute('SET LOCAL search_path TO qsr_test_m17')
                cur.execute(path.read_text(encoding='utf-8'))
            conn.commit()
    finally:
        conn.close()
    try:
        utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
        rows = {r[0]: tuple(r[1:]) for r in sql("""SELECT core, analyze_from, analyze_run_at, analyze_auto_at, owed_at, analyzers
                                                   FROM qsr_test_m17.cores""", fetch=True)}
        assert rows == {'C1': (utc(2026, 10, 2), utc(2026, 10, 2), utc(2026, 10, 14, 4), None, None),
                        'C2': (None, None, utc(2026, 10, 13, 4), None, None), 'C3': (None, None, None, None, None)}
        jobs = {r[0]: tuple(r[1:]) for r in sql("""SELECT station, analyze_from, analyze_run_at, analyze_auto_at
                                                   FROM qsr_test_m17.stations""", fetch=True)}
        assert jobs == {'A': (None,) * 3, 'B': (None,) * 3, 'C': (None,) * 3,
                        'D': (utc(2026, 10, 3), utc(2026, 10, 3), utc(2026, 10, 15, 4))}
        own = sql('SELECT station, name FROM qsr_test_m17.station_settings ORDER BY 1, 2', fetch=True)
        assert [tuple(r) for r in own] == [('A', 'crossfade_s')]
    finally:
        sql('DROP SCHEMA IF EXISTS qsr_test_m17 CASCADE')


def test_migration_0018_counts_each_stations_players():
    """Every station has two players until its Core (radio.lua 2.2.0) reports one; only 1 or 2 fit."""
    sql('DROP SCHEMA IF EXISTS qsr_test_m18 CASCADE; CREATE SCHEMA qsr_test_m18')
    conn = db.connect(search_path=False)
    try:
        for version, _, path, _ in migrate.discover():
            if version > 18:
                break
            if version == 18:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path TO qsr_test_m18; INSERT INTO stations (station) VALUES ('A'), ('B')")
            with conn.cursor() as cur:
                cur.execute('SET LOCAL search_path TO qsr_test_m18')
                cur.execute(path.read_text(encoding='utf-8'))
            conn.commit()
    finally:
        conn.close()
    try:
        assert [tuple(r) for r in sql('SELECT station, players FROM qsr_test_m18.stations ORDER BY 1', fetch=True)] == [('A', 2), ('B', 2)]
        with pytest.raises(Exception, match='stations_players_check'):
            sql("UPDATE qsr_test_m18.stations SET players = 3 WHERE station = 'A'")
        sql("UPDATE qsr_test_m18.stations SET players = 1 WHERE station = 'A'")
    finally:
        sql('DROP SCHEMA IF EXISTS qsr_test_m18 CASCADE')


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


def test_header_clock_is_server_time_with_ntp_check(app):
    import socket
    import struct
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)          # a fake NTP server 5 s ahead
    srv.bind(('127.0.0.1', 0))
    srv.settimeout(10)

    def answer():
        try:
            for _ in range(5):
                req, addr = srv.recvfrom(48)
                t = time.time() + 5 + 2208988800
                ts = struct.pack('!II', int(t), int((t % 1) * 2 ** 32))
                srv.sendto(b'\x1c' + 31 * b'\0' + ts + ts, addr)
        except OSError:
            pass
    threading.Thread(target=answer, daemon=True).start()
    from server import clock
    port = srv.getsockname()[1]
    real = clock.sntp_offset
    clock.sntp_offset = lambda host, port_=port, timeout=2.0: real(host, port_, timeout)
    admin = signed_in(app, 'admin')
    try:
        c = admin.get('/api/v1/now').json['clock']
        assert abs(c['server_ms'] / 1000 - time.time()) < 5 and c['ntp']['server'] is None
        assert admin.post('/api/v1/server-settings', json={'settings': {'ntp_server': 'bad host'}}, headers=WEB).status_code == 400
        assert admin.post('/api/v1/server-settings', json={'settings': {'ntp_server': '127.0.0.1'}},
                          headers=WEB).json['changed'] == ['ntp_server']
        for _ in range(50):
            ntp = admin.get('/api/v1/now').json['clock']['ntp']
            if ntp['offset_s'] is not None:
                break
            time.sleep(0.1)
        assert ntp['server'] == '127.0.0.1' and abs(ntp['offset_s'] - 5) < 0.5 and ntp['ok'] is False
    finally:
        admin.post('/api/v1/server-settings', json={'settings': {'ntp_server': None}}, headers=WEB)
        clock.sntp_offset = real
        srv.close()
    assert admin.get('/api/v1/now').json['clock']['ntp']['server'] is None


def test_settings_spec_matches_radio_lua():
    """Every station setting is defined once on each side, with the same defaults."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), '..', 'qsys', 'radio', 'radio.lua')).read()
    block = re.search(r'^SETTINGS = \{(.*?)^\}', src, re.S | re.M).group(1)
    lua = {k: v.strip().strip('"') for k, v in re.findall(r'(\w+) = ("[^"]*"|-?[\d.]+)', block)}
    assert set(lua) == set(core_settings.BY_NAME) - core_settings.SERVER_ONLY
    for name, spec in core_settings.BY_NAME.items():
        if name in core_settings.SERVER_ONLY:
            continue
        default = spec[7]
        assert (lua[name] == default) if isinstance(default, str) else float(lua[name]) == float(default), name


def test_panel_and_help_name_the_stations_cfg_entry():
    """2.0 sets each station's players and meter in its CFG.STATIONS entry, 2.1 the analysis players in
    CFG.ANALYZERS: no page, script, help text or server message names the 1.x keys (CFG.MIXER, MIX_A, ...), a fixed
    RadioOut mixer, or analysis on an off-air deck muted at the mixer."""
    import re
    old = re.compile(r'CFG\.(MIXER|METER|PLAYER_A|PLAYER_B|STATION_ID)\b|\bMIX_[AB]\b|\bMETER_[AB]\b|RadioOut|off-air deck|'
                     r'muted at the mixer|\bmixer\b')
    root = os.path.join(os.path.dirname(__file__), '..', 'server')
    hits = []
    for folder, _, names in os.walk(root):
        for name in names:
            if name.endswith(('.py', '.js', '.html')):
                with open(os.path.join(folder, name), encoding='utf-8') as f:
                    hits += [f'{name}:{n}: {line.strip()}' for n, line in enumerate(f, 1) if old.search(line)]
    assert not hits
    with open(os.path.join(root, 'static', 'js', 'library.js'), encoding='utf-8') as f:
        src = f.read()
    assert '<b>CFG.ANALYZERS</b> in radio.lua 2.1.0' in src        # no analysis player: says where to set it
    # a faulted station: the fault is named first (2.0.0 dev blamed a correct CFG)
    assert src.index("(fault ? '<span><b>Station fault</b>: ' + esc(fault)") < src.index('<b>CFG.ANALYZERS</b>')


def test_version_matches_everywhere():
    """Every change is its own version: VERSION, radio.lua and the changelog's top section agree."""
    import re
    root = os.path.join(os.path.dirname(__file__), '..')
    version = open(os.path.join(root, 'VERSION')).read().strip()
    assert re.fullmatch(r'\d+\.\d+\.\d+', version)
    lua = open(os.path.join(root, 'qsys', 'radio', 'radio.lua')).read()
    assert re.search(r'^RADIO_VERSION = "([^"]+)"', lua, re.M).group(1) == version
    changelog = open(os.path.join(root, 'CHANGELOG.md')).read()
    assert re.search(r'^## \[([^\]]+)\]', changelog, re.M).group(1) == version


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
