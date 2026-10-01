"""Finalize expired deployment claims only after their process is stopped."""

import structlog
from sqlalchemy import text

from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES, WorkflowHistoryRepo

logger = structlog.get_logger(__name__)

_EXPIRED = """status IN ('queued','running','waiting_approval') AND revision_id IS NOT NULL
    AND ((execution_lease_until IS NOT NULL AND execution_lease_until <= now())
        OR (runtime_claim IS NULL AND dispatch_deadline <= now()))"""


async def expire_deployment_invocations(runtime):
    async with session_scope_admin() as db:
        candidates = (
            (
                await db.execute(
                    text("SELECT id,tenant_id,revision_id,runtime_claim FROM deployment_invocations WHERE " + _EXPIRED)
                )
            )
            .mappings()
            .all()
        )
    for candidate in candidates:
        tenant = str(candidate["tenant_id"])
        if candidate["runtime_claim"] is not None:
            try:
                stopped = await runtime.stop_expired_invocation(
                    tenant_id=tenant,
                    revision_id=str(candidate["revision_id"]),
                    invocation_id=str(candidate["id"]),
                )
            except Exception:
                logger.warning("deployment_expiry_shutdown_unconfirmed", invocation_id=str(candidate["id"]))
                continue
            if not stopped:
                continue
        async with short_session_scope(tenant_id=tenant) as db:
            # Recheck after shutdown; a renewed claim or terminal result wins.
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT * FROM deployment_invocations WHERE id=:id AND "
                            + _EXPIRED
                            + " AND runtime_claim IS NOT DISTINCT FROM :claim FOR UPDATE"
                        ),
                        {"id": candidate["id"], "claim": candidate["runtime_claim"]},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                continue
            history = WorkflowHistoryRepo(db)
            execution_id = str(row["id"])
            run = await history.get(execution_id)
            code = "execution_lost" if row["runtime_claim"] is not None else "execution_dispatch_failed"
            if run is not None and run["status"] not in TERMINAL_STATUSES:
                if run["cancel_requested_at"] is not None:
                    await history.confirm_cancelled(execution_id)
                else:
                    await history.fail(execution_id, error_code=code)
                run = await history.get(execution_id)
            await DeploymentInvocationsRepo(db).mark_terminal(
                row["id"],
                status=run["status"] if run else "failed",
                latency_ms=None,
                error=run.get("error_code") if run else code,
                runtime_claim=row["runtime_claim"],
            )
