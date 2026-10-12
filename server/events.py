"""Ingest Core events (POST /api/v1/events) into core_events + play_log.

Each event is stored once, keyed by (boot, seq): the Core replays its outbox
after an outage, so repeats are counted as duplicates and change nothing.
Only newly stored events build play_log rows and go to syslog.
"""
import json
import logging
from datetime import datetime

from .logs import log

START_KINDS = {'track_start': 'music', 'bumper_start': 'imaging'}
END_KINDS = {'track_end': 'music', 'bumper_end': 'imaging'}
WARN_KINDS = {'file_unsupported', 'dead_air_guard'}
_META = ('kind', 't', 'at', 'boot', 'seq')


class BadRequest(ValueError):
    pass


def _ts(value):
    """Core UTC stamp 'YYYY-MM-DDTHH:MM:SSZ' -> aware datetime."""
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        raise BadRequest(f'bad event time {value!r}')
    if dt.tzinfo is None:
        raise BadRequest(f'event time {value!r} has no zone')
    return dt


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v, limit=1000):
    return str(v)[:limit] if v not in (None, '') else None


def validate(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('events'), list):
        raise BadRequest('expected {"station": ..., "events": [...]}')
    station = _text(payload.get('station'), 200) or 'core'
    events = []
    for ev in payload['events']:
        if not isinstance(ev, dict):
            raise BadRequest('each event must be an object')
        kind, boot, seq = ev.get('kind'), ev.get('boot'), ev.get('seq')
        if not isinstance(kind, str) or not kind or not isinstance(boot, str) or not boot \
                or not isinstance(seq, int) or isinstance(seq, bool):
            raise BadRequest('each event needs kind, boot and an integer seq')
        events.append(dict(ev, _at=_ts(ev.get('t'))))
    events.sort(key=lambda e: (e['boot'], e['seq']))
    return station, _text(payload.get('radio_version'), 50), events


def _syslog(station, ev, replayed):
    kind = ev['kind']
    level = logging.WARNING if kind in WARN_KINDS else logging.INFO
    parts = [f'station={station}']
    if kind in START_KINDS:
        who = ' - '.join(x for x in (ev.get('artist'), ev.get('title')) if x) or ev.get('file')
        parts.insert(0, f'PLAY {START_KINDS[kind]} "{who}"')
    elif kind in END_KINDS:
        parts.insert(0, f'END {END_KINDS[kind]} "{ev.get("file")}"')
    else:
        parts.insert(0, kind.upper())
    for k in sorted(ev):
        if k not in _META and not k.startswith('_') and ev[k] is not None:
            parts.append(f'{k}={ev[k]}')
    parts.append(f'at={ev["t"]}')
    if replayed:
        parts.append('replayed=1')
    log.log(level, ' '.join(parts))


def _close_stale(cur, station, boot, at):
    """A new boot means anything still open from an older boot was cut off."""
    cur.execute("""UPDATE play_log SET ended_at = GREATEST(started_at, %s), end_reason = 'core_restart'
                   WHERE station = %s AND boot <> %s AND ended_at IS NULL""", (at, station, boot))
    if cur.rowcount:
        log.warning('station=%s restarted: closed %d open play(s)', station, cur.rowcount)


def _apply(cur, station, ev):
    kind = ev['kind']
    if kind == 'boot':
        _close_stale(cur, station, ev['boot'], ev['_at'])
    elif kind in START_KINDS:
        cur.execute("""INSERT INTO play_log (station, kind, file, directory, artist, title, deck,
                           started_at, duration_s, boot, start_seq)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (boot, start_seq) DO NOTHING""",
                    (station, START_KINDS[kind], _text(ev.get('file')) or '?', _text(ev.get('dir')),
                     _text(ev.get('artist')), _text(ev.get('title')), _text(ev.get('deck'), 10),
                     ev['_at'], _num(ev.get('duration')), ev['boot'], ev['seq']))
    elif kind in END_KINDS:
        # the newest open play on that deck (same file when the Core sent one)
        cur.execute("""UPDATE play_log SET ended_at = %s, played_s = %s, end_reason = %s, end_seq = %s
                       WHERE id = (SELECT id FROM play_log
                                   WHERE boot = %s AND deck IS NOT DISTINCT FROM %s AND kind = %s
                                     AND ended_at IS NULL AND start_seq < %s
                                     AND (%s::text IS NULL OR file = %s)
                                   ORDER BY start_seq DESC LIMIT 1)""",
                    (ev['_at'], _num(ev.get('played')), _text(ev.get('reason'), 50), ev['seq'],
                     ev['boot'], _text(ev.get('deck'), 10), END_KINDS[kind], ev['seq'],
                     _text(ev.get('file')), _text(ev.get('file'))))


def ingest(conn, payload, now):
    """Store a batch. Returns (station, radio_version, accepted, duplicates). Commits."""
    station, radio_version, events = validate(payload)
    accepted = dupes = 0
    with conn.cursor() as cur:
        # one batch per station at a time (a retry can overlap a slow first try)
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (f'qsysradio:events:{station}',))
        for ev in events:
            data = {k: v for k, v in ev.items() if not k.startswith('_')}
            cur.execute("""INSERT INTO core_events (station, boot, seq, kind, event_at, core_clock,
                               radio_version, data)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (boot, seq) DO NOTHING RETURNING id""",
                        (station, ev['boot'], ev['seq'], ev['kind'][:50], ev['_at'],
                         _num(ev.get('at')), radio_version, json.dumps(data, default=str)))
            if cur.fetchone() is None:
                dupes += 1
                continue
            accepted += 1
            _apply(cur, station, ev)
            _syslog(station, ev, replayed=(now - ev['_at']).total_seconds() > 60)
    conn.commit()
    dropped = payload.get('dropped')
    if isinstance(dropped, int) and dropped > 0:
        log.warning('station=%s outbox overflowed: %d event(s) lost on the Core', station, dropped)
    return station, radio_version, accepted, dupes
