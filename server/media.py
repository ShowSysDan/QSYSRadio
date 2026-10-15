"""A file's figures (length, quiet start / end, operator trims, average level, peak, unsupported, analysis
state) belong to the file, not the station: media_files has one row per file, shared by every station whose
library holds that file, on any Core. A file is known by its content fingerprint once its Core has reported one
(radio.lua 2.4.0: the sha1 of its size, its first, middle and last 64 KiB; fingerprints.py lists those owed one),
else by its name (no folder) and size in bytes. A file moved to another folder or station keeps its figures,
a renamed one too once fingerprinted; an edited one (new size) starts afresh, operator trims too; two files of
the same name and size but different content part once fingerprinted; a file of unknown size is never shared
(a row of its own). library.file_id points each station's file at its row.

Reads: the view library_figures (library + its file's figures, as library columns); a query that only
needs to leave unsupported files out reads library with supported(). Writes: media_files WHERE file_of()
(the row of one station's file); an operator's change reaches the Core of every station that has the file
through a rescan each (stations_of).
"""
import re

import psycopg2
import psycopg2.errors
from psycopg2.extras import execute_values

from .db import text

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


def link(cur, station, rows, scan):
    """Files of a scan (all of them, or one part) into the station's library (inside the caller's
    transaction, under its station lock), present and marked with the scan id, each pointed at its file's
    figures: a file fingerprinted at its size is on the row of its fingerprint (kept, or back on it after a scan
    at another size); any other the row of its name and size (one not fingerprinted first), made when new; a file of unknown size keeps its own row (a new one when it has
    none). Files not in the scan stay as they are until it is complete (gone()). A few bulk statements,
    whatever the number of files. rows: {key: (kind, directory, file, artist, title, size,
    duration, lead, trail, peak)}; the last four only come from old Cores, which sent what they had
    measured with the list (it still counts)."""
    keys = list(rows)
    known = sorted({(r[2], r[5]) for r in rows.values() if r[5] is not None})
    if known:
        # one statement in a fixed order: two stations adding the same new files at once wait, never deadlock
        cur.execute("""INSERT INTO media_files (file, size)
                       SELECT v.file, v.size FROM unnest(%s::text[], %s::bigint[]) AS v (file, size)
                       WHERE NOT EXISTS (SELECT 1 FROM media_files m WHERE m.file = v.file AND m.size = v.size)
                       ORDER BY 1, 2
                       ON CONFLICT (file, size) WHERE fingerprint IS NULL DO NOTHING""",
                    ([f for f, _ in known], [z for _, z in known]))
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
    execute_values(cur, """INSERT INTO library (station, key, kind, directory, file, artist, title, size, file_id, scan)
                           SELECT v.station, v.key, v.kind, v.directory, v.file, v.artist, v.title, v.size, COALESCE(v.own, m.id), v.scan
                           FROM (VALUES %s) AS v (station, key, kind, directory, file, artist, title, size, own, scan)
                           LEFT JOIN LATERAL (SELECT id FROM media_files m WHERE m.file = v.file AND m.size = v.size
                                              ORDER BY m.fingerprint IS NOT NULL, m.id LIMIT 1) m ON TRUE
                           ON CONFLICT (station, key) DO UPDATE SET kind = EXCLUDED.kind, directory = EXCLUDED.directory,
                               file = EXCLUDED.file, artist = EXCLUDED.artist, title = EXCLUDED.title, size = EXCLUDED.size,
                               file_id = CASE WHEN library.fingerprint IS NOT NULL AND library.fingerprint_size = EXCLUDED.size
                                              THEN COALESCE((SELECT f.id FROM media_files f WHERE f.fingerprint = library.fingerprint),
                                                            EXCLUDED.file_id)
                                              ELSE EXCLUDED.file_id END,
                               present = TRUE, last_seen = NOW(), scan = EXCLUDED.scan""",
                   [(station, k) + rows[k][:6] + (own.get(k), scan) for k in keys],
                   template='(%s, %s, %s, %s, %s, %s, %s, %s::bigint, %s::bigint, %s)', page_size=1000)
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


