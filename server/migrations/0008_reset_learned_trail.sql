-- Up to now the Core measured a file's quiet end even after a crossfade had
-- faded it out, so learned trails were the crossfade length (about 4 s) and
-- songs crossfaded that much early. Forget them; real silent tails are
-- learned again on the next play.
UPDATE library SET trail_s = NULL WHERE trail_s IS NOT NULL;
