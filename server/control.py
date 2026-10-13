"""Station control: the Core's sync + library upload, operator commands, live
status for the web page, library search, station settings. Ad schedules: ads.py.

One advisory lock per station serialises the Core's sync with operators'
commands, so command ids are committed in order and none can be skipped by
the Core's "everything after my last ack" request.
"""
import json

from psycopg2.extras import execute_values

from . import ads, core_settings, events, logs
from .db import clean_json, iso, like, number, text
from .events import BadRequest

KINDS = ('music', 'imaging', 'ad')
COMMAND_TTL_S = 60          # a command the Core has not picked up by then expires
RESULT_TTL_S = 300          # a picked-up command with no result by then is marked failed
ONLINE_S = 10               # a Core not heard from for this long shows as offline

# command -> (label, needs a library key)
COMMANDS = {
    'skip': ('Skip', False),
    'bumper_now': ('Bumper now', False),
    'ad_now': ('Ad break now', False),
    'stop_after': ('Stop after current', False),
    'stop_now': ('Stop now', False),
    'start': ('Start automation', False),
    'queue_add': ('Play next', True),
    'play_now': ('Play now', True),
    'queue_remove': ('Remove from queue', False),
    'queue_move': ('Move in queue', False),
    'queue_clear': ('Clear queue', False),
    'rescan': ('Rescan library', False),
}


def _lock_station(cur, station):
    cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (f'qsysradio:station:{station}',))


def _station_name(payload):
    station = text(payload.get('station'), 200) if isinstance(payload, dict) else None
    if not station:
        raise BadRequest('station is required')
    return station


def _int(value, lo, hi, default=None):
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if lo <= v <= hi else default


def _clean_state(state):
    """The Core's state as stored and served: only the expected shapes."""
    if not isinstance(state, dict):
        return None
    s = clean_json(state)
    for key in ('queue',):
        if not isinstance(s.get(key), list):
            s[key] = []
    for key in ('decks', 'library', 'settings'):
        if not isinstance(s.get(key), dict):
            s[key] = {}
    for key in ('automation', 'stop_after', 'bumper_requested', 'ad_requested'):
        s[key] = s.get(key) is True
    return s


# ── Core side ────────────────────────────────────────────────────────────────

