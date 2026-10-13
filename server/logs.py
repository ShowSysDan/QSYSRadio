"""Logging: stderr (journald) always, plus a remote syslog server when a host
is set: on the Settings page (admins; stored in app_settings as 'syslog.*'),
else in .env, which gives the defaults:

    SYSLOG_HOST=10.0.0.5        # empty = no remote syslog
    SYSLOG_PORT=514
    SYSLOG_PROTOCOL=udp         # udp | tcp
    SYSLOG_FORMAT=rfc3164       # rfc3164 | rfc5424
    SYSLOG_FACILITY=local0
    SYSLOG_LEVEL=INFO
    SYSLOG_APP_NAME=QSYSRadio

Messages carry the full header (timestamp, hostname, app name) so the
collector can split out the APP column: rfc3164
`<PRI>Mmm dd hh:mm:ss host QSYSRadio[pid]: msg`, rfc5424
`<PRI>1 <ISO ts> host QSYSRadio pid - - msg`; TCP uses newline framing.

Sending happens on a background thread fed by a bounded queue, so a slow or
dead syslog server never blocks a request: when the queue is full messages
are dropped, and a failed TCP connection is retried every 30 s. Each worker
re-reads the saved settings every RELOAD_S (app.py) and rebuilds the sender
only when they changed.
"""
import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from . import config

LOGGER_NAME = 'qsysradio'
log = logging.getLogger(LOGGER_NAME)

# English month names: %b is locale-bound and RFC 3164 needs these exactly.
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
_SEVERITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}
_FACILITIES = logging.handlers.SysLogHandler.facility_names
_RETRY_S, _CONNECT_TIMEOUT_S, _QUEUE_MAX = 30, 2, 10000
RELOAD_S = 10
PREFIX = 'syslog.'
_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')


class _WireFormatter(logging.Formatter):
    def __init__(self, rfc, app, facility):
        super().__init__()
        self.rfc, self.app, self.facility = rfc, app, facility
        self.host = socket.gethostname().split('.')[0] or '-'

    def format(self, record):
        msg = record.getMessage()
        if record.exc_info:
            msg += ' | ' + self.formatException(record.exc_info).replace('\n', ' | ')
        pri = self.facility * 8 + _SEVERITY.get(record.levelno, 6)
        msg = msg.replace('\n', ' ')
        if self.rfc == 'rfc5424':
            ts = datetime.fromtimestamp(record.created, timezone.utc).astimezone()
            return f'<{pri}>1 {ts.isoformat(timespec="milliseconds")} {self.host} {self.app} {os.getpid()} - - {msg}'
        lt = time.localtime(record.created)
        return (f'<{pri}>{_MONTHS[lt.tm_mon - 1]} {lt.tm_mday:2d} {time.strftime("%H:%M:%S", lt)} '
                f'{self.host} {self.app}[{os.getpid()}]: {msg}')


class _Sender(logging.Handler):
    """Writes formatted lines to the syslog server. Runs on the listener thread only."""

    def __init__(self, host, port, tcp):
        super().__init__()
        self.host, self.port, self.tcp = host, port, tcp
        self.sock, self.addr, self.retry_at, self.warned = None, None, 0.0, False

    def _open(self):
        if self.tcp:
            self.sock = socket.create_connection((self.host, self.port), timeout=_CONNECT_TIMEOUT_S)
        else:
            info = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)[0]
            self.sock = socket.socket(info[0], socket.SOCK_DGRAM)
            self.addr = info[4]
        self.sock.settimeout(_CONNECT_TIMEOUT_S)

    def _fail(self, exc):
        if self.sock:
            self.sock.close()
        self.sock, self.retry_at = None, time.monotonic() + _RETRY_S
        if not self.warned:
            sys.stderr.write(f'qsysradio: syslog {self.host}:{self.port} unreachable ({exc}); '
                             f'retrying every {_RETRY_S}s\n')
            self.warned = True

    def emit(self, record):
        if self.sock is None and time.monotonic() < self.retry_at:
            return
        data = self.format(record).encode('utf-8', 'replace')
        try:
            if self.sock is None:
                self._open()
                if self.warned:
                    sys.stderr.write(f'qsysradio: syslog {self.host}:{self.port} reachable again\n')
                    self.warned = False
            if self.tcp:
                self.sock.sendall(data + b'\n')
            else:
                self.sock.sendto(data, self.addr)
        except OSError as exc:
            self._fail(exc)


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


