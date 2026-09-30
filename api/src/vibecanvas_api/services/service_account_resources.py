"""Explicit delegation of workflow Skill/MCP installations to durable actors."""
from __future__ import annotations

from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.authorization.dependencies import authz_service_for_session
from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
from vibecanvas_api.authorization.types import Action, AuthzRequestContext, ConsistencyPreference, PrincipalRef, PrincipalType, ResourceRef, ResourceType
from vibecanvas_api.services.workflow_resources import collect_subagent_resources
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
from vibecanvas_api.storage.repo_skills import SkillsRepo
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo


async def bind_workflow_resources(session, *, tenant_id: UUID, service_account_id: UUID,
                                  created_by: str, workflow: dict) -> tuple[tuple[str, str], ...]:
    nodes = collect_subagent_resources(workflow)
    wanted = {(kind, ref["id"]) for node in nodes.values()
              for collection, kind in (("skills", "skill_installation"), ("mcp_servers", "mcp_installation"))
              for ref in node[collection]}
    if not wanted:
        return ()
    membership = (await session.execute(text("""SELECT m.membership_id,m.org_role,m.status
        FROM org_memberships m JOIN users u ON u.user_id=m.user_id
        WHERE m.user_id=:user AND m.tenant_id=:tenant AND m.status='active' AND u.status='active'"""),
        {"user": UUID(created_by), "tenant": tenant_id})).mappings().one_or_none()
    if membership is None:
        raise HTTPException(403, "workflow_resource_delegation_denied")
    client = openfga_client_from_config()
    try:
        service = authz_service_for_session(session=session, organization_id=str(tenant_id), openfga_client=client)
        context = AuthzRequestContext(active_organization_id=str(tenant_id),
            membership_id=str(membership["membership_id"]), membership_role=membership["org_role"],
            membership_status=membership["status"], consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        for kind, identifier in sorted(wanted):
            decision = await service.check(PrincipalRef(PrincipalType.USER, created_by), Action.USE,
                ResourceRef(ResourceType(kind), identifier, str(tenant_id)), context)
            if not decision.allowed:
                raise HTTPException(403, "workflow_resource_delegation_denied")
            repo = SkillsRepo(session) if kind == "skill_installation" else McpServersRepo(session)
            row = await repo.get(UUID(identifier))
            if row is None or (not row.get("revision_hash") if kind == "skill_installation" else not row.get("enabled")):
                raise HTTPException(422, "workflow_resource_unavailable")
        accounts = ServiceAccountsRepo(session)
        for kind, identifier in sorted(wanted):
            await accounts.bind_resource(tenant_id=tenant_id, service_account_id=service_account_id,
                resource_type=kind, resource_id=UUID(identifier))
        return await accounts.resource_refs(service_account_id)
    finally:
        await client.close()


async def delegate_new_resources(*, session, coordinator, tenant_id: str,
                                 service_account_id: str, created_by: str, workflow: dict):
    """Add only previously unseen dependencies; never restore revoked grants.

    Caller fences the owning resource and account, commits the returned durable
    projection intent, then applies it before resolving an execution snapshot.
    Existing grants remain available for accepted older executions.
    """
    from uuid import uuid4
    from vibecanvas_api.authorization.mutations import MutationEdge
    from vibecanvas_api.authorization.projection import enqueue_structural_delta

    account_id = UUID(service_account_id)
    accounts = ServiceAccountsRepo(session)
    existing = set(await accounts.resource_refs(account_id, include_revoked=True))
    nodes = collect_subagent_resources(workflow)
    additions = {}
    for node_id, refs in nodes.items():
        config = {}
        for collection, kind in (("skills", "skill_installation"), ("mcp_servers", "mcp_installation")):
            config[collection] = [ref for ref in refs[collection] if (kind, ref["id"]) not in existing]
        if any(config.values()):
            additions[node_id] = {"node_type": "SubAgentNode", "node_config": config}
    if not additions:
        return ()
    bound = set(await bind_workflow_resources(session, tenant_id=UUID(tenant_id),
        service_account_id=account_id, created_by=created_by, workflow=additions))
    edges = {MutationEdge(tenant_id, kind, identifier, "consumer", "service_account", service_account_id)
             for kind, identifier in bound - existing}
    if not edges:
        return ()
    return await enqueue_structural_delta(session=session, coordinator=coordinator,
        actor_type="service_account", actor_id=service_account_id, before=frozenset(),
        after=edges, operation_id=uuid4().hex, source="workflow-resource-dependencies")


async def refresh_scheduled_resources(*, tenant_id: str, user_id: str, workflow_id: str,
                                      execution_id: str, service_account_id: str,
                                      generation: int, workflow: dict) -> None:
    """Delegate new refs from the accepted schedule snapshot, before execution.

    Uses the live execution gate and verifies the durable snapshot. Browser
    inputs cannot introduce resources into an already accepted execution.
    """
    if not collect_subagent_resources(workflow):
        return
    from types import SimpleNamespace
    from starlette.requests import Request
    from vibecanvas_api.authorization.mutations import AuthzMutationCoordinator
    from vibecanvas_api.authorization.projection import apply_committed_structural_mutations
    from vibecanvas_api.config import config
    from vibecanvas_api.services.agent_runtime.model_capability import authorization_model_generation
    from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    client = openfga_client_from_config()
    coordinator = AuthzMutationCoordinator(client=client, organization_id=tenant_id)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/internal/schedule-resources',
        'headers': [], 'query_string': b'',
        'app': SimpleNamespace(state=SimpleNamespace(openfga_client=client))})
    claims = SimpleNamespace(organization_id=tenant_id, user_id=user_id, workflow_id=workflow_id,
        execution_id=execution_id, execution_resource_type='task_execution',
        principal_type='service_account', principal_id=service_account_id,
        principal_generation=generation,
        authorization_generation=authorization_model_generation(model_id=config.openfga_authorization_model_id))

    async def resolve(*, session, **unused):
        account = (await session.execute(text('''SELECT service_account_id FROM service_accounts
            WHERE service_account_id=:id AND tenant_id=:tenant AND status='active'
              AND generation=:generation AND created_by=:creator FOR UPDATE'''),
            {'id': UUID(service_account_id), 'tenant': UUID(tenant_id),
             'generation': generation, 'creator': UUID(user_id)})).first()
        if account is None:
            raise PermissionError('scheduled_resource_identity_unavailable')
        execution = await TasksRepo(session).get_scheduled_execution(UUID(execution_id))
        if (execution is None or execution.status != 'running'
                or execution.workflow_id != workflow_id
                or (execution.workflow_snapshot or {}).get('workflow') != workflow):
            raise PermissionError('scheduled_resource_snapshot_mismatch')
        return await delegate_new_resources(session=session, coordinator=coordinator,
            tenant_id=tenant_id, service_account_id=service_account_id,
            created_by=user_id, workflow=workflow)

    try:
        mutations = await authorize_workflow_execution(request, claims, resolve=resolve)
        # The shared gate's database context has committed before projection.
        await apply_committed_structural_mutations(coordinator, mutations)
    finally:
        await client.close()
