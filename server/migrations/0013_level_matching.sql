-- Level matching: each file's average level in dB (the player meter's rms at
-- unity gain, sound only), the energy average of every measurement weighted by
-- the seconds it heard (level_s). peak_dbfs now keeps the highest peak seen.
-- stations.meters: the decks' meter readout from the Core's last sync.
-- Every station analyzes all of its files again (as Re-analyze all does), so
-- each gets an average level from the middle of the file.
ALTER TABLE library ADD COLUMN level_db DOUBLE PRECISION, ADD COLUMN level_s DOUBLE PRECISION;
ALTER TABLE stations ADD COLUMN meters JSONB;
UPDATE stations SET analyze_from = NOW(), analyze_run_at = NOW();
