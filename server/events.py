"""Core events -> core_events (every event once) + play_log (one row per airing).

(station, boot, seq) identifies an event: the Core replays its outbox after outages,
so repeats count as duplicates and change nothing. Each event is stored in
its own savepoint, so one malformed event is rejected on its own instead of
failing the batch (which would make the Core resend it forever). Only newly
stored events touch play_log, the per-file figures (lead-in, trailing silence,
average level, peak, unsupported; the file's, shared by every station that
has it: media.py) and the log; log lines are returned to the caller to write
after the transaction commits. An event about a file the station does not
have changes no figures.
"""
import json
import logging
from datetime import datetime

import psycopg2

from . import media
from .db import clean_json, number, text

START_KINDS = {'track_start': 'music', 'bumper_start': 'imaging', 'ad_start': 'ad'}
END_KINDS = {'track_end': 'music', 'bumper_end': 'imaging', 'ad_end': 'ad'}
WARN_KINDS = {'file_unsupported', 'file_failed', 'dead_air_guard', 'library_empty'}
_META = ('kind', 't', 'at', 'boot', 'seq')
SEQ_MAX = 2 ** 31 - 1
# one measurement's level figures (%(level)s dB over %(level_s)s seconds, %(peak)s): a file's average level is the
# energy average of every measurement, weighted by the seconds each heard; its peak is the highest seen
LEVEL_SET = """level_db = CASE WHEN %(level)s::float8 IS NULL THEN level_db
                              WHEN level_db IS NULL OR level_s IS NULL THEN %(level)s
                              ELSE 10 * log((power(10, level_db / 10) * level_s + power(10, %(level)s / 10) * %(level_s)s)
                                            / (level_s + %(level_s)s)) END,
               level_s = CASE WHEN %(level)s::float8 IS NULL THEN level_s ELSE COALESCE(level_s, 0) + %(level_s)s END,
               peak_dbfs = GREATEST(peak_dbfs, %(peak)s)"""


class BadRequest(ValueError):
    pass


def _event_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def clean(ev):
    """A validated copy of one Core event, or None if it cannot be stored."""
    if not isinstance(ev, dict):
        return None
    kind, boot, seq = text(ev.get('kind'), 50), text(ev.get('boot'), 100), ev.get('seq')
    at = _event_time(ev.get('t'))
    if not kind or not boot or not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq <= SEQ_MAX or not at:
        return None
    data = clean_json(ev)
    data.update(kind=kind, boot=boot, seq=seq)
    data['_at'] = at
    return data


def _levels(ev):
    """The level figures in a Core event, for LEVEL_SET: dB on the meter's scale, seconds > 0 (else not used)."""
    level, secs, peak = number(ev.get('level')), number(ev.get('level_s')), number(ev.get('peak'))
    ok = level is not None and -120 < level <= 40 and secs is not None and 0 < secs <= 86400
    return {'level': level if ok else None, 'level_s': secs if ok else None,
            'peak': peak if peak is not None and -120 < peak <= 40 else None}


def _log_line(station, ev, replayed):
    kind = ev['kind']
    if kind in START_KINDS:
        who = ' - '.join(x for x in (ev.get('artist'), ev.get('title')) if x) or ev.get('file')
        head = f'PLAY {START_KINDS[kind]} "{who}"'
    elif kind in END_KINDS:
        head = f'END {END_KINDS[kind]} "{ev.get("file")}"'
    else:
        head = kind.upper()
    parts = [head, f'station={station}']
    parts += [f'{k}={ev[k]}' for k in sorted(ev) if k not in _META and not k.startswith('_') and ev[k] is not None]
    parts.append(f'at={ev["t"]}')
    if replayed:
        parts.append('replayed=1')
    return (logging.WARNING if kind in WARN_KINDS else logging.INFO), ' '.join(parts)


