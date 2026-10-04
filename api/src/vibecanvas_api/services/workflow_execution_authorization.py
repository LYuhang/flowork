"""Shared live execution authorization for Workflow model and resource brokers.

Callers verify their audience-specific capability before entering this gate.
The resolver runs only after identity, permissions and execution state checks.
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from fastapi import HTTPException, Request
from sqlalchemy import or_, select, text

from vibecanvas_api.authorization.dependencies import authz_service_for_session
from vibecanvas_api.authorization.types import (
    Action, AuthzRequestContext, ConsistencyPreference, PrincipalRef,
    PrincipalType, ResourceRef, ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.model_capability import authorization_model_generation
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.db import session_scope, temporary_tenant_scope
from vibecanvas_api.storage.models import User, WorkflowRunState
from vibecanvas_api.storage.models_tasks import ScheduledRunExecution, Task, TaskSchedule
from vibecanvas_api.storage.models_org import OrgMembership
from vibecanvas_api.storage.models_service_accounts import ServiceAccount


async def authorize_workflow_execution(
    request: Request, capability, *, resolve,
):
    """Revalidate a durable Workflow execution lease before model egress.

    Interactive Workflow runs remain bound to the current user membership.
    Task and Deployment runs are bound to an active, generation-fenced Service
    Account and its current resource/credential permissions.
    """
    try:
        organization_uuid = uuid.UUID(capability.organization_id)
        user_uuid = uuid.UUID(capability.user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_model_capability_invalid"},
        ) from exc

    current_generation = authorization_model_generation(
        model_id=config.openfga_authorization_model_id,
    )
    if capability.authorization_generation != current_generation:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_model_authorization_generation_stale"},
        )

    try:
        principal_type = PrincipalType(capability.principal_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_model_capability_invalid"},
        ) from exc
    membership = None
    if principal_type is PrincipalType.USER:
        if capability.principal_id != capability.user_id:
            raise HTTPException(
                status_code=401,
                detail={"code": "runtime_model_capability_invalid"},
            )
        async with session_scope() as identity_session:
            user = (
                await identity_session.execute(
                    select(User).where(
                        User.user_id == user_uuid,
                        User.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if user is None:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "runtime_model_actor_revoked"},
                )
            await identity_session.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"),
                {"user_id": capability.user_id},
            )
            membership = (
                await identity_session.execute(
                    select(OrgMembership).where(
                        OrgMembership.user_id == user_uuid,
                        OrgMembership.tenant_id == organization_uuid,
                        OrgMembership.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "runtime_model_membership_revoked"},
                )

    async with session_scope(tenant_id=capability.organization_id, user_id=capability.user_id) as session:
        service = authz_service_for_session(
            session=session,
            organization_id=capability.organization_id,
            openfga_client=getattr(request.app.state, "openfga_client", None),
        )
        if principal_type is PrincipalType.SERVICE_ACCOUNT:
            try:
                account_uuid = uuid.UUID(capability.principal_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=401,
                    detail={"code": "runtime_model_capability_invalid"},
                ) from exc
            account = (
                await session.execute(
                    select(ServiceAccount).where(
                        ServiceAccount.service_account_id == account_uuid,
                        ServiceAccount.tenant_id == organization_uuid,
                        ServiceAccount.status == "active",
                        ServiceAccount.generation
                        == capability.principal_generation,
                        ServiceAccount.created_by == user_uuid,
                    )
                )
            ).scalar_one_or_none()
            if account is None:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "runtime_model_service_account_revoked"},
                )
            principal = PrincipalRef(
                PrincipalType.SERVICE_ACCOUNT,
                capability.principal_id,
            )
            authz_context = AuthzRequestContext(
                active_organization_id=capability.organization_id,
                request_id=f"runtime-workflow-model:{capability.execution_id}",
                authentication_strength="runtime_service_account_lease",
                authz_generation=capability.principal_generation,
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
            )
        else:
            principal = PrincipalRef(PrincipalType.USER, capability.user_id)
            authz_context = AuthzRequestContext(
                active_organization_id=capability.organization_id,
                request_id=f"runtime-workflow-model:{capability.execution_id}",
                membership_id=str(membership.membership_id),
                membership_role=membership.org_role,
                membership_status=membership.status,
                authentication_strength="runtime_workflow_lease",
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
            )
        execution_resource_type = ResourceType(
            capability.execution_resource_type
        )
        execution_decision = await service.check(
            principal,
            Action.EXECUTE,
            ResourceRef(
                execution_resource_type,
                capability.execution_id,
                capability.organization_id,
            ),
            authz_context,
        )
        if not execution_decision.allowed:
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_model_execution_access_revoked"},
            )
        workflow_owner = await _active_workflow_source_organization(session, capability)
        if workflow_owner is None:
            raise HTTPException(status_code=403, detail={"code": "runtime_model_execution_inactive"})
        if not await workflow_source_access_allowed(session=session, service=service, principal=principal,
                context=authz_context, workflow_id=capability.workflow_id, workflow_owner=workflow_owner):
            raise HTTPException(status_code=403, detail={"code": "runtime_model_workflow_access_revoked"})
        return await resolve(
            session=session,
            service=service,
            principal=principal,
            authz_context=authz_context,
            capability=capability,
        )


async def workflow_source_access_allowed(*, session, service, principal, context, workflow_id, workflow_owner):
    """Check a persisted execution's source without widening resource resolution.

    The caller validates the execution identity before passing its source owner.
    The service uses this same session, so revocation guards read the source
    organization's ledger. Credentials remain in the caller's original scope.
    """
    source_context = replace(context,
        admitted_resource_organization_id=str(workflow_owner),
        admitted_resource_type=ResourceType.WORKFLOW.value,
        admitted_resource_id=workflow_id)
    source = ResourceRef(ResourceType.WORKFLOW, workflow_id, str(workflow_owner))
    async with temporary_tenant_scope(session, workflow_owner):
        from vibecanvas_api.authorization.parent_resolvers import collaboration_root_exists
        if not await collaboration_root_exists(session, source):
            return False
        decision = await service.check(principal, Action.EXECUTE, source, source_context)
        return decision.allowed


async def _workflow_execution_is_active(session, capability) -> bool:
    """Shared liveness check for execution caches and runtime brokers."""
    return await _active_workflow_source_organization(session, capability) is not None


async def _active_workflow_source_organization(
    session,
    capability,
) -> str | None:
    """Fence a Workflow model lease against its live control-plane record."""
    resource_type = ResourceType(capability.execution_resource_type)
    service_account_id: uuid.UUID | None = None
    if capability.principal_type == "service_account":
        try:
            service_account_id = uuid.UUID(capability.principal_id)
        except ValueError:
            return None
    if resource_type is ResourceType.WORKFLOW_EXECUTION:
        if service_account_id is not None:
            return None
        state = (
            await session.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.wf_id == capability.workflow_id,
                    WorkflowRunState.creator_user_id == uuid.UUID(capability.user_id),
                    WorkflowRunState.status == "running",
                    or_(
                        WorkflowRunState.turn_id == capability.execution_id,
                        WorkflowRunState.wf_id == capability.execution_id,
                    ),
                )
            )
        ).scalar_one_or_none()
        return str(state.workflow_tenant_id) if state is not None else None
    if resource_type is ResourceType.AGENT_RUN:
        if service_account_id is not None:
            return None
        run = await AgentRunsRepo(session).get(capability.execution_id)
        if run is None or run.status != "running" or str(run.creator_user_id) != capability.user_id:
            return None
        from vibecanvas_api.storage.shared_resource_locator import shared_resource_roots
        roots = await shared_resource_roots(capability.user_id, active_organization_id=capability.organization_id, resource_type="workflow",
                                           resource_id=capability.workflow_id, limit=2)
        if len(roots) > 1:
            return None
        return str(roots[0].owner_tenant_id) if roots else capability.organization_id
    try:
        execution_uuid = uuid.UUID(capability.execution_id)
    except ValueError:
        return None
    if resource_type is ResourceType.TASK:
        actor_filter = (
            Task.service_account_id == service_account_id
            if service_account_id is not None
            else Task.user_id == uuid.UUID(capability.user_id)
        )
        task = (
            await session.execute(
                select(Task).where(
                    Task.id == execution_uuid,
                    Task.workflow_id == capability.workflow_id,
                    actor_filter,
                    Task.status.in_(("running", "resuming")),
                )
            )
        ).scalar_one_or_none()
        return str(task.workflow_tenant_id) if task is not None else None
    if resource_type is ResourceType.TASK_EXECUTION:
        actor_filter = (
            TaskSchedule.service_account_id == service_account_id
            if service_account_id is not None
            else TaskSchedule.user_id == uuid.UUID(capability.user_id)
        )
        execution = (
            await session.execute(
                select(ScheduledRunExecution)
                .join(
                    TaskSchedule,
                    TaskSchedule.id == ScheduledRunExecution.schedule_id,
                )
                .where(
                    ScheduledRunExecution.id == execution_uuid,
                    ScheduledRunExecution.workflow_id == capability.workflow_id,
                    ScheduledRunExecution.status == "running",
                    actor_filter,
                )
            )
        ).scalar_one_or_none()
        return str(execution.workflow_tenant_id) if execution is not None else None
    if resource_type is ResourceType.DEPLOYMENT_INVOCATION:
        row = (
            await session.execute(
                text(
                    """
                    SELECT i.status, i.wf_id, i.workflow_tenant_id, d.user_id,
                           d.service_account_id
                    FROM deployment_invocations AS i
                    JOIN deployments AS d ON d.id = i.deployment_id
                    WHERE i.id = CAST(:invocation_id AS uuid)
                      AND d.deleted_at IS NULL
                    """
                ),
                {"invocation_id": capability.execution_id},
            )
        ).mappings().one_or_none()
        active = bool(
            row is not None
            and row["status"] in {"running", "waiting_approval"}
            and row["wf_id"] == capability.workflow_id
            and (
                str(row["service_account_id"]) == capability.principal_id
                if service_account_id is not None
                else str(row["user_id"]) == capability.user_id
            )
        )
        return str(row["workflow_tenant_id"]) if active else None
    return None
