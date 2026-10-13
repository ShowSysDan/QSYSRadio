"""Ad scheduling. Each ad is mixed into ad breaks (the default), played a set
number of times per clock hour, or off. The server works out which ads are
owed and sends the Core a plan on its sync; the Core only plays it.

An hourly ad with N plays an hour has N slots; all hourly ads' slots are
placed together, evenly over the hour (spread), so no two share a minute:
four ads at 4 an hour fall due every 3.75 min in turn (:02, :06, :09, :13,
:17 ...), and ads with different counts interleave the same way. A slot is owed from its time for its window (10 min, or half the gap
to the ad's next slot if shorter); the Core starts a break at the next song
change while one is owed. A slot that cannot play in its window (station
stopped, a long song) is skipped, not made up later in the hour.

The server also names the next song (next_song): normally a random one of
the least recently played, so every song gets airtime; near an ad slot the
one whose length makes the song change fall just after the slot opens.
"""
import hashlib
import json
from datetime import timedelta

from . import core_settings
from .db import text
from .events import BadRequest

MODES = ('mix', 'hourly', 'off')
POOL = 40                  # at most this many least recently played songs to pick the next one from
PER_HOUR_MAX = 30


WINDOW_MAX_MIN = 10
SLOT_GAP_MIN = 4           # minutes kept between two ads' slots, when the hour has room


def window_minutes(per_hour):
    """How long after its time a slot may still play: at most 10 min, and
    never more than half the gap to the ad's next slot."""
    return min(WINDOW_MAX_MIN, 30 / per_hour)


def slot_minutes(per_hour, phase):
    return [(k + phase) / per_hour * 60 for k in range(per_hour)]


def spread(counts):
    """Every hourly ad's slots, placed together. Each ad wants its k-th slot at
    (k + phase) / N of the hour (phases staggered by position); slots keep those
    times, except that neighbours are pushed apart to at least SLOT_GAP_MIN (less
    when the hour is too full for it), so two ads never share a break."""
    wants = sorted(((k + (i + 0.5) / len(counts)) / n * 60, i, k) for i, n in enumerate(counts) for k in range(n))
    if not wants:
        return [[] for _ in counts]
    gap = min(SLOT_GAP_MIN, 60 / len(wants))
    at = [t for t, _, _ in wants]
    for j in range(1, len(at)):                          # forward: at least `gap` after the previous one
        at[j] = max(at[j], at[j - 1] + gap)
    at[-1] = min(at[-1], 60 - gap / 2)
    for j in range(len(at) - 2, -1, -1):                 # back: all inside the hour
        at[j] = min(at[j], at[j + 1] - gap)
    out = [[0.0] * n for n in counts]
    for m, (_, i, k) in zip(at, wants):
        out[i][k] = m
    return [sorted(x) for x in out]


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
    for r, slots in zip(hourly, spread([r['per_hour'] for r in hourly])):
        r['slots'] = slots
        r['states'] = slot_states(slots, window_minutes(r['per_hour']), [float(p) for p in r['plays']], float(r['now_m']))
    return {r['key']: r for r in rows}, rows[0]['hour_start']


def _setting(cur, name):
    return core_settings.load(cur).get(name, core_settings.BY_NAME[name][7])


