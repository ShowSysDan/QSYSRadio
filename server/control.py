"""Station control: the Core's sync + library upload, operator commands, live
status for the web page, library search, station settings. Ad schedules: ads.py.

One radio.lua runs every station of its Core: one sync request a second
carries all of them, each station handled in its own transaction (a 1.x
Core's single-station body is still accepted). One advisory lock per station
(db.lock_station, taken before any of the station's rows is touched) serialises
the Core's sync with every web write to the station: so command ids are
committed in order and none can be skipped by the Core's "everything after my
last ack" request, and no web write can deadlock with that station's sync. A
file's figures (media_files) are shared by every station that has it, so a
change to many files at once (Forget measured for a kind) can still meet
another station's sync writing the same rows; PostgreSQL then cancels one of
the two: the sync part is answered {error} (the Core sends it again), the web
write 409 (app.py; nothing changed, try again).
"""
import json
import time
import uuid
from datetime import timedelta

import psycopg2
import psycopg2.extensions

from . import ads, analysis, breaks, clock, core_settings, cores, db, events, logs, media
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
    if 'fault' in s:                # the Core could not build or run the station: why (it retries by itself)
        s['fault'] = text(s['fault'], 300)
    return s


def _clean_meters(meters):
    """The decks' readout from the Core: {A: {peak, rms, gain}, B: …} in dB (meter -120…+20, player gain
    -100…+20), or None."""
    if not isinstance(meters, dict):
        return None
    out = {}
    for deck in ('A', 'B'):
        m = meters.get(deck)
        if isinstance(m, dict):
            out[deck] = {k: round(max(lo, min(20.0, v)), 1) for k, lo in (('peak', -120.0), ('rms', -120.0), ('gain', -100.0))
                         if (v := number(m.get(k))) is not None}
    return out or None


# ── Core side ────────────────────────────────────────────────────────────────
SYNC_STATIONS_MAX = 100     # stations in one sync body (any more are left out)
LIBRARY_PARTS_MAX = 1000    # parts in one library scan
FAIL_LOG_S = 60             # a station whose sync keeps failing is logged at most this often


class ScanIncomplete(BadRequest):
    """The last part of a library scan came without every other part: 409, the Core sends the scan again."""


def _station_sync(cur, station, part, core, version, boot, now):
    """One station's part of the Core's sync, in its own transaction under its station lock: events,
    optional state, command ack and meters in; commands, settings, ads plan and files to analyze out.
    core None: a 1.x Core (stations.core unchanged). Returns (reply, log_lines)."""
    ack = _int(part.get('ack'), 0, 2 ** 62, 0)
    state = _clean_state(part.get('state'))
    reply, lines = {}, []
    db.lock_station(cur, station)
    cur.execute('SELECT boot FROM stations WHERE station = %s', (station,))
    prev = cur.fetchone()
    if prev and prev['boot'] != boot:
        # the Core restarted (it starts again from ack 0): a command it picked up
        # but never reported must not run twice; undelivered ones still go
        cur.execute("""UPDATE commands SET done_at = NOW(), ok = FALSE, note = 'the Core restarted before reporting'
                       WHERE station = %s AND done_at IS NULL AND delivered_at IS NOT NULL""", (station,))
    meters = _clean_meters(part.get('meters'))
    settings_rev = text(part.get('settings_rev'), 50) or ''
    cur.execute("""INSERT INTO stations (station, radio_version, boot, settings_rev, last_seen, meters, core)
                   VALUES (%s, %s, %s, %s, NOW(), %s, %s)
                   ON CONFLICT (station) DO UPDATE SET radio_version = EXCLUDED.radio_version,
                       boot = EXCLUDED.boot, settings_rev = EXCLUDED.settings_rev, last_seen = NOW(),
                       meters = EXCLUDED.meters, core = COALESCE(EXCLUDED.core, stations.core)""",
                (station, version, boot, settings_rev, json.dumps(meters) if meters else None, core))
    if state is not None:
        cur.execute('UPDATE stations SET state = %s, state_at = NOW() WHERE station = %s', (json.dumps(state), station))
    accepted = 0
    evs = [] if part.get('events') == {} else part.get('events')       # Lua encodes an empty table as {}
    if evs is not None:
        accepted, dupes, rejected, lines = events.ingest(cur, station, version, evs, now)
        reply.update(accepted=accepted, duplicates=dupes, rejected=rejected)
    dropped = part.get('dropped')
    if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0:
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
    reply['commands'] = [{'id': r['id'], 'cmd': r['cmd'], 'args': r['args'], 'by': r['requested_by']} for r in rows]
    cur.execute('SELECT COALESCE(MAX(id), 0) FROM commands WHERE station = %s', (station,))
    newest = cur.fetchone()[0]
    if ack > newest:            # server DB was reset: tell the Core to start over
        reply['ack_reset'] = newest
    values = core_settings.for_core(core_settings.merged(cur, station))
    rev = core_settings.rev(values)
    if settings_rev != rev:
        reply['settings'], reply['settings_rev'] = values, rev
    plan, rev = ads.cached_plan(cur, station, fresh=accepted > 0)
    if (text(part.get('ads_rev'), 50) or '') != rev:
        reply['ads'], reply['ads_rev'] = plan, rev
    more, done = analysis.sync(cur, station, (text(part.get('analyze_rev'), 50) or '') if 'analyze_rev' in part else None)
    reply.update(more)
    return reply, lines + done


