"""Station settings: defined, documented and validated here, edited on the web
page, pushed to the Core on its next sync.

Only overrides are stored (app_settings key 'core.<name>'); a setting with no
override uses the fallback in radio.lua's SETTINGS table (the `default`
column below must match it). The Core reports the values it is running,
which the settings page shows next to each field.
"""
import hashlib
import json
import math

# name, label, group, type, min, max, step, radio.lua default, help
SPEC = [
    ('music_dir', 'Music folder', 'Library', 'dir', None, None, None, 'Test/Tuning/',
     "Relative to the players' root (Audio/). Changing it rescans the library."),
    ('imaging_dir', 'Imaging folder', 'Library', 'dir', None, None, None, 'Test/Imaging/',
     'Bumpers, stingers and IDs: one pool. Empty = no bumpers.'),
    ('ads_dir', 'Ads folder', 'Library', 'dir', None, None, None, '',
     'Ads (each scheduled on the Library page: mixed into breaks, N per hour, or off). Empty = no ads.'),

    ('bumper_every_n_tracks', 'Bumper every N songs', 'Bumpers & ads', 'int', 0, 50, 1, 4,
     'How many songs between bumpers. 0 = never (Bumper now still works).'),
    ('imaging_gain_db', 'Bumper level', 'Bumpers & ads', 'float', -40, 20, 0.5, 0,
     'Play gain for imaging (dB). Lower it if bumpers are hotter than the music.'),
    ('ad_every_n_tracks', 'Ad break every N songs', 'Bumpers & ads', 'int', 0, 50, 1, 0,
     'How many songs between ad breaks. 0 = only when an hourly ad is owed (Ad break now still works).'),
    ('ads_per_break', 'Ads per break', 'Bumpers & ads', 'int', 1, 10, 1, 2,
     'At most this many ads back to back. Ads owed this hour go first, then mix-ins.'),
    ('ad_gain_db', 'Ad level', 'Bumpers & ads', 'float', -40, 20, 0.5, 0, 'Play gain for ads (dB).'),
    ('bumper_fade_s', 'Song fade under a break', 'Bumpers & ads', 'float', 0, 20, 0.5, 2,
     'How long the outgoing song takes to fade out once a bumper or ad starts (seconds).'),
    ('bumper_tail_s', 'Next song starts under a break', 'Bumpers & ads', 'float', 0, 10, 0.5, 1,
     'The next song starts when the last bumper or ad of a break has this much left (seconds).'),

    ('crossfade_s', 'Crossfade', 'Fades', 'float', 0, 20, 0.5, 4,
     'Outgoing fade when a song ends normally (seconds).'),
    ('fade_in_s', 'Fade in', 'Fades', 'float', 0, 10, 0.5, 0,
     'Incoming song fade-in (seconds; 0 = start at full level).'),
    ('skip_fade_s', 'Skip fade', 'Fades', 'float', 0, 10, 0.5, 1.5,
     'Outgoing fade when an operator skips or plays something now (seconds).'),
    ('silence_fade_s', 'Silence fade', 'Fades', 'float', 0, 10, 0.5, 1,
     'Outgoing fade when trailing silence is detected (seconds).'),
    ('stop_fade_s', 'Stop fade', 'Fades', 'float', 0, 20, 0.5, 2,
     'Fade when the station is stopped from the panel (seconds).'),

    ('play_gain_db', 'Music level', 'Levels', 'float', -40, 20, 0.5, 0, 'Play gain for songs (dB).'),

    ('silence_threshold_dbfs', 'Silence threshold', 'Silence detection', 'float', -90, -10, 1, -50,
     'Below this level (dBFS, at unity gain) audio counts as silence.'),
    ('silence_hold_s', 'Silence hold', 'Silence detection', 'float', 0.5, 15, 0.5, 2,
     'How long it must stay below the threshold (seconds).'),
    ('end_guard_s', 'End guard', 'Silence detection', 'float', 0, 120, 1, 20,
     'Silence only counts within this many seconds of the end of a song.'),

    ('artist_separation', 'Artist separation', 'Rotation', 'int', 0, 20, 1, 2,
     'Avoid repeating an artist within the last N songs.'),
    ('dead_air_s', 'Dead-air guard', 'Safety', 'float', 0.5, 30, 0.5, 1.5,
     'If nothing is playing for this long, start something (seconds).'),
]
BY_NAME = {s[0]: s for s in SPEC}
REQUIRED = {'music_dir'}
PREFIX = 'core.'


def coerce(name, raw):
    """Validate one value from the form. Returns the typed value or raises ValueError."""
    _, label, _, typ, lo, hi, _, _, _ = BY_NAME[name]
    if typ == 'dir':
        if not isinstance(raw, str):
            raise ValueError(f'{label}: must be a folder name')
        v = raw.strip().lstrip('/')
        if '..' in v or '\\' in v or '\x00' in v or len(v) > 200:
            raise ValueError(f'{label}: not a valid folder')
        if not v and name in REQUIRED:
            raise ValueError(f'{label}: required')
        return v + '/' if v and not v.endswith('/') else v
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f'{label}: must be a number')
    try:
        f = float(raw)
    except ValueError:
        raise ValueError(f'{label}: must be a number')
    if not math.isfinite(f):
        raise ValueError(f'{label}: must be a number')
    if typ == 'int':
        if f != int(f):
            raise ValueError(f'{label}: must be a whole number')
        f = int(f)
    if not lo <= f <= hi:
        raise ValueError(f'{label}: must be between {lo} and {hi}')
    return f


def load(cur):
    """{name: value} overrides stored on the server."""
    cur.execute('SELECT key, value FROM app_settings WHERE key LIKE %s', (PREFIX + '%',))
    out = {}
    for r in cur.fetchall():
        name = r['key'][len(PREFIX):]
        if name in BY_NAME:
            try:
                out[name] = json.loads(r['value'])
            except ValueError:
                pass
    return out


def rev(overrides):
    return hashlib.sha1(json.dumps(overrides, sort_keys=True).encode()).hexdigest()[:12]


def save(cur, changes):
    """changes: {name: value or None (= back to the radio.lua default)}."""
    for name, value in changes.items():
        if value is None:
            cur.execute('DELETE FROM app_settings WHERE key = %s', (PREFIX + name,))
        else:
            cur.execute("""INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW())
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                        (PREFIX + name, json.dumps(value)))
