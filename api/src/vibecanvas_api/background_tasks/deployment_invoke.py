"""Deliver admitted asynchronous deployment calls to their sandbox owner.

DBOS stores only the invocation ID; its adapter decrypts the frozen payload.
Resident deliveries return after sandboxd commits its ownership claim. The
sandbox then owns approval waits, results and artifact cleanup independently
of the queue worker. Delivery completion is not workflow completion.

The tenant ContextVar is bound before any repository work, and each short DB
session receives the same tenant explicitly. Older deliveries without a
resident revision retain their legacy completion and cleanup path.
"""
from __future__ import annotations

import asyncio
import uuid
from time import perf_counter

import structlog

from vibecanvas_api.authorization.types import ResourceType
from vibecanvas_api.services.tenant_db import tenant_id_var
from vibecanvas_api.services.workflow_runner import (
    load_workflow_version,
    run_workflow_sandboxed_sync,
)
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.vfs_run_repo import PostgresVfsRunStore

logger = structlog.get_logger(__name__)


async def _fail_history(session, invocation_id, code):
    from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo

    history = WorkflowHistoryRepo(session)
    if await history.get(invocation_id) is not None:
        await history.fail(invocation_id, error_code=code)


def deployment_invoke(
    *,
    task_id: str,
    tenant_id: str,
    deployment_id: str,
    inputs: dict,
    snapshot: dict | None = None,
) -> None:
    """Durable Step entry — sync shell around an asyncio driver.

    DBOS carries only an opaque invocation id. The DBOS adapter loads and
    decrypts these arguments from Flowork's business database before entering
    this runtime-neutral implementation.

    Args:
        task_id: UUID string of the deployment invocation.
        tenant_id: UUID string — bound to ``tenant_id_var`` on line 1 of
            the body and forwarded explicitly to every worker-safe session.
        deployment_id: UUID string — looked up via ``DeploymentsRepo.get``
            under the tenant scope (RLS).
        inputs: Decrypted workflow inputs loaded by the adapter. For ``api``
            triggers this is the raw request body; for ``webhook`` triggers it
            is ``{"payload": <parsed-json>}``.

    Side effects:
      * dispatches the admitted revision under its service account
      * records failures that happen before sandbox ownership
      * leaves resident execution files with the sandbox owner
    """
    # Spec §9 invariant — MUST be the first executable line of the body.
    # Every nested ``short_session_scope(tenant_id=...)`` call below passes
    # the string form explicitly, but the CV is the canonical anchor:
    # downstream async helpers (``load_workflow_version`` →
    # ``session_scope_admin``) inherit it via the asyncio.run context.
    tenant_id_var.set(uuid.UUID(tenant_id))
    try:
        asyncio.run(_run(
            task_id=task_id,
            tenant_id=tenant_id,
            deployment_id=deployment_id,
            inputs=inputs,
            snapshot=snapshot,
        ))
    finally:
        # Resident executions outlive this delivery task. Their sandbox owner
        # persists artifacts and releases scratch files at actual completion.
        if not (snapshot or {}).get("revision_id"):
            try:
                current_sync_tenant_id.set(tenant_id)
                PostgresVfsRunStore().release_sync(run_id=task_id, retain=False)
            except Exception:  # pragma: no cover - fail-soft, never crash the task
                logger.warning("run_release_failed", run_id=task_id, retain=False,
                               site="background_deployment_invoke", exc_info=True)