_failed_logged = {}         # station (or core:<id>) -> monotonic time its failure was last logged


def _part_failed(conn, name, exc):
    """One station's part (or the Core's own bookkeeping, name core:<id>) failed: roll it back so the rest
    goes on, and say so (to the Core: {error}; in the log at most every FAIL_LOG_S). The database itself
    gone is everyone's: DatabaseUnavailable, 503 for the whole request."""
    lost = isinstance(exc, db.DatabaseUnavailable) or (
        isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))
        and not isinstance(exc, psycopg2.extensions.TransactionRollbackError))     # a deadlock is one station's
    if not lost:
        try:
            conn.rollback()
        except psycopg2.Error:
            lost = True
    if lost or conn.closed:
        if isinstance(exc, db.DatabaseUnavailable):
            raise exc
        raise db.DatabaseUnavailable(f'database lost during the Core sync: {exc}'.strip()) from exc
    error = text(str(exc).strip() or type(exc).__name__, 300)
    t, lines = time.monotonic(), []
    if t - _failed_logged.get(name, -1e9) >= FAIL_LOG_S:
        if len(_failed_logged) > 1000:
            _failed_logged.clear()
        _failed_logged[name] = t
        lines.append((30 if isinstance(exc, BadRequest) else 40,
                      f'{name if name.startswith("core:") else "station=" + name} sync failed, rolled back '
                      f'(the other stations go on): {type(exc).__name__}: {error}'))
    return {'error': error}, lines