def _env_defaults():
    def pick(key, default, allowed, case=str.lower):
        v = case(config.get(key, default) or default)
        return v if v in allowed else default
    port = config.get_int('SYSLOG_PORT', 514)
    return {
        'host': config.get('SYSLOG_HOST', '') or '',
        'port': port if 1 <= port <= 65535 else 514,
        'protocol': pick('SYSLOG_PROTOCOL', 'udp', ('udp', 'tcp')),
        'format': pick('SYSLOG_FORMAT', 'rfc3164', ('rfc3164', 'rfc5424')),
        'facility': pick('SYSLOG_FACILITY', 'local0', tuple(_FACILITIES)),
        'level': pick('SYSLOG_LEVEL', 'INFO', _LEVELS, str.upper),
        'app': config.get('SYSLOG_APP_NAME', 'QSYSRadio') or 'QSYSRadio',
    }


def spec():
    """The syslog settings for the Settings page, in core_settings.SPEC's shape;
    the defaults are .env's (or the built-in ones)."""
    d = _env_defaults()
    return [
        ('host', 'Syslog server', 'Syslog', 'host', None, None, None, d['host'],
         'IP address or host name of your syslog server. Empty = journald only.'),
        ('port', 'Port', 'Syslog', 'int', 1, 65535, 1, d['port'], 'Usually 514.'),
        ('protocol', 'Protocol', 'Syslog', 'choice', ('udp', 'tcp'), None, None, d['protocol'],
         'UDP (fire and forget) or TCP (newline framed; retried every 30 s if the server is down).'),
        ('format', 'Format', 'Syslog', 'choice', ('rfc3164', 'rfc5424'), None, None, d['format'],
         'rfc3164: "<PRI>Mmm dd hh:mm:ss host app[pid]: msg"; rfc5424: "<PRI>1 ISO-time host app pid - - msg".'),
        ('facility', 'Facility', 'Syslog', 'choice', tuple(sorted(_FACILITIES)), None, None, d['facility'],
         'Syslog facility, e.g. local0.'),
        ('level', 'Lowest level sent', 'Syslog', 'choice', _LEVELS, None, None, d['level'],
         'INFO sends every play and command; WARNING only problems.'),
        ('app', 'App name', 'Syslog', 'name', None, None, None, d['app'], 'The app / tag column on the collector.'),
    ]


def syslog_settings(overrides=None):
    s = _env_defaults()
    s.update({k: v for k, v in (overrides or {}).items() if k in s})
    return s


_lock = threading.Lock()
_current = {'settings': None, 'handler': None, 'listener': None}


def configure(overrides=None):
    """(Re)build the syslog sender for these settings; nothing happens when
    they are unchanged. Returns the settings in use."""
    s = syslog_settings(overrides)
    with _lock:
        if s == _current['settings']:
            return s
        old_handler, old_listener = _current['handler'], _current['listener']
        if old_handler:
            log.removeHandler(old_handler)
        if old_listener:
            old_listener.stop()          # sends what is queued, then ends the thread
        _current.update(settings=s, handler=None, listener=None)
        if s['host']:
            sender = _Sender(s['host'], s['port'], s['protocol'] == 'tcp')
            sender.setFormatter(_WireFormatter(s['format'], s['app'], _FACILITIES[s['facility']]))
            q = queue.Queue(_QUEUE_MAX)
            handler = _DroppingQueueHandler(q)
            handler.setLevel(getattr(logging, s['level'], logging.INFO))
            listener = logging.handlers.QueueListener(q, sender)
            listener.start()
            log.addHandler(handler)
            _current.update(handler=handler, listener=listener)
    if s['host']:
        log.info('syslog -> %s:%s/%s %s facility=%s level=%s', s['host'], s['port'],
                 s['protocol'], s['format'], s['facility'], s['level'])
    elif old_handler:
        log.info('syslog off')
    return s


def setup():
    """Configure the 'qsysradio' logger once per process (syslog from .env
    until the saved settings are read)."""
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    log.propagate = False
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.INFO)
    err.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    log.addHandler(err)
    configure()
    return log
