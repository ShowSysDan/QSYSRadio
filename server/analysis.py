"""File analysis: the Core measures each file's quiet start and end on the
deck that is off air (its PlayerSum input muted; the meter is before the
mixer): the first seconds, then the last 15. The server keeps the job: which
files are owed, the daily run (setting analyze_at), progress. Every sync
reply carries the next few owed files when that list changes (`analyze`,
`analyze_rev`); the Core reports each result as an `analysis` event, which
drops the file from the list. Analyzed figures are final: plays no longer
learn over them (an operator's own figures still win).
"""
import hashlib
import json

from . import core_settings
from .db import iso
from .events import BadRequest

BATCH = 20
SCOPES = ('new', 'all', 'stop')
OWED = """station = %s AND present AND NOT unsupported AND (analyzed_at IS NULL OR analyzed_at < %s)"""


def _job(cur, station):
    cur.execute('SELECT analyze_from, analyze_auto_at FROM stations WHERE station = %s', (station,))
    return cur.fetchone()


def owed(cur, station, since, limit=BATCH):
    """The next files to analyze (short imaging and ads first, they are quick)."""
    cur.execute(f"SELECT key FROM library WHERE {OWED} ORDER BY kind = 'music', key LIMIT %s", (station, since, limit))
    return [r['key'] for r in cur.fetchall()]


def sync(cur, station, core_rev):
    """For core_sync, inside its transaction: start the daily run when due, end a
    finished job, and the owed files when the Core's list differs. core_rev None =
    this Core cannot analyze (no mixer). Returns (reply fields, log lines)."""
    if core_rev is None:
        return {}, []
    lines, job = [], _job(cur, station)
    since = job['analyze_from']
    at = core_settings.load(cur).get('analyze_at', core_settings.BY_NAME['analyze_at'][7])
    if at != 'off':
        cur.execute("""SELECT date_trunc('day', NOW()) + %s::interval <= NOW()
                              AND (%s::timestamptz IS NULL OR %s < date_trunc('day', NOW()) + %s::interval) AS due""",
                    (at, job['analyze_auto_at'], job['analyze_auto_at'], at))
        if cur.fetchone()['due']:
            cur.execute("""UPDATE stations SET analyze_auto_at = NOW(), analyze_from = COALESCE(analyze_from, '-infinity')
                           WHERE station = %s RETURNING analyze_from""", (station,))
            since = cur.fetchone()['analyze_from']
            lines.append((20, f'ANALYSIS station={station} daily run at {at}: files not analyzed yet'))
    keys = []
    if since is not None:
        keys = owed(cur, station, since)
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
        if key:
            cur.execute("""UPDATE library SET analyzed_at = NULL
                           WHERE station = %s AND key = %s AND present AND NOT unsupported""", (station, key))
            if not cur.rowcount:
                raise BadRequest('that file is not in the library')
        frm = {'stop': None, 'all': 'NOW()'}.get(scope if not key else 'new', "COALESCE(analyze_from, '-infinity')")
        cur.execute(f'UPDATE stations SET analyze_from = {frm or "NULL"} WHERE station = %s', (station,))
    conn.commit()
    what = f'file="{key}"' if key else {'new': 'files not analyzed yet', 'all': 'every file', 'stop': 'stopped'}[scope]
    return 20, f'ANALYSIS station={station} {what} by={user.get("username")}'


def view(cur, station, state):
    """For the panel: {active, left, can, at}."""
    job = _job(cur, station)
    since = job and job['analyze_from']
    left = 0
    if since is not None:
        cur.execute(f'SELECT count(*) FROM library WHERE {OWED}', (station, since))
        left = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM library WHERE {OWED}", (station, '-infinity'))
    never = cur.fetchone()[0]
    can = (state.get('analyze') or {}).get('can') is True
    return {'active': since is not None, 'left': left, 'never': never, 'can': can,
            'auto_at': iso(job and job['analyze_auto_at']),
            'at': core_settings.load(cur).get('analyze_at', core_settings.BY_NAME['analyze_at'][7])}
