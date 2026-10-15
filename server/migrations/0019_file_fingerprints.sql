-- radio.lua 2.4.0 fingerprints files: the sha1 of a file's size, its first and last 64 KiB (fingerprints.py, media.py).
-- media_files.fingerprint: the content of the file the row's figures belong to (NULL: not known yet; the row is then
-- the file of its name + size, one per name + size as before). Two different files of the same name and size can
-- coexist once fingerprinted; one row per fingerprint.
ALTER TABLE media_files ADD COLUMN fingerprint TEXT;
ALTER TABLE media_files DROP CONSTRAINT media_files_file_size_key;
CREATE UNIQUE INDEX media_files_name_size_unknown ON media_files (file, size) WHERE fingerprint IS NULL;
CREATE INDEX media_files_name_size ON media_files (file, size);
CREATE UNIQUE INDEX media_files_fingerprint ON media_files (fingerprint) WHERE fingerprint IS NOT NULL;

-- library.fingerprint / fingerprint_size: what the Core reported for this station's file and the size it had then;
-- fingerprint_at / fingerprint_error: its last report (an error, or a size other than the library's, waits for the
-- next scan or a day). Owed (library_fingerprint_owed): present files of known size not fingerprinted at their size.
ALTER TABLE library ADD COLUMN fingerprint TEXT, ADD COLUMN fingerprint_size BIGINT, ADD COLUMN fingerprint_at TIMESTAMPTZ,
    ADD COLUMN fingerprint_error TEXT;
CREATE INDEX library_fingerprint_owed ON library (station, key)
    WHERE present AND size IS NOT NULL AND (fingerprint IS NULL OR fingerprint_size IS DISTINCT FROM size);

-- each Core's list of files to fingerprint ([{station, key}]) and its rev, cached like the analysis list (owed*)
ALTER TABLE cores ADD COLUMN fp_owed JSONB, ADD COLUMN fp_rev TEXT, ADD COLUMN fp_at TIMESTAMPTZ;

-- library_figures reads library's columns as they were when it was made: made again with the new ones
DROP VIEW library_figures;
CREATE VIEW library_figures AS
SELECT l.*, m.duration_s, m.lead_in_s, m.trail_s, m.lead_set_s, m.trail_set_s, m.peak_dbfs, m.level_db, m.level_s,
       m.unsupported, m.analyzed_at, m.analyze_failed_at, m.analyze_error
FROM library l JOIN media_files m ON m.id = l.file_id;
