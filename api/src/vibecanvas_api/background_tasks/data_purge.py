"""Periodic consumer for durable account erasure jobs."""
from __future__ import annotations

import asyncio

from vibecanvas_api.config import config
from vibecanvas_api.security.purge import run_one_due_purge


def run_due() -> dict[str, object]:
    if not config.purge_worker_enabled:
        return {"status": "disabled", "processed": False}
    processed = asyncio.run(run_one_due_purge())
    return {"status": "ok", "processed": processed}