def gone(cur, station, scan):
    """A complete scan: the station's files not in it are gone (present = FALSE; kept for the history)."""
    cur.execute('UPDATE library SET present = FALSE WHERE station = %s AND present AND scan IS DISTINCT FROM %s', (station, scan))


FINGERPRINT = re.compile(r'[0-9a-f]{40}')
_FIGURES = ('duration_s', 'lead_in_s', 'trail_s', 'lead_set_s', 'trail_set_s', 'peak_dbfs', 'level_db', 'level_s',
            'unsupported', 'analyzed_at', 'analyze_failed_at', 'analyze_error', 'first_seen')


def fingerprinted(cur, station, key, fp, size, error=None):
    """The Core's fingerprint of one station's file (inside the event's savepoint, under the station's lock): fp
    (40 hex digits) of the file at `size` bytes, or error. Recorded on the library row (an error, or a size other
    than the library's: the file changed since the scan, tried again after the next scan or a day). Then the
    file's row: another row with that fingerprint is the file's (a renamed or moved file keeps its figures: the
    row it leaves, when not fingerprinted, gives it the figures it lacks (merge) and goes when no library row
    uses it); a row fingerprinted otherwise is another file of the same name and size (a row of its own, no
    figures yet); a row not fingerprinted yet takes it. Another station's transaction taking the same fingerprint
    at the same moment (two Cores, the same content under two names) is followed, as if it had been first.

    Locks: this station's library row (its lock covers it) and at most two media_files rows, by primary key,
    locked together in id order before any change. Other stations' parts write a media_files row by primary
    key too (one per statement, a play's or an analysis's figures), so two parts meet only on a file both have,
    and only one holding a row from an earlier event of its batch can deadlock with this: PostgreSQL then
    cancels one, that station's part is answered {error} and its Core sends its events again (nothing lost).
    Returns a log line when the file's row changed, else None."""
    cur.execute("""SELECT l.file, l.size, l.file_id, m.fingerprint FROM library l JOIN media_files m ON m.id = l.file_id
                   WHERE l.station = %s AND l.key = %s""", (station, key))
    row = cur.fetchone()
    if row is None:
        return None
    where = dict(station=station, key=key)
    if error or not fp or size != row['size']:
        error = error or (f'{size} bytes on the Core, {row["size"]} in the library: changed since the scan' if fp
                          else 'not a fingerprint')
        cur.execute("""UPDATE library SET fingerprint_at = NOW(), fingerprint_error = %(error)s
                       WHERE station = %(station)s AND key = %(key)s""", dict(where, error=text(error, 200)))
        return None
    cur.execute("""UPDATE library SET fingerprint = %(fp)s, fingerprint_size = %(size)s, fingerprint_at = NOW(),
                          fingerprint_error = NULL WHERE station = %(station)s AND key = %(key)s""", dict(where, fp=fp, size=size))
    old, what = row['file_id'], None
    target = _file_of_fingerprint(cur, fp)
    if target is None:
        if row['fingerprint'] is None:                          # the row of its name and size was not fingerprinted yet
            if _claim(cur, 'UPDATE media_files SET fingerprint = %s WHERE id = %s', (fp, old)):
                return None
        else:                                                   # same name and size, another file: a row of its own
            target = _claim(cur, 'INSERT INTO media_files (file, size, fingerprint) VALUES (%s, %s, %s) RETURNING id',
                            (row['file'], size, fp))
            what = target and 'another file of the same name and size: measured afresh'
        if target is None:                                      # another station's file took the fingerprint meanwhile
            target = _file_of_fingerprint(cur, fp)
            if target is None:
                raise ValueError(f'fingerprint {fp} taken and gone')
    if target == old:
        return None
    if what is None:
        cur.execute(f'SELECT id, fingerprint, {", ".join(_FIGURES)} FROM media_files WHERE id = ANY(%s) ORDER BY id FOR UPDATE',
                    ([old, target],))
        rows = {r['id']: r for r in cur.fetchall()}
        if rows[old]['fingerprint'] is None:
            _merge(cur, rows[old], target)
        what = 'the same file as before (renamed or moved): its figures follow it'
    cur.execute('UPDATE library SET file_id = %(target)s WHERE station = %(station)s AND key = %(key)s', dict(where, target=target))
    if row['fingerprint'] is None:                              # a row left by name + size: gone once nothing uses it
        cur.execute('SAVEPOINT fp_drop')
        try:
            cur.execute('DELETE FROM media_files m WHERE id = %s AND NOT EXISTS (SELECT 1 FROM library l WHERE l.file_id = m.id)',
                        (old,))
            cur.execute('RELEASE SAVEPOINT fp_drop')
        except psycopg2.IntegrityError:                         # another station's file just took it: it stays
            cur.execute('ROLLBACK TO SAVEPOINT fp_drop')
    return 20, f'FINGERPRINT station={station} key="{key}" {what}'