def _apply(cur, station, ev):
    kind = ev['kind']
    if kind == 'command':
        try:
            cid = int(ev.get('id'))
        except (TypeError, ValueError, OverflowError):
            return None
        cur.execute("""UPDATE commands SET done_at = %s, ok = %s, note = %s, delivered_at = COALESCE(delivered_at, %s)
                       WHERE id = %s AND station = %s""",
                    (ev['_at'], bool(ev.get('ok')), text(ev.get('note'), 500), ev['_at'], cid, station))
    elif kind == 'boot':
        # a new boot means anything still open from an older boot was cut off
        cur.execute("""UPDATE play_log SET ended_at = GREATEST(started_at, %s), end_reason = 'core_restart'
                       WHERE station = %s AND boot <> %s AND ended_at IS NULL""", (ev['_at'], station, ev['boot']))
        if cur.rowcount:
            return logging.WARNING, f'station={station} restarted: closed {cur.rowcount} open play(s)'
    elif kind in START_KINDS:
        directory, file = text(ev.get('dir'), 500) or '', text(ev.get('file'), 500) or '?'
        duration = number(ev.get('duration'))
        cur.execute("""INSERT INTO play_log (station, kind, file, directory, artist, title, deck,
                           started_at, duration_s, boot, start_seq)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (station, boot, start_seq) DO NOTHING""",
                    (station, START_KINDS[kind], file, directory, text(ev.get('artist'), 500),
                     text(ev.get('title'), 500), text(ev.get('deck'), 10), ev['_at'], duration, ev['boot'], ev['seq']))
        cur.execute('UPDATE library SET last_played = GREATEST(last_played, %s) WHERE station = %s AND key = %s',
                    (ev['_at'], station, directory + file))
        if duration:
            cur.execute(f'UPDATE media_files SET duration_s = %(d)s WHERE {media.file_of()} AND duration_s IS DISTINCT FROM %(d)s',
                        dict(d=duration, station=station, key=directory + file))
    elif kind in END_KINDS:
        # the newest open play on that deck (same file when the Core sent one)
        file = text(ev.get('file'), 500)
        cur.execute("""UPDATE play_log SET ended_at = %s, played_s = %s, end_reason = %s, end_seq = %s
                       WHERE id = (SELECT id FROM play_log
                                   WHERE station = %s AND boot = %s AND deck IS NOT DISTINCT FROM %s AND kind = %s
                                     AND ended_at IS NULL AND start_seq < %s
                                     AND (%s::text IS NULL OR file = %s)
                                   ORDER BY start_seq DESC LIMIT 1)""",
                    (ev['_at'], number(ev.get('played')), text(ev.get('reason'), 50), ev['seq'], station, ev['boot'],
                     text(ev.get('deck'), 10), END_KINDS[kind], ev['seq'], file, file))
        if kind in END_KINDS:
            # this play's measurements (every kind); trailing silence keeps the shortest seen (LEAST skips NULL);
            # an analyzed file keeps its analyzed quiet start / end; the level always counts
            cur.execute(f"""UPDATE media_files SET lead_in_s = CASE WHEN analyzed_at IS NULL THEN COALESCE(%(lead)s, lead_in_s) ELSE lead_in_s END,
                                trail_s = CASE WHEN analyzed_at IS NULL THEN LEAST(trail_s, %(trail)s) ELSE trail_s END, {LEVEL_SET}
                            WHERE {media.file_of()}""",
                        dict(_levels(ev), lead=number(ev.get('lead_in')), trail=number(ev.get('trail')), station=station,
                             key=(text(ev.get('dir'), 500) or '') + (file or '')))
    elif kind == 'analysis':
        # a file analyzed off air: its quiet start / end replace what plays learned, its level joins them;
        # a failed one is retried by the next run. Either way no station still asks for that file.
        args = dict(station=station, key=(text(ev.get('dir'), 500) or '') + (text(ev.get('file'), 500) or ''))
        if ev.get('ok') is True:
            cur.execute(f"""UPDATE media_files SET analyzed_at = NOW(), analyze_failed_at = NULL, analyze_error = NULL,
                                lead_in_s = %(lead)s, trail_s = %(trail)s, duration_s = COALESCE(%(duration)s, duration_s), {LEVEL_SET}
                            WHERE {media.file_of()}""",
                        dict(_levels(ev), lead=number(ev.get('lead_in')), trail=number(ev.get('trail')),
                             duration=number(ev.get('duration')), **args))
        else:
            cur.execute(f'UPDATE media_files SET analyze_failed_at = NOW(), analyze_error = %(error)s WHERE {media.file_of()}',
                        dict(error=text(ev.get('status'), 200) or 'failed', **args))
        cur.execute(f'UPDATE library SET analyze_wanted = FALSE WHERE {media.file_of("file_id")} AND analyze_wanted', args)
    elif kind == 'file_unsupported':
        cur.execute(f'UPDATE media_files SET unsupported = TRUE WHERE {media.file_of()} AND NOT unsupported',
                    dict(station=station, key=(text(ev.get('dir'), 500) or '') + (text(ev.get('file'), 500) or '')))
    return None


def ingest(cur, station, radio_version, events, now):
    """Store a batch inside the caller's transaction.
    Returns (accepted, duplicates, rejected, log_lines)."""
    if not isinstance(events, list):
        raise BadRequest('events must be a list')
    cleaned = [clean(e) for e in events[:1000]]
    accepted = dupes = 0
    rejected = sum(1 for e in cleaned if e is None) + max(0, len(events) - 1000)
    lines = []
    for raw in sorted((e for e in cleaned if e), key=lambda e: (e['boot'], e['seq'])):
        cur.execute('SAVEPOINT ev')
        try:
            data = {k: v for k, v in raw.items() if not k.startswith('_')}
            cur.execute("""INSERT INTO core_events (station, boot, seq, kind, event_at, core_clock, radio_version, data)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (station, boot, seq) DO NOTHING RETURNING id""",
                        (station, raw['boot'], raw['seq'], raw['kind'], raw['_at'], number(raw.get('at')),
                         radio_version, json.dumps(data)))
            if cur.fetchone() is None:
                dupes += 1
            else:
                accepted += 1
                note = _apply(cur, station, raw)
                if note:
                    lines.append(note)
                lines.append(_log_line(station, raw, replayed=(now - raw['_at']).total_seconds() > 60))
            cur.execute('RELEASE SAVEPOINT ev')
        except (psycopg2.DataError, psycopg2.IntegrityError, ValueError) as exc:
            cur.execute('ROLLBACK TO SAVEPOINT ev')
            rejected += 1
            lines.append((logging.WARNING, f'station={station} rejected event seq={raw.get("seq")}: {exc}'.strip()))
    return accepted, dupes, rejected, lines
