"""Dispatch an admitted call once; sandboxd owns it after claiming the row."""

import uuid

import structlog
from fastapi import HTTPException
from sqlalchemy import text

from vibecanvas_api.authorization.types import ResourceType
from vibecanvas_api.services.workflow_runner import run_workflow_sandboxed_async
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo

logger = structlog.get_logger(__name__)


async def dispatch_invocation(*, dep, revision, lease, workflow, inputs, tenant_id, invocation_id,
                              wait_for_result=True):
    """Dispatch once; sandboxd owns final results after its durable claim."""
    try:
        identity = (
            {
                "execution_principal_type": "service_account",
                "execution_principal_id": str(lease.service_account_id),
                "execution_principal_generation": lease.generation,
            }
            if lease is not None
            else {}
        )
        await run_workflow_sandboxed_async(
            workflow_id=dep["wf_id"],
            inputs=inputs,
            tenant_id=tenant_id,
            user_id=str(lease.created_by if lease is not None else dep["user_id"]),
            run_id=invocation_id,
            workflow_dict=workflow,
            mount_enabled=revision["spec"]["mount_enabled"],
            deployment_id=str(dep["id"]),
            revision_id=str(revision["id"]),
            execution_resource_type=ResourceType.DEPLOYMENT_INVOCATION.value,
            wait_for_result=wait_for_result,
            **identity,
        )
    except Exception as exc:
        from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo

        failure_code = {429: "execution_quota_exceeded", 503: "execution_unavailable"}.get(
            exc.status_code if isinstance(exc, HTTPException) else None,
            "execution_dispatch_failed",
        )
        from vibecanvas_api.services.sandbox.service import SandboxServiceError

        if isinstance(exc, SandboxServiceError) and exc.code in {
            "sandbox_unavailable", "sandbox_deadline_exceeded",
        }:
            failure_code = "execution_unavailable"
        async with session_scope(tenant_id=tenant_id) as session:
            # Serialize against sandboxd's claim. An unclaimed failed dispatch
            # is fenced terminal before any delayed RPC can begin execution.
            # A claimed invocation belongs to sandboxd even if this RPC failed.
            row = (
                (
                    await session.execute(
                        text("""SELECT runtime_claim,status FROM deployment_invocations
                WHERE id=:id FOR UPDATE"""),
                        {"id": uuid.UUID(invocation_id)},
                    )
                )
                .mappings()
                .one()
            )
            if row["runtime_claim"] is None and row["status"] in {"queued", "running"}:
                await WorkflowHistoryRepo(session).fail(invocation_id, error_code=failure_code)
                await DeploymentInvocationsRepo(session).mark_terminal(
                    uuid.UUID(invocation_id),
                    status="failed",
                    latency_ms=None,
                    error=failure_code,
                )
        logger.warning("deployment_dispatch_failed", invocation_id=invocation_id)
