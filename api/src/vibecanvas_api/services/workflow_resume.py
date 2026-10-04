"""Host-only execution identity and fresh authorization after a human wait.

The identity comes from trusted admission code and never from the reviewer or
sandbox. It remains in sandboxd memory; a lost process is not recoverable.
"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

from starlette.requests import Request

from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
from vibecanvas_api.authorization.types import Action, ResourceRef, ResourceType
from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.model_capability import authorization_model_generation
from vibecanvas_api.services.agent_runtime.workflow_mcp_capability import mint_workflow_mcp_capability
from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution
from vibecanvas_api.services.workflow_resources import resolve_workflow_resources
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


IDENTITY_KEY = "_host_execution_identity"


def execution_identity(
    *,
    tenant_id,
    user_id,
    workflow_id,
    execution_id,
    execution_resource_type,
    principal_type="user",
    principal_id=None,
    principal_generation=0,
):
    return dict(
        organization_id=str(tenant_id),
        user_id=str(user_id),
        workflow_id=workflow_id,
        execution_id=execution_id,
        execution_resource_type=execution_resource_type,
        principal_type=principal_type,
        principal_id=principal_id or str(user_id),
        principal_generation=principal_generation,
        authorization_generation=authorization_model_generation(model_id=config.openfga_authorization_model_id),
    )


async def refresh_execution_context(*, tenant_id, execution_id, workflow, context):
    claims = deepcopy(context.get(IDENTITY_KEY))
    if not isinstance(claims, dict) or claims.get("organization_id") != tenant_id:
        raise PermissionError("execution_identity_missing")
    async with short_session_scope(tenant_id=tenant_id) as session:
        run = await WorkflowHistoryRepo(session).get(execution_id)
        if (
            run is None
            or str(run["initiator_user_id"]) != claims.get("user_id")
            or run["wf_id"] != claims.get("workflow_id")
        ):
            raise PermissionError("execution_identity_mismatch")
    client = openfga_client_from_config()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/workflow-resume",
            "headers": [],
            "query_string": b"",
            "app": SimpleNamespace(state=SimpleNamespace(openfga_client=client)),
        }
    )

    async def resolve(*, session, service, principal, authz_context, capability):
        from vibecanvas_api.services.llm_credentials_inject import (
            build_llm_credentials_extra,
            collect_referenced_credential_names,
        )
        from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo
        from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo

        delegated = None
        if claims["principal_type"] == "service_account":
            accounts = ServiceAccountsRepo(session)
            account_id = UUID(claims["principal_id"])
            delegated = set(await accounts.resource_refs(account_id))
            # Model credentials have their own delegation table; the generic
            # resource table contains only Skill and MCP installations.
            delegated.update(
                (str(ResourceType.LLM_CREDENTIAL), str(identifier))
                for identifier in await accounts.credential_ids(account_id)
            )

        async def allowed(kind, identifier):
            if (
                delegated is not None
                and kind != ResourceType.SKILL_REVISION
                and (str(kind), identifier) not in delegated
            ):
                return False
            decision = await service.check(
                principal, Action.USE, ResourceRef(kind, identifier, tenant_id), authz_context
            )
            return decision.allowed

        names = collect_referenced_credential_names(workflow)
        if names:
            for row in await LlmCredentialsRepo(session).list_for_user(claims["user_id"]):
                if row["name"] in names and not await allowed(ResourceType.LLM_CREDENTIAL, str(row["id"])):
                    raise PermissionError("execution_model_access_revoked")
        credentials = await build_llm_credentials_extra(
            workflow,
            session,
            **{key: value for key, value in claims.items() if key != "authorization_generation"},
        )
        resources = deepcopy(context.get("workflow_resources") or {})
        if resources:
            current = await resolve_workflow_resources(
                session=session,
                workflow=workflow,
                service=service,
                principal=principal,
                context=authz_context,
            )
            # Keep immutable Skill versions already mounted for this execution.
            # Revoked optional Skills disappear; revoked selected Skills fail.
            retained = []
            for skill in resources["skills"]:
                if await allowed(ResourceType.SKILL_INSTALLATION, skill["id"]) and await allowed(
                    ResourceType.SKILL_REVISION, skill["revision_id"]
                ):
                    retained.append(skill)
            ids = {skill["id"] for skill in retained}
            if any(skill["id"] not in ids for node in resources["nodes"].values() for skill in node["skills"]):
                raise PermissionError("execution_skill_access_revoked")
            resources["skills"] = retained
            resources["mcp_servers"] = current["mcp_servers"]
            for server in resources["mcp_servers"]:
                server["broker_url"] = (
                    config.mcp.platform_internal_base_url.rstrip("/") + f"/api/internal/workflow-mcp/v1/{server['id']}"
                )
                server["capability"] = mint_workflow_mcp_capability(
                    **claims,
                    server_id=server["id"],
                    tools_fingerprint=server["tools_fingerprint"],
                    secret=config.signing_secret,
                    ttl_s=config.mcp.runtime_model_capability_ttl_s,
                )
            servers = {server["id"]: server for server in resources["mcp_servers"]}
            for node in resources["nodes"].values():
                node["mcp_servers"] = [dict(servers[server["id"]]) for server in node["mcp_servers"]]
        return {"llm_credentials": credentials, "workflow_resources": resources}

    try:
        return await authorize_workflow_execution(request, SimpleNamespace(**claims), resolve=resolve)
    finally:
        await client.close()