def core_sync(conn, payload, now):
    """The Core's once-a-second request: events, optional state, command ack.
    Returns (reply, log_lines)."""
    if not isinstance(payload, dict):
        raise BadRequest('expected a JSON object')
    station = _station_name(payload)
    ack = _int(payload.get('ack'), 0, 2 ** 62, 0)
    state = _clean_state(payload.get('state'))
    reply, lines = {}, []
    boot = text(payload.get('boot'), 100)
    with conn.cursor() as cur:
        _lock_station(cur, station)
        cur.execute('SELECT boot FROM stations WHERE station = %s', (station,))
        prev = cur.fetchone()
        if prev and prev['boot'] != boot:
            # the Core restarted (it starts again from ack 0): a command it picked up
            # but never reported must not run twice; undelivered ones still go
            cur.execute("""UPDATE commands SET done_at = NOW(), ok = FALSE, note = 'the Core restarted before reporting'
                           WHERE station = %s AND done_at IS NULL AND delivered_at IS NOT NULL""", (station,))
        cur.execute("""INSERT INTO stations (station, radio_version, boot, settings_rev, last_seen)
                       VALUES (%s, %s, %s, %s, NOW())
                       ON CONFLICT (station) DO UPDATE SET radio_version = EXCLUDED.radio_version,
                           boot = EXCLUDED.boot, settings_rev = EXCLUDED.settings_rev, last_seen = NOW()""",
                    (station, text(payload.get('version'), 50), boot, text(payload.get('settings_rev'), 50) or ''))
        if state is not None:
            cur.execute('UPDATE stations SET state = %s, state_at = NOW() WHERE station = %s',
                        (json.dumps(state), station))
        if payload.get('events') is not None:
            accepted, dupes, rejected, lines = events.ingest(
                cur, station, text(payload.get('version'), 50), payload['events'], now)
            reply.update(accepted=accepted, duplicates=dupes, rejected=rejected)
            dropped = payload.get('dropped')
            if isinstance(dropped, int) and dropped > 0:
                lines.append((30, f'station={station} outbox overflowed: {dropped} event(s) lost on the Core'))
        cur.execute(f"""UPDATE commands SET done_at = NOW(), ok = FALSE,
                           note = CASE WHEN delivered_at IS NULL THEN 'expired: the Core did not pick it up'
                                       ELSE 'no result from the Core' END
                        WHERE station = %s AND done_at IS NULL
                          AND ((delivered_at IS NULL AND created_at < NOW() - INTERVAL '{COMMAND_TTL_S} seconds')
                            OR delivered_at < NOW() - INTERVAL '{RESULT_TTL_S} seconds')""", (station,))
        cur.execute(f"""SELECT id, cmd, args, requested_by FROM commands
                        WHERE station = %s AND id > %s AND done_at IS NULL
                          AND created_at > NOW() - INTERVAL '{COMMAND_TTL_S} seconds'
                        ORDER BY id LIMIT 20""", (station, ack))
        rows = cur.fetchall()
        if rows:
            cur.execute('UPDATE commands SET delivered_at = NOW() WHERE id = ANY(%s) AND delivered_at IS NULL',
                        ([r['id'] for r in rows],))
        reply['commands'] = [{'id': r['id'], 'cmd': r['cmd'], 'args': r['args'], 'by': r['requested_by']}
                             for r in rows]
        cur.execute('SELECT COALESCE(MAX(id), 0) FROM commands WHERE station = %s', (station,))
        newest = cur.fetchone()[0]
        if ack > newest:            # server DB was reset: tell the Core to start over
            reply['ack_reset'] = newest
        overrides = core_settings.load(cur)
        rev = core_settings.rev(overrides)
        if (text(payload.get('settings_rev'), 50) or '') != rev:
            reply['settings'], reply['settings_rev'] = overrides, rev
        plan, rev = ads.plan(cur, station)
        if (text(payload.get('ads_rev'), 50) or '') != rev:
            reply['ads'], reply['ads_rev'] = plan, rev
    conn.commit()
    return reply, lines


def core_library(conn, payload):
    """The Core's file list after a scan. Returns (count, per-file figures the
    Core uses: {key: {trail, unsupported}}, log_line)."""
    station = _station_name(payload)
    items = payload.get('items')
    if items == {}:                 # Lua encodes an empty table as {}
        items = []
    if not isinstance(items, list):
        raise BadRequest('items must be a list')
    rows = {}
    for it in items[:100000]:
        if not isinstance(it, dict) or it.get('kind') not in KINDS or not text(it.get('key'), 1000):
            continue
        size = it.get('size')
        rows[text(it['key'], 1000)] = (
            station, text(it['key'], 1000), it['kind'], text(it.get('dir'), 500) or '',
            text(it.get('file'), 500) or text(it['key'], 500), text(it.get('artist'), 500), text(it.get('title'), 500),
            size if isinstance(size, int) and not isinstance(size, bool) and 0 <= size < 2 ** 62 else None,
            number(it.get('duration')), number(it.get('lead_in')), number(it.get('trail')), number(it.get('peak')))
    with conn.cursor() as cur:
        _lock_station(cur, station)
        cur.execute('UPDATE library SET present = FALSE WHERE station = %s AND present', (station,))
        execute_values(cur, """INSERT INTO library (station, key, kind, directory, file, artist, title, size,
                                   duration_s, lead_in_s, trail_s, peak_dbfs) VALUES %s
                               ON CONFLICT (station, key) DO UPDATE SET kind = EXCLUDED.kind,
                                   directory = EXCLUDED.directory, file = EXCLUDED.file,
                                   artist = EXCLUDED.artist, title = EXCLUDED.title, size = EXCLUDED.size,
                                   duration_s = COALESCE(EXCLUDED.duration_s, library.duration_s),
                                   lead_in_s = COALESCE(EXCLUDED.lead_in_s, library.lead_in_s),
                                   trail_s = COALESCE(EXCLUDED.trail_s, library.trail_s),
                                   peak_dbfs = COALESCE(EXCLUDED.peak_dbfs, library.peak_dbfs),
                                   unsupported = library.unsupported AND library.size IS NOT DISTINCT FROM EXCLUDED.size,
                                   present = TRUE, last_seen = NOW()""", list(rows.values()), page_size=1000)
        cur.execute("""INSERT INTO stations (station, library_at, music_dir, imaging_dir, ads_dir)
                       VALUES (%s, NOW(), %s, %s, %s)
                       ON CONFLICT (station) DO UPDATE SET library_at = NOW(), music_dir = EXCLUDED.music_dir,
                           imaging_dir = EXCLUDED.imaging_dir, ads_dir = EXCLUDED.ads_dir""",
                    (station, text(payload.get('music_dir'), 500), text(payload.get('imaging_dir'), 500),
                     text(payload.get('ads_dir'), 500)))
        cur.execute("""SELECT key, trail_s, unsupported FROM library
                       WHERE station = %s AND present AND (trail_s IS NOT NULL OR unsupported)""", (station,))
        learned = {r['key']: ({'unsupported': True} if r['unsupported'] else {'trail': round(r['trail_s'], 2)})
                   for r in cur.fetchall()}
    conn.commit()
    counts = ' '.join(f'{k}={sum(1 for r in rows.values() if r[2] == k)}' for k in KINDS)
    return len(rows), learned, (20, f'LIBRARY station={station} {counts}')


