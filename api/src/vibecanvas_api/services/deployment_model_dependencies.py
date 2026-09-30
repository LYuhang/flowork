"""Delegate newly referenced manual APIs for an accepted Deployment execution.

Following a major can introduce model nodes after deployment creation. Refresh
only dependencies of the host-selected execution snapshot, never caller inputs.
Existing grants remain subject to the model broker's live authorization checks;
this helper does not repair/regrant explicitly revoked existing dependencies.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select, text

from vibecanvas_api.authorization.dependencies import authz_service_for_session
from vibecanvas_api.authorization.mutations import (
    AuthzMutationCoordinator,
    MutationEdge,
)
from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.services.llm_credentials_inject import (
    collect_referenced_credential_names,
)
from vibecanvas_api.services.workflow_model_policy import is_workflow_credential
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.models import User
from vibecanvas_api.storage.models_org import OrgMembership
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo


async def refresh_deployment_model_dependencies(
    *, tenant_id: str, user_id: str, workflow_id: str, execution_id: str,
    service_account_id: str, generation: int, workflow: dict,
) -> None:
    names = collect_referenced_credential_names(workflow)
    from vibecanvas_api.services.workflow_resources import collect_subagent_resources
    if not names and not collect_subagent_resources(workflow):
        return
    client = openfga_client_from_config()
    coordinator = AuthzMutationCoordinator(client=client, organization_id=tenant_id)
    try:
        async with short_session_scope(tenant_id=tenant_id) as session:
            mutations = await _refresh(
                session=session, client=client, coordinator=coordinator,
                tenant_id=tenant_id, user_id=user_id, workflow_id=workflow_id,
                execution_id=execution_id, service_account_id=service_account_id,
                generation=generation, names=names, workflow=workflow,
            )
        # The dependency facts and durable projection intents must commit before
        # the broker sees a capability; never issue a token on projection failure.
        await apply_committed_structural_mutations(coordinator, mutations)
    finally:
        await client.close()


async def _refresh(
    *, session, client, coordinator, tenant_id, user_id, workflow_id,
    execution_id, service_account_id, generation, names, workflow=None,
):
    account_id = uuid.UUID(service_account_id)
    # Serialize dependency additions with disable/rotation and concurrent calls.
    # The invocation must already exist, be active and belong to this exact SA.
    identity = (await session.execute(text("""
        SELECT sa.service_account_id
        FROM deployment_invocations i
        JOIN deployments d ON d.id = i.deployment_id
        JOIN service_accounts sa ON sa.service_account_id = d.service_account_id
        WHERE i.id = :execution_id AND i.status = 'running'
          AND i.wf_id = :workflow_id AND d.wf_id = :workflow_id
          AND d.deleted_at IS NULL AND sa.tenant_id = :tenant_id
          AND sa.service_account_id = :account_id AND sa.status = 'active'
          AND sa.generation = :generation AND sa.created_by = :user_id
          AND sa.owner_resource_type = 'deployment'
          AND sa.owner_resource_id = d.id::text
        FOR UPDATE OF sa
    """), {"execution_id": uuid.UUID(execution_id), "workflow_id": workflow_id,
           "tenant_id": uuid.UUID(tenant_id), "account_id": account_id,
           "generation": generation, "user_id": uuid.UUID(user_id)})).first()
    if identity is None:
        raise PermissionError("deployment_execution_identity_unavailable")
    resource_mutations = ()
    if workflow:
        from vibecanvas_api.services.workflow_resources import collect_subagent_resources
        from vibecanvas_api.services.service_account_resources import bind_workflow_resources
        repo = ServiceAccountsRepo(session)
        existing_resources = set(await repo.resource_refs(account_id, include_revoked=True))
        wanted = {(kind, ref["id"]) for node in collect_subagent_resources(workflow).values()
                  for collection, kind in (("skills", "skill_installation"), ("mcp_servers", "mcp_installation"))
                  for ref in node[collection]}
        if wanted - existing_resources:
            service = authz_service_for_session(session=session, organization_id=tenant_id, openfga_client=client)
            context = AuthzRequestContext(active_organization_id=tenant_id,
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
            principal = PrincipalRef(PrincipalType.SERVICE_ACCOUNT, service_account_id)
            for kind, identifier in ((ResourceType.WORKFLOW, workflow_id),
                                     (ResourceType.DEPLOYMENT_INVOCATION, execution_id)):
                decision = await service.check(principal, Action.EXECUTE,
                    ResourceRef(kind, identifier, tenant_id), context)
                if not decision.allowed:
                    raise PermissionError("deployment_execution_access_revoked")
            bound = set(await bind_workflow_resources(session, tenant_id=uuid.UUID(tenant_id),
                service_account_id=account_id, created_by=user_id, workflow=workflow))
            resource_mutations = await enqueue_structural_delta(
                session=session, coordinator=coordinator, actor_type="service_account",
                actor_id=service_account_id, before=frozenset(),
                after={MutationEdge(tenant_id, kind, identifier, "consumer", "service_account", service_account_id)
                       for kind, identifier in bound - existing_resources},
                operation_id=uuid.uuid4().hex, source="deployment-resource-dependencies",
            )
    rows = await LlmCredentialsRepo(session).list_for_user(user_id)
    eligible = {row["name"]: row for row in rows if is_workflow_credential(row)}
    if names - eligible.keys():
        raise PermissionError("deployment_model_dependency_unavailable")
    repo = ServiceAccountsRepo(session)
    existing = {str(value) for value in await repo.credential_ids(account_id)}
    additions = {str(eligible[name]["id"]) for name in names} - existing
    if not additions:
        return resource_mutations

    # New delegation requires the creator's CURRENT active membership and USE
    # permission, not merely a matching credential name or tenant identifier.
    membership = (await session.execute(select(OrgMembership).join(
        User, User.user_id == OrgMembership.user_id,
    ).where(OrgMembership.user_id == uuid.UUID(user_id),
            OrgMembership.tenant_id == uuid.UUID(tenant_id),
            OrgMembership.status == "active", User.status == "active"))).scalar_one_or_none()
    if membership is None:
        raise PermissionError("deployment_model_dependency_delegation_denied")
    service = authz_service_for_session(
        session=session, organization_id=tenant_id, openfga_client=client,
    )
    context = AuthzRequestContext(
        active_organization_id=tenant_id, request_id=f"deployment-dependencies:{execution_id}",
        membership_id=str(membership.membership_id), membership_role=membership.org_role,
        membership_status=membership.status,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    principal = PrincipalRef(PrincipalType.SERVICE_ACCOUNT, service_account_id)
    for resource_type, resource_id in (
        (ResourceType.WORKFLOW, workflow_id),
        (ResourceType.DEPLOYMENT_INVOCATION, execution_id),
    ):
        decision = await service.check(principal, Action.EXECUTE,
            ResourceRef(resource_type, resource_id, tenant_id), context)
        if not decision.allowed:
            raise PermissionError("deployment_execution_access_revoked")
    for credential_id in sorted(additions):
        decision = await service.check(PrincipalRef(PrincipalType.USER, user_id), Action.USE,
            ResourceRef(ResourceType.LLM_CREDENTIAL, credential_id, tenant_id), context)
        if not decision.allowed:
            raise PermissionError("deployment_model_dependency_delegation_denied")
    for credential_id in sorted(additions):
        await repo.bind_credential(tenant_id=uuid.UUID(tenant_id),
            service_account_id=account_id, credential_id=uuid.UUID(credential_id))
    # Keep old bindings for accepted calls on older snapshots. Their permission
    # is still checked live; never grant every credential in the user's catalog.
    model_mutations = await enqueue_structural_delta(
        session=session, coordinator=coordinator, actor_type="service_account",
        actor_id=service_account_id, before=frozenset(),
        after={MutationEdge(tenant_id, "llm_credential", credential_id,
            "consumer", "service_account", service_account_id) for credential_id in additions},
        operation_id=uuid.uuid4().hex, source="deployment-model-dependencies",
    )

    return (*resource_mutations, *model_mutations)
