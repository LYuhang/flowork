"""One manual-API policy for Workflow discovery, validation and execution.

Chat account connections and platform-default models are intentionally outside
this catalog. Provider names (including OpenRouter) do not determine eligibility.
"""
from __future__ import annotations

from vibecanvas_api.authorization.types import Action, ResourceRef, ResourceType
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo


def is_workflow_credential(row: dict) -> bool:
    return (
        row.get("connection_kind") == "manual"
        and row.get("enabled") is True
        and not row.get("deleted_at")
        and bool(row.get("secret_ref"))
        and bool(str(row.get("name") or "").strip())
        and bool(str(row.get("model_name") or "").strip())
    )


def model_catalog(rows: list[dict]) -> dict[str, dict]:
    """Allowlisted public projection; never serialize connection material."""
    return {
        row["name"]: {
            "provider": row.get("provider"),
            "description": row.get("description"),
            "context_window_tokens": row.get("model_context_tokens"),
        }
        for row in rows if is_workflow_credential(row)
    }


async def workflow_model_catalog_for_user(session, user_id: str, *, service, principal, authz_context) -> dict[str, dict]:
    rows = await LlmCredentialsRepo(session).list_for_user(user_id)
    allowed = []
    for row in rows:
        if not is_workflow_credential(row):
            continue
        decision = await service.check(
            principal, Action.USE,
            ResourceRef(ResourceType.LLM_CREDENTIAL, str(row["id"]), authz_context.active_organization_id),
            authz_context,
        )
        if decision.allowed:
            allowed.append(row)
    return model_catalog(allowed)


async def models_for_context(ctx) -> dict[str, dict]:
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.services.agent_resources.authorization import _service, _principal, _request_context
    from vibecanvas_api.authorization.types import ConsistencyPreference

    async with session_scope(tenant_id=ctx.tenant_id) as session:
        return await workflow_model_catalog_for_user(
            session, ctx.username, service=_service(ctx, session), principal=_principal(ctx),
            authz_context=_request_context(ctx, consistency=ConsistencyPreference.HIGHER_CONSISTENCY),
        )


async def available_workflow_model_ids(ctx) -> set[str]:
    return set(await models_for_context(ctx))
