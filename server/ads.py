"""Ad scheduling. Each ad is mixed into ad breaks (the default), played a set
number of times per clock hour, or off. The server works out which ads are
owed and sends the Core a plan on its sync; the Core only plays it.

An hourly ad with N plays an hour has N slots, at (k + phase) / N of the
hour; the phases stagger the hourly ads evenly, so four ads at 4 an hour
fall due every 3.75 min in turn (:02, :06, :09, :13, :17 ...), not all at
once. A slot is owed from its time for its window (10 min, or half the gap
to the ad's next slot if shorter); the Core starts a break at the next song
change while one is owed. A slot that cannot play in its window (station
stopped, a long song) is skipped, not made up later in the hour. To land
breaks on time the server also names a "fill" song: the next song (after the
one on air) chosen by length so the song change falls just after the next slot.
"""
import hashlib
import json
import math
from datetime import timedelta

from . import core_settings
from .db import text
from .events import BadRequest

MODES = ('mix', 'hourly', 'off')
FIT_SAMPLE = 40            # songs considered when fitting the rotation to an ad slot
FIT_MAX_LATE_S = 120       # no fill when even the best choice lands later than this
PER_HOUR_MAX = 30


WINDOW_MAX_MIN = 10


def window_minutes(per_hour):
    """How long after its time a slot may still play: at most 10 min, and
    never more than half the gap to the ad's next slot."""
    return min(WINDOW_MAX_MIN, 30 / per_hour)


def slot_minutes(per_hour, phase):
    return [(k + phase) / per_hour * 60 for k in range(per_hour)]


def slot_states(slots, window, plays, now_m):
    """Each slot this hour: done (played in its window), due (open and not
    played yet), missed (window passed without a play: skipped, never made
    up) or next (still to come). plays / now_m: minutes past the hour."""
    out = []
    for m in slots:
        if any(m <= p <= m + window for p in plays):
            out.append('done')
        elif now_m < m:
            out.append('next')
        else:
            out.append('due' if now_m <= m + window else 'missed')
    return out


def schedule(cur, station):
    """This hour for the station's ads: ({key: row}, hour_start). Hourly rows
    also get slots (minutes past the hour; hourly ads are staggered evenly in
    key order so ads with the same count fall due one at a time) and states."""
    cur.execute("""SELECT l.key, l.artist, l.title, l.file, COALESCE(r.mode, 'mix') AS mode, r.per_hour,
                          COALESCE(p.plays, '{}') AS plays, h.hour_start,
                          EXTRACT(EPOCH FROM NOW() - h.hour_start) / 60 AS now_m
                   FROM (SELECT date_trunc('hour', NOW()) AS hour_start) h
                   JOIN library l ON l.station = %s AND l.present AND l.kind = 'ad' AND NOT l.unsupported
                   LEFT JOIN ad_rules r ON r.station = l.station AND r.key = l.key
                   LEFT JOIN (SELECT COALESCE(directory, '') AS directory, file,
                                     array_agg(EXTRACT(EPOCH FROM started_at - date_trunc('hour', NOW())) / 60) AS plays
                              FROM play_log WHERE station = %s AND kind = 'ad' AND started_at >= date_trunc('hour', NOW())
                              GROUP BY 1, 2) p ON p.directory = COALESCE(l.directory, '') AND p.file = l.file
                   ORDER BY l.key""", (station, station))
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        cur.execute("SELECT date_trunc('hour', NOW())")
        return {}, cur.fetchone()[0]
    hourly = [r for r in rows if r['mode'] == 'hourly' and r['per_hour']]
    for i, r in enumerate(hourly):
        r['slots'] = slot_minutes(r['per_hour'], (i + 0.5) / len(hourly))
        r['states'] = slot_states(r['slots'], window_minutes(r['per_hour']), [float(p) for p in r['plays']],
                                  float(r['now_m']))
    return {r['key']: r for r in rows}, rows[0]['hour_start']


def _setting(cur, name):
    return core_settings.load(cur).get(name, core_settings.BY_NAME[name][7])


