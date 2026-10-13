-- Ads: a third kind of file, with a per-ad schedule. What the Core used to keep
-- in learned.json (unsupported files) now lives in library.
ALTER TABLE play_log DROP CONSTRAINT play_log_kind_check;
ALTER TABLE play_log ADD CONSTRAINT play_log_kind_check CHECK (kind IN ('music', 'imaging', 'ad'));
ALTER TABLE library DROP CONSTRAINT library_kind_check;
ALTER TABLE library ADD CONSTRAINT library_kind_check CHECK (kind IN ('music', 'imaging', 'ad'));

-- the player could not load it ("Unsupported ..."); cleared when the file's size changes
ALTER TABLE library ADD COLUMN unsupported BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE stations ADD COLUMN ads_dir TEXT;

-- How each ad is scheduled. No row = 'mix' (mixed into ad breaks).
-- 'hourly' = per_hour plays each clock hour, spread over the hour; 'off' = never.
CREATE TABLE ad_rules (
    station     TEXT NOT NULL,
    key         TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('mix', 'hourly', 'off')),
    per_hour    INTEGER CHECK (per_hour BETWEEN 1 AND 30),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT,
    PRIMARY KEY (station, key),
    CHECK (mode <> 'hourly' OR per_hour IS NOT NULL)
);
CREATE INDEX play_log_station_kind_started ON play_log (station, kind, started_at);
