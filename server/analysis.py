"""File analysis: each Core measures files on its own analysis players (radio.lua CFG.ANALYZERS: an Audio
Player and a meter channel routed to no output; nothing on air is touched), for every station on it, at any
hour: the quiet start, the average level of the middle, the quiet end. The job is the Core's (cores): which
files are owed, the runs, the list it is sent. A 2.x sync reply carries the Core's next files (`analyze`:
[{station, key}], at most BATCH, in the order to measure; `analyze_rev`) when its list differs from the
Core's (a Core sends `analyze_rev` only when it has an analysis player); the Core reports each result as an
`analysis` event of the station the job named (events.py). The result is the file's: it counts for every
station that has it, on any Core (media.py). A Core older than 2.1.0 (per-station analyze_rev, or the 1.x
body) gets no analysis work: control._station_sync clears its list. A 1.x station's run waits on its own row
until the station syncs as part of a 2.x Core, which takes it over (adopt).

Owed: the files present in the libraries of the Core's stations, not unsupported, each once (one station that
has it), asked for on their own (Analyze on a row: library.analyze_wanted; first) or owed by the run: never
analyzed or analyzed before the run started (analyze_from: '-infinity' = only files never analyzed, NOW() =
every file again) and not failed in it (a failure waits for the next run: never a tight loop). Short imaging
and ads before music. Runs: Analyze new now / Re-analyze all on a library page, the daily run (setting
analyze_at 'HH:00'), and in 'right away' mode (the default), whenever no other run is on, a run of the files
never analyzed that tries a failed one again RETRY_H hours after it failed. An analyzed file is not analyzed
again unless asked (Analyze again, Re-analyze all, Forget measured) or replaced (new size). Analyzed figures
are final: plays no longer learn over them (an operator's own figures still win).

The list is cached on the Core's row (owed, owed_rev, owed_at), so a sync costs one row read: made again
when a result comes in, when something it is made from changed (stale(): a library upload, a run started or
stopped, a file asked for, the setting, Forget measured) or LIST_MAX_AGE_S old (the clock: a retry's
cool-down, a station gone from the Core). Making it reads at most BATCH rows per station while many files
are owed (library_analyze_order), the whole of its stations' libraries when few are.
"""
import hashlib
import json

from . import core_settings, cores, db, media
from .db import iso
from .events import BadRequest

BATCH = 20
SCOPES = ('new', 'all', 'stop')
RETRY_H = 24                # 'right away': a file whose analysis failed is tried again this long after
LIST_MAX_AGE_S = 60         # the Core's cached list is made again at least this often
GONE_MIN = 10               # a station its Core has not synced for this long is no longer on it (left out)
RESULTS = ('analysis', 'file_unsupported')     # event kinds after which the Core's list is made again


def run_sql(lib='library_figures', since='%s', run_at='%s'):
    """SQL: does the run (since NULL = none) owe this row's file (lib: library_figures or media_files)? Analyzed
    (on any station) before the run started, or never, and not failed in it."""
    return (f'({since}::timestamptz IS NOT NULL AND ({lib}.analyzed_at IS NULL OR {lib}.analyzed_at < {since}) '
            f'AND ({lib}.analyze_failed_at IS NULL OR {lib}.analyze_failed_at < {run_at}))')


def owed_sql(lib='library_figures', since='%s', run_at='%s'):
    """SQL: is this library_figures row owed? Asked for on its own, or by the run."""
    return f'{lib}.present AND NOT {lib}.unsupported AND ({lib}.analyze_wanted OR {run_sql(lib, since, run_at)})'


def _job(cur, core, lock=False):
    """The Core's job, its cached list, its analysis players and whether it syncs now (online: its players'
    report is current) (None: no such Core)."""
    cur.execute(f"""SELECT analyze_from, analyze_run_at, analyze_auto_at, owed, owed_rev, analyzers,
                           last_seen > NOW() - INTERVAL '{cores.ONLINE_S} seconds' AS online,
                           owed_at > NOW() - INTERVAL '{LIST_MAX_AGE_S} seconds' AS cached,
                           NOW() - INTERVAL '{RETRY_H} hours' AS retry_at
                    FROM cores WHERE core = %s""" + (' FOR UPDATE' if lock else ''), (core,))
    return cur.fetchone()


