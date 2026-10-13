-- Event identity is (station, boot, seq): two Cores can never collide.
ALTER TABLE core_events DROP CONSTRAINT core_events_boot_seq_key;
ALTER TABLE core_events ADD CONSTRAINT core_events_station_boot_seq_key UNIQUE (station, boot, seq);
ALTER TABLE play_log DROP CONSTRAINT play_log_boot_start_seq_key;
ALTER TABLE play_log ADD CONSTRAINT play_log_station_boot_start_seq_key UNIQUE (station, boot, start_seq);
