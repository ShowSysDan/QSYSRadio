"""Content fingerprints: which file a station's file is, so its figures (media.py) follow it when it is renamed or
moved, and two different files of the same name and size are told apart. radio.lua 2.4.0 makes one per file (the
sha1 of its size in bytes as decimal text, a newline, its first, middle and last 64 KiB; the whole file when no
longer than 192 KiB) when its Core can (Crypto.Digest and io seek: a Core that cannot sends no fingerprint_rev, and its
files stay known by name + size).

Owed: the present files of known size of the Core's stations (on it now: analysis.on_core; not faulted: a station
whose build failed has no players' root, and its files would hold up the others' in the list) not fingerprinted at
their size (index library_fingerprint_owed), except one whose last report failed (an error, or a size other than
the library's), which waits for the next scan or RETRY_H hours. A 2.x sync reply carries the Core's next files
(`fingerprint`: [{station, key}], at most BATCH; `fingerprint_rev`) when its list differs from the Core's; the
Core reports each as a `fingerprint` event of the station the list named (events.py, media.fingerprinted).

The list is cached on the Core's row (fp_owed, fp_rev, fp_at), so a sync costs one row read: made again when a
result comes in, after a library upload of one of its stations (stale()) or LIST_MAX_AGE_S old. Making it reads
at most BATCH index entries per station (plus those waiting after a failure).
"""
import json

from . import analysis, core_settings

BATCH = 20
RETRY_H = 24                # a file whose fingerprint failed is tried again this long after (or after the next scan)
LIST_MAX_AGE_S = 60
RESULTS = ('fingerprint',)  # event kinds after which the Core's list is made again
# a library row (l) owed one: index library_fingerprint_owed's predicate (keep the two the same)
OWED = 'l.present AND l.size IS NOT NULL AND (l.fingerprint IS NULL OR l.fingerprint_size IS DISTINCT FROM l.size)'


def owed(cur, core, limit=BATCH):
    """The Core's next files to fingerprint, [{station, key}], station by station in key order."""
    cur.execute(f"""SELECT y.station, y.key
                    FROM (SELECT station FROM stations
                          WHERE core = %(core)s AND {analysis.on_core()} AND state->>'fault' IS NULL) s
                    CROSS JOIN LATERAL (
                        SELECT l.station, l.key FROM library l
                        WHERE l.station = s.station AND {OWED}
                          AND (l.fingerprint_at IS NULL OR l.fingerprint_at < l.last_seen
                               OR l.fingerprint_at < NOW() - INTERVAL '{RETRY_H} hours')
                        ORDER BY l.key LIMIT %(limit)s) y
                    ORDER BY y.station, y.key LIMIT %(limit)s""", dict(core=core, limit=limit))
    return [{'station': r['station'], 'key': r['key']} for r in cur.fetchall()]


def stale(cur, station=None):
    """A library upload of the station (station None: fingerprints forgotten on any station): its Core's list (every
    Core's) is made again on its next sync (inside that transaction)."""
    cur.execute('UPDATE cores SET fp_at = NULL WHERE fp_at IS NOT NULL'
                + (' AND core = (SELECT core FROM stations WHERE station = %s)' if station else ''), (station,) if station else None)


def sync(cur, core, core_rev, fresh=False):
    """For core_sync, after the stations' parts, in its own transaction (only for a Core that sent fingerprint_rev):
    the Core's next files when its list (core_rev) differs. fresh: a result came in. Returns (reply fields, log lines)."""
    cur.execute(f"""SELECT fp_owed, fp_rev, fp_at > NOW() - INTERVAL '{LIST_MAX_AGE_S} seconds' AS cached
                    FROM cores WHERE core = %s""", (core,))
    job = cur.fetchone()
    if job is None:
        return {}, []
    if job['cached'] and not fresh:
        keys, rev = job['fp_owed'] or [], job['fp_rev'] or ''
    else:
        keys = owed(cur, core)
        rev = core_settings.rev(keys) if keys else ''
        cur.execute('UPDATE cores SET fp_owed = %s, fp_rev = %s, fp_at = NOW() WHERE core = %s', (json.dumps(keys), rev, core))
    return ({'fingerprint': keys, 'fingerprint_rev': rev} if rev != core_rev else {}), []
