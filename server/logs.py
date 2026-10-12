"""Logging: stderr (journald) always, plus a remote syslog server when
SYSLOG_HOST is set in .env.

    SYSLOG_HOST=10.0.0.5        # empty = no remote syslog
    SYSLOG_PORT=514
    SYSLOG_PROTOCOL=udp         # udp | tcp
    SYSLOG_FORMAT=rfc3164       # rfc3164 | rfc5424
    SYSLOG_FACILITY=local0
    SYSLOG_LEVEL=INFO
    SYSLOG_APP_NAME=QSYSRadio

The remote handler writes the full header itself (timestamp, hostname, app
name), so the collector can split out the APP column: rfc3164
`<PRI>Mmm dd hh:mm:ss host QSYSRadio[pid]: msg`, rfc5424
`<PRI>1 <ISO ts> host QSYSRadio pid - - msg`. TCP uses newline framing.
A syslog server that is down never breaks the app: sends are dropped and
the connection is retried every 30 s.
"""
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from . import config

LOGGER_NAME = 'qsysradio'
log = logging.getLogger(LOGGER_NAME)

# English month names: %b is locale-bound and RFC 3164 needs these exactly.
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
_RETRY_S = 30


class _WireFormatter(logging.Formatter):
    def __init__(self, rfc, app, tcp):
        super().__init__()
        self.rfc, self.app, self.tcp = rfc, app, tcp
        self.host = socket.gethostname().split('.')[0] or '-'

    def format(self, record):
        msg = record.getMessage()
        if record.exc_info:
            msg += ' | ' + self.formatException(record.exc_info).replace('\n', ' | ')
        msg = msg.replace('\n', ' ')
        if self.rfc == 'rfc5424':
            ts = datetime.fromtimestamp(record.created, timezone.utc).astimezone()
            line = f'1 {ts.isoformat(timespec="milliseconds")} {self.host} {self.app} {os.getpid()} - - {msg}'
        else:
            lt = time.localtime(record.created)
            line = (f'{_MONTHS[lt.tm_mon - 1]} {lt.tm_mday:2d} {time.strftime("%H:%M:%S", lt)} '
                    f'{self.host} {self.app}[{os.getpid()}]: {msg}')
        return line + '\n' if self.tcp else line


class _SysLogHandler(logging.handlers.SysLogHandler):
    broken = False

    def handleError(self, record):   # no traceback spam; the wrapper reconnects
        self.broken = True


class RemoteSyslog(logging.Handler):
    """Builds the SysLogHandler lazily and rebuilds it after a failure."""

    def __init__(self, host, port, tcp, facility, formatter):
        super().__init__()
        self.host, self.port, self.tcp, self.facility = host, port, tcp, facility
        self.wire = formatter
        self._inner = None
        self._retry_at = 0.0
        self._warned = False
        self._build_lock = threading.Lock()

    def _connect(self):
        h = _SysLogHandler(address=(self.host, self.port), facility=self.facility,
                           socktype=socket.SOCK_STREAM if self.tcp else socket.SOCK_DGRAM)
        h.append_nul = False
        h.setFormatter(self.wire)
        return h

    def emit(self, record):
        with self._build_lock:
            if self._inner is None:
                if time.monotonic() < self._retry_at:
                    return
                try:
                    self._inner = self._connect()
                    if self._warned:
                        sys.stderr.write(f'qsysradio: syslog {self.host}:{self.port} reachable again\n')
                    self._warned = False
                except OSError as exc:
                    self._retry_at = time.monotonic() + _RETRY_S
                    if not self._warned:
                        sys.stderr.write(f'qsysradio: syslog {self.host}:{self.port} unreachable ({exc}); '
                                         f'retrying every {_RETRY_S}s\n')
                        self._warned = True
                    return
            inner = self._inner
        inner.emit(record)
        if inner.broken:
            with self._build_lock:
                inner.close()
                if self._inner is inner:
                    self._inner = None
                    self._retry_at = time.monotonic() + _RETRY_S

    def close(self):
        with self._build_lock:
            if self._inner is not None:
                self._inner.close()
                self._inner = None
        super().close()


def syslog_settings():
    host = config.get('SYSLOG_HOST', '')
    proto = (config.get('SYSLOG_PROTOCOL', 'udp') or 'udp').lower()
    rfc = (config.get('SYSLOG_FORMAT', 'rfc3164') or 'rfc3164').lower()
    facility = (config.get('SYSLOG_FACILITY', 'local0') or 'local0').lower()
    return {
        'host': host,
        'port': config.get_int('SYSLOG_PORT', 514),
        'protocol': proto if proto in ('udp', 'tcp') else 'udp',
        'format': rfc if rfc in ('rfc3164', 'rfc5424') else 'rfc3164',
        'facility': facility if facility in logging.handlers.SysLogHandler.facility_names else 'local0',
        'level': (config.get('SYSLOG_LEVEL', 'INFO') or 'INFO').upper(),
        'app': config.get('SYSLOG_APP_NAME', 'QSYSRadio'),
    }


_configured = False


def setup():
    """Configure the 'qsysradio' logger once per process."""
    global _configured
    if _configured:
        return log
    _configured = True
    log.setLevel(logging.DEBUG)
    log.propagate = False
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.INFO)
    err.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    log.addHandler(err)
    s = syslog_settings()
    if s['host']:
        tcp = s['protocol'] == 'tcp'
        remote = RemoteSyslog(s['host'], s['port'], tcp,
                              logging.handlers.SysLogHandler.facility_names[s['facility']],
                              _WireFormatter(s['format'], s['app'], tcp))
        remote.setLevel(getattr(logging, s['level'], logging.INFO))
        log.addHandler(remote)
        log.info('syslog -> %s:%s/%s %s facility=%s level=%s', s['host'], s['port'],
                 s['protocol'], s['format'], s['facility'], s['level'])
    return log
