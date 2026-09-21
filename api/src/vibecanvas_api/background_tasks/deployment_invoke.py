"""Background worker implementation for API and webhook Deployment invocations.

Both external trigger types funnel through this single task. Deployment
observability belongs to Deployment logs/history, not the Task Center table;
recurring execution is owned by scheduled Tasks.

Architecture choice — async-driven worker (deviation from batch_exec):

  ``batch_exec`` runs in a fully synchronous style: a sync task body,
  ``SyncWorkflowRepo`` for loads, ``run_in_short_session`` for each DB
  write. That fit the row-by-row CSV use-case where every write opens
  its own short transaction.

  The DBOS Step body is a sync shell around ``asyncio.run(_run(...))``. The
  engine run itself goes through the sandbox runner:
  ``_run`` calls the sync+blocking
  ``run_workflow_sandboxed_sync`` (offloaded via ``asyncio.to_thread`` since
  ``_run`` is on a loop), which runs the engine inside gVisor when available and
  falls back in-process otherwise. The runner owns the whole run-dir lifecycle
  (RunWorkspace: temporary run_dir + cleanup), so
  ``_run`` no longer builds its own run context. ``run_id=task_id`` keeps the
  run-tier id consistent with the invocation id and the sync shell's release.

Tenant ContextVar invariant:

  ``tenant_id_var.set(...)`` is the FIRST line of the task body, before
  anything that could read a stale tenant context. Every subsequent
  ``short_session_scope(tenant_id=...)`` receives that SAME tenant id
  explicitly, so RLS GUC binding is deterministic per transaction.  The
  worker-safe scope owns a NullPool engine on the current ``asyncio.run``
  loop and disposes it before that loop closes; a background invocation never
  borrows a connection from the web process' loop-bound global pool.

DB state:

  This worker no longer writes ``tasks`` or ``task_events``. It logs terminal
  outcome through structlog until Deployment-specific invocation history is
  introduced.
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


def deployment_invoke(
    *,
    task_id: str,
    tenant_id: str,
    deployment_id: str,
    inputs: dict,
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
      * runs the deployment's pinned workflow version
      * logs terminal success/failure
      * releases the invocation run-tier
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
        ))
    finally:
        # RE-2 E0: release the run-tier at run-end. ``_run`` has no single
        # try/finally funnel (early ``return`` + multiple ``_finalize``s), so
        # release here in the genuinely-SYNC background worker shell (the ``asyncio.run``
        # above has returned), where ``release_sync`` (→ its own ``asyncio.run``)
        # is legal (C1). The run_id is the background job id (the resolved per-run
        # id). Set the sync tenant CV here in case ``_run`` raised before
        # reaching it. Production batch → retain=False; ``release`` is idempotent
        # so recovery is safe. Fail-soft: never crash the task.
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
) -> None:
    """Async driver — load deployment + version, run engine, finalize.

    Three failure modes funnel through the same ``_finalize`` call:
      1. Deployment was soft-deleted between submit and pickup → 'failed'
         with a stable, asserted-against error string.
      2. Workflow version missing / engine init failure → 'failed' with
         the exception's ``str()``.
      3. Engine produced a non-empty ``error_dict`` → 'failed' with a
         joined error summary; ``outputs`` still rides on ``result`` so
         a partial-progress UI can surface what DID succeed.
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
            await DeploymentInvocationsRepo(s).mark_running(invocation_uuid)
    except (LookupError, ValueError):
        async with short_session_scope(tenant_id=tenant_id) as s:
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

    # Cut over to the sandbox runner. ``load_workflow_version`` still resolves
    # the deployment's pinned, possibly non-head version and is
    # threaded through to the runner so the sandbox/in-process fallback both
    # run that exact content. ``run_workflow_sandboxed_sync`` is sync+blocking
    # and owns the whole temporary run-dir lifecycle, so ``_run`` no longer
    # builds its own ``build_run_context`` nor sweeps it. It runs the
    # engine inside gVisor when a sandbox is available and falls back in-process
    # on SandboxUnavailable / EngineNeedsHostNode / in-memory-store (run_dir
    # None). ``_run`` is driven by ``asyncio.run(_run())`` so it's ON a loop —
    # the blocking sync runner is offloaded via ``asyncio.to_thread``.
    #
    # ``run_id=task_id`` keeps the run-tier id consistent with the ``tasks`` row
    # and the SYNC shell's ``release_sync(run_id=task_id)`` in ``deployment_invoke``.
    outputs: dict = {}
    errors: dict = {}
    try:
        workflow_dict = await load_workflow_version(dep)
        outputs, errors, _exec_secs = await asyncio.to_thread(
            run_workflow_sandboxed_sync,
            workflow_id=dep["wf_id"], inputs=inputs,
            tenant_id=tenant_id, user_id=str(lease.created_by),
            run_id=task_id, workflow_dict=workflow_dict,
            execution_resource_type=ResourceType.DEPLOYMENT_INVOCATION.value,
            execution_principal_type="service_account",
            execution_principal_id=str(lease.service_account_id),
            execution_principal_generation=lease.generation,
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
