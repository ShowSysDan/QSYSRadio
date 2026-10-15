-- A file's figures belong to the file, not the station: one media_files row per file name (no folder)
-- and size in bytes, shared by every station whose library holds that file, on any Core. A file moved to
-- another folder or station keeps its figures; an edited one (new size) starts afresh; one of unknown
-- size is never shared (a row of its own). library.file_id points each station's file at its row; the
-- view library_figures reads them back as library columns (recreate it when library's columns change).
CREATE TABLE media_files (
    id                BIGSERIAL PRIMARY KEY,
    file              TEXT NOT NULL,
    size              BIGINT,
    duration_s        DOUBLE PRECISION,
    lead_in_s         DOUBLE PRECISION,
    trail_s           DOUBLE PRECISION,
    lead_set_s        DOUBLE PRECISION,
    trail_set_s       DOUBLE PRECISION,
    peak_dbfs         DOUBLE PRECISION,
    level_db          DOUBLE PRECISION,
    level_s           DOUBLE PRECISION,
    unsupported       BOOLEAN NOT NULL DEFAULT FALSE,
    analyzed_at       TIMESTAMPTZ,
    analyze_failed_at TIMESTAMPTZ,
    analyze_error     TEXT,
    first_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (file, size)
);
ALTER TABLE library ADD COLUMN file_id BIGINT;

-- What each station had, merged per file: the quiet start / end of the latest analysis (else the most
-- recently seen station's), the latest operator trims, the levels averaged by the seconds each heard (as
-- events.LEVEL_SET does), the highest peak, the latest analysis state. A row of unknown size: its own.
UPDATE library SET size = NULL WHERE size = 0;                -- the Core sends 0 when it has no size: unknown
UPDATE library SET file_id = nextval(pg_get_serial_sequence('media_files', 'id')) WHERE size IS NULL;
INSERT INTO media_files (id, file, size, duration_s, lead_in_s, trail_s, lead_set_s, trail_set_s, peak_dbfs,
                         level_db, level_s, unsupported, analyzed_at, analyze_failed_at, analyze_error, first_seen)
SELECT COALESCE(min(file_id), nextval(pg_get_serial_sequence('media_files', 'id'))), file, size,
       (array_agg(duration_s ORDER BY last_seen DESC, station, key) FILTER (WHERE duration_s IS NOT NULL))[1],
       CASE WHEN max(analyzed_at) IS NOT NULL THEN (array_agg(lead_in_s ORDER BY analyzed_at DESC NULLS LAST, station, key))[1]
            ELSE (array_agg(lead_in_s ORDER BY last_seen DESC, station, key) FILTER (WHERE lead_in_s IS NOT NULL))[1] END,
       CASE WHEN max(analyzed_at) IS NOT NULL THEN (array_agg(trail_s ORDER BY analyzed_at DESC NULLS LAST, station, key))[1]
            ELSE (array_agg(trail_s ORDER BY last_seen DESC, station, key) FILTER (WHERE trail_s IS NOT NULL))[1] END,
       (array_agg(lead_set_s ORDER BY last_seen DESC, station, key) FILTER (WHERE lead_set_s IS NOT NULL))[1],
       (array_agg(trail_set_s ORDER BY last_seen DESC, station, key) FILTER (WHERE trail_set_s IS NOT NULL))[1],
       max(peak_dbfs),
       10 * log(sum(power(10, level_db / 10) * level_s) FILTER (WHERE level_db IS NOT NULL AND level_s > 0)
                / sum(level_s) FILTER (WHERE level_db IS NOT NULL AND level_s > 0)),
       sum(level_s) FILTER (WHERE level_db IS NOT NULL AND level_s > 0),
       bool_or(unsupported), max(analyzed_at), max(analyze_failed_at),
       CASE WHEN max(analyze_failed_at) IS NOT NULL
            THEN (array_agg(analyze_error ORDER BY analyze_failed_at DESC NULLS LAST, station, key))[1] END,
       min(first_seen)
FROM library
GROUP BY file, size, CASE WHEN size IS NULL THEN file_id END;
UPDATE library l SET file_id = m.id FROM media_files m WHERE l.size IS NOT NULL AND m.file = l.file AND m.size = l.size;
ALTER TABLE library ALTER COLUMN file_id SET NOT NULL,
    ADD CONSTRAINT library_file_id_fkey FOREIGN KEY (file_id) REFERENCES media_files (id);
CREATE INDEX library_file_id ON library (file_id);

-- The figures now live only in media_files. The rotation leaves unsupported files out through the few
-- of them (media_files_unsupported), not a lookup per song. A run's files owed analysis are read in
-- their order (library_analyze_order: a long run stops after the first few) or, when few are left,
-- from the files not analyzed (media_files_analyzed). A library upload finds the rows a file of unknown
-- size left behind (its size known now) through media_files_unknown_size.
DROP INDEX library_rotation;
ALTER TABLE library DROP COLUMN duration_s, DROP COLUMN lead_in_s, DROP COLUMN trail_s, DROP COLUMN lead_set_s,
    DROP COLUMN trail_set_s, DROP COLUMN peak_dbfs, DROP COLUMN level_db, DROP COLUMN level_s, DROP COLUMN unsupported,
    DROP COLUMN analyzed_at, DROP COLUMN analyze_failed_at, DROP COLUMN analyze_error;
CREATE INDEX library_rotation ON library (station, kind, last_played NULLS FIRST) WHERE present;
CREATE INDEX media_files_unsupported ON media_files (id) WHERE unsupported;
CREATE INDEX media_files_unknown_size ON media_files (id) WHERE size IS NULL;
CREATE INDEX media_files_analyzed ON media_files (analyzed_at);
CREATE INDEX library_analyze_order ON library (station, (kind = 'music'), key) WHERE present;

CREATE VIEW library_figures AS
SELECT l.*, m.duration_s, m.lead_in_s, m.trail_s, m.lead_set_s, m.trail_set_s, m.peak_dbfs, m.level_db, m.level_s,
       m.unsupported, m.analyzed_at, m.analyze_failed_at, m.analyze_error
FROM library l JOIN media_files m ON m.id = l.file_id;
