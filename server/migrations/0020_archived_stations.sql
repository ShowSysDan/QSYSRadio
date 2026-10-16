-- A station taken out of its Core's CFG.STATIONS is archived (control.archive_missing): its Core syncs on without
-- it, so it is hidden from the panel (the station list, the Stations page, the header) instead of showing offline.
-- Its rows (play log, library, settings) stay; listed again, it comes back with them (archived_at cleared).
ALTER TABLE stations ADD COLUMN archived_at TIMESTAMPTZ;
