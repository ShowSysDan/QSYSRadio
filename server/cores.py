"""Each Core: one radio.lua runs every station of a Core and syncs them all in one request a second
(control.core_sync). Kept per Core (CORE_ID, else its design name): its boot, radio.lua version, last
contact and Lua memory, so a leak in radio.lua is reported before it matters.

One sample of the Lua memory (collectgarbage("count")) includes garbage not yet collected: it saws up and
down between collections. So only the lowest sample of each WINDOW_MIN-minute window counts (a collection
cycle runs well within one): the settled low is the lowest window low after the first SETTLE_MIN minutes
of a boot (start-up done), and one WARNING (at most once an hour) says so when a whole window's low stays
above LEAK_FACTOR times that and LEAK_MIN_KB. A new boot starts over.
"""
import logging

SETTLE_MIN = 10
WINDOW_MIN = 10
LEAK_FACTOR = 2
LEAK_MIN_KB = 32768
WARN_EVERY_MIN = 60


def seen(cur, core, boot, version, mem_kb):
    """Record one sync of this Core (inside the caller's transaction). mem_kb: its Lua memory (KB) or None.
    Returns log lines (the leak warning)."""
    cur.execute(f"""SELECT boot, win_at, win_low_kb, mem_low_kb, win_at <= NOW() - INTERVAL '{WINDOW_MIN} minutes' AS over,
                           win_at >= booted_at + INTERVAL '{SETTLE_MIN} minutes' AS settled,
                           mem_warned_at > NOW() - INTERVAL '{WARN_EVERY_MIN} minutes' AS warned
                    FROM cores WHERE core = %s FOR UPDATE""", (core,))
    row = cur.fetchone()
    if row is None:
        cur.execute("""INSERT INTO cores (core, boot, version, mem_kb, win_low_kb) VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (core) DO NOTHING""", (core, boot, version, mem_kb, mem_kb))
        return []
    if row['boot'] != boot:            # restarted: a new start-up, nothing kept from the last boot
        cur.execute("""UPDATE cores SET boot = %s, version = %s, last_seen = NOW(), mem_kb = %s, booted_at = NOW(),
                           win_at = NOW(), win_low_kb = %s, mem_low_kb = NULL, mem_low_at = NULL WHERE core = %s""",
                    (boot, version, mem_kb, mem_kb, core))
        return []
    win, low, lower, warn = row['win_low_kb'], row['mem_low_kb'], False, False
    if row['over'] and row['settled'] and win is not None:     # a whole window after start-up: its lowest sample
        lower = low is None or win < low
        low = win if lower else low
        warn = not row['warned'] and win > LEAK_FACTOR * low and win > LEAK_MIN_KB
    # this sample opens the next window, or is the current window's new lowest
    win_low = mem_kb if row['over'] else min((x for x in (win, mem_kb) if x is not None), default=None)
    cur.execute("""UPDATE cores SET version = %(version)s, last_seen = NOW(), mem_kb = %(mem)s, win_low_kb = %(win_low)s,
                       win_at = CASE WHEN %(over)s THEN NOW() ELSE win_at END, mem_low_kb = %(low)s,
                       mem_low_at = CASE WHEN %(lower)s THEN %(win_at)s ELSE mem_low_at END,
                       mem_warned_at = CASE WHEN %(warn)s THEN NOW() ELSE mem_warned_at END
                   WHERE core = %(core)s""",
                dict(version=version, mem=mem_kb, win_low=win_low, over=row['over'], low=low, lower=lower,
                     win_at=row['win_at'], warn=warn, core=core))
    if not warn:
        return []
    return [(logging.WARNING, f'core={core} Lua memory {win / 1024:.1f} MB at its lowest in {WINDOW_MIN} min, '
                              f'was {low / 1024:.1f} MB after start: possible leak')]
