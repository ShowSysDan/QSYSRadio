-- Settings by station: a station's own values (only those that differ) over the
-- All-stations ones (app_settings 'core.<name>'), which are over radio.lua's
-- fallbacks. value: JSON, as in app_settings.
CREATE TABLE station_settings (
    station    TEXT NOT NULL,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (station, name)
);

-- The folders are per station only: every station already known keeps the
-- ones it plays from, and a new station never inherits them.
INSERT INTO station_settings (station, name, value, updated_at)
SELECT s.station, substr(a.key, 6), a.value, a.updated_at
FROM stations s CROSS JOIN app_settings a
WHERE a.key IN ('core.music_dir', 'core.imaging_dir', 'core.ads_dir');
DELETE FROM app_settings WHERE key IN ('core.music_dir', 'core.imaging_dir', 'core.ads_dir');
