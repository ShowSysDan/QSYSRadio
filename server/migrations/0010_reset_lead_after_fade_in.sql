-- Up to 1.0.1 the Core measured a song's quiet start even when it faded the
-- song in (fade_in_s > 0), so learned lead-ins were the fade time or longer
-- and grew with each play. Only stations with Fade in set were affected:
-- forget their measured lead-ins (measured again on plays without a fade-in).
-- Operator figures (lead_set_s) stay.
UPDATE library SET lead_in_s = NULL
WHERE lead_in_s IS NOT NULL
  AND EXISTS (SELECT 1 FROM app_settings
              WHERE key = 'core.fade_in_s'
                AND CASE WHEN value ~ '^[0-9]+(\.[0-9]+)?$' THEN value::double precision > 0 ELSE FALSE END);
