"""Bind Workflow-declared model dependencies to an execution identity."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.services.llm_credentials_inject import (
    collect_referenced_credential_names,
    platform_default_model_aliases,
)
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo


async def bind_workflow_credentials(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    service_account_id: uuid.UUID,
    created_by: str,
    workflow: dict,
) -> tuple[str, ...]:
    """Snapshot only referenced saved credentials into the SA dependency set.

    Platform-default aliases are host configuration and do not need a
    per-resource binding. Unknown names remain unbound and fail closed during
    model resolution instead of expanding the account's authority.
    """
    from vibecanvas_api.services.service_account_resources import bind_workflow_resources
    await bind_workflow_resources(session, tenant_id=tenant_id,
        service_account_id=service_account_id, created_by=created_by, workflow=workflow)
    names = (
        collect_referenced_credential_names(workflow)
        - platform_default_model_aliases()
    )
    if not names:
        return ()
    rows = await LlmCredentialsRepo(session).list_for_user(created_by)
    by_name = {
        str(row.get("name") or ""): row
        for row in rows
        if row.get("enabled") and row.get("id")
    }
    repo = ServiceAccountsRepo(session)
    bound: list[str] = []
    for name in sorted(names):
        row = by_name.get(name)
        if row is None:
            continue
        credential_id = uuid.UUID(str(row["id"]))
        await repo.bind_credential(
            tenant_id=tenant_id,
            service_account_id=service_account_id,
            credential_id=credential_id,
        )
        bound.append(str(credential_id))
    return tuple(bound)


__all__ = ["bind_workflow_credentials"]
