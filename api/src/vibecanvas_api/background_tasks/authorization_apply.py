"""Event-driven application of one durable OpenFGA mutation."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from vibecanvas_api.authorization.mutations import (
    AuthzMutationCoordinator,
    AuthzMutationSupersededError,
)
from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
from vibecanvas_api.storage.models_authorization import AuthzMutation
from vibecanvas_api.storage.sync_session import short_admin_session


async def _organization_id(mutation_id: uuid.UUID) -> str:
    async with short_admin_session() as session:
        organization_id = (
            await session.execute(
                select(AuthzMutation.tenant_id).where(
                    AuthzMutation.mutation_id == mutation_id
                )
            )
        ).scalar_one_or_none()
        if organization_id is None:
            raise LookupError(f"authorization mutation {mutation_id} not found")
        return str(organization_id)


async def _apply(mutation_id: uuid.UUID) -> None:
    organization_id = await _organization_id(mutation_id)
    client = openfga_client_from_config()
    try:
        coordinator = AuthzMutationCoordinator(
            client=client,
            organization_id=organization_id,
        )
        try:
            await coordinator.apply_mutation(mutation_id)
        except AuthzMutationSupersededError:
            # A newer edge revision is authoritative; this event is complete.
            return
    finally:
        await client.close()


def apply_authorization_mutation(*, mutation_id: str) -> None:
    """Sync DBOS step entry point; the mutation id is the only payload."""
    asyncio.run(_apply(uuid.UUID(mutation_id)))


__all__ = ["apply_authorization_mutation"]
