-- File analysis moves to each Core's own analysis players (radio.lua CFG.ANALYZERS: an Audio Player and a
-- meter channel routed to no output), which measure files for every station on that Core; the job moves from
-- the stations to their Core (analysis.py). cores.analyze_from: the run in progress (files analyzed before it,
-- or never, are owed; '-infinity' = only files never analyzed; NULL = none); analyze_run_at: when it started
-- (a file that failed in it waits for the next run); analyze_auto_at: when the daily run last started.
-- owed / owed_rev / owed_at: the Core's list of files to measure ([{station, key}]) and its rev, cached: made
-- again when a result comes in, when something it is made from changed (owed_at NULL) or when it is a minute
-- old. analyzers: the Core's analysis players as it last reported them ([{name, state, station?, key?}];
-- NULL = never reported: radio.lua before 2.1.0).
ALTER TABLE cores ADD COLUMN analyze_from TIMESTAMPTZ, ADD COLUMN analyze_run_at TIMESTAMPTZ,
    ADD COLUMN analyze_auto_at TIMESTAMPTZ, ADD COLUMN owed JSONB, ADD COLUMN owed_rev TEXT,
    ADD COLUMN owed_at TIMESTAMPTZ, ADD COLUMN analyzers JSONB;

-- a run in progress on one of a Core's stations goes on as the Core's (the widest: the latest start); a daily
-- run already started today on one of them counts for the Core. A station with no Core row yet (a 1.x Core's:
-- its run, such as 1.3.0's level re-analysis, is likely still going) keeps its job in its own columns until it
-- first syncs as part of a 2.x Core, which takes it over the same way (analysis.adopt); a later version drops them.
UPDATE cores c SET analyze_from = j.analyze_from, analyze_run_at = j.analyze_run_at, analyze_auto_at = j.analyze_auto_at
FROM (SELECT core, max(analyze_from) AS analyze_from, max(analyze_run_at) AS analyze_run_at,
             max(analyze_auto_at) AS analyze_auto_at
      FROM stations WHERE core IS NOT NULL GROUP BY core) j
WHERE c.core = j.core;
UPDATE stations s SET analyze_from = NULL, analyze_run_at = NULL, analyze_auto_at = NULL
WHERE EXISTS (SELECT 1 FROM cores c WHERE c.core = s.core);

-- "Analyze new files" (analyze_at) applies to the whole Core: set for All stations only
DELETE FROM station_settings WHERE name = 'analyze_at';
