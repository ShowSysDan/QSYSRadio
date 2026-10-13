-- When each file last went to air, kept by the play log's start events: the
-- server's rotation picks the least recently played songs (never played first).
ALTER TABLE library ADD COLUMN last_played TIMESTAMPTZ;
UPDATE library l SET last_played = p.last
FROM (SELECT station, COALESCE(directory, '') || file AS key, MAX(started_at) AS last
      FROM play_log GROUP BY 1, 2) p
WHERE p.station = l.station AND p.key = l.key;
CREATE INDEX library_rotation ON library (station, kind, last_played NULLS FIRST) WHERE present AND NOT unsupported;
