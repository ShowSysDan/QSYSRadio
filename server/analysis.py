"""File analysis: the Core measures each file's quiet start and end on the
deck that is off air (its RadioOut input muted; the meter is before the
mixer): the first seconds, then the last 15. The server keeps the job: which
files are owed, the daily run (setting analyze_at), progress. Every sync
reply carries the next few owed files when that list changes (`analyze`,
`analyze_rev`); the Core reports each result as an `analysis` event, which
drops the file from the list. Analyzed figures are final: plays no longer
learn over them (an operator's own figures still win). An analyzed file is
not analyzed again unless asked (Analyze again, Re-analyze all, Forget
measured) or replaced (new size). A failed one is skipped for the rest of
that run (analyze_run_at) and tried again by the next. One file asked for on
its own (analyze_wanted) is analyzed without starting a run; it keeps its
figures until the new ones come. The job is per station, the result is the
file's: a file analyzed (or failed) on any station counts for every station
that has it (media.py).
"""
import hashlib
import json

from . import core_settings, media
from .db import iso
from .events import BadRequest

BATCH = 20
SCOPES = ('new', 'all', 'stop')


def run_sql(lib='library_figures', since='%s', run_at='%s'):
    """SQL: does the run (since NULL = none) owe this library_figures row's file? Analyzed (on any station)
    before the run started, or never, and not failed in it."""
    return (f'({since}::timestamptz IS NOT NULL AND ({lib}.analyzed_at IS NULL OR {lib}.analyzed_at < {since}) '
            f'AND ({lib}.analyze_failed_at IS NULL OR {lib}.analyze_failed_at < {run_at}))')


def owed_sql(lib='library_figures', since='%s', run_at='%s'):
    """SQL: is this library_figures row owed? Asked for on its own, or by the run."""
    return f'{lib}.present AND NOT {lib}.unsupported AND ({lib}.analyze_wanted OR {run_sql(lib, since, run_at)})'


def _job(cur, station):
    cur.execute('SELECT analyze_from, analyze_auto_at, analyze_run_at FROM stations WHERE station = %s', (station,))
    return cur.fetchone()


def owed(cur, station, since, run_at, limit=BATCH):
    """The next files to analyze: those asked for on their own, then the run's; short imaging and ads first
    (they are quick). Two queries, so each reads only what it needs: the files asked for through their index,
    the run's in index order (library_analyze_order: stops after the first few while many are left) or, when
    few are left, from the files not analyzed (media_files_analyzed)."""
    some = ("SELECT key FROM library_figures WHERE station = %s AND present AND NOT unsupported AND {} "
            "ORDER BY kind = 'music', key LIMIT %s")
    cur.execute(some.format('analyze_wanted'), (station, limit))
    keys = [r['key'] for r in cur.fetchall()]
    if since is not None and len(keys) < limit:
        cur.execute(some.format('NOT analyze_wanted AND ' + run_sql()), (station, since, since, run_at, limit - len(keys)))
        keys += [r['key'] for r in cur.fetchall()]
    return keys


def sync(cur, station, core_rev):
    """For core_sync, inside its transaction: start the daily run when due, end a
    finished job, and the owed files when the Core's list differs. core_rev None =
    this Core cannot analyze (no mixer). Returns (reply fields, log lines)."""
    if core_rev is None:
        return {}, []
    lines, job = [], _job(cur, station)
    since, run_at = job['analyze_from'], job['analyze_run_at']
    at = core_settings.effective(cur, station)['analyze_at']
    if at != 'off':
        cur.execute("""SELECT date_trunc('day', NOW()) + %s::interval <= NOW()
                              AND (%s::timestamptz IS NULL OR %s < date_trunc('day', NOW()) + %s::interval) AS due""",
                    (at, job['analyze_auto_at'], job['analyze_auto_at'], at))
        if cur.fetchone()['due']:
            cur.execute("""UPDATE stations SET analyze_auto_at = NOW(), analyze_from = COALESCE(analyze_from, '-infinity'),
                                  analyze_run_at = NOW()
                           WHERE station = %s RETURNING analyze_from, analyze_run_at""", (station,))
            since, run_at = cur.fetchone()
            lines.append((20, f'ANALYSIS station={station} daily run at {at}: files not analyzed yet'))
    keys = []
    if since is None:                  # no run: only files asked for on their own
        cur.execute('SELECT EXISTS (SELECT 1 FROM library WHERE station = %s AND analyze_wanted)', (station,))
        if cur.fetchone()[0]:
            keys = owed(cur, station, None, None)
    else:
        keys = owed(cur, station, since, run_at)
        if not keys:
            cur.execute('UPDATE stations SET analyze_from = NULL WHERE station = %s', (station,))
            lines.append((20, f'ANALYSIS station={station} done'))
    rev = hashlib.md5(json.dumps(keys).encode()).hexdigest()[:12] if keys else ''
    return ({'analyze': keys, 'analyze_rev': rev} if rev != core_rev else {}), lines


def start(conn, station, scope, key, user):
    """scope: new (files never analyzed), all (everything again), stop; or key = that file again.
    Returns a log line."""
    if not key and scope not in SCOPES:
        raise BadRequest('scope must be new, all or stop (or give a file key)')
    with conn.cursor() as cur:
        if key:                        # just this file; its figures stay in use until the new ones come
            cur.execute(f"""UPDATE library SET analyze_wanted = TRUE
                            WHERE station = %s AND key = %s AND present AND {media.supported()}""", (station, key))
            if not cur.rowcount:
                raise BadRequest('that file is not in the library')
        else:
            frm = {'stop': None, 'all': 'NOW()'}.get(scope, "COALESCE(analyze_from, '-infinity')")
            run = ', analyze_run_at = NOW()' if frm else ''      # a new run: files that failed before are tried again
            cur.execute(f'UPDATE stations SET analyze_from = {frm or "NULL"}{run} WHERE station = %s', (station,))
            if scope == 'stop':
                cur.execute('UPDATE library SET analyze_wanted = FALSE WHERE station = %s AND analyze_wanted', (station,))
    conn.commit()
    what = f'file="{key}"' if key else {'new': 'files not analyzed yet', 'all': 'every file', 'stop': 'stopped'}[scope]
    return 20, f'ANALYSIS station={station} {what} by={user.get("username")}'


def view(cur, station, state):
    """For the panel: {active, left, never, failed, can, at, auto_at, last}; last = the latest result
    (analyzed or failed), so a library page reloads when one comes in."""
    job = _job(cur, station)
    since = job and job['analyze_from']
    cur.execute(f"""SELECT count(*) FILTER (WHERE {owed_sql('f')}) AS left,
                           count(*) FILTER (WHERE analyzed_at IS NULL) AS never,
                           count(*) FILTER (WHERE analyzed_at IS NULL AND analyze_failed_at IS NOT NULL) AS failed,
                           max(GREATEST(analyzed_at, analyze_failed_at)) AS last
                    FROM library_figures f WHERE station = %s AND present AND NOT unsupported""",
                (since, since, job and job['analyze_run_at'], station))
    counts = cur.fetchone()
    left = counts['left']
    can = (state.get('analyze') or {}).get('can') is True
    return {'active': since is not None or left > 0, 'left': left, 'never': counts['never'], 'failed': counts['failed'], 'can': can,
            'auto_at': iso(job and job['analyze_auto_at']), 'last': iso(counts['last']),
            'at': core_settings.effective(cur, station)['analyze_at']}
