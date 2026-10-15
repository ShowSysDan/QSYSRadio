"""Operator holds on ads / bumpers, and station IDs on a clock schedule.

Holds: "no ads until 05:00" (or until resumed). While ads are held the ad
plan names no ads (nothing owed, nothing mixed in); while bumpers are held
the plan tells the Core to skip its every-N-songs bumper. "Ad break now" /
"Bumper now" still play: an operator asked for them.

Station IDs: imaging files marked as IDs play at the first song change after
:00 (hourly), :00 and :30, or every quarter hour (setting ident_schedule),
first in that break and instead of the regular bumper. A random ID, never
the one that played last when there are several. An ID not played within
ident_window_min of its time is skipped. Bumper holds do not stop IDs.

A station with one player (radio.lua 2.2.0: a background-music zone) plays
songs only: no breaks, so holds, IDs, ad rules and Bumper / Ad break now are
refused for it (one_player).
"""
import hashlib
from datetime import datetime, timedelta, timezone

from . import ads, core_settings, db, media
from .db import iso
from .events import BadRequest

WHAT = ('ads', 'bumpers')
EVERY_MIN = {'off': 0, 'hourly': 60, 'half-hourly': 30, 'quarter-hourly': 15}
ONE_PLAYER = 'this station has one player: it plays songs only (no bumpers, advertisements or station IDs)'


def one_player(cur, station):
    """Has the station one player (it reports players = 1 in its state; 2 until it does)?"""
    cur.execute('SELECT players FROM stations WHERE station = %s', (station,))
    r = cur.fetchone()
    return bool(r) and r['players'] == 1


def refuse_one_player(cur, station, what):
    """BadRequest (400) for anything about breaks on a one-player station."""
    if one_player(cur, station):
        raise BadRequest(f'{what}: {ONE_PLAYER}')


def holds(cur, station):
    """{'ads': {'until': datetime | None}, ...} for the holds in force (until None = until resumed)."""
    cur.execute("""SELECT what, until, set_by FROM station_holds
                   WHERE station = %s AND (until IS NULL OR until > NOW())""", (station,))
    return {r['what']: {'until': r['until'], 'by': r['set_by']} for r in cur.fetchall()}


def holds_view(cur, station):
    return {w: {'until': iso(h['until']), 'by': h['by']} for w, h in holds(cur, station).items()}


def held(hold, at):
    """Is this hold (from holds()) in force at datetime `at`?"""
    return bool(hold) and (hold['until'] is None or at < hold['until'])


def set_hold(conn, station, what, until, user):
    """until: ISO time, None (until resumed) or 'off' (resume). Returns a log line."""
    if what not in WHAT:
        raise BadRequest('what must be ads or bumpers')
    with conn.cursor() as cur:
        db.lock_station(cur, station)
        if until != 'off':                 # resuming an old hold is always fine
            refuse_one_player(cur, station, 'Pause')
        if until == 'off':
            cur.execute('DELETE FROM station_holds WHERE station = %s AND what = %s', (station, what))
            line = f'HOLD station={station} {what} resumed by={user.get("username")}'
        else:
            when = None
            if until is not None:
                try:
                    when = datetime.fromisoformat(str(until).replace('Z', '+00:00'))
                except ValueError:
                    raise BadRequest('until must be a time like 2026-10-13T05:00:00Z')
                if when.tzinfo is None or when <= datetime.now(timezone.utc):
                    raise BadRequest('until must be a future time with its zone')
            cur.execute("""INSERT INTO station_holds (station, what, until, set_by, set_at) VALUES (%s, %s, %s, %s, NOW())
                           ON CONFLICT (station, what) DO UPDATE SET until = EXCLUDED.until, set_by = EXCLUDED.set_by,
                               set_at = NOW()""", (station, what, when, user.get('username')))
            line = f'HOLD station={station} {what} until={iso(when) if when else "resumed"} by={user.get("username")}'
        ads.stale(cur, station)
    conn.commit()
    return 20, line


def set_ident(conn, station, key, ident):
    with conn.cursor() as cur:
        db.lock_station(cur, station)
        refuse_one_player(cur, station, 'Station ID')
        cur.execute("""UPDATE library SET ident = %s WHERE station = %s AND key = %s AND kind = 'imaging' AND present""",
                    (bool(ident), station, key or ''))
        if not cur.rowcount:
            raise BadRequest('that imaging file is not in the library')
        ads.stale(cur, station)
    conn.commit()


def idents(cur, station, now=None):
    """The ID schedule: {'due': key | None, 'next_at': datetime | None, 'window_s', 'keys': [...]}.
    due = the ID to play at the next song change (its slot is open and no ID played since it opened),
    then also due_until: when its slot's window closes."""
    s = core_settings.effective(cur, station)
    every = EVERY_MIN.get(s['ident_schedule'], 0)
    window = int(s['ident_window_min']) * 60
    cur.execute(f"""SELECT key FROM library WHERE station = %s AND kind = 'imaging' AND ident AND present AND {media.supported()}
                    ORDER BY key""", (station,))
    keys = [r['key'] for r in cur.fetchall()]
    out = {'due': None, 'next_at': None, 'window_s': window, 'keys': keys}
    if not every or not keys:
        return out
    cur.execute('SELECT NOW()')
    now = now or cur.fetchone()[0]
    hour = now.replace(minute=0, second=0, microsecond=0)
    slot = hour + timedelta(minutes=(now - hour).seconds // 60 // every * every)
    out['next_at'] = slot + timedelta(minutes=every)
    if (now - slot).total_seconds() > window:
        return out
    cur.execute("""SELECT COALESCE(directory, '') || file AS key, started_at FROM play_log
                   WHERE station = %s AND kind = 'imaging' AND started_at > %s - INTERVAL '1 day'
                     AND COALESCE(directory, '') || file = ANY(%s)
                   ORDER BY started_at DESC LIMIT 1""", (station, slot, keys))
    last = cur.fetchone()
    if last and last['started_at'] >= slot:
        return out                     # this slot's ID has played
    choices = [k for k in keys if not last or k != last['key']] or keys
    out['due'] = min(choices, key=lambda k: hashlib.md5((k + slot.isoformat()).encode()).hexdigest())
    out['due_until'] = slot + timedelta(seconds=window)
    return out