def next_song(cur, station, slot=None):
    """The song the Core should play after the one on air: (key, why) or (None, None).

    Rotation: a random one of the least recently played quarter of the
    eligible songs (never played first; at most POOL), keeping artist
    separation, so every song airs within about 1¼ passes of the library,
    rescans and Core restarts notwithstanding. 'ad': picked by length when
    the rotation pick would start the next per-hour ad too late (choose()).
    Stable while one song plays (seeded by its play). None when the operator queue has songs,
    automation is off or nothing qualifies. slot: (datetime the next per-hour
    slot opens, its window in seconds) or None. See choose()."""
    cur.execute("""SELECT s.state, p.id, p.started_at, p.duration_s, l.trail_s, l.lead_in_s, l.key
                   FROM stations s
                   LEFT JOIN LATERAL (SELECT * FROM play_log WHERE station = s.station AND kind = 'music' AND ended_at IS NULL
                                      ORDER BY started_at DESC LIMIT 1) p ON TRUE
                   LEFT JOIN library l ON l.station = p.station AND l.key = COALESCE(p.directory, '') || p.file
                   WHERE s.station = %s""", (station,))
    on_air = cur.fetchone()
    state = (on_air and on_air['state']) or {}
    if not on_air or state.get('queue') or state.get('automation') is not True:
        return None, None
    cur.execute("""SELECT key, duration_s, trail_s, lead_in_s, count(*) OVER () AS n FROM library
                   WHERE station = %s AND present AND NOT unsupported AND kind = 'music' AND key IS DISTINCT FROM %s
                     AND lower(COALESCE(artist, '')) NOT IN (
                         SELECT lower(artist) FROM (SELECT artist FROM play_log WHERE station = %s AND kind = 'music'
                                                    ORDER BY started_at DESC LIMIT %s) a WHERE artist IS NOT NULL)
                   ORDER BY last_played NULLS FIRST, key LIMIT %s""",
                (station, on_air['key'], station, int(_setting(cur, 'artist_separation')), POOL))
    oldest = cur.fetchall()
    if not oldest:
        return None, None
    seed, n = str(on_air['id']), oldest[0]['n']

    def shuffled(share):               # the least recently played `share` of the eligible songs, in a stable random order
        return sorted(oldest[:max(1, min(POOL, int(n * share)))],
                      key=lambda r: hashlib.md5((r['key'] + seed).encode()).hexdigest())
    quarter, half = shuffled(0.25), shuffled(0.5)
    if not (slot and on_air['duration_s']):
        return quarter[0]['key'], 'rotation'
    air = airtime({n: float(_setting(cur, n)) for n in ('crossfade_s', 'lead_skip_max_s', 'lead_skip_keep_s')})
    gap = (slot[0] - on_air['started_at']).total_seconds() - air(on_air)     # from the next song's start to the slot
    timed = lambda pool: [(r['key'], air(r)) for r in pool if r['duration_s']]
    return choose(quarter[0]['key'], timed(quarter), timed(half), gap, float(_setting(cur, 'ad_fit_tolerance_s')), slot[1])


def airtime(settings):
    """A song row's seconds from its start to the next item's start: quiet start
    skipped, crossfade early by the learned tail. settings: name -> value."""
    def air(r):
        skip = min(settings['lead_skip_max_s'], max(0, (r['lead_in_s'] or 0) - settings['lead_skip_keep_s']))
        return r['duration_s'] - settings['crossfade_s'] - (r['trail_s'] or 0) - skip
    return air


def lateness(cands, i, gap):
    """Seconds after the slot opens (gap s from now) that the break starts if
    cands[i] plays next, followed if needed by the best other candidate (0 when
    two songs cannot reach the slot: nothing to decide yet)."""
    length = cands[i][1]
    if length >= gap:
        return length - gap
    rest = gap - length
    # no follow-up reaches the slot: a third song will, and is picked (and timed) when its turn comes
    return min((other - rest for j, (_, other) in enumerate(cands) if j != i and other >= rest), default=0)


def choose(rotation, quarter, half, gap, tolerance, window):
    """The next song with an ad slot gap s away: (key, 'rotation' | 'ad').

    Fair rotation first: `rotation` (from the least recently played quarter)
    unless it would start the break more than `tolerance` s late. Then the
    quarter's best-timed song, if the slot still plays (within its `window`);
    the oldest half only when that alone saves the slot. quarter / half:
    [(key, airtime s)], songs of unknown length left out."""
    rot = next((i for i, (k, _) in enumerate(quarter) if k == rotation), None)
    longest = max((length for _, length in half or quarter), default=0)
    if gap is None or gap <= 0 or rot is None or gap > 2 * longest:
        return rotation, 'rotation'    # the break comes anyway, too far off to plan, or the pick's length unknown
    if lateness(quarter, rot, gap) <= tolerance:
        return rotation, 'rotation'
    for pool in (quarter, half):
        best = min(range(len(pool)), key=lambda i: lateness(pool, i, gap))
        if lateness(pool, best, gap) <= window:
            return pool[best][0], ('rotation' if pool[best][0] == rotation else 'ad')
    return rotation, 'rotation'


def plan(cur, station):
    """({'due': [keys, longest waiting first], 'mix': [keys], 'next': key of the
    song to play next, 'next_why': 'rotation' | 'ad'}, rev) for the Core."""
    rows, hour_start = schedule(cur, station)
    rows = rows.values()
    mix = [r['key'] for r in rows if r['mode'] == 'mix']
    due = sorted((r['slots'][r['states'].index('due')], r['key']) for r in rows if 'due' in r.get('states', ()))
    upcoming = [(m, r['per_hour']) for r in rows for m, st in zip(r.get('slots', ()), r.get('states', ())) if st == 'next']
    upcoming += [(r['slots'][0] + 60, r['per_hour']) for r in rows if r.get('slots')]   # the next hour's first ones
    out = {'due': [k for _, k in due], 'mix': mix}
    m, per_hour = min(upcoming) if upcoming else (None, None)
    song, why = next_song(cur, station, (hour_start + timedelta(minutes=m), window_minutes(per_hour) * 60) if m is not None else None)
    if song:
        out['next'], out['next_why'] = song, why
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
