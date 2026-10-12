"""Play log queries (what played when)."""

_COLS = """id, station, kind, file, directory, artist, title, deck,
           started_at, ended_at, duration_s, played_s, end_reason"""


def _row(r):
    d = dict(r)
    for k in ('started_at', 'ended_at'):
        if d[k] is not None:
            d[k] = d[k].isoformat()
    return d


def between(conn, start, end, search=None, kind=None, station=None, limit=500):
    """Plays that started in [start, end), oldest first. start/end are ISO
    strings; without an offset they are read in the server's TIMEZONE."""
    sql = f'SELECT {_COLS} FROM play_log WHERE started_at >= %s::timestamptz AND started_at < %s::timestamptz'
    args = [start, end]
    if kind:
        sql += ' AND kind = %s'
        args.append(kind)
    if station:
        sql += ' AND station = %s'
        args.append(station)
    if search:
        sql += ' AND (file ILIKE %s OR artist ILIKE %s OR title ILIKE %s)'
        pat = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        args += [pat, pat, pat]
    sql += ' ORDER BY started_at, id LIMIT %s'
    args.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [_row(r) for r in cur.fetchall()]


def at(conn, when):
    """Everything on air at one moment (two rows during a crossfade)."""
    with conn.cursor() as cur:
        cur.execute(f"""SELECT {_COLS} FROM play_log
                        WHERE started_at <= %s::timestamptz
                          AND COALESCE(ended_at, started_at + make_interval(secs => COALESCE(duration_s, 0)))
                              > %s::timestamptz
                          AND started_at > %s::timestamptz - INTERVAL '1 day'
                        ORDER BY started_at, id""", (when, when, when))
        return [_row(r) for r in cur.fetchall()]


def recent(conn, limit=20):
    """Open plays (on air now) and the latest plays, newest first."""
    with conn.cursor() as cur:
        cur.execute(f'SELECT {_COLS} FROM play_log WHERE ended_at IS NULL '
                    f"AND started_at > NOW() - INTERVAL '1 day' ORDER BY started_at DESC")
        on_air = [_row(r) for r in cur.fetchall()]
        cur.execute(f'SELECT {_COLS} FROM play_log ORDER BY started_at DESC, id DESC LIMIT %s', (limit,))
        return on_air, [_row(r) for r in cur.fetchall()]
