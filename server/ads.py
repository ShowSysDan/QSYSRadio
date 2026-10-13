"""Ad scheduling. Each ad is mixed into ad breaks (the default), played a set
number of times per clock hour, or off. The server works out which ads are
owed and sends the Core a plan on its sync; the Core only plays it.

An hourly ad with N plays an hour is owed its k-th play at (k - 0.5) / N of
the hour (N = 4: at :07, :22, :37, :52), and goes into the first break after
that; the Core starts a break at the next song change when an ad is owed.
"""
import hashlib
import json
import math

from .db import text
from .events import BadRequest

MODES = ('mix', 'hourly', 'off')
PER_HOUR_MAX = 30


def owed(per_hour, frac):
    """Plays an hourly ad should have had by this fraction of the hour."""
    return math.floor(per_hour * frac + 0.5)


def plan(cur, station):
    """({'due': [keys, most behind first], 'mix': [keys]}, rev) for the Core."""
    cur.execute("""SELECT l.key, COALESCE(r.mode, 'mix') AS mode, r.per_hour, COALESCE(p.plays, 0) AS plays,
                          EXTRACT(EPOCH FROM NOW() - date_trunc('hour', NOW())) / 3600 AS frac
                   FROM library l
                   LEFT JOIN ad_rules r ON r.station = l.station AND r.key = l.key
                   LEFT JOIN (SELECT COALESCE(directory, '') AS directory, file, count(*) AS plays
                              FROM play_log WHERE station = %s AND kind = 'ad' AND started_at >= date_trunc('hour', NOW())
                              GROUP BY 1, 2) p ON p.directory = COALESCE(l.directory, '') AND p.file = l.file
                   WHERE l.station = %s AND l.present AND l.kind = 'ad' AND NOT l.unsupported
                   ORDER BY l.key""", (station, station))
    due, mix = [], []
    for r in cur.fetchall():
        if r['mode'] == 'mix':
            mix.append(r['key'])
        elif r['mode'] == 'hourly' and r['per_hour']:
            behind = owed(r['per_hour'], float(r['frac'])) - r['plays']
            if behind > 0:
                due.append((behind, r['per_hour'], r['key']))
    out = {'due': [k for _, _, k in sorted(due, key=lambda d: (-d[0], -d[1], d[2]))], 'mix': mix}
    return out, hashlib.sha1(json.dumps(out).encode()).hexdigest()[:12]


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
