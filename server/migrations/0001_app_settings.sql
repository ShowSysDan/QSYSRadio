-- Server-managed settings (key/value). Per-machine settings that must differ
-- between servers sharing this database belong in .env instead.
CREATE TABLE app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
