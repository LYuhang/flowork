"""Cancellation-aware waiting for sandbox process groups."""
from __future__ import annotations
import subprocess
import time


def communicate(proc, *, timeout: float, cancel_event=None):
    if cancel_event is None:
        return proc.communicate(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if cancel_event.is_set() or remaining <= 0:
            # Providers use their existing group kill + communicate + cleanup
            # path for both timeout and cancellation, including grandchildren.
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            return proc.communicate(timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            continue