async def _run(
    *,
    task_id: str,
    tenant_id: str,
    deployment_id: str,
    inputs: dict,
    snapshot: dict | None = None,
) -> None:
    """Check delivery authority, then hand resident execution to sandboxd.

    Legacy deliveries without a revision use the older result path below.
    """
    dep_uuid = uuid.UUID(deployment_id)
    invocation_uuid = uuid.UUID(task_id)
    current_sync_tenant_id.set(tenant_id)
    started = perf_counter()

    # Tenant-scoped read for the deployment row. ``DeploymentsRepo.get``
    # already filters ``deleted_at IS NULL`` so a soft-deleted row
    # returns None without us having to special-case the column.
    async with short_session_scope(tenant_id=tenant_id) as s:
        dep = await DeploymentsRepo(s).get(dep_uuid)

    if dep is None:
        async with short_session_scope(tenant_id=tenant_id) as s:
            await _fail_history(s, task_id, "execution_unavailable")
            await DeploymentInvocationsRepo(s).mark_terminal(
                invocation_uuid,
                status="failed",
                latency_ms=(perf_counter() - started) * 1000.0,
                error="deployment not found",
            )
        logger.warning(
            "deployment_invoke_missing_deployment",
            invocation_id=task_id,
            deployment_id=deployment_id,
        )
        return

    try:
        service_account_id = dep.get("service_account_id")
        if service_account_id is None:
            raise LookupError("service_account_unavailable")
        async with short_session_scope(tenant_id=tenant_id) as s:
            lease = await ServiceAccountsRepo(s).require_active_lease(
                service_account_id=uuid.UUID(str(service_account_id)),
                owner_resource_type="deployment",
                owner_resource_id=deployment_id,
            )
            if not await DeploymentInvocationsRepo(s).mark_running(invocation_uuid):
                # DBOS can retry a step after its executor committed completion
                # but before the acknowledgement reached the queue worker.
                return
    except (LookupError, ValueError):
        async with short_session_scope(tenant_id=tenant_id) as s:
            await _fail_history(s, task_id, "execution_unavailable")
            await DeploymentInvocationsRepo(s).mark_terminal(
                invocation_uuid,
                status="failed",
                latency_ms=(perf_counter() - started) * 1000.0,
                error="service_account_unavailable",
            )
        logger.warning(
            "deployment_service_account_unavailable",
            invocation_id=task_id,
            deployment_id=deployment_id,
        )
        return

    if (snapshot or {}).get("revision_id"):
        from vibecanvas_api.services.deployment_dispatch import dispatch_invocation

        await dispatch_invocation(
            dep=dep,
            revision={"id": snapshot["revision_id"], "spec": {"mount_enabled": snapshot.get("mount_enabled", True)}},
            lease=lease, workflow=snapshot["workflow"], inputs=inputs,
            tenant_id=tenant_id, invocation_id=task_id, wait_for_result=False,
        )
        return

    # Compatibility for older deliveries without a resident revision.
    # ``load_workflow_version`` still resolves
    # the deployment's pinned, possibly non-head version and is
    # threaded through to the sandbox runner to execute that exact content. ``run_workflow_sandboxed_sync`` is sync+blocking
    # and owns the whole temporary run-dir lifecycle, so ``_run`` no longer
    # builds its own ``build_run_context`` nor sweeps it. It runs the
    # engine inside the selected sandbox and fails if isolation is unavailable. ``_run`` is driven by ``asyncio.run(_run())`` so it's ON a loop —
    # the blocking sync runner is offloaded via ``asyncio.to_thread``.
    #
    # ``run_id=task_id`` keeps the run-tier id consistent with the ``tasks`` row
    # and the SYNC shell's ``release_sync(run_id=task_id)`` in ``deployment_invoke``.
    outputs: dict = {}
    errors: dict = {}
    try:
        workflow_dict = snapshot["workflow"] if snapshot else await load_workflow_version(dep)
        outputs, errors, _exec_secs = await asyncio.to_thread(
            run_workflow_sandboxed_sync,
            workflow_id=dep["wf_id"], inputs=inputs,
            tenant_id=tenant_id, user_id=str(lease.created_by),
            run_id=task_id, workflow_dict=workflow_dict,
            execution_resource_type=ResourceType.DEPLOYMENT_INVOCATION.value,
            execution_principal_type="service_account",
            execution_principal_id=str(lease.service_account_id),
            execution_principal_generation=lease.generation,
            mount_enabled=(snapshot or dep).get("mount_enabled", True),
            deployment_id=deployment_id,
            revision_id=(snapshot or {}).get("revision_id"),
        )
    except Exception as exc:
        # Top-level engine / loader failure — file it under a stable
        # synthetic key so callers can distinguish "engine couldn't
        # start" from "node X failed".
        errors["__top__"] = f"{type(exc).__name__}: {exc}"

    if errors:
        async with short_session_scope(tenant_id=tenant_id) as s:
            await DeploymentInvocationsRepo(s).mark_terminal(
                invocation_uuid,
                status="failed",
                latency_ms=(perf_counter() - started) * 1000.0,
                error="execution_failed",
                result_summary={
                    "output_count": len(outputs) if isinstance(outputs, dict) else 0,
                    "error_count": len(errors) if isinstance(errors, dict) else 0,
                },
            )
        logger.warning(
            "deployment_invoke_failed",
            invocation_id=task_id,
            deployment_id=deployment_id,
            errors=errors,
            outputs=outputs,
        )
    else:
        async with short_session_scope(tenant_id=tenant_id) as s:
            await DeploymentInvocationsRepo(s).mark_terminal(
                invocation_uuid,
                status="succeeded",
                latency_ms=(perf_counter() - started) * 1000.0,
                result_summary={
                    "output_count": len(outputs) if isinstance(outputs, dict) else 0,
                    "error_count": 0,
                },
            )
        logger.info(
            "deployment_invoke_finished",
            invocation_id=task_id,
            deployment_id=deployment_id,
            outputs=outputs,
        )