def _run(since, run_at, retry_at, at):
    """(since, run_at) of the run in force: the one started (since: cores.analyze_from), else in 'right away'
    mode the files never analyzed (a failed one again after RETRY_H), else none (None, None)."""
    if since is not None:
        return since, run_at
    if at == core_settings.RIGHT_AWAY and retry_at is not None:
        return '-infinity', retry_at
    return None, None


def on_core(s='stations'):
    """SQL: is the stations row s on its Core now (synced in the last GONE_MIN minutes, not archived)? A station
    taken out of its Core's CFG.STATIONS keeps its core, but none of its files is listed, shown queued or counted
    as going."""
    return f"{s}.last_seen > NOW() - INTERVAL '{GONE_MIN} minutes' AND {s}.archived_at IS NULL"


def _core_of(cur, station):
    """(core, on): the station's Core (None: none, or radio.lua before 2.0.0) and whether it is on it now (on_core)."""
    cur.execute(f'SELECT core, {on_core()} AS on_core FROM stations WHERE station = %s', (station,))
    row = cur.fetchone()
    return (row['core'], row['on_core']) if row else (None, False)


def _setting(cur):
    """analyze_at: the whole Core's, set for All stations only."""
    return core_settings.effective(cur, None)['analyze_at']


def run_of(cur, station):
    """(can, since, run_at) for owed_sql on the station's rows: can its Core measure files now (it reports an
    analysis player and syncs; not a Core before 2.1.0, nor one without CFG.ANALYZERS, nor one offline, nor a
    station no longer on it: then no row of it is shown queued, not even a file asked for), and the run in force
    on it ((None, None): none)."""
    core, on = _core_of(cur, station)
    job = core and on and _job(cur, core)
    if not (job and job['analyzers'] and job['online']):
        return False, None, None
    return (True,) + _run(job['analyze_from'], job['analyze_run_at'], job['retry_at'], _setting(cur))


def adopt(cur, station, core):
    """A station's job from before 2.1.0 (a 1.x Core's run, kept on its own row by migration 0017: such as
    1.3.0's level re-analysis) goes on as its Core's, merged as the migration merges a 2.0.0 Core's stations
    (the widest run), at its first sync as part of a 2.x Core. Inside that sync's transaction, under the station's
    lock. No row for the Core yet: it stays for the next sync. Returns log lines."""
    cur.execute("""UPDATE cores c SET analyze_from = GREATEST(c.analyze_from, s.analyze_from),
                          analyze_run_at = GREATEST(c.analyze_run_at, s.analyze_run_at),
                          analyze_auto_at = GREATEST(c.analyze_auto_at, s.analyze_auto_at), owed_at = NULL
                   FROM stations s WHERE c.core = %s AND s.station = %s RETURNING s.analyze_from""", (core, station))
    row = cur.fetchone()
    if row is None:
        return []
    cur.execute('UPDATE stations SET analyze_from = NULL, analyze_run_at = NULL, analyze_auto_at = NULL WHERE station = %s',
                (station,))
    return [(20, f"ANALYSIS station={station} core={core} its run goes on as the Core's")] if row[0] is not None else []


