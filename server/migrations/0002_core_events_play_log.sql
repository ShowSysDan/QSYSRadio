-- Every event the Core reports, stored once. (boot, seq) identifies an event:
-- the Core replays its outbox after outages, so repeats are ignored.
CREATE TABLE core_events (
    id            BIGSERIAL PRIMARY KEY,
    station       TEXT NOT NULL,
    boot          TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    event_at      TIMESTAMPTZ NOT NULL,
    core_clock    DOUBLE PRECISION,
    radio_version TEXT,
    data          JSONB NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (boot, seq)
);
CREATE INDEX core_events_event_at ON core_events (event_at);
CREATE INDEX core_events_kind_event_at ON core_events (kind, event_at);

-- One row per time something went to air (music or imaging), built from the
-- track/bumper start and end events. ended_at is NULL while it is playing.
CREATE TABLE play_log (
    id          BIGSERIAL PRIMARY KEY,
    station     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('music', 'imaging')),
    file        TEXT NOT NULL,
    directory   TEXT,
    artist      TEXT,
    title       TEXT,
    deck        TEXT,
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    duration_s  DOUBLE PRECISION,
    played_s    DOUBLE PRECISION,
    end_reason  TEXT,
    boot        TEXT NOT NULL,
    start_seq   INTEGER NOT NULL,
    end_seq     INTEGER,
    UNIQUE (boot, start_seq)
);
CREATE INDEX play_log_started_at ON play_log (started_at);
CREATE INDEX play_log_file ON play_log (file);
CREATE INDEX play_log_artist ON play_log (lower(artist));
CREATE INDEX play_log_open ON play_log (station) WHERE ended_at IS NULL;