def fill(cur, station, slot_at):
    """The song to play after the one on air so the break lands soon after
    the next ad slot opens (slot_at), or None. Looks two songs ahead: the
    candidate's own end, or the best follow-up's end. Candidates: a sample of
    present music with a known length, not played in the last hour, keeping
    artist separation; the sample is seeded by the play on air, so the pick
    is stable while that song plays. Only when the operator queue is empty."""
    cur.execute("""SELECT p.id, p.started_at, p.duration_s, l.trail_s, s.state
                   FROM stations s
                   JOIN play_log p ON p.station = s.station AND p.kind = 'music' AND p.ended_at IS NULL
                   LEFT JOIN library l ON l.station = p.station AND l.key = COALESCE(p.directory, '') || p.file
                   WHERE s.station = %s ORDER BY p.started_at DESC LIMIT 1""", (station,))
    on_air = cur.fetchone()
    state = (on_air and on_air['state']) or {}
    if not on_air or not on_air['duration_s'] or state.get('queue') or state.get('automation') is not True:
        return None
    xfade = float(_setting(cur, 'crossfade_s'))

    def air(duration, trail):          # start to the next item's start: crossfade early by the learned tail
        return duration - xfade - (trail or 0)
    gap = (slot_at - on_air['started_at']).total_seconds() - air(on_air['duration_s'], on_air['trail_s'])
    if gap <= 0:
        return None
    cur.execute("""SELECT l.key, l.duration_s, l.trail_s FROM library l
                   WHERE l.station = %s AND l.present AND l.kind = 'music' AND NOT l.unsupported AND l.duration_s > 0
                     AND NOT EXISTS (SELECT 1 FROM play_log p WHERE p.station = l.station AND p.kind = 'music'
                                       AND p.started_at > NOW() - INTERVAL '1 hour'
                                       AND COALESCE(p.directory, '') || p.file = l.key)
                     AND lower(COALESCE(l.artist, '')) NOT IN (
                         SELECT lower(artist) FROM (SELECT artist FROM play_log WHERE station = %s AND kind = 'music'
                                                    ORDER BY started_at DESC LIMIT %s) a WHERE artist IS NOT NULL)
                   ORDER BY md5(l.key || %s) LIMIT %s""",
                (station, station, int(_setting(cur, 'artist_separation')), str(on_air['id']), FIT_SAMPLE))
    cands = [(r['key'], air(r['duration_s'], r['trail_s'])) for r in cur.fetchall()]
    if not cands or gap > 2 * max(length for _, length in cands):
        return None                    # too far off: ordinary rotation for now

    def late(i):
        length = cands[i][1]
        if length >= gap:
            return length - gap
        rest = gap - length
        return min((other - rest for j, (_, other) in enumerate(cands) if j != i and other >= rest), default=math.inf)
    best = min(range(len(cands)), key=late)
    return cands[best][0] if late(best) <= FIT_MAX_LATE_S else None


def plan(cur, station):
    """({'due': [keys, longest waiting first], 'mix': [keys], 'fill': key of the
    song to play next so the next ad slot is met, if any}, rev) for the Core."""
    rows, hour_start = schedule(cur, station)
    rows = rows.values()
    mix = [r['key'] for r in rows if r['mode'] == 'mix']
    due = sorted((r['slots'][r['states'].index('due')], r['key']) for r in rows if 'due' in r.get('states', ()))
    upcoming = [m for r in rows for m, st in zip(r.get('slots', ()), r.get('states', ())) if st == 'next']
    upcoming += [r['slots'][0] + 60 for r in rows if r.get('slots')]           # the next hour's first ones
    out = {'due': [k for _, k in due], 'mix': mix}
    song = upcoming and fill(cur, station, hour_start + timedelta(minutes=min(upcoming)))
    if song:
        out['fill'] = song
    return out, hashlib.sha1(json.dumps(out).encode()).hexdigest()[:12]


def next_ad(cur, station):
    """The dashboard's next scheduled ad: {'owed': n, 'key', 'title', 'at' (ISO) | None}."""
    rows, hour_start = schedule(cur, station)
    owed_now = sum(1 for r in rows.values() if 'due' in r.get('states', ()))
    best = None
    for r in rows.values():
        for m, st in zip(r.get('slots', ()), r.get('states', ())):
            if st in ('due', 'next') and (best is None or m < best[0]):
                best = (m, r)
    if not best:
        return {'owed': owed_now, 'at': None}
    m, r = best
    return {'owed': owed_now, 'key': r['key'], 'title': ' - '.join(x for x in (r['artist'], r['title']) if x) or r['file'],
            'at': (hour_start + timedelta(minutes=m)).isoformat()}


def save_rule(conn, station, payload, user):
    """An operator's schedule for one ad. Returns a log line."""
    key = text(payload.get('key'), 1000)
    mode = payload.get('mode')
    if mode not in MODES:
        raise BadRequest('mode must be mix, hourly or off')
    per_hour = None
    if mode == 'hourly':
        per_hour = payload.get('per_hour')
        if isinstance(per_hour, str) and per_hour.strip().isdigit():
            per_hour = int(per_hour)
        if not isinstance(per_hour, int) or isinstance(per_hour, bool) or not 1 <= per_hour <= PER_HOUR_MAX:
            raise BadRequest(f'plays per hour must be a whole number from 1 to {PER_HOUR_MAX}')
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM library WHERE station = %s AND key = %s AND kind = 'ad' AND present",
                    (station, key or ''))
        if not cur.fetchone():
            raise BadRequest('that ad is not in the library')
        cur.execute("""INSERT INTO ad_rules (station, key, mode, per_hour, updated_at, updated_by)
                       VALUES (%s, %s, %s, %s, NOW(), %s)
                       ON CONFLICT (station, key) DO UPDATE SET mode = EXCLUDED.mode, per_hour = EXCLUDED.per_hour,
                           updated_at = NOW(), updated_by = EXCLUDED.updated_by""",
                    (station, key, mode, per_hour, user.get('username')))
    conn.commit()
    rule = f'{per_hour}/hour' if mode == 'hourly' else mode
    return 20, f'AD RULE station={station} key="{key}" {rule} by={user.get("username")}'