def owed(cur, core, since, run_at, limit=BATCH):
    """The Core's next files to analyze, [{station, key}]: those asked for on their own, then the run's (since
    None: no run); each file once (its first row in this order), short imaging and ads before music. The run's
    are read per station in index order (library_analyze_order), at most `limit` each, so a big backlog costs
    little."""
    stations = f"SELECT station FROM stations WHERE core = %(core)s AND {on_core()}"
    parts = [f"""SELECT l.station, l.key, l.kind = 'music' AS music, l.file_id, TRUE AS wanted FROM library l
                 WHERE l.station IN ({stations}) AND l.analyze_wanted AND l.present AND {media.supported('l')}"""]
    if since is not None:
        parts.append(f"""SELECT y.* FROM ({stations}) s CROSS JOIN LATERAL (
                             SELECT l.station, l.key, l.kind = 'music' AS music, l.file_id, FALSE AS wanted
                             FROM library l JOIN media_files m ON m.id = l.file_id
                             WHERE l.station = s.station AND l.present AND NOT m.unsupported
                               AND {run_sql('m', '%(since)s', '%(run_at)s')}
                             ORDER BY l.kind = 'music', l.key LIMIT %(limit)s) y""")
    cur.execute(f"""SELECT station, key FROM (SELECT DISTINCT ON (file_id) * FROM ({' UNION ALL '.join(parts)}) u
                                              ORDER BY file_id, wanted DESC, music, key, station) x
                    ORDER BY NOT wanted, music, key, station LIMIT %(limit)s""",
                dict(core=core, since=since, run_at=run_at, limit=limit))
    return [{'station': r['station'], 'key': r['key']} for r in cur.fetchall()]


def stale(cur, station=None):
    """Something the list of files to analyze is made from changed (a library, figures forgotten, a file asked
    for, the setting): the list of the station's Core (station None: every Core's) is made again on its next
    sync. Inside the transaction that makes the change."""
    cur.execute('UPDATE cores SET owed_at = NULL WHERE owed_at IS NOT NULL'
                + (' AND core = (SELECT core FROM stations WHERE station = %s)' if station else ''), (station,) if station else None)


def sync(cur, core, core_rev, fresh=False):
    """For core_sync, after the stations' parts, in its own transaction: start the daily run when due, end a
    finished run, and the Core's next files when its list (core_rev) differs. core_rev None: no analysis
    player on this Core. fresh: a result came in (the list is made again). Returns (reply fields, log lines)."""
    if core_rev is None:
        return {}, []
    job = _job(cur, core, lock=True)
    if job is None:
        return {}, []
    at, lines = _setting(cur), []
    since, run_at, cached = job['analyze_from'], job['analyze_run_at'], job['cached'] and not fresh
    if at not in (core_settings.RIGHT_AWAY, 'off'):
        cur.execute("""SELECT date_trunc('day', NOW()) + %(at)s::interval <= NOW()
                              AND (%(auto)s::timestamptz IS NULL OR %(auto)s < date_trunc('day', NOW()) + %(at)s::interval)""",
                    dict(at=at, auto=job['analyze_auto_at']))
        if cur.fetchone()[0]:
            cur.execute("""UPDATE cores SET analyze_auto_at = NOW(), analyze_from = COALESCE(analyze_from, '-infinity'),
                                  analyze_run_at = NOW() WHERE core = %s RETURNING analyze_from, analyze_run_at""", (core,))
            (since, run_at), cached = cur.fetchone(), False
            lines.append((20, f'ANALYSIS core={core} daily run at {at}: files not analyzed yet'))
    if cached:
        keys, rev = job['owed'] or [], job['owed_rev'] or ''
    else:
        keys = owed(cur, core, *_run(since, run_at, job['retry_at'], at))
        done = not keys and since is not None       # the run is done ('right away' goes on with the files never analyzed)
        if done:
            keys = owed(cur, core, *_run(None, None, job['retry_at'], at))
            lines.append((20, f'ANALYSIS core={core} done'))
        rev = hashlib.md5(json.dumps(keys, sort_keys=True).encode()).hexdigest()[:12] if keys else ''
        cur.execute("""UPDATE cores SET owed = %s, owed_rev = %s, owed_at = NOW(),
                              analyze_from = CASE WHEN %s THEN NULL ELSE analyze_from END WHERE core = %s""",
                    (json.dumps(keys), rev, done, core))
    return ({'analyze': keys, 'analyze_rev': rev} if rev != core_rev else {}), lines


