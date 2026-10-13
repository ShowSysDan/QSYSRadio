-- File analysis: the Core measures each file's quiet start / end on a deck
-- that is off air. analyzed_at: when (NULL = not analyzed; cleared when the
-- file changes). stations.analyze_from: the job in progress (files analyzed
-- before it are owed; '-infinity' = only files never analyzed; NULL = none).
-- analyze_auto_at: when the daily run last started.
ALTER TABLE library ADD COLUMN analyzed_at TIMESTAMPTZ;
ALTER TABLE stations ADD COLUMN analyze_from TIMESTAMPTZ, ADD COLUMN analyze_auto_at TIMESTAMPTZ;
