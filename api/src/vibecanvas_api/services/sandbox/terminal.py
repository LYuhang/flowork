"""Bounded interactive PTYs owned by the in-sandbox supervisor process.

This module must only be dispatched inside the sandbox. The host authenticates
the connection and supplies an opaque per-connection ID; no host shell is used.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

_lock = threading.RLock()
_sessions: dict[str, dict] = {}
_janitor_started = False
_IDLE_SECONDS = 120


def _close(identifier: str):
    session = _sessions.pop(identifier, None)
    if session is None:
        return
    proc = session['process']
    try:
        os.close(session['master'])
    except OSError:
        pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGHUP)
            proc.wait(timeout=1)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=1)


def _reap():
    while True:
        time.sleep(10)
        with _lock:
            for identifier, session in list(_sessions.items()):
                if time.monotonic() - session['touched'] > _IDLE_SECONDS:
                    _close(identifier)


def _resize(master: int, columns, rows):
    if type(columns) is not int or type(rows) is not int or not 2 <= columns <= 500 or not 1 <= rows <= 200:
        raise ValueError('invalid_terminal_dimensions')
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))


def terminal_operation(op: dict) -> dict:
    global _janitor_started
    identifier = str(uuid.UUID(str(op.get('terminal_id', ''))))
    action = op.get('action')
    with _lock:
        if action == 'close':
            _close(identifier)
            return {'ok': True}
        if action == 'open':
            if identifier in _sessions:
                raise ValueError('terminal_already_open')
            if len(_sessions) >= 4:
                raise RuntimeError('terminal_capacity_exhausted')
            master, slave = pty.openpty()
            proc = None
            try:
                _resize(master, op.get('columns', 80), op.get('rows', 24))
                # Popen's built-in setsid runs safely before the interpreter;
                # the child claims its PTY, then replaces itself with Bash.
                launcher = 'import fcntl,termios,os; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv("/bin/bash",["bash","--noprofile","--norc","-i"])'
                proc = subprocess.Popen([sys.executable, '-c', launcher],
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                    cwd='/run' if os.path.isdir('/run') else '/',
                    env={**os.environ, 'TERM': 'xterm-256color', 'PS1': r'\w $ '})
                os.set_blocking(master, False)
                _sessions[identifier] = {'process': proc, 'master': master, 'touched': time.monotonic()}
            except BaseException:
                os.close(master)
                if proc is not None:
                    proc.kill()
                    proc.wait()
                raise
            finally:
                os.close(slave)
            if not _janitor_started:
                threading.Thread(target=_reap, daemon=True, name='terminal-reaper').start()
                _janitor_started = True
            return {'ok': True, 'terminal_id': identifier}
        session = _sessions.get(identifier)
        if session is None:
            return {'ok': False, 'error': 'terminal_closed'}
        session['touched'] = time.monotonic()
        master, proc = session['master'], session['process']
        if action == 'resize':
            _resize(master, op.get('columns'), op.get('rows'))
            return {'ok': True}
        if action == 'write':
            encoded = op.get('data', '')
            if not isinstance(encoded, str) or len(encoded) > 24_000:
                raise ValueError('terminal_input_too_large')
            data = base64.b64decode(encoded, validate=True)
            try:
                written = os.write(master, data)
            except BlockingIOError:
                written = 0
            return {'ok': True, 'written': written}
        if action == 'read':
            try:
                data = os.read(master, 32_768)
            except BlockingIOError:
                data = b''
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b''
            return {'ok': True, 'data': base64.b64encode(data).decode('ascii'), 'exit_code': None if data else proc.poll()}
        raise ValueError('unknown_terminal_operation')