def _file_of_fingerprint(cur, fp):
    cur.execute('SELECT id FROM media_files WHERE fingerprint = %s', (fp,))
    return (cur.fetchone() or [None])[0]


def _claim(cur, query, args):
    """query (a fingerprint set on a row, or a new row RETURNING id) in a savepoint of its own: its result (True when
    it returns nothing), or None when another station's transaction took that fingerprint first (index
    media_files_fingerprint): the caller then follows the row that has it."""
    cur.execute('SAVEPOINT fp_claim')
    try:
        cur.execute(query, args)
        got = cur.fetchone()[0] if cur.description else True
    except psycopg2.errors.UniqueViolation:
        cur.execute('ROLLBACK TO SAVEPOINT fp_claim')
        return None
    cur.execute('RELEASE SAVEPOINT fp_claim')
    return got


def _merge(cur, old, target):
    """The figures of a row not fingerprinted (old: its row) into the file's row (target id), for what that one
    lacks: the levels count together (events.LEVEL_SET), the highest peak, an analysis (quiet start / end) when
    it has none, operator trims, length, unsupported. The levels then live on the target only: old (still used by
    another station's file, which may follow later) gives them once, never twice. Both rows are locked."""
    from .events import LEVEL_SET           # events imports this module
    ok = old['level_db'] is not None and (old['level_s'] or 0) > 0
    cur.execute(f"""UPDATE media_files SET {LEVEL_SET}, duration_s = COALESCE(duration_s, %(duration_s)s),
                           lead_in_s = CASE WHEN analyzed_at IS NULL AND %(analyzed_at)s::timestamptz IS NOT NULL THEN %(lead_in_s)s
                                            ELSE COALESCE(lead_in_s, %(lead_in_s)s) END,
                           trail_s = CASE WHEN analyzed_at IS NULL AND %(analyzed_at)s::timestamptz IS NOT NULL THEN %(trail_s)s
                                          ELSE COALESCE(trail_s, %(trail_s)s) END,
                           analyzed_at = COALESCE(analyzed_at, %(analyzed_at)s),
                           analyze_failed_at = COALESCE(analyze_failed_at, %(analyze_failed_at)s),
                           analyze_error = CASE WHEN analyze_failed_at IS NULL THEN %(analyze_error)s ELSE analyze_error END,
                           lead_set_s = COALESCE(lead_set_s, %(lead_set_s)s), trail_set_s = COALESCE(trail_set_s, %(trail_set_s)s),
                           unsupported = unsupported OR %(unsupported)s, first_seen = LEAST(first_seen, %(first_seen)s)
                    WHERE id = %(target)s""",
                dict(old, level=old['level_db'] if ok else None, level_s=old['level_s'] if ok else None,
                     peak=old['peak_dbfs'], target=target))
    if old['level_db'] is not None or old['level_s'] is not None:
        cur.execute('UPDATE media_files SET level_db = NULL, level_s = NULL WHERE id = %s', (old['id'],))
