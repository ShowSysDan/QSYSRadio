-- radio.lua 2.2.0 runs one-player stations (background-music zones: one Audio Player, songs only) next to
-- two-player ones. stations.players: how many players the station has, from its state (2 until it reports one);
-- a one-player station's ads plan names only the next song, and its panel has no bumpers, ads or station IDs.
ALTER TABLE stations ADD COLUMN players SMALLINT NOT NULL DEFAULT 2 CHECK (players IN (1, 2));
