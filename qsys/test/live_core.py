#!/usr/bin/env python3
"""Run radio.lua in real time against a real QSYSRadio server, with MockCore
standing in for the Q-SYS runtime (players, meters, mixers, media folders) and
real HTTP for the Core's HttpClient. For demos and end-to-end testing of the web
control panel without a Core.  Needs: pip install lupa

    python3 qsys/test/live_core.py --server http://127.0.0.1:5410 [--token T] [--station Main] [--stations 3] [--zones 2] [--analyzers 1]

Ctrl-C to stop. One simulated Core runs --stations two-player stations (ids
Main, Main-2, Main-3, ...) and --zones one-player stations (background-music
zones: Zone1, Zone2, ...; songs only), all on the same small library of
fictional songs, bumpers and ads with realistic lengths, and --analyzers analysis players
(Analysis_Player, Analysis_Player2, ...; 0 = none: no file analysis); --speed 4
runs the clock four times faster.
"""
import argparse
import os
import queue
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    import lupa.lua53 as lupa
except ImportError:  # pragma: no cover
    raise SystemExit('lupa is required: pip install lupa')

LIBRARY = r'''
local function song(a, t, ext, dur, lead, trail, lvl) return { name = a .. " - " .. t .. "." .. ext, duration = dur, lead = lead or 0.4, trail = trail or 0.6, level = lvl or -10 } end
return {
  ["Music/"] = {
    song("Amber Coast", "Lanterns Over Water", "flac", 214, 1.2, 0.8, -9),
    song("Amber Coast", "Saltwater Static", "mp3", 197),
    song("The Velvet Hours", "Midnight Transit", "flac", 243, 0.3, 4.5, -8),
    song("The Velvet Hours", "Paper Satellites", "mp3", 188),
    song("Northbound Signal", "Copper Wire Heart", "wav", 226, 0.8, 0.5, -11),
    song("Northbound Signal", "Glasshouse", "mp3", 201),
    song("Juniper Fields", "Slow Return", "flac", 259, 2.4, 1.0, -12),
    song("Juniper Fields", "Kite Weather", "mp3", 172),
    song("Marlowe & The Tide", "Blue Hour Radio", "mp3", 231),
    song("Marlowe & The Tide", "Harbor Lights", "flac", 208, 0.5, 6.0, -9),
    song("Sable Avenue", "Neon Orchard", "mp3", 195),
    song("Sable Avenue", "Overpass", "wav", 248),
    song("Cinder & Fern", "Wildflower Static", "flac", 219),
    song("Cinder & Fern", "Second Light", "mp3", 186),
    song("Lowtide Choir", "Echoes in Brass", "mp3", 264, 1.8, 0.4, -13),
    song("Lowtide Choir", "Small Hours", "flac", 202),
    song("Paper Moons", "Cassette Summer", "mp3", 177),
    song("Paper Moons", "Driftwood", "flac", 233),
    song("Ivory Transit", "Mile Marker Nine", "mp3", 211),
    song("Ivory Transit", "Long Way Home", "flac", 256),
    { name = "Old Session - Untitled.wma", duration = 120, unsupported = true, status = "Unsupported file format" },
  },
  ["Imaging/"] = {
    { name = "Station ID - Main.wav", duration = 6, lead = 0.1, trail = 0.2, level = -6 },
    { name = "Station ID - Night.wav", duration = 8, lead = 0.1, trail = 0.3, level = -6 },
    { name = "Promo - Summer Series.wav", duration = 15, lead = 0.2, trail = 0.4, level = -7 },
    { name = "Sweeper - Fast.wav", duration = 4, lead = 0.0, trail = 0.2, level = -5 },
  },
  ["Ads/"] = {
    { name = "Harbor Bakery - Fresh Bread.wav", duration = 30, lead = 0.1, trail = 0.3, level = -7 },
    { name = "City Library - Summer Reading.wav", duration = 15, lead = 0.1, trail = 0.3, level = -7 },
    { name = "Riverside Cafe - Open Late.wav", duration = 30, lead = 0.1, trail = 0.3, level = -7 },
  },
}
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--server', required=True)
    ap.add_argument('--token', default='')
    ap.add_argument('--station', default='Main', help="the first station's id; the others get -2, -3, ...")
    ap.add_argument('--stations', type=int, default=1, help='how many two-player stations this Core runs')
    ap.add_argument('--zones', type=int, default=0, help='how many one-player stations (Zone1, Zone2, ...) it runs too')
    ap.add_argument('--core', default='', help='CFG.CORE_ID ("" = the design name, "mock")')
    ap.add_argument('--analyzers', type=int, default=1, help='analysis players on this Core (0 = no file analysis)')
    ap.add_argument('--speed', type=float, default=1.0)
    args = ap.parse_args()
    ids = [args.station] + [f'{args.station}-{i}' for i in range(2, max(1, args.stations) + 1)]
    zones = [f'Zone{i}' for i in range(1, max(0, args.zones) + 1)]

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    work = tempfile.mkdtemp(prefix='livecore-')
    os.symlink(os.path.join(repo, 'qsys'), os.path.join(work, 'qsys'))
    os.chdir(work)

    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    L.execute('dofile("qsys/test/mock_core.lua")')
    g = L.globals()
    g.LIVE_LIBRARY, g.LIVE_IDS, g.LIVE_N_ANALYZERS = L.execute(LIBRARY), L.table(*ids), max(0, args.analyzers)
    g.LIVE_ZONES = L.table(*zones)
    L.execute('LIVE_WIRING, LIVE_STATIONS = MockCore.stations(#LIVE_IDS, { ids = LIVE_IDS }); '
              'local zw, zc = MockCore.zones(#LIVE_ZONES, { ids = LIVE_ZONES }); '
              'for i = 1, #zw do LIVE_WIRING[#LIVE_WIRING + 1], LIVE_STATIONS[#LIVE_STATIONS + 1] = zw[i], zc[i] end; '
              'LIVE_ANALYZERS = MockCore.analyzers(LIVE_N_ANALYZERS, LIVE_WIRING); '
              'MockCore.setup{ seed = os.time(), dirs = LIVE_LIBRARY, players = LIVE_WIRING }')

    done = queue.Queue()

    def worker(url, data, headers, handler, req):
        r = urllib.request.Request(url, data=data.encode(), headers=headers, method='POST')
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                done.put((handler, req, resp.status, resp.read().decode(), None))
        except urllib.error.HTTPError as e:
            done.put((handler, req, e.code, e.read().decode(errors='replace'), None))
        except Exception as e:  # noqa: BLE001  (timeouts, refused: the Core sees code 0)
            done.put((handler, req, 0, '', str(e)))

    def upload(req):
        headers = dict(req.Headers.items()) if req.Headers else {}
        threading.Thread(target=worker, daemon=True,
                         args=(req.Url, req.Data or '', headers, req.EventHandler, req)).start()

    g.HttpClient = L.table(Upload=upload)
    g.LIVE_SERVER, g.LIVE_TOKEN, g.LIVE_CORE = args.server, args.token, args.core
    # CFG before radio.lua boots; the folders of the simulated library are every station's fallback
    L.execute('MockCore.loadRadio(function(cfg) '
              'cfg.SERVER_URL, cfg.SERVER_TOKEN, cfg.CORE_ID, cfg.STATIONS = LIVE_SERVER, LIVE_TOKEN, LIVE_CORE, LIVE_STATIONS; '
              'cfg.ANALYZERS = LIVE_ANALYZERS; '
              'SETTINGS.music_dir, SETTINGS.imaging_dir, SETTINGS.ads_dir = "Music/", "Imaging/", "Ads/" end)')
    run = L.eval('MockCore.run')
    step = 0.05
    print(f'simulated Core ({", ".join(ids + zones)}; {max(0, args.analyzers)} analysis player(s)) -> {args.server} (Ctrl-C to stop)')
    nxt = time.monotonic()
    try:
        while True:
            while not done.empty():
                handler, req, code, body, err = done.get_nowait()
                handler(req, code, body, err, L.table())
            run(step * args.speed)
            nxt += step
            time.sleep(max(0, nxt - time.monotonic()))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
