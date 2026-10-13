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
    L.globals().MockCore.httpHandler = handler
    L.execute('dofile("qsys/radio/radio.lua")')
    L.execute("""CFG.SERVER_URL, CFG.SERVER_TOKEN, CFG.STATION_ID = "http://radio-server", "tok", "E2E"
                 Server.queue(EVENTS[#EVENTS]); Server.libraryDirty = true
                 for t = 60, 1200, 75 do MockCore.at(t, function() Library.scan() end) end""")
    L.eval('MockCore.run')(1260)
    played = {r[0] for r in sql("SELECT DISTINCT file FROM qsr_test.play_log WHERE station = 'E2E'", fetch=True)}
    starts = sql("SELECT count(*) FROM qsr_test.play_log WHERE station = 'E2E'", fetch=True)[0][0]
    assert len(played) == 24, f'{24 - len(played)} of 24 songs never played in {starts} plays'


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
        sql("UPDATE qsr_test.library SET duration_s = %s WHERE station = 'FF' AND key = %s",
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
