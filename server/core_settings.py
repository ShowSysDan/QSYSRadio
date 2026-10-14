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
import re

# name, label, group, type, min, max, step, radio.lua default, help
# (types: int, float, dir, choice (min holds the choices); logs.spec() adds host and name)
SPEC = [
    ('music_dir', 'Music folder', 'Library', 'dir', None, None, None, 'Test/Tuning/',
     "Relative to the players' root (Audio/). Changing it rescans the library."),
    ('imaging_dir', 'Imaging folder', 'Library', 'dir', None, None, None, 'Test/Imaging/',
     'Bumpers, stingers and IDs: one pool. Empty = no bumpers.'),
    ('ads_dir', 'Advertisements folder', 'Library', 'dir', None, None, None, '',
     'Advertisements (each scheduled on the Advertisements page: mixed into breaks, N per hour, or off). Empty = none.'),

    ('bumper_every_n_tracks', 'Bumper every N songs', 'Bumpers & advertisements', 'int', 0, 50, 1, 4,
     'How many songs between bumpers. 0 = never (Bumper now still works).'),
    ('imaging_gain_db', 'Bumper level', 'Bumpers & advertisements', 'float', -40, 20, 0.5, 0,
     'Play gain for imaging (dB). Lower it if bumpers are hotter than the music.'),
    ('ad_every_n_tracks', 'Ad break every N songs', 'Bumpers & advertisements', 'int', 0, 50, 1, 0,
     'How many songs between ad breaks. 0 = only when an hourly ad is owed (Ad break now still works).'),
    ('ads_per_break', 'Advertisements per break', 'Bumpers & advertisements', 'int', 1, 10, 1, 2,
     'At most this many advertisements back to back. Those owed this hour go first, then mix-ins.'),
    ('ad_gain_db', 'Advertisement level', 'Bumpers & advertisements', 'float', -40, 20, 0.5, 0, 'Play gain for advertisements (dB).'),
    ('bumper_fade_s', 'Song fade under a break', 'Bumpers & advertisements', 'float', 0, 20, 0.5, 2,
     'How long the outgoing song takes to fade out once a bumper or ad starts (seconds).'),
    ('bumper_tail_s', 'Next song starts under a break', 'Bumpers & advertisements', 'float', 0, 10, 0.5, 1,
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
    ('lead_skip_max_s', 'Skip quiet start, at most', 'Silence detection', 'float', 0, 10, 0.5, 3,
     'Songs start past their quiet beginning (measured on earlier plays), skipping at most this much '
     '(seconds). 0 = always play from the start.'),
    ('lead_skip_keep_s', 'Keep before the music', 'Silence detection', 'float', 0, 2, 0.1, 0.3,
     'Start this long before the measured first sound, so soft fade-ins are not clipped (seconds).'),
    ('fade_out_early_s', 'Start early over a fade-out, at most', 'Silence detection', 'float', 0, 10, 0.5, 2,
     'When the end of a song, bumper or ad fades away (a ringing note, a dying beat), start the next item up '
     'to this much earlier than its regular crossfade point (seconds). 0 = off.'),
    ('fade_out_drop_db', 'Fade-out level', 'Silence detection', 'float', 6, 40, 1, 20,
     'The end counts as faded once it stays this far below the file\'s own peak for half a second (dB).'),

    ('analyze_at', 'Analyze new files daily at', 'Silence detection', 'choice',
     ('off',) + tuple(f'{h:02d}:00' for h in range(24)), None, None, '04:00',
     'Each day at this time (server time) the Core measures the quiet start and end of every file not analyzed yet, '
     'on the deck that is off air (its RadioOut input muted), between songs. Analyzed figures are final: plays no '
     'longer learn over them. The library pages can also start a run.'),

    ('artist_separation', 'Artist separation', 'Rotation', 'int', 0, 20, 1, 2,
     'Avoid repeating an artist within the last N songs.'),
    ('ident_schedule', 'Station ID schedule', 'Bumpers & advertisements', 'choice',
     ('off', 'hourly', 'half-hourly', 'quarter-hourly'), None, None, 'off',
     'Plays a station ID (an imaging file marked "ID" on the Imaging page; a random one if several) at the first '
     'song change after :00 (hourly), :00 and :30, or every quarter hour, before any advertisements. Not stopped by a bumper pause.'),
    ('ident_window_min', 'Station ID may be late by', 'Bumpers & advertisements', 'int', 1, 15, 1, 8,
     'An ID that cannot play within this many minutes of its time (station stopped, a long song) is skipped.'),
    ('ad_fit_tolerance_s', 'Ad timing tolerance', 'Rotation', 'int', 0, 600, 15, 180,
     'The next song is the fair rotation pick unless it would start a per-hour ad more than this late; only then '
     'is a song picked for its length. Higher = fairer rotation, advertisements a little later. 0 = always pick by length.'),
    ('dead_air_s', 'Dead-air guard', 'Safety', 'float', 0.5, 30, 0.5, 1.5,
     'If nothing is playing for this long, start something (seconds).'),
]
BY_NAME = {s[0]: s for s in SPEC}
REQUIRED = {'music_dir'}
SERVER_ONLY = {'ad_fit_tolerance_s', 'ident_schedule', 'ident_window_min', 'analyze_at'}   # the server's own; not sent to the Core
PREFIX = 'core.'


_HOST = re.compile(r'^[A-Za-z0-9._:\-]{0,253}$')
_NAME = re.compile(r'^[!-~]{1,48}$')


def coerce(name, raw, by_name=None):
    """Validate one value from the form. Returns the typed value or raises ValueError."""
    _, label, _, typ, lo, hi, _, _, _ = (by_name or BY_NAME)[name]
    if typ in ('host', 'name', 'choice'):
        v = raw.strip() if isinstance(raw, str) else None
        if typ == 'host' and (v is None or not _HOST.match(v)):
            raise ValueError(f'{label}: must be an IP address or host name (empty = off)')
        if typ == 'name' and (v is None or not _NAME.match(v)):
            raise ValueError(f'{label}: 1 to 48 characters, no spaces')
        if typ == 'choice' and v not in lo:
            raise ValueError(f'{label}: must be one of {", ".join(lo)}')
        return v
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


def load(cur, prefix=PREFIX, by_name=None):
    """{name: value} overrides stored on the server."""
    cur.execute('SELECT key, value FROM app_settings WHERE key LIKE %s', (prefix + '%',))
    out = {}
    for r in cur.fetchall():
        name = r['key'][len(prefix):]
        if name in (by_name or BY_NAME):
            try:
                out[name] = json.loads(r['value'])
            except ValueError:
                pass
    return out


def for_core(overrides):
    return {k: v for k, v in overrides.items() if k not in SERVER_ONLY}


def rev(overrides):
    return hashlib.sha1(json.dumps(overrides, sort_keys=True).encode()).hexdigest()[:12]


def save(cur, changes, prefix=PREFIX):
    """changes: {name: value or None (= back to the default)}."""
    for name, value in changes.items():
        if value is None:
            cur.execute('DELETE FROM app_settings WHERE key = %s', (prefix + name,))
        else:
            cur.execute("""INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW())
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                        (prefix + name, json.dumps(value)))
