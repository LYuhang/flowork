"""Close history after proven process loss; never reconstruct or replay runs."""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import text

from vibecanvas_api.services.sandbox.process_identity import host_identity, process_group_gone
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.execution_repo import ExecutionRepo
from vibecanvas_api.storage.models_tasks import ScheduledRunExecution
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES, WorkflowHistoryRepo


async def reap_lost_workflow_processes():
    host, _ = host_identity()
    async with session_scope_admin() as db:
        candidates = (
            (
                await db.execute(
                    text("""SELECT id,tenant_id,source_type,generation,runtime_process
            FROM workflow_execution_runs WHERE status IN ('running','waiting_approval')
            AND (runtime_process->>'host_id'=:host OR runtime_process->>'kind'='local_cli')"""),
                    {"host": host},
                )
            )
            .mappings()
            .all()
        )
    for candidate in candidates:
        identity = candidate["runtime_process"]
        if identity.get("kind") == "local_cli":
            from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
            try:
                exited = await get_sandbox_manager().local_execution_exited(
                    str(candidate["tenant_id"]), identity["sandbox_id"], identity["run_id"])
            except Exception:
                # A failed observation is never proof that execution ended.
                continue
            if exited is not True:
                continue
        elif not await asyncio.to_thread(process_group_gone, identity):
            continue
        async with short_session_scope(tenant_id=str(candidate["tenant_id"])) as db:
            invocation = None
            if candidate["source_type"] == "deployment":
                # Match the deployment finalizer's lock order.
                invocation = (
                    (
                        await db.execute(
                            text("SELECT * FROM deployment_invocations WHERE id=:id FOR UPDATE"),
                            {"id": candidate["id"]},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            history = WorkflowHistoryRepo(db)
            execution_id = str(candidate["id"])
            run = await history.get(execution_id, lock=True)
            if (
                run is None
                or run["status"] in TERMINAL_STATUSES
                or run["generation"] != candidate["generation"]
                or run["runtime_process"] != candidate["runtime_process"]
            ):
                continue
            cancelled = run["cancel_requested_at"] is not None
            if cancelled:
                await history.confirm_cancelled(execution_id)
            else:
                await history.fail(execution_id, error_code="execution_lost", generation=run["generation"])
            if run["source_type"] == "workflow":
                repo = ExecutionRepo(db, str(run["initiator_user_id"]))
                record = await repo.get_execution(execution_id)
                if record and record["status"] in {"pending", "running"}:
                    if cancelled:
                        await repo.stop_execution(execution_id)
                    else:
                        await repo.finish_execution(execution_id, status="error", error="execution_lost")
            elif run["source_type"] == "task":
                scheduled = await db.get(ScheduledRunExecution, candidate["id"], with_for_update=True)
                if scheduled and scheduled.status in {"queued", "running", "cancelling"}:
                    await TasksRepo(db).update_scheduled_execution(
                        candidate["id"],
                        status="cancelled" if cancelled else "failed",
                        error="execution_cancelled" if cancelled else "execution_lost",
                        finished_at=datetime.now(timezone.utc),
                    )
            elif invocation is not None:
                await DeploymentInvocationsRepo(db).mark_terminal(
                    candidate["id"],
                    status="cancelled" if cancelled else "failed",
                    latency_ms=None,
                    error="execution_cancelled" if cancelled else "execution_lost",
                    runtime_claim=invocation["runtime_claim"],
                )