# ── Web side ─────────────────────────────────────────────────────────────────

def stations(conn):
    with conn.cursor() as cur:
        cur.execute(f"""SELECT station, last_seen, radio_version,
                               last_seen > NOW() - INTERVAL '{ONLINE_S} seconds' AS online,
                               state->'automation' = 'true'::jsonb AS automation,
                               state->'stop_after' = 'true'::jsonb AS stop_after
                        FROM stations ORDER BY last_seen DESC""")
        return [{'station': r['station'], 'online': r['online'], 'version': r['radio_version'],
                 'automation': bool(r['automation']), 'stop_after': bool(r['stop_after']),
                 'last_seen': iso(r['last_seen'])} for r in cur.fetchall()]


def resolve_station(conn, station):
    """The requested station, or the most recently seen one."""
    with conn.cursor() as cur:
        if station:
            cur.execute('SELECT station FROM stations WHERE station = %s', (station,))
        else:
            cur.execute('SELECT station FROM stations ORDER BY last_seen DESC LIMIT 1')
        r = cur.fetchone()
        return r['station'] if r else None


def status(conn, station):
    """Live Core state + recent commands for the dashboard. state_age_s says
    how old the Core's elapsed/remaining figures are."""
    with conn.cursor() as cur:
        cur.execute("""SELECT station, state, radio_version, boot, last_seen, library_at,
                              EXTRACT(EPOCH FROM NOW() - last_seen) AS age,
                              EXTRACT(EPOCH FROM NOW() - state_at) AS state_age
                       FROM stations WHERE station = %s""", (station,))
        r = cur.fetchone()
        if not r:
            return None
        cur.execute("""SELECT id, cmd, label, requested_by, created_at, delivered_at, done_at, ok, note
                       FROM commands WHERE station = %s ORDER BY id DESC LIMIT 12""", (station,))
        cmds = [{'id': c['id'], 'cmd': c['cmd'], 'label': c['label'], 'by': c['requested_by'],
                 'created_at': iso(c['created_at']),
                 'state': ('done' if c['ok'] else 'failed') if c['done_at'] else ('sent' if c['delivered_at'] else 'pending'),
                 'note': c['note']} for c in cur.fetchall()]
        state = _clean_state(r['state']) or {}
        _fill_durations(cur, station, state)
    age = float(r['age'])
    return {'station': r['station'], 'online': age < ONLINE_S, 'age_s': round(age, 1),
            'state_age_s': round(float(r['state_age']), 2) if r['state_age'] is not None else None,
            'version': r['radio_version'], 'boot': r['boot'], 'last_seen': iso(r['last_seen']),
            'library_at': iso(r['library_at']), 'state': state, 'commands': cmds}


