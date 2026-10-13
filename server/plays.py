"""Play log queries (what played when). All of them are per station."""
from .db import iso, like

_COLS = """id, station, kind, file, directory, artist, title, deck,
           started_at, ended_at, duration_s, played_s, end_reason"""


def _rows(cur):
    return [dict(r, started_at=iso(r['started_at']), ended_at=iso(r['ended_at'])) for r in cur.fetchall()]


def between(conn, station, start, end, search=None, kind=None, limit=500):
    """Plays that started in [start, end), oldest first. start/end are ISO
    strings; without an offset they are read in the server's TIMEZONE."""
    sql = (f'SELECT {_COLS} FROM play_log WHERE station = %s '
           f'AND started_at >= %s::timestamptz AND started_at < %s::timestamptz')
    args = [station, start, end]
    if kind:
        sql += ' AND kind = %s'
        args.append(kind)
    if search:
        sql += ' AND (file ILIKE %s OR artist ILIKE %s OR title ILIKE %s)'
        args += [like(search)] * 3
    with conn.cursor() as cur:
        cur.execute(sql + ' ORDER BY started_at, id LIMIT %s', args + [limit])
        return _rows(cur)


def at(conn, station, when):
    """Everything on air at one moment (two rows during a crossfade). A play
    with no end and no known length counts as on air until the next one."""
    with conn.cursor() as cur:
        cur.execute(f"""SELECT {_COLS} FROM play_log p
                        WHERE station = %s AND started_at <= %s::timestamptz
                          AND started_at > %s::timestamptz - INTERVAL '1 day'
                          AND COALESCE(ended_at,
                                       CASE WHEN duration_s > 0 THEN started_at + make_interval(secs => duration_s) END,
                                       (SELECT MIN(n.started_at) FROM play_log n
                                        WHERE n.station = p.station AND n.started_at > p.started_at),
                                       'infinity'::timestamptz) > %s::timestamptz
                        ORDER BY started_at, id""", (station, when, when, when))
        return _rows(cur)


def recent(conn, station, limit=20):
    """Open plays (on air now) and the latest plays, newest first."""
    with conn.cursor() as cur:
        cur.execute(f"""SELECT {_COLS} FROM play_log WHERE station = %s AND ended_at IS NULL
                          AND started_at > NOW() - INTERVAL '1 day' ORDER BY started_at DESC""", (station,))
        on_air = _rows(cur)
        cur.execute(f'SELECT {_COLS} FROM play_log WHERE station = %s ORDER BY started_at DESC, id DESC LIMIT %s',
                    (station, limit))
        return on_air, _rows(cur)