def core_sync(conn, payload, now, log):
    """The Core's once-a-second request. Returns the reply; log(lines) is called after each commit.
    2.x: {version, boot, core, mem_kb, stations: [{station, ack, settings_rev, ads_rev, analyze_rev?, meters,
    state?, events?, dropped?}]} -> {stations: {id: reply | {error}}}; each station is its own transaction
    (its lock held for its work only, committed before the next): one station's failure rolls back that
    station alone, which gets {error} and sends its events again. 1.x: one station's part at the top level
    ({station, version, boot, ack, ...}) -> that station's reply, as before 2.0."""
    if not isinstance(payload, dict):
        raise BadRequest('expected a JSON object')
    version, boot = text(payload.get('version'), 50), text(payload.get('boot'), 100)
    if 'stations' not in payload:
        station = _station_name(payload)
        with conn.cursor() as cur:
            reply, lines = _station_sync(cur, station, payload, None, version, boot, now)
        conn.commit()
        log(lines)
        return reply
    parts = [] if payload['stations'] == {} else payload['stations']
    if not isinstance(parts, list):
        raise BadRequest('stations must be a list')
    core = text(payload.get('core'), 200)
    if core:
        mem = number(payload.get('mem_kb'))
        try:
            with conn.cursor() as cur:
                lines = cores.seen(cur, core, boot, version, mem if mem is not None and 0 <= mem < 2 ** 40 else None)
            conn.commit()
        except Exception as exc:          # noqa: BLE001 (the stations' work goes on)
            lines = _part_failed(conn, 'core:' + core, exc)[1]
        log(lines)
    replies = {}
    for part in parts[:SYNC_STATIONS_MAX]:
        station = text(part.get('station'), 200) if isinstance(part, dict) else None
        if not station or station in replies:
            continue
        try:
            with conn.cursor() as cur:
                reply, lines = _station_sync(cur, station, part, core, version, boot, now)
            conn.commit()
        except Exception as exc:          # noqa: BLE001 (one station's failure is that station's alone)
            reply, lines = _part_failed(conn, station, exc)
        replies[station] = reply
        log(lines)
    return {'stations': replies}