def _fill_durations(cur, station, state):
    """Lengths the Core has not measured this boot (up next, queue), from the library."""
    items = [i for i in [state.get('next_up'), state.get('next_bumper'), state.get('next_ad')] + (state.get('queue') or [])
             if isinstance(i, dict) and i.get('duration') is None and isinstance(i.get('key'), str)]
    if not items:
        return
    cur.execute('SELECT key, duration_s FROM library WHERE station = %s AND key = ANY(%s) AND duration_s IS NOT NULL',
                (station, list({i['key'] for i in items})))
    known = {k: d for k, d in cur.fetchall()}
    for i in items:
        i['duration'] = known.get(i['key'])


def _library_item(cur, station, key):
    cur.execute("""SELECT key, kind, artist, title, file FROM library
                   WHERE station = %s AND key = %s AND present AND NOT unsupported""", (station, key))
    return cur.fetchone()


def add_command(conn, station, cmd, args, user):
    """Validate and queue an operator command. Returns (id, log_line)."""
    if cmd not in COMMANDS:
        raise BadRequest(f'unknown command {cmd!r}')
    label, needs_key = COMMANDS[cmd]
    args = args if isinstance(args, dict) else {}
    clean = {}
    with conn.cursor() as cur:
        key = text(args.get('key'), 1000)
        if needs_key or (cmd in ('bumper_now', 'ad_now') and key):
            item = _library_item(cur, station, key or '')
            if not item:
                raise BadRequest('that file is not in the library')
            if cmd == 'bumper_now' and item['kind'] != 'imaging':
                raise BadRequest('Bumper now needs an imaging file')
            if cmd == 'ad_now' and item['kind'] != 'ad':
                raise BadRequest('Ad break now needs an ad')
            if cmd == 'queue_add' and item['kind'] != 'music':
                raise BadRequest('only music can be queued; use Play now for imaging and ads')
            clean['key'] = item['key']
            label += ': ' + (' - '.join(x for x in (item['artist'], item['title']) if x) or item['file'])
        if cmd == 'queue_add':
            clean['position'] = 'next' if args.get('position') == 'next' else 'end'
            if clean['position'] == 'end':
                label = label.replace('Play next', 'Add to queue', 1)
        elif cmd == 'stop_after':
            clean['on'] = args.get('on') is not False
            if not clean['on']:
                label = 'Cancel stop after current'
        elif cmd in ('queue_remove', 'queue_move'):
            clean['index'] = _int(args.get('index'), 1, 1000)
            if clean['index'] is None:
                raise BadRequest('index must be a queue position')
            if cmd == 'queue_move':
                clean['to'] = _int(args.get('to'), 0, 1001)
                if clean['to'] is None:
                    raise BadRequest('to must be a queue position')
            if key:
                clean['key'] = key
        _lock_station(cur, station)
        cur.execute("""INSERT INTO commands (station, cmd, args, label, requested_by, requested_by_id)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (station, cmd, json.dumps(clean), label, user.get('username'), user.get('user_id')))
        cid = cur.fetchone()['id']
    conn.commit()
    return cid, (20, f'COMMAND station={station} id={cid} {label} by={user.get("username")}')


_SORTS = {
    'artist': 'lower(COALESCE(l.artist, l.title, l.file)), lower(COALESCE(l.title, l.file))',
    'title': 'lower(COALESCE(l.title, l.file))',
    'plays': 'plays DESC',
    'last_played': 'last_played DESC NULLS LAST',
    'least_recent': 'last_played ASC NULLS FIRST',
}


def library(conn, station, q=None, kind=None, sort='artist', limit=200, offset=0):
    """Files (newest scan) with play counts; for ads also the schedule and this
    hour's / today's plays (the server's clock and TIMEZONE)."""
    sql = """SELECT l.key, l.kind, l.directory, l.file, l.artist, l.title, l.duration_s, l.unsupported,
                    COALESCE(p.plays, 0) AS plays, p.last_played, COALESCE(p.plays_hour, 0) AS plays_hour,
                    COALESCE(p.plays_today, 0) AS plays_today, COALESCE(r.mode, 'mix') AS mode, r.per_hour
             FROM library l
             LEFT JOIN LATERAL (SELECT count(*) AS plays, MAX(started_at) AS last_played,
                                       count(*) FILTER (WHERE started_at >= date_trunc('hour', NOW())) AS plays_hour,
                                       count(*) FILTER (WHERE started_at >= date_trunc('day', NOW())) AS plays_today
                                FROM play_log pl
                                WHERE pl.station = l.station AND pl.file = l.file
                                  AND COALESCE(pl.directory, '') = l.directory) p ON TRUE
             LEFT JOIN ad_rules r ON r.station = l.station AND r.key = l.key
             WHERE l.station = %s AND l.present"""
    args = [station]
    if kind in KINDS:
        sql += ' AND l.kind = %s'
        args.append(kind)
    if q:
        sql += ' AND (l.file ILIKE %s OR l.artist ILIKE %s OR l.title ILIKE %s)'
        args += [like(q)] * 3
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM ({sql}) x', args)
        total = cur.fetchone()[0]
        cur.execute(sql + f' ORDER BY {_SORTS.get(sort, _SORTS["artist"])}, l.key LIMIT %s OFFSET %s',
                    args + [limit, offset])
        items = [dict(r, last_played=iso(r['last_played'])) for r in cur.fetchall()]
    return total, items


def _setting_rows(spec, overrides, running):
    return [{'name': n, 'label': lb, 'group': g, 'type': t, 'min': None if t == 'choice' else lo,
             'max': hi, 'step': st, 'choices': list(lo) if t == 'choice' else None, 'default': d, 'help': h,
             'override': overrides.get(n), 'running': running.get(n)}
            for n, lb, g, t, lo, hi, st, d, h in spec]


def settings_view(conn, station):
    with conn.cursor() as cur:
        overrides = core_settings.load(cur)
        running, rev_core = {}, None
        if station:
            cur.execute('SELECT state, settings_rev FROM stations WHERE station = %s', (station,))
            r = cur.fetchone()
            if r:
                running = (_clean_state(r['state']) or {}).get('settings') or {}
                rev_core = r['settings_rev']
    rev = core_settings.rev(overrides)
    return {'rev': rev, 'core_rev': rev_core, 'in_sync': rev_core == rev,
            'settings': _setting_rows(core_settings.SPEC, overrides, running)}


def _save(conn, form, user, spec, prefix, what):
    """form: {name: value, or None to go back to the default}.
    Returns (changed names, log_line or None)."""
    if not isinstance(form, dict):
        raise BadRequest('expected an object of settings')
    by_name = {x[0]: x for x in spec}
    changes = {}
    for name, raw in form.items():
        if name not in by_name:
            raise BadRequest(f'unknown setting {name!r}')
        try:
            changes[name] = None if raw is None else core_settings.coerce(name, raw, by_name)
        except ValueError as exc:
            raise BadRequest(str(exc))
    with conn.cursor() as cur:
        before = core_settings.load(cur, prefix, by_name)
        changed = {k: v for k, v in changes.items() if before.get(k) != v}
        core_settings.save(cur, changed, prefix)
    conn.commit()
    line = None
    if changed:
        line = (20, f'{what} changed by={user.get("username")} ' +
                ' '.join(f'{k}={"default" if v is None else v}' for k, v in sorted(changed.items())))
    return sorted(changed), line


def save_settings(conn, form, user):
    return _save(conn, form, user, core_settings.SPEC, core_settings.PREFIX, 'SETTINGS')


# ── Server settings (syslog) ─────────────────────────────────────────────────

def syslog_overrides(conn):
    spec = logs.spec()
    with conn.cursor() as cur:
        return core_settings.load(cur, logs.PREFIX, {x[0]: x for x in spec})


def syslog_view(conn):
    """The syslog settings; 'running' = what this worker is using."""
    overrides = syslog_overrides(conn)
    return {'settings': _setting_rows(logs.spec(), overrides, logs.configure(overrides))}


def save_syslog(conn, form, user):
    changed, line = _save(conn, form, user, logs.spec(), logs.PREFIX, 'SYSLOG settings')
    logs.configure(syslog_overrides(conn))
    return changed, line
