"""Logging: stderr (journald) always, plus a remote syslog server when
SYSLOG_HOST is set in .env.

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
are dropped, and a failed TCP connection is retried every 30 s.
"""
import logging
import logging.handlers
import os
import queue
import socket
import sys
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


def syslog_settings():
    proto = (config.get('SYSLOG_PROTOCOL', 'udp') or 'udp').lower()
    rfc = (config.get('SYSLOG_FORMAT', 'rfc3164') or 'rfc3164').lower()
    facility = (config.get('SYSLOG_FACILITY', 'local0') or 'local0').lower()
    return {
        'host': config.get('SYSLOG_HOST', ''),
        'port': config.get_int('SYSLOG_PORT', 514),
        'protocol': proto if proto in ('udp', 'tcp') else 'udp',
        'format': rfc if rfc in ('rfc3164', 'rfc5424') else 'rfc3164',
        'facility': facility if facility in _FACILITIES else 'local0',
        'level': (config.get('SYSLOG_LEVEL', 'INFO') or 'INFO').upper(),
        'app': config.get('SYSLOG_APP_NAME', 'QSYSRadio'),
    }


_listener = None


def setup():
    """Configure the 'qsysradio' logger once per process."""
    global _listener
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    log.propagate = False
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.INFO)
    err.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    log.addHandler(err)
    s = syslog_settings()
    if s['host']:
        sender = _Sender(s['host'], s['port'], s['protocol'] == 'tcp')
        sender.setFormatter(_WireFormatter(s['format'], s['app'], _FACILITIES[s['facility']]))
        q = queue.Queue(_QUEUE_MAX)
        handler = _DroppingQueueHandler(q)
        handler.setLevel(getattr(logging, s['level'], logging.INFO))
        log.addHandler(handler)
        _listener = logging.handlers.QueueListener(q, sender)
        _listener.start()
        log.info('syslog -> %s:%s/%s %s facility=%s level=%s', s['host'], s['port'],
                 s['protocol'], s['format'], s['facility'], s['level'])
    return log
