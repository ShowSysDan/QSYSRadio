"""What is coming up: the next items (songs, ads, bumpers) with estimated
start times, for the dashboard. Built from the Core's reported state (song on
air and time left, operator queue, bumper / ad counters), the ad schedule and
the library, following the rules the Core plays by. A reference only: the
Core decides at each song change (mix-in ads and bumpers are its own random
pick, shown here without a name), and the list is rebuilt on every poll.
"""
from datetime import datetime, timedelta, timezone

from . import ads, breaks, core_settings

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
    why: queue | rotation | for_ad (song) · hourly | mix | requested (ad) · bumper | ident · stop.
    Holds and the station ID schedule are followed."""
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

    song_air = ads.airtime({n: float(s[n]) for n in ('crossfade_s', 'lead_skip_max_s', 'lead_skip_keep_s')})

    def air(key):                      # a song: start to the next item's start (quiet start skipped, crossfade early)
        r = lib.get(key) or {}
        return song_air(dict(r, duration_s=length(key, SONG_GUESS_S), lead_in_s=r.get('lead_in_s'), trail_s=r.get('trail_s')))

    hold = breaks.holds(cur, station)
    ids = breaks.idents(cur, station)
    every = breaks.EVERY_MIN.get(s['ident_schedule'], 0) if ids['keys'] else 0
    taken_idents = set()

    def ident_slot(at):                # the ID slot open at `at` and not yet played, as minutes past hour_start, or None
        if not every:
            return None
        minute = (at - hour_start).total_seconds() / 60
        m = minute // every * every
        if minute - m > ids['window_s'] / 60 or m in taken_idents:
            return None
        if at - timedelta(minutes=minute - m) <= now and not ids['due']:
            return None                # the slot already open now: owed only if the server says so (not played yet)
        return m

    def next_slot(at):                 # the next ad / ID slot opening after `at`: (datetime, window s) or None
        minute = (at - hour_start).total_seconds() / 60
        later = [] if breaks.held(hold.get('ads'), at) else [
            (m + hour, ads.window_minutes(r['per_hour']) * 60) for key, r in sched.items()
            for m, st in zip(r.get('slots', ()), r.get('states', ()))
            for hour in (0, 60) if (hour or st not in ('done', 'missed')) and m + hour > minute
            and (key, m + hour) not in taken_slots]
        if every:
            later.append((minute // every * every + every, ids['window_s']))
        if not later:
            return None
        m, window = min(later)
        return hour_start + timedelta(minutes=m), window

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
            if breaks.held(hold.get('ads'), t):
                owed = []                      # held: nothing owed or mixed in (the slots are skipped)
            for _, key, slot in sorted(owed):
                taken_slots.add(slot)
                spots.append(('ad', key, 'hourly'))
            if (ad_req and not out) or (s['ad_every_n_tracks'] > 0 and since_ad >= s['ad_every_n_tracks']
                                        and not breaks.held(hold.get('ads'), t)):
                spots += [('ad', None, 'mix')] * (int(s['ads_per_break']) - len(spots)) if mixes else []
            spots = spots[:int(s['ads_per_break'])]
            id_m = ident_slot(t)
            if id_m is not None:               # the station ID: first, and instead of the regular bumper
                taken_idents.add(id_m)
                due_now = t - timedelta(minutes=(t - hour_start).total_seconds() / 60 - id_m) <= now
                spots.insert(0, ('imaging', ids['due'] if due_now else None, 'ident'))
            elif bumper_req and not out or (s['bumper_every_n_tracks'] > 0 and since_bumper >= s['bumper_every_n_tracks']
                                            and not breaks.held(hold.get('bumpers'), t)):
                spots.append(('imaging', (state.get('next_bumper') or {}).get('key') if bumper_req and not out else None,
                              'bumper'))
            for i, (kind, key, why) in enumerate(spots):
                guess = SPOT_GUESS_S
                dur = length(key, guess) if key else guess
                add(key, kind, why, dur, None if key else {'ad': 'Mix-in ad', 'ident': 'Station ID'}.get(
                    kind if kind == 'ad' else why, 'Bumper'))
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
            eligible = [r for r in pool if r['key'] not in played and (r['artist'] or '').lower() not in recent] \
                or [r for r in pool if r['key'] not in played]
            if not eligible:
                break
            # as the server will pick when it gets there: by length for an ad slot within two songs, else the oldest
            slot = next_slot(t)
            share = lambda f: eligible[:max(1, min(ads.POOL, int(len(eligible) * f)))]
            timed = lambda pool: [(r['key'], air(r['key'])) for r in pool if r['duration_s']]
            key, why = ads.choose(eligible[0]['key'], timed(share(0.25)), timed(share(0.5)),
                                  (slot[0] - t).total_seconds() if slot else None, float(s['ad_fit_tolerance_s']),
                                  slot[1] if slot else 0)
            why = 'for_ad' if why == 'ad' else why
        played.add(key)
        history.append((lib.get(key) or {}).get('artist'))
        add(key, 'music', why, length(key, SONG_GUESS_S))
        t += timedelta(seconds=max(0, air(key)))
        since_bumper, since_ad = since_bumper + 1, since_ad + 1
    return out[:n]
