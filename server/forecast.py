"""What is coming up: the next items (songs, ads, bumpers) with estimated
start times, for the dashboard. Built from the Core's reported state (song on
air and time left, operator queue, bumper / ad counters), the ad schedule and
the library, following the rules the Core plays by. A reference only: the
Core decides at each song change (mix-in ads and bumpers are its own random
pick, shown here without a name), and the list is rebuilt on every poll.
"""
from datetime import datetime, timedelta, timezone

from . import ads, core_settings

SONG_GUESS_S = 210         # length assumed for a file never played
SPOT_GUESS_S = 30


def _settings(cur, state):
    s = {n: spec[7] for n, spec in core_settings.BY_NAME.items()}
    s.update(core_settings.load(cur))
    s.update({k: v for k, v in (state.get('settings') or {}).items() if k in s and isinstance(v, (int, float, str))})
    return s


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def forecast(cur, station, state, age_s, n=10):
    """[{kind, key, file, artist, title, why, at (ISO), duration}]: the next n items after the one on air.
    why: queue | rotation | for_ad (song) · hourly | mix | requested (ad) · bumper · stop."""
    if not state.get('automation'):
        return []
    s = _settings(cur, state)
    cur.execute("""SELECT key, kind, file, artist, title, duration_s, trail_s, lead_in_s, last_played FROM library
                   WHERE station = %s AND present AND NOT unsupported""", (station,))
    lib = {r['key']: dict(r) for r in cur.fetchall()}
    sched, hour_start = ads.schedule(cur, station)
    now = datetime.now(timezone.utc)

    def length(key, guess):
        r = lib.get(key) or {}
        return r.get('duration_s') or guess

    def air(key):                      # a song: start to the next item's start (quiet start skipped, crossfade early)
        r = lib.get(key) or {}
        skip = min(s['lead_skip_max_s'], max(0, (r.get('lead_in_s') or 0) - s['lead_skip_keep_s']))
        return length(key, SONG_GUESS_S) - s['crossfade_s'] - (r.get('trail_s') or 0) - skip

    # when the next item starts: the live song's (or break's) time left, less the crossfade and its tail
    decks = [d for d in (state.get('decks') or {}).values() if isinstance(d, dict)]
    live = next((d for d in decks if d.get('live') and d.get('state') == 'playing'), None) or \
        next((d for d in decks if d.get('state') == 'playing'), None)
    if not live or _num(live.get('remaining'), None) is None:
        return []
    left = max(0.0, _num(live.get('remaining')) - age_s)
    if live.get('kind') == 'music':
        left -= s['crossfade_s'] + ((lib.get(live.get('key')) or {}).get('trail_s') or 0)
    else:
        left -= s['bumper_tail_s']
    t = now + timedelta(seconds=max(0.0, left))

    out, played = [], {live.get('key')}
    history = [live.get('artist')] if live.get('artist') else []
    since_bumper, since_ad = int(_num(state.get('tracks_since_bumper'))), int(_num(state.get('tracks_since_ad')))
    queue = [q for q in state.get('queue') or [] if isinstance(q, dict) and q.get('key') in lib]
    taken_slots = set()
    pool = sorted((r for r in lib.values() if r['kind'] == 'music'),
                  key=lambda r: (r['last_played'] is not None, r['last_played'] or now, r['key']))
    ad_req, bumper_req = state.get('ad_requested') or state.get('next_ad'), state.get('bumper_requested') or state.get('next_bumper')
    plan = ads.plan(cur, station)[0]
    server_pick = plan.get('next')                              # what the Core will take for the next song
    pick_why = 'for_ad' if plan.get('next_why') == 'ad' else 'rotation'
    mixes = [k for k, r in sched.items() if r['mode'] == 'mix']
    in_break = live.get('kind') != 'music'

    def add(key, kind, why, dur, generic=None):
        r = lib.get(key) or {}
        out.append({'kind': kind, 'key': key, 'file': r.get('file'), 'artist': r.get('artist'),
                    'title': generic or r.get('title'), 'why': why, 'at': t.isoformat(), 'duration': dur})

    while len(out) < n:
        if state.get('stop_after') and not out:
            out.append({'kind': 'stop', 'why': 'stop', 'title': 'Station stops', 'at': t.isoformat()})
            break
        if not in_break:               # a song change: is a break due here?
            minute = (t - hour_start).total_seconds() / 60
            owed = []
            for key, r in sched.items():
                for i, m in enumerate(r.get('slots', ())):
                    for hour, state_now in ((0, r['states'][i]), (60, 'next')):   # this hour, and the next
                        slot = (key, m + hour)
                        if state_now in ('done', 'missed') or slot in taken_slots:
                            continue
                        if m + hour <= minute <= m + hour + ads.window_minutes(r['per_hour']):
                            owed.append((m + hour, key, slot))
            spots = []
            if ad_req and not out:
                spots.append(('ad', (state.get('next_ad') or {}).get('key'), 'requested'))
            for _, key, slot in sorted(owed):
                taken_slots.add(slot)
                spots.append(('ad', key, 'hourly'))
            if ad_req or (s['ad_every_n_tracks'] > 0 and since_ad >= s['ad_every_n_tracks']):
                spots += [('ad', None, 'mix')] * (int(s['ads_per_break']) - len(spots)) if mixes else []
            spots = spots[:int(s['ads_per_break'])]
            if bumper_req and not out or (s['bumper_every_n_tracks'] > 0 and since_bumper >= s['bumper_every_n_tracks']):
                spots.append(('imaging', (state.get('next_bumper') or {}).get('key') if bumper_req and not out else None,
                              'bumper'))
            for i, (kind, key, why) in enumerate(spots):
                guess = SPOT_GUESS_S
                dur = length(key, guess) if key else guess
                add(key, kind, why, dur, None if key else ('Mix-in ad' if kind == 'ad' else 'Bumper'))
                last = i == len(spots) - 1
                t += timedelta(seconds=max(0, dur - (s['bumper_tail_s'] if last else 0)))
                if kind == 'ad':
                    since_ad = 0
                else:
                    since_bumper = 0
            ad_req = bumper_req = False
        in_break = False
        if len(out) >= n:
            break
        # the song: the operator queue, then the server's pick (least recently played, artist separation)
        if queue:
            key, why = queue.pop(0)['key'], 'queue'
        elif server_pick in lib and server_pick not in played:
            key, why, server_pick = server_pick, pick_why, None
        else:
            recent = {a.lower() for a in history[-int(s['artist_separation']):] if a} if s['artist_separation'] else set()
            pick = next((r for r in pool if r['key'] not in played and (r['artist'] or '').lower() not in recent), None) \
                or next((r for r in pool if r['key'] not in played), None)
            if not pick:
                break
            key, why = pick['key'], 'rotation'
        played.add(key)
        history.append((lib.get(key) or {}).get('artist'))
        add(key, 'music', why, length(key, SONG_GUESS_S))
        t += timedelta(seconds=max(0, air(key)))
        since_bumper, since_ad = since_bumper + 1, since_ad + 1
    return out[:n]
