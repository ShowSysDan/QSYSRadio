"""Server clock: the time the web panel shows, and an optional NTP check of it.

Set an NTP server on the Settings page (Server settings; NTP_SERVER in .env
is the default). Each worker asks it every CHECK_S on a background thread
(SNTP, one UDP packet, 2 s timeout) and keeps the measured offset; the
header clock turns red when the server is off by more than the allowed error.
"""
import os
import socket
import struct
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, logs

PREFIX = 'clock.'
CHECK_S = 600
_NTP_EPOCH = 2208988800            # 1900-01-01 to 1970-01-01


def spec():
    """In core_settings.SPEC's shape; defaults from .env."""
    return [
        ('ntp_server', 'NTP server', 'Clock', 'host', None, None, None, config.get('NTP_SERVER', '') or '',
         "Checks this server's clock every 10 min (e.g. pool.ntp.org or your domain controller). Empty = no check."),
        ('ntp_max_offset_s', 'Allowed clock error', 'Clock', 'float', 0.1, 60, 0.1, 1.0,
         'The header clock turns red when the server is off by more than this (seconds).'),
    ]


def sntp_offset(host, port=123, timeout=2.0):
    """Seconds to add to this machine's clock to get the NTP server's time."""
    packet = b'\x1b' + 47 * b'\0'                      # LI 0, version 3, mode 3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t1 = time.time()
        s.sendto(packet, (host, port))
        data, _ = s.recvfrom(48)
        t4 = time.time()
    if len(data) < 48:
        raise OSError('short NTP reply')

    def ts(i):
        sec, frac = struct.unpack('!II', data[i:i + 8])
        return sec - _NTP_EPOCH + frac / 2 ** 32
    t2, t3 = ts(32), ts(40)
    if t3 <= 0:
        raise OSError('NTP server sent no time')
    return ((t2 - t1) + (t3 - t4)) / 2


_lock = threading.Lock()
_state = {'server': '', 'max': 1.0, 'offset_s': None, 'checked_at': None, 'error': None, 'warned': False}
_wake = threading.Event()
_thread = None


def _check():
    with _lock:
        host = _state['server']
    if not host:
        return
    try:
        offset, error = sntp_offset(host), None
    except OSError as exc:
        offset, error = None, f'{type(exc).__name__}: {exc}'.strip()
    with _lock:
        if _state['server'] != host:
            return                                      # changed meanwhile
        _state.update(offset_s=offset, checked_at=time.time(), error=error)
        bad = error is not None or abs(offset) > _state['max']
        warn, _state['warned'] = bad and not _state['warned'], bad
    if warn:
        logs.log.warning('clock: %s', f'NTP {host} unreachable ({error})' if error else
                         f'server clock is {offset:+.3f} s off (NTP {host}); check the server time sync')


def _loop():
    while True:
        _check()
        _wake.wait(CHECK_S)
        _wake.clear()


def configure(overrides=None):
    """Apply the saved settings; (re)check now when the NTP server changed."""
    global _thread
    s = {name: default for name, *_, default, _help in spec()}
    s.update({k: v for k, v in (overrides or {}).items() if k in s})
    with _lock:
        changed = s['ntp_server'] != _state['server']
        _state.update(server=s['ntp_server'], max=float(s['ntp_max_offset_s']))
        if changed:
            _state.update(offset_s=None, checked_at=None, error=None, warned=False)
    if s['ntp_server'] and (_thread is None or not _thread.is_alive() or _thread.pid != os.getpid()):
        _thread = threading.Thread(target=_loop, name='qsysradio-ntp', daemon=True)
        _thread.pid = os.getpid()                         # a forked worker needs its own thread
        _thread.start()
    elif changed:
        _wake.set()
    return s


def _zone():
    name = config.get('TIMEZONE')
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def status():
    """For the header clock: server time (epoch ms), its zone, and the NTP check."""
    now = time.time()
    local = datetime.fromtimestamp(now, _zone()) if _zone() else datetime.fromtimestamp(now).astimezone()
    with _lock:
        st = dict(_state)
    ok = None
    if st['server'] and st['checked_at']:
        ok = st['error'] is None and abs(st['offset_s']) <= st['max']
    return {'server_ms': round(now * 1000), 'utc_offset_min': round(local.utcoffset().total_seconds() / 60),
            'zone': local.tzname(), 'ntp': {
                'server': st['server'] or None, 'ok': ok, 'error': st['error'], 'max_s': st['max'],
                'offset_s': None if st['offset_s'] is None else round(st['offset_s'], 3),
                'checked_ago_s': None if not st['checked_at'] else round(now - st['checked_at'])}}
