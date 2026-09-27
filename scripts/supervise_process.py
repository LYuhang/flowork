#!/usr/bin/env python3
"""Keep one critical child process alive until the supervisor is stopped.

The script stays in the foreground so the existing daemonizer can own its PID
file. SIGINT/SIGTERM are forwarded to the child's process group; unexpected
exits are restarted with bounded exponential backoff.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


class ProcessSupervisor:
    def __init__(
        self,
        command: list[str],
        *,
        min_backoff_s: float = 0.5,
        max_backoff_s: float = 10.0,
        stable_after_s: float = 30.0,
        stop_timeout_s: float = 30.0,
    ) -> None:
        self.command = command
        self.min_backoff_s = max(0.05, min_backoff_s)
        self.max_backoff_s = max(self.min_backoff_s, max_backoff_s)
        self.stable_after_s = max(1.0, stable_after_s)
        self.stop_timeout_s = max(0.0, stop_timeout_s)
        self.stop_deadline: float | None = None
        self.child: subprocess.Popen | None = None
        self.stopping = False

    def request_stop(self, _signum: int, _frame: object) -> None:
        self.stopping = True
        if self.stop_deadline is None:
            self.stop_deadline = time.monotonic() + self.stop_timeout_s
        child = self.child
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _wait_backoff(self, delay: float) -> None:
        deadline = time.monotonic() + delay
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        delay = self.min_backoff_s
        while not self.stopping:
            started_at = time.monotonic()
            self.child = subprocess.Popen(
                self.command,
                env=os.environ,
                start_new_session=True,
            )
            # A stop can arrive between the loop guard and Popen completing.
            if self.stopping:
                self.request_stop(signal.SIGTERM, None)
            while True:
                try:
                    return_code = self.child.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if self.stop_deadline is not None and time.monotonic() >= self.stop_deadline:
                        try:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return_code = self.child.wait()
                        break
            runtime = time.monotonic() - started_at
            self.child = None
            if self.stopping:
                return 0
            print(
                "supervisor: child exited unexpectedly "
                f"(code={return_code}, runtime={runtime:.2f}s); "
                f"restarting in {delay:.2f}s",
                file=sys.stderr,
                flush=True,
            )
            if runtime >= self.stable_after_s:
                delay = self.min_backoff_s
            self._wait_backoff(delay)
            delay = min(self.max_backoff_s, delay * 2)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-backoff", type=float, default=0.5)
    parser.add_argument("--max-backoff", type=float, default=10.0)
    parser.add_argument("--stable-after", type=float, default=30.0)
    parser.add_argument("--stop-timeout", type=float, default=30.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    return ProcessSupervisor(
        command,
        min_backoff_s=args.min_backoff,
        max_backoff_s=args.max_backoff,
        stable_after_s=args.stable_after,
        stop_timeout_s=args.stop_timeout,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
