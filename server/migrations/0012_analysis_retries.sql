-- A failed analysis no longer counts as analyzed: it is skipped for the rest
-- of that run (stations.analyze_run_at = when the run started) and tried
-- again by the next one. analyze_wanted: one file asked for on its own
-- (Analyze on its row), analyzed without starting a run. Files marked
-- analyzed by a failure up to 1.1.0 are owed again.
ALTER TABLE library ADD COLUMN analyze_failed_at TIMESTAMPTZ, ADD COLUMN analyze_error TEXT,
    ADD COLUMN analyze_wanted BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX library_analyze_wanted ON library (station) WHERE analyze_wanted;
ALTER TABLE stations ADD COLUMN analyze_run_at TIMESTAMPTZ;
UPDATE library l SET analyzed_at = NULL
WHERE analyzed_at IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM core_events e
                  WHERE e.station = l.station AND e.kind = 'analysis' AND e.data->>'ok' = 'true'
                    AND COALESCE(e.data->>'dir', '') || COALESCE(e.data->>'file', '') = l.key);
