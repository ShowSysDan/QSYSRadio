-- One radio.lua runs every station of a Core and syncs them all in one request a second.
-- cores: each Core (CORE_ID, else its design name): its boot, radio.lua version, last contact and Lua
-- memory in KB (mem_kb: the latest sample, which includes garbage not yet collected), for the leak
-- warning (cores.py): win_low_kb = the lowest sample of the 10-minute window that began at win_at;
-- mem_low_kb = the lowest window low after the first 10 minutes of this boot (booted_at = when the
-- server first saw this boot; mem_low_at = when that window began); mem_warned_at: at most once an hour.
CREATE TABLE cores (
    core          TEXT PRIMARY KEY,
    boot          TEXT,
    version       TEXT,
    booted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mem_kb        DOUBLE PRECISION,
    win_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    win_low_kb    DOUBLE PRECISION,
    mem_low_kb    DOUBLE PRECISION,
    mem_low_at    TIMESTAMPTZ,
    mem_warned_at TIMESTAMPTZ
);

-- stations.core: the Core the station was last seen on. The ads plan (ads.plan) is cached per station:
-- made again when the station's events were accepted, when it is stale (plan_at NULL: something it is
-- made from changed), 30 s old, or at plan_until (when it changes by the clock: an ad or ID slot opens,
-- a slot's window closes, a hold ends, the hour turns). A library scan comes in parts: each part's rows
-- are marked with the scan id (library.scan); stations.scan / scan_parts = the scan being received and
-- the parts in hand; only its last part, with every part in, marks the files not in it gone.
ALTER TABLE stations ADD COLUMN core TEXT, ADD COLUMN plan JSONB, ADD COLUMN plan_rev TEXT, ADD COLUMN plan_at TIMESTAMPTZ,
    ADD COLUMN plan_until TIMESTAMPTZ, ADD COLUMN scan TEXT, ADD COLUMN scan_parts INTEGER[] NOT NULL DEFAULT '{}';
ALTER TABLE library ADD COLUMN scan TEXT;

-- library_figures reads library's columns as they were when it was made: made again with the new one
DROP VIEW library_figures;
CREATE VIEW library_figures AS
SELECT l.*, m.duration_s, m.lead_in_s, m.trail_s, m.lead_set_s, m.trail_set_s, m.peak_dbfs, m.level_db, m.level_s,
       m.unsupported, m.analyzed_at, m.analyze_failed_at, m.analyze_error
FROM library l JOIN media_files m ON m.id = l.file_id;
