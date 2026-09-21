"""Periodic durable OpenFGA mutation and relationship-drift reconciliation."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from vibecanvas_api.authorization.openfga_client import (
    openfga_client_from_config,
)
from vibecanvas_api.authorization.projection import (
    ReconcileStats,
    reconcile_all,
)
from vibecanvas_api.storage.sync_session import short_admin_connection


RECONCILE_INTERVAL_SECONDS = 30.0


def reconcile_authorization() -> dict[str, int]:
    """Run the low-frequency PostgreSQL/OpenFGA safety audit."""
    return asyncio.run(_run())


async def _run() -> dict[str, int]:
    # The scheduler may enqueue the next tick while a large tenant inventory is still
    # being reconciled. Without a process-independent singleton guard, every
    # overlapping pass walks all tenants and holds its own database session,
    # eventually starving user mutations such as scheduled Task creation.
    lock_key = "vibecanvas:authorization-reconcile"
    async with short_admin_connection() as connection:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
        ).scalar_one()
        if not acquired:
            stats = ReconcileStats(skipped=1)
            return {
                field: int(getattr(stats, field))
                for field in stats.__dataclass_fields__
            }

        client = openfga_client_from_config()
        try:
            await client.probe()
            stats = await reconcile_all(client)
            return {
                field: int(getattr(stats, field))
                for field in stats.__dataclass_fields__
            }
        finally:
            await client.close()
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
            await connection.commit()
