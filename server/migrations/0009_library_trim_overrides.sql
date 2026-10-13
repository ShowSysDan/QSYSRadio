-- Operator figures for a file's quiet start / end; NULL = use what the Core measured.
ALTER TABLE library ADD COLUMN lead_set_s DOUBLE PRECISION, ADD COLUMN trail_set_s DOUBLE PRECISION;
