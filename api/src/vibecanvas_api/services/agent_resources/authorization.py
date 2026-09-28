"""Host-owned resource authorization shared by CLI and render services."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
    scope_authz_service,
)
from vibecanvas_api.authorization.mutations import (
    AuthzMutationCoordinator,
)
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaUnavailableError,
)
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    resource_root_edges,
)
from vibecanvas_api.authorization.service import (
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    Decision,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models_agent_runs import AgentRun
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


@dataclass(frozen=True, slots=True)
class AuthorizedWorkflowSnapshot:
    meta: dict[str, Any]
    workflow: dict[str, Any]
    decision: Decision


def _principal(ctx) -> PrincipalRef:
    user_id = str(getattr(ctx, "username", "") or "").strip()
    if not user_id:
        raise ToolError(
            "permission_denied",
            "The resource is unavailable or access is denied.",
        )
    return PrincipalRef(PrincipalType.USER, user_id)


def _request_context(
    ctx,
    *,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthzRequestContext:
    organization_id = str(getattr(ctx, "tenant_id", "") or "").strip()
    return AuthzRequestContext(
        active_organization_id=organization_id,
        request_id=f"agent-resource:{getattr(ctx, 'turn_id', '')}",
        session_id=str(
            getattr(ctx, "authorization_session_id", "") or ""
        ),
        session_generation=int(
            getattr(ctx, "authorization_session_generation", 0) or 0
        ),
        membership_id=str(
            getattr(ctx, "authorization_membership_id", "") or ""
        ),
        membership_role=str(
            getattr(ctx, "authorization_membership_role", "") or ""
        ),
        membership_status=str(
            getattr(ctx, "authorization_membership_status", "") or ""
        ),
        authentication_strength=str(
            getattr(
                ctx,
                "authorization_authentication_strength",
                "",
            )
            or ""
        ),
        consistency=consistency,
    )


def agent_auth_context(ctx) -> AuthContext:
    return AuthContext(
        user_id=str(getattr(ctx, "username", "") or ""),
        tenant_id=str(getattr(ctx, "tenant_id", "") or ""),
        email="",
        active_organization_id=str(getattr(ctx, "tenant_id", "") or ""),
        membership_id=str(
            getattr(ctx, "authorization_membership_id", "") or ""
        ),
        membership_role=str(
            getattr(ctx, "authorization_membership_role", "") or ""
        ),
        membership_status=str(
            getattr(ctx, "authorization_membership_status", "") or ""
        ),
        session_generation=int(
            getattr(ctx, "authorization_session_generation", 0) or 0
        ),
        authentication_strength=str(
            getattr(ctx, "authorization_authentication_strength", "") or ""
        ),
        session_id=str(getattr(ctx, "authorization_session_id", "") or ""),
        session_audience=str(
            getattr(ctx, "authorization_session_audience", "web") or "web"
        ),
        privileged_access_request_id=str(
            getattr(
                ctx,
                "authorization_privileged_access_request_id",
                "",
            )
            or ""
        ),
        privileged_resource_type=str(
            getattr(ctx, "authorization_privileged_resource_type", "") or ""
        ),
        privileged_resource_id=str(
            getattr(ctx, "authorization_privileged_resource_id", "") or ""
        ),
        privileged_actions=frozenset(
            getattr(ctx, "authorization_privileged_actions", ()) or ()
        ),
        privileged_expires_at=getattr(
            ctx,
            "authorization_privileged_expires_at",
            None,
        ),
    )


def _workflow_resource(ctx, workflow_id: str) -> ResourceRef:
    return ResourceRef(
        ResourceType.WORKFLOW,
        workflow_id,
        str(getattr(ctx, "tenant_id", "") or ""),
    )


def _chat_resource(ctx) -> ResourceRef:
    return ResourceRef(
        ResourceType.CHAT,
        str(getattr(ctx, "chat_id", "") or ""),
        str(getattr(ctx, "tenant_id", "") or ""),
    )


def _service(ctx, session) -> AuthzService:
    service = authz_service_for_session(
        session=session,
        organization_id=str(getattr(ctx, "tenant_id", "") or ""),
        openfga_client=getattr(ctx, "authorization_client", None),
    )
    return scope_authz_service(
        service,
        session=session,
        auth=agent_auth_context(ctx),
    )


def _permission_error() -> ToolError:
    # Preserve the same non-enumerating contract as direct HTTP object routes.
    return ToolError(
        "permission_denied",
        "The resource is unavailable or access is denied.",
    )


async def _decision(
    *,
    ctx,
    service: AuthzService,
    action: Action,
    resource: ResourceRef,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> Decision:
    try:
        decision = await service.check(
            _principal(ctx),
            action,
            resource,
            _request_context(ctx, consistency=consistency),
        )
    except OpenFgaUnavailableError as exc:
        raise ToolError(
            "authorization_unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc
    if not decision.allowed:
        raise _permission_error()
    return decision


async def require_organization_create(ctx) -> Decision:
    organization_id = str(getattr(ctx, "tenant_id", "") or "")
    async with session_scope(tenant_id=organization_id) as session:
        return await _decision(
            ctx=ctx,
            service=_service(ctx, session),
            action=Action.CREATE,
            resource=ResourceRef(
                ResourceType.ORGANIZATION,
                organization_id,
                organization_id,
            ),
        )


async def require_workflow_action(
    ctx,
    workflow_id: str,
    action: Action,
    *,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> Decision:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        raise ToolError("no_workflow", "An explicit workflow ID is required.")
    organization_id = str(getattr(ctx, "tenant_id", "") or "")
    async with session_scope(tenant_id=organization_id) as session:
        return await _decision(
            ctx=ctx,
            service=_service(ctx, session),
            action=action,
            resource=_workflow_resource(ctx, workflow_id),
            consistency=consistency,
        )


async def require_chat_action(
    ctx,
    action: Action,
    *,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> Decision:
    """Authorize an operation in the originating Chat at current membership."""
    chat_id = str(getattr(ctx, "chat_id", "") or "").strip()
    if not chat_id:
        raise _permission_error()
    organization_id = str(getattr(ctx, "tenant_id", "") or "")
    async with session_scope(tenant_id=organization_id) as session:
        return await _decision(
            ctx=ctx,
            service=_service(ctx, session),
            action=action,
            resource=_chat_resource(ctx),
            consistency=consistency,
        )


async def load_authorized_workflow(
    ctx,
    workflow_id: str,
    action: Action,
) -> AuthorizedWorkflowSnapshot:
    """Authorize before reading either metadata or workflow content."""
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        raise ToolError("no_workflow", "An explicit workflow ID is required.")
    organization_id = str(getattr(ctx, "tenant_id", "") or "")
    async with session_scope(tenant_id=organization_id) as session:
        service = _service(ctx, session)
        decision = await _decision(
            ctx=ctx,
            service=service,
            action=action,
            resource=_workflow_resource(ctx, workflow_id),
        )
        repo = WorkflowRepo(session, str(getattr(ctx, "username", "") or ""))
        meta = await repo.get_meta(workflow_id)
        if not meta:
            raise _permission_error()
        workflow = await repo.get_current_workflow(workflow_id)
        return AuthorizedWorkflowSnapshot(meta, workflow, decision)


async def list_authorized_workflows(
    ctx, *, limit: int = 1000, offset: int = 0, include_access: bool = True,
) -> list[dict[str, Any]]:
    """List only metadata-authorized rows and attach effective capabilities."""
    organization_id = str(getattr(ctx, "tenant_id", "") or "")
    principal = _principal(ctx)
    context = _request_context(ctx, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    async with session_scope(tenant_id=organization_id) as session:
        service = _service(ctx, session)
        try:
            authorized_ids = await service.list_authorized_ids(
                principal,
                Action.VIEW_METADATA,
                ResourceType.WORKFLOW,
                context,
            )
        except OpenFgaUnavailableError as exc:
            raise ToolError(
                "authorization_unavailable",
                "Authorization is temporarily unavailable.",
            ) from exc
        repo = WorkflowRepo(session, principal.id)
        rows, _total = await repo.list_authorized_workflows(
            authorized_ids,
            limit=limit,
            offset=offset,
        )
        if not include_access:
            return rows
        resources = [
            _workflow_resource(ctx, str(row["wf_id"])) for row in rows
        ]
        try:
            decisions = await batch_resource_decisions(
                service,
                principal=principal,
                resources=resources,
                context=context,
            )
        except OpenFgaUnavailableError as exc:
            raise ToolError(
                "authorization_unavailable",
                "Authorization is temporarily unavailable.",
            ) from exc
        return [
            {
                **row,
                "access": {
                    "capabilities": sorted(
                        action.value
                        for action in decisions[resource].capabilities
                    ),
                    "effective_role": decisions[resource].effective_role,
                    "source": "computed",
                },
            }
            for row, resource in zip(rows, resources, strict=True)
        ]


async def _require_active_chat_write(session, ctx) -> None:
    """Fence a write to its active run; keep the lock through commit."""
    if not getattr(ctx, "runtime_session_id", None):
        raise ToolError("runtime_unavailable", "The Chat Runtime binding is missing.")
    run = (await session.execute(
        select(AgentRun).where(
            AgentRun.run_id == ctx.turn_id,
            AgentRun.chat_id == ctx.chat_id,
            AgentRun.creator_user_id == uuid.UUID(str(ctx.username)),
        ).with_for_update()
    )).scalar_one_or_none()
    if run is None or run.status != "running" or run.cancel_requested_at is not None:
        raise ToolError("runtime_unavailable", "The originating Agent turn is no longer active.")
    from vibecanvas_api.storage.chat_repo import ChatRepo
    binding = await ChatRepo(session, ctx.username).get_platform_context_binding(ctx.chat_id, for_update=True)
    if not binding or binding["runtime_session_id"] != ctx.runtime_session_id:
        raise ToolError("runtime_unavailable", "The originating Chat Runtime is no longer active.")
    await _decision(
        ctx=ctx, service=_service(ctx, session), action=Action.EXECUTE,
        resource=_chat_resource(ctx), consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )


async def _require_workflow_read(session, ctx, workflow_id: str) -> None:
    # Descriptions/tags are private content: metadata visibility alone is not
    # enough to return metadata. Never read a graph for metadata queries.
    service = _service(ctx, session)
    try:
        for action in (Action.USE, Action.VIEW):
            await _decision(
                ctx=ctx, service=service, action=action,
                resource=_workflow_resource(ctx, workflow_id),
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
            )
    except ToolError as exc:
        if str(exc) == "permission_denied":
            raise ToolError("workflow_unavailable", "The workflow does not exist or is no longer accessible.") from exc
        raise  # Authorization outages are not disconnected states.


async def get_authorized_workflow_metadata(ctx, workflow_id: str, changes: dict | None = None) -> dict:
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        if changes is not None:
            await _require_active_chat_write(session, ctx)
        await _require_workflow_read(session, ctx, workflow_id)
        repo = WorkflowRepo(session, ctx.username)
        meta = await repo.get_meta(workflow_id, for_update=changes is not None)
        if not meta:
            raise ToolError("workflow_unavailable", "The workflow is unavailable.")
        if changes is not None:
            await _decision(ctx=ctx, service=_service(ctx, session), action=Action.UPDATE,
                resource=_workflow_resource(ctx, workflow_id), consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
            fields = {"workflow_name" if key == "name" else key: value for key, value in changes.items()}
            meta = await repo.update_meta(workflow_id, **fields)
        return meta


async def create_authorized_workflow(
    ctx,
    *,
    name: str,
    description: str,
    tags: list[str] | None = None,
    initial_workflow: dict | None = None,
) -> AuthorizedWorkflowSnapshot:
    """Create a workflow and its structural relationships atomically."""
    organization_id = str(getattr(ctx, "tenant_id", "") or "")
    principal = _principal(ctx)
    async with session_scope(tenant_id=organization_id, user_id=ctx.username) as session:
        await _require_active_chat_write(session, ctx)
        service = _service(ctx, session)
        await _decision(
            ctx=ctx,
            service=service,
            action=Action.CREATE,
            resource=ResourceRef(
                ResourceType.ORGANIZATION,
                organization_id,
                organization_id,
            ),
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        )
        repo = WorkflowRepo(session, principal.id)
        workflow_id = uuid.uuid4().hex[:12]
        workflow = deepcopy(initial_workflow) if initial_workflow is not None else {}
        # Source IDs/versions are never used as storage identity. Do not mutate
        # the supplied object or its sandbox source file.
        workflow.setdefault("__meta__", {}).update({
            "workflow_id": workflow_id, "workflow_name": name,
            "workflow_version": 1, "workflow_subversion": 0,
        })
        meta = await repo.create_workflow(
            wf_id=workflow_id,
            name=name,
            description=description,
            tags=tags,
            initial_workflow=workflow,
            creator_user_id=principal.id,
        )
        coordinator = AuthzMutationCoordinator(
            client=getattr(ctx, "authorization_client", None),
            organization_id=organization_id,
        )
        mutation_ids = await enqueue_structural_delta(
            session=session,
            coordinator=coordinator,
            actor_type="user",
            actor_id=principal.id,
            before=frozenset(),
            after=resource_root_edges(
                organization_id=organization_id,
                object_type="workflow",
                object_id=str(meta["wf_id"]),
                owner_relation="manager",
                owner_type="user",
                owner_id=principal.id,
            ),
            operation_id=uuid.uuid4().hex,
            source="flowork-cli-workflow-create",
        )
        await session.commit()

    try:
        await apply_committed_structural_mutations(
            coordinator,
            mutation_ids,
        )
        snapshot = await load_authorized_workflow(ctx, workflow_id, Action.VIEW)
    except Exception as exc:
        raise ToolError("authorization_pending",
            "Workflow was created, but authorization synchronization has not completed. Do not create another copy.",
            info={"id": workflow_id, "created": True, "authorization_ready": False}) from exc

    return snapshot

__all__ = [
    "AuthorizedWorkflowSnapshot", "agent_auth_context",
    "create_authorized_workflow", "list_authorized_workflows",
    "load_authorized_workflow", "require_organization_create",
    "require_chat_action", "require_workflow_action",
]
