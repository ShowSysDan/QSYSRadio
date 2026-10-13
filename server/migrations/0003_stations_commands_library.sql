-- Each Core: last contact (every sync, ~1 s) and its live state (sent when it
-- changes, at least every 10 s; state_at = when it was received).
CREATE TABLE stations (
    station        TEXT PRIMARY KEY,
    state          JSONB NOT NULL DEFAULT '{}',
    radio_version  TEXT,
    boot           TEXT,
    settings_rev   TEXT,
    last_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state_at       TIMESTAMPTZ,
    library_at     TIMESTAMPTZ,
    music_dir      TEXT,
    imaging_dir    TEXT
);

-- Operator commands from the web panel. The Core picks them up on its next
-- poll (id > its last ack) and reports the result as a 'command' event.
-- Commands the Core has not picked up within 60 s expire.
CREATE TABLE commands (
    id               BIGSERIAL PRIMARY KEY,
    station          TEXT NOT NULL,
    cmd              TEXT NOT NULL,
    args             JSONB NOT NULL DEFAULT '{}',
    label            TEXT,
    requested_by     TEXT,
    requested_by_id  INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at     TIMESTAMPTZ,
    done_at          TIMESTAMPTZ,
    ok               BOOLEAN,
    note             TEXT
);
CREATE INDEX commands_station_id ON commands (station, id);
CREATE INDEX commands_open ON commands (station, created_at) WHERE done_at IS NULL;

-- Files the Core found in its music / imaging folders (uploaded after every
-- scan). Files that disappear stay with present = FALSE for the history.
CREATE TABLE library (
    station     TEXT NOT NULL,
    key         TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('music', 'imaging')),
    directory   TEXT,
    file        TEXT NOT NULL,
    artist      TEXT,
    title       TEXT,
    size        BIGINT,
    duration_s  DOUBLE PRECISION,
    lead_in_s   DOUBLE PRECISION,
    trail_s     DOUBLE PRECISION,
    peak_dbfs   DOUBLE PRECISION,
    present     BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (station, key)
);
CREATE INDEX library_station_kind ON library (station, kind) WHERE present;
CREATE INDEX play_log_dir_file ON play_log (directory, file);
