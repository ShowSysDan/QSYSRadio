"""A file's figures (length, quiet start / end, operator trims, average level, peak, unsupported, analysis
state) belong to the file, not the station: media_files has one row per file name (no folder) and size in
bytes, shared by every station whose library holds that file, on any Core. A file moved to another folder
or station keeps its figures; an edited one (new size) starts afresh, operator trims too; a file of unknown
size is never shared (a row of its own). library.file_id points each station's file at its row.

Reads: the view library_figures (library + its file's figures, as library columns); a query that only
needs to leave unsupported files out reads library with supported(). Writes: media_files WHERE file_of()
(the row of one station's file); an operator's change reaches the Core of every station that has the file
through a rescan each (stations_of).
"""
from psycopg2.extras import execute_values

_SEQ = "nextval(pg_get_serial_sequence('media_files', 'id'))"


def file_of(column='id', present=False):
    """SQL: `column` is the media_files id of the station's file %(station)s / %(key)s (a file the station
    does not have: no row matches, nothing changes)."""
    return (f'{column} = (SELECT file_id FROM library WHERE station = %(station)s AND key = %(key)s'
            + (' AND present)' if present else ')'))


def stations_of(cur, station, ids):
    """The station, then every other station whose library holds one of these files (media_files ids, present
    there): the stations whose Core must be told when the files' figures change (a rescan; a Core takes the
    figures from its library upload reply)."""
    cur.execute('SELECT DISTINCT station FROM library WHERE present AND file_id = ANY(%s) AND station <> %s ORDER BY station',
                (list(ids), station))
    return [station] + [r[0] for r in cur.fetchall()]


def supported(table='library'):
    """SQL: this library row's file is not marked unsupported. Checked against the few unsupported files
    (index media_files_unsupported, hashed once), so a whole library is filtered without a lookup per row."""
    return f'{table}.file_id NOT IN (SELECT id FROM media_files WHERE unsupported)'


def link(cur, station, rows):
    """The Core's files after a scan into the station's library (inside the caller's transaction, under its
    station lock), each pointed at its file's figures: the row of its name and size, made when new; a file
    of unknown size keeps its own row (a new one when it has none). Files not in the list are gone
    (present = FALSE). A few bulk statements, whatever the number of files. rows: {key: (kind, directory,
    file, artist, title, size, duration, lead, trail, peak)}; the last four only come from old Cores, which
    sent what they had measured with the list (it still counts)."""
    keys = list(rows)
    cur.execute('UPDATE library SET present = FALSE WHERE station = %s AND present AND key NOT IN (SELECT unnest(%s::text[]))',
                (station, keys))
    known = sorted({(r[2], r[5]) for r in rows.values() if r[5] is not None})
    if known:
        # one statement in a fixed order: two stations adding the same new files at once wait, never deadlock
        cur.execute("""INSERT INTO media_files (file, size)
                       SELECT v.file, v.size FROM unnest(%s::text[], %s::bigint[]) AS v (file, size)
                       WHERE NOT EXISTS (SELECT 1 FROM media_files m WHERE m.file = v.file AND m.size = v.size)
                       ORDER BY 1, 2
                       ON CONFLICT (file, size) DO NOTHING""", ([f for f, _ in known], [z for _, z in known]))
    own = {}                               # size unknown: the row it has (same name), else a new one
    unknown = [k for k in keys if rows[k][5] is None]
    if unknown:
        cur.execute(f"""WITH had AS (SELECT v.key, v.file, m.id FROM unnest(%s::text[], %s::text[]) AS v (key, file)
                                     LEFT JOIN library l ON l.station = %s AND l.key = v.key
                                     LEFT JOIN media_files m ON m.id = l.file_id AND m.size IS NULL AND m.file = v.file),
                             fresh AS (SELECT key, file, {_SEQ} AS id FROM had WHERE id IS NULL),
                             made AS (INSERT INTO media_files (id, file) SELECT id, file FROM fresh)
                        SELECT key, id FROM had WHERE id IS NOT NULL UNION ALL SELECT key, id FROM fresh""",
                    (unknown, [rows[k][2] for k in unknown], station))
        own = dict(cur.fetchall())
    execute_values(cur, """INSERT INTO library (station, key, kind, directory, file, artist, title, size, file_id)
                           SELECT v.station, v.key, v.kind, v.directory, v.file, v.artist, v.title, v.size, COALESCE(v.own, m.id)
                           FROM (VALUES %s) AS v (station, key, kind, directory, file, artist, title, size, own)
                           LEFT JOIN media_files m ON m.file = v.file AND m.size = v.size
                           ON CONFLICT (station, key) DO UPDATE SET kind = EXCLUDED.kind, directory = EXCLUDED.directory,
                               file = EXCLUDED.file, artist = EXCLUDED.artist, title = EXCLUDED.title, size = EXCLUDED.size,
                               file_id = EXCLUDED.file_id, present = TRUE, last_seen = NOW()""",
                   [(station, k) + rows[k][:6] + (own.get(k),) for k in keys],
                   template='(%s, %s, %s, %s, %s, %s, %s, %s::bigint, %s::bigint)', page_size=1000)
    # a file whose size is known now leaves the row of its unknown size behind: nothing else can use it
    # (found through index media_files_unknown_size: normally none)
    cur.execute("""DELETE FROM media_files m WHERE m.size IS NULL
                     AND NOT EXISTS (SELECT 1 FROM library l WHERE l.file_id = m.id)""")
    legacy = [(station, k) + rows[k][6:] for k in keys if any(v is not None for v in rows[k][6:])]
    if legacy:
        execute_values(cur, """UPDATE media_files m SET duration_s = COALESCE(v.duration, m.duration_s),
                                   lead_in_s = COALESCE(v.lead, m.lead_in_s), trail_s = COALESCE(v.trail, m.trail_s),
                                   peak_dbfs = COALESCE(v.peak, m.peak_dbfs)
                               FROM (VALUES %s) AS v (station, key, duration, lead, trail, peak)
                               JOIN library l ON l.station = v.station AND l.key = v.key
                               WHERE m.id = l.file_id AND (m.duration_s, m.lead_in_s, m.trail_s, m.peak_dbfs) IS DISTINCT FROM
                                     (COALESCE(v.duration, m.duration_s), COALESCE(v.lead, m.lead_in_s),
                                      COALESCE(v.trail, m.trail_s), COALESCE(v.peak, m.peak_dbfs))""",
                       legacy, template='(%s, %s, %s::float8, %s::float8, %s::float8, %s::float8)', page_size=1000)