def start(conn, station, scope, key, user):
    """On the station's Core: scope new (files not analyzed yet; a failed one too), all (every file again),
    stop (the run and the files asked for); or key = that file of the station again. Returns a log line."""
    if not key and scope not in SCOPES:
        raise BadRequest('scope must be new, all or stop (or give a file key)')
    with conn.cursor() as cur:
        if key:                        # just this file; its figures stay in use until the new ones come
            db.lock_station(cur, station)
            cur.execute(f"""UPDATE library SET analyze_wanted = TRUE
                            WHERE station = %s AND key = %s AND present AND {media.supported()}""", (station, key))
            if not cur.rowcount:
                raise BadRequest('that file is not in the library')
            stale(cur, station)
            core = None
        else:
            cur.execute('SELECT core FROM stations WHERE station = %s', (station,))
            core = (cur.fetchone() or [None])[0]
            if not core:
                raise BadRequest("this station's Core does not analyze files: it needs radio.lua 2.1.0 or later "
                                 'with an analysis player (CFG.ANALYZERS)')
            db.lock_station(cur, core=core)          # every station of the Core: their files' requests
            frm = {'stop': None, 'all': 'NOW()'}.get(scope, "COALESCE(analyze_from, '-infinity')")
            run = ', analyze_run_at = NOW()' if frm else ''      # a new run: files that failed before are tried again
            cur.execute(f'UPDATE cores SET analyze_from = {frm or "NULL"}{run}, owed_at = NULL WHERE core = %s', (core,))
            if scope == 'stop':
                cur.execute("""UPDATE library SET analyze_wanted = FALSE
                               WHERE analyze_wanted AND station IN (SELECT station FROM stations WHERE core = %s)""", (core,))
    conn.commit()
    what = f'file="{key}"' if key else {'new': 'files not analyzed yet', 'all': 'every file', 'stop': 'stopped'}[scope]
    return 20, f'ANALYSIS station={station}{" core=" + core if core else ""} {what} by={user.get("username")}'


def view(cur, station):
    """For the panel: the station's files {active (some owed and the Core, online, can measure them), left, never,
    failed, last (the latest result, analyzed or failed: a library page reloads when one comes in), gone (no
    longer on its Core: on_core; then can is False)} and its Core's {run (a run started, or files asked for, which
    Stop ends), can, online (it syncs now; else its players' report is its last, not what they do), players (its
    analysis players as it last reported them, [{name, state, station?, key?}]; None = not reported: no Core, or
    radio.lua before 2.1.0), at, auto_at}."""
    core, on = _core_of(cur, station)
    job = core and _job(cur, core)
    at = _setting(cur)
    since, run_at = _run(job['analyze_from'], job['analyze_run_at'], job['retry_at'], at) if job else (None, None)
    cur.execute(f"""SELECT count(*) FILTER (WHERE {owed_sql('f')}) AS left,
                           count(*) FILTER (WHERE analyzed_at IS NULL) AS never,
                           count(*) FILTER (WHERE analyzed_at IS NULL AND analyze_failed_at IS NOT NULL) AS failed,
                           count(*) FILTER (WHERE analyze_wanted) AS wanted,
                           max(GREATEST(analyzed_at, analyze_failed_at)) AS last
                    FROM library_figures f WHERE station = %s AND present AND NOT unsupported""",
                (since, since, run_at, station))
    counts = cur.fetchone()
    players = job['analyzers'] if job else None
    can, online = bool(players) and on, bool(job and job['online'])
    return {'active': can and online and counts['left'] > 0, 'left': counts['left'], 'never': counts['never'],
            'gone': bool(core) and not on,
            'failed': counts['failed'], 'run': bool(job and job['analyze_from'] is not None) or counts['wanted'] > 0,
            'can': can, 'online': online, 'players': players,
            'at': at, 'auto_at': iso(job and job['analyze_auto_at']), 'last': iso(counts['last'])}
