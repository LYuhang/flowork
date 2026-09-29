"""Bounded, request-local phase timings with no resource or user data."""
from __future__ import annotations

import re
from time import perf_counter


class RequestTimings:
    def __init__(self, scope: dict):
        self.scope = scope
        self.previous = perf_counter()

    def mark(self, name: str) -> None:
        # Names are code constants; reject unsafe values before header encoding.
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
            raise ValueError("invalid timing name")
        now = perf_counter()
        phases = self.scope.setdefault("state", {}).setdefault("response_timings", [])
        if len(phases) < 16:
            phases.append((name, (now - self.previous) * 1000))
        self.previous = now