def core_library(conn, payload):
    """The Core's file list after a scan. 2.x: in parts ({station, boot, scan, part, parts, music_dir,
    imaging_dir, ads_dir, items}, at most 1000 items each, one at a time); 1.x: whole (no scan): one complete
    scan. Each part's files are upserted and marked with the scan id; only the last part, with every part of
    the scan in, marks the station's files not in it gone, so an interrupted or partial upload never shrinks
    the library (its last part without the others: ScanIncomplete, 409; the Core sends the scan again).
    Returns (reply, log line or None): before the last part {ok, scan, part, parts}; the last {ok, items (in
    the scan), learned: the per-file figures the Core uses, {key: {trail, lead, level, peak} | {unsupported}}}.
    The figures are the file's, shared by every station that has it (media.py)."""
    station = _station_name(payload)
    items = payload.get('items')
    if items == {}:                 # Lua encodes an empty table as {}
        items = []
    if not isinstance(items, list):
        raise BadRequest('items must be a list')
    scan = text(payload.get('scan'), 100)
    if scan is None:                # 1.x: the whole list at once
        scan, part, parts = 'whole-' + uuid.uuid4().hex, 1, 1
    else:
        part, parts = _int(payload.get('part'), 1, LIBRARY_PARTS_MAX), _int(payload.get('parts'), 1, LIBRARY_PARTS_MAX)
        if part is None or parts is None or part > parts:
            raise BadRequest(f'part and parts must be whole numbers, 1 <= part <= parts <= {LIBRARY_PARTS_MAX}')
    rows = {}
    for it in items[:100000]:
        if not isinstance(it, dict) or it.get('kind') not in KINDS or not text(it.get('key'), 1000):
            continue
        size = it.get('size')       # 0: the Core could not tell (unknown, so never shared)
        rows[text(it['key'], 1000)] = (
            it['kind'], text(it.get('dir'), 500) or '', text(it.get('file'), 500) or text(it['key'], 500),
            text(it.get('artist'), 500), text(it.get('title'), 500),
            size if isinstance(size, int) and not isinstance(size, bool) and 0 < size < 2 ** 62 else None,
            number(it.get('duration')), number(it.get('lead_in')), number(it.get('trail')), number(it.get('peak')))
    with conn.cursor() as cur:
        db.lock_station(cur, station)
        cur.execute('SELECT scan, scan_parts FROM stations WHERE station = %s', (station,))
        had = cur.fetchone()
        got = sorted(set(had['scan_parts'] if had and had['scan'] == scan else []) | {part})
        missing = sorted(set(range(1, parts + 1)) - set(got)) if part == parts else None
        if missing:
            conn.rollback()           # nothing written; the handler logs it outside the transaction
            raise ScanIncomplete(f'library scan {scan} of station {station}: part(s) {", ".join(map(str, missing[:10]))} '
                                 f'of {parts} not received; send the whole scan again')
        media.link(cur, station, rows, scan)
        if part < parts:
            cur.execute("""INSERT INTO stations (station, scan, scan_parts) VALUES (%s, %s, %s)
                           ON CONFLICT (station) DO UPDATE SET scan = EXCLUDED.scan, scan_parts = EXCLUDED.scan_parts""",
                        (station, scan, got))
            conn.commit()
            return {'ok': True, 'scan': scan, 'part': part, 'parts': parts}, None
        media.gone(cur, station, scan)
        cur.execute("""INSERT INTO stations (station, library_at, music_dir, imaging_dir, ads_dir, scan, scan_parts)
                       VALUES (%s, NOW(), %s, %s, %s, %s, %s)
                       ON CONFLICT (station) DO UPDATE SET library_at = NOW(), music_dir = EXCLUDED.music_dir,
                           imaging_dir = EXCLUDED.imaging_dir, ads_dir = EXCLUDED.ads_dir, scan = EXCLUDED.scan,
                           scan_parts = EXCLUDED.scan_parts""",
                    (station, text(payload.get('music_dir'), 500), text(payload.get('imaging_dir'), 500),
                     text(payload.get('ads_dir'), 500), scan, got))
        ads.stale(cur, station)
        # what the Core uses: unsupported files, the quiet start / end (where to start, when to hand over) and
        # the average level with its peak (level matching); fixed = the operator set a figure, analyzed =
        # measured off air: the Core does not learn over either
        cur.execute("""SELECT key, unsupported, COALESCE(trail_set_s, trail_s) AS trail, COALESCE(lead_set_s, lead_in_s) AS lead,
                              trail_set_s IS NOT NULL OR lead_set_s IS NOT NULL AS fixed, analyzed_at IS NOT NULL AS analyzed,
                              level_db AS level, CASE WHEN level_db IS NOT NULL THEN peak_dbfs END AS peak
                       FROM library_figures
                       WHERE station = %s AND present
                         AND (unsupported OR trail_s IS NOT NULL OR lead_in_s IS NOT NULL OR trail_set_s IS NOT NULL
                              OR lead_set_s IS NOT NULL OR level_db IS NOT NULL)""", (station,))
        learned = {r['key']: {'unsupported': True} if r['unsupported'] else
                   dict({k: round(r[k], 2) for k in ('trail', 'lead') if r[k] is not None},
                        **{k: round(r[k], 1) for k in ('level', 'peak') if r[k] is not None},
                        **{k: True for k in ('fixed', 'analyzed') if r[k]})
                   for r in cur.fetchall()}
        cur.execute('SELECT kind, count(*) FROM library WHERE station = %s AND present GROUP BY kind', (station,))
        counts = dict(cur.fetchall())
    conn.commit()
    return ({'ok': True, 'items': sum(counts.values()), 'learned': learned},
            (20, f'LIBRARY station={station} ' + ' '.join(f'{k}={counts.get(k, 0)}' for k in KINDS)
                 + (f' parts={parts}' if parts > 1 else '')))


# ── Web side ─────────────────────────────────────────────────────────────────

