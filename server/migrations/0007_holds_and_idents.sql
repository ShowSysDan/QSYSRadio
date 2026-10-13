-- Operator holds on ads / bumpers ("no more ads until 05:00"); until NULL =
-- until someone resumes. A row whose until has passed is no longer a hold.
CREATE TABLE station_holds (
    station  TEXT NOT NULL,
    what     TEXT NOT NULL CHECK (what IN ('ads', 'bumpers')),
    until    TIMESTAMPTZ,
    set_by   TEXT,
    set_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (station, what)
);

-- Imaging files marked as station IDs (played on the ID schedule).
ALTER TABLE library ADD COLUMN ident BOOLEAN NOT NULL DEFAULT FALSE;