def stations(conn):
    with conn.cursor() as cur:
        cur.execute(f"""SELECT station, last_seen, radio_version, core,
                               last_seen > NOW() - INTERVAL '{ONLINE_S} seconds' AS online,
                               state->'automation' = 'true'::jsonb AS automation,
                               state->'stop_after' = 'true'::jsonb AS stop_after,
                               left(state->>'fault', 300) AS fault
                        FROM stations ORDER BY last_seen DESC""")
        return [{'station': r['station'], 'online': r['online'], 'version': r['radio_version'], 'core': r['core'],
                 'automation': bool(r['automation']), 'stop_after': bool(r['stop_after']), 'fault': r['fault'],
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
        cur.execute("""SELECT station, state, radio_version, boot, core, last_seen, library_at, meters,
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
        next_ad = ads.next_ad(cur, station)
        holds = breaks.holds_view(cur, station)
        ids = breaks.idents(cur, station)
        analyze = analysis.view(cur, station, state)
    age = float(r['age'])
    return {'station': r['station'], 'online': age < ONLINE_S, 'age_s': round(age, 1),
            'state_age_s': round(float(r['state_age']), 2) if r['state_age'] is not None else None,
            'version': r['radio_version'], 'boot': r['boot'], 'core': r['core'], 'last_seen': iso(r['last_seen']),
            'library_at': iso(r['library_at']), 'state': state, 'meters': r['meters'], 'commands': cmds, 'next_ad': next_ad,
            'holds': holds, 'ident': {'due': ids['due'], 'next_at': iso(ids['next_at']), 'count': len(ids['keys'])},
            'analysis': analyze}


def _fill_durations(cur, station, state):
    """Lengths the Core has not measured this boot (up next, queue), from the library."""
    items = [i for i in [state.get('next_up'), state.get('next_bumper'), state.get('next_ad')] + (state.get('queue') or [])
             if isinstance(i, dict) and i.get('duration') is None and isinstance(i.get('key'), str)]
    if not items:
        return
    cur.execute('SELECT key, duration_s FROM library_figures WHERE station = %s AND key = ANY(%s) AND duration_s IS NOT NULL',
                (station, list({i['key'] for i in items})))
    known = {k: d for k, d in cur.fetchall()}
    for i in items:
        i['duration'] = known.get(i['key'])


def _library_item(cur, station, key):
    cur.execute(f"""SELECT key, kind, artist, title, file FROM library
                    WHERE station = %s AND key = %s AND present AND {media.supported()}""", (station, key))
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
        db.lock_station(cur, station)
        cur.execute("""INSERT INTO commands (station, cmd, args, label, requested_by, requested_by_id)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (station, cmd, json.dumps(clean), label, user.get('username'), user.get('user_id')))
        cid = cur.fetchone()['id']
        ads.stale(cur, station)
    conn.commit()
    return cid, (20, f'COMMAND station={station} id={cid} {label} by={user.get("username")}')


def set_trim(conn, station, key, lead, trail, user):
    """The operator's quiet start / end for a file (seconds; None = use what the Core measures), for every
    station that has the file. Returns (log line, the stations to rescan: this one and every other that has
    the file, so each Core picks it up)."""
    vals = []
    for name, v in (('quiet start', lead), ('quiet end', trail)):
        n = None if v is None else number(v)
        if v is not None and (n is None or not 0 <= n <= 600):
            raise BadRequest(f'{name} must be 0–600 seconds, or empty to use the measured one')
        vals.append(None if n is None else round(n, 2))
    with conn.cursor() as cur:
        db.lock_station(cur, station)
        cur.execute(f"""UPDATE media_files SET lead_set_s = %(lead)s, trail_set_s = %(trail)s
                        WHERE {media.file_of(present=True)} RETURNING id""",
                    dict(lead=vals[0], trail=vals[1], station=station, key=key or ''))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            raise BadRequest('that file is not in the library')
        stations = media.stations_of(cur, station, ids)
    conn.commit()
    show = lambda v: 'measured' if v is None else f'{v}s'
    return (20, f'TRIM station={station} key="{key}" start={show(vals[0])} end={show(vals[1])} by={user.get("username")}'), stations


def forget_measured(conn, station, key, kind, user):
    """Forget the measured quiet start / end and level of one file (key) or of every file of a kind the
    station has, for every station that has them; they are measured again on the next play. Operator
    figures stay. Returns (files, log line, the stations to rescan: this one and every other that has
    one of the files forgotten)."""
    if not key and kind not in KINDS:
        raise BadRequest('give a file (key) or a kind')
    with conn.cursor() as cur:
        db.lock_station(cur, station)
        cur.execute("""UPDATE media_files SET lead_in_s = NULL, trail_s = NULL, analyzed_at = NULL, analyze_failed_at = NULL,
                              level_db = NULL, level_s = NULL, peak_dbfs = NULL
                       WHERE (lead_in_s IS NOT NULL OR trail_s IS NOT NULL OR analyzed_at IS NOT NULL
                              OR level_db IS NOT NULL OR peak_dbfs IS NOT NULL)
                         AND """ + (media.file_of() if key else
                                    'id IN (SELECT file_id FROM library WHERE station = %(station)s AND present AND kind = %(kind)s)')
                    + ' RETURNING id', dict(station=station, key=key, kind=kind))
        ids = [r[0] for r in cur.fetchall()]
        stations = media.stations_of(cur, station, ids)
    conn.commit()
    return len(ids), (20, f'FORGET station={station} {"key=" + repr(key) if key else "kind=" + kind} files={len(ids)} '
                          f'by={user.get("username")}'), stations


def rescan(conn, stations, user):
    """Queue a rescan on each station, so its Core takes the files' figures from its library upload reply.
    Returns the log lines."""
    return [add_command(conn, s, 'rescan', {}, user)[1] for s in stations]


_SORTS = {
    'artist': 'lower(COALESCE(artist, title, file)), lower(COALESCE(title, file))',
    'title': 'lower(COALESCE(title, file))',
    'plays': 'plays DESC',
    'last_played': 'last_played DESC NULLS LAST',
    'least_recent': 'last_played ASC NULLS FIRST',
    'loudest': 'level_db DESC NULLS LAST',
    'quietest': 'level_db ASC NULLS LAST',
}


# list filters: name -> SQL condition (the select below is aliased x)
FILTERS = {'never': 'last_played IS NULL', 'unsupported': 'unsupported', 'ident': 'ident',
           'adjusted': 'lead_set_s IS NOT NULL OR trail_set_s IS NOT NULL',
           'not_analyzed': 'analyzed_at IS NULL AND NOT unsupported',
           'hourly': "mode = 'hourly'", 'mix': "mode = 'mix'", 'off': "mode = 'off'"}


def library(conn, station, q=None, kind=None, sort='artist', limit=200, offset=0, only=None):
    """Files (newest scan) with play counts and their length without the
    measured quiet start and end (audible_s); for ads also the schedule and
    this hour's / today's plays (the server's clock and TIMEZONE). Returns
    (total, a page of items, totals over every matching file)."""
    sql = """SELECT l.key, l.kind, l.directory, l.file, l.artist, l.title, l.duration_s, l.unsupported, l.ident,
                    l.lead_in_s, l.trail_s, l.lead_set_s, l.trail_set_s, l.analyzed_at, l.analyze_failed_at, l.analyze_error,
                    l.level_db, l.level_s, l.peak_dbfs,
                    COALESCE({owed}, FALSE) AS analyze_queued,
                    GREATEST(0, l.duration_s - COALESCE(l.lead_set_s, l.lead_in_s, 0)
                                             - COALESCE(l.trail_set_s, l.trail_s, 0)) AS audible_s,
                    COALESCE(p.plays, 0) AS plays, p.last_played, COALESCE(p.plays_hour, 0) AS plays_hour,
                    COALESCE(p.plays_today, 0) AS plays_today, COALESCE(r.mode, 'mix') AS mode, r.per_hour
             FROM library_figures l
             LEFT JOIN LATERAL (SELECT count(*) AS plays, MAX(started_at) AS last_played,
                                       count(*) FILTER (WHERE started_at >= date_trunc('hour', NOW())) AS plays_hour,
                                       count(*) FILTER (WHERE started_at >= date_trunc('day', NOW())) AS plays_today
                                FROM play_log pl
                                WHERE pl.station = l.station AND pl.file = l.file
                                  AND COALESCE(pl.directory, '') = l.directory) p ON TRUE
             LEFT JOIN ad_rules r ON r.station = l.station AND r.key = l.key
             LEFT JOIN stations s ON s.station = l.station
             WHERE l.station = %s AND l.present""".replace('{owed}', analysis.owed_sql('l', 's.analyze_from', 's.analyze_run_at'))
    args = [station]
    if kind in KINDS:
        sql += ' AND l.kind = %s'
        args.append(kind)
    if q:
        sql += ' AND (l.file ILIKE %s OR l.artist ILIKE %s OR l.title ILIKE %s)'
        args += [like(q)] * 3
    if only in FILTERS:
        sql = f'SELECT * FROM ({sql}) x WHERE {FILTERS[only]}'
    else:
        sql = f'SELECT * FROM ({sql}) x'
    with conn.cursor() as cur:
        cur.execute(f"""SELECT count(*) AS n, COALESCE(sum(duration_s), 0) AS duration_s,
                               COALESCE(sum(audible_s), 0) AS audible_s, count(*) FILTER (WHERE duration_s IS NULL) AS unknown,
                               count(*) FILTER (WHERE last_played IS NULL) AS never_played, min(last_played) AS oldest_played
                        FROM ({sql}) x""", args)
        totals = dict(cur.fetchone())
        totals['oldest_played'] = iso(totals['oldest_played'])
        total = totals['n']
        cur.execute(sql + f' ORDER BY {_SORTS.get(sort, _SORTS["artist"])}, key LIMIT %s OFFSET %s',
                    args + [limit, offset])
        items = [dict(r, last_played=iso(r['last_played']), analyzed_at=iso(r['analyzed_at']),
                      analyze_failed_at=iso(r['analyze_failed_at'])) for r in cur.fetchall()]
        if any(i['kind'] == 'ad' for i in items):
            sched, hour_start = ads.schedule(cur, station)
            for i in items:
                r = sched.get(i['key'])
                if r and 'slots' in r:      # this hour's times for an hourly ad: done / due / missed / next
                    i['slots'] = [{'at': iso(hour_start + timedelta(minutes=m)), 'state': st}
                                  for m, st in zip(r['slots'], r['states'])]
    return total, items, totals


def _setting_rows(spec, overrides, running, extra=lambda n: {}):
    return [dict({'name': n, 'label': lb, 'group': g, 'type': t, 'min': None if t == 'choice' else lo,
                  'max': hi, 'step': st, 'choices': list(lo) if t == 'choice' else None, 'default': d, 'help': h,
                  'override': overrides.get(n), 'running': running.get(n), 'server_only': n in core_settings.SERVER_ONLY},
                 **extra(n))
            for n, lb, g, t, lo, hi, st, d, h in spec]


def settings_view(conn, station):
    """The Settings page. station None = All stations (no folders: those are per station); else that station's
    view: per row its own value, the All-stations one, where the value in force comes from (station | all |
    default) and what the Core runs (the server's own settings: what the server uses for it).
    'override' = the value set in the scope shown."""
    with conn.cursor() as cur:
        every, own = core_settings.layers(cur, station)
        r = None
        if station:
            cur.execute('SELECT state, settings_rev FROM stations WHERE station = %s', (station,))
            r = cur.fetchone()
    if not station:
        spec = [x for x in core_settings.SPEC if x[0] not in core_settings.STATION_ONLY]
        return {'scope': 'all', 'station': None, 'settings': _setting_rows(spec, every, {}, lambda n: {
            'all': every.get(n), 'source': 'all' if n in every else 'default', 'station_only': False})}
    values = {**every, **own}
    running = ((_clean_state(r['state']) if r else None) or {}).get('settings') or {}
    for name in core_settings.SERVER_ONLY:          # the server's own: "running" is what it uses
        running[name] = values.get(name, core_settings.DEFAULTS[name])
    rev, rev_core = core_settings.rev(core_settings.for_core(values)), r and r['settings_rev']
    return {'scope': 'station', 'station': station, 'rev': rev, 'core_rev': rev_core, 'in_sync': rev_core == rev,
            'settings': _setting_rows(core_settings.SPEC, own, running, lambda n: {
                'all': every.get(n), 'own': own.get(n), 'station_only': n in core_settings.STATION_ONLY,
                'source': 'station' if n in own else 'all' if n in every else 'default'})}


def _save(conn, form, user, spec, what, prefix=core_settings.PREFIX, station=None):
    """form: {name: value, or None to go back to the layer below: the default, or for a station its
    All-stations value (its folders: the default)}. station: into that station's own layer.
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
        if prefix == core_settings.PREFIX:
            db.lock_station(cur, station)        # All stations: every station's lock (their plans go stale)
        before = core_settings.layer(cur, station) if station else core_settings.load(cur, prefix, by_name)
        changed = {k: v for k, v in changes.items() if before.get(k) != v}
        core_settings.save(cur, changed, prefix, station)
        if changed and prefix == core_settings.PREFIX:
            ads.stale(cur, station)              # All stations: every station's plan
    conn.commit()
    below = lambda k: 'all-stations' if station and k not in core_settings.STATION_ONLY else 'default'
    line = None
    if changed:
        line = (20, f'{what} changed by={user.get("username")} ' +
                ' '.join(f'{k}={below(k) if v is None else v}' for k, v in sorted(changed.items())))
    return sorted(changed), line


def save_settings(conn, form, user, station=None):
    """Into one layer: All stations (station None; never the folders) or that station's own values."""
    if not station and isinstance(form, dict):
        folder = next((x[1] for x in core_settings.SPEC if x[0] in form and x[0] in core_settings.STATION_ONLY), None)
        if folder:
            raise BadRequest(f'{folder} is set per station: choose the station, not All stations')
    return _save(conn, form, user, core_settings.SPEC, f'SETTINGS station={station or "all"}', station=station)


# ── Server settings (syslog, clock) ──────────────────────────────────────────
# module: its spec(), PREFIX and configure(overrides) -> the settings in use
SERVER_GROUPS = ((logs, 'SYSLOG settings'), (clock, 'CLOCK settings'))


def _overrides(cur, module):
    return core_settings.load(cur, module.PREFIX, {x[0]: x for x in module.spec()})


def apply_server_settings(conn):
    """Load the saved server settings and apply them in this worker. Returns {module: settings in use}."""
    with conn.cursor() as cur:
        return {m: m.configure(_overrides(cur, m)) for m, _ in SERVER_GROUPS}


def server_settings_view(conn):
    """Syslog and clock settings; 'running' = what this worker is using."""
    rows = []
    with conn.cursor() as cur:
        for m, _ in SERVER_GROUPS:
            overrides = _overrides(cur, m)
            rows += _setting_rows(m.spec(), overrides, m.configure(overrides))
    return {'settings': rows}


def save_server_settings(conn, form, user):
    if not isinstance(form, dict):
        raise BadRequest('expected an object of settings')
    names = {x[0] for m, _ in SERVER_GROUPS for x in m.spec()}
    unknown = set(form) - names
    if unknown:
        raise BadRequest(f'unknown setting {sorted(unknown)[0]!r}')
    changed, lines = [], []
    for m, what in SERVER_GROUPS:
        part = {k: v for k, v in form.items() if k in {x[0] for x in m.spec()}}
        if part:
            c, line = _save(conn, part, user, m.spec(), what, m.PREFIX)
            changed += c
            lines.append(line)
    apply_server_settings(conn)
    return sorted(changed), lines
