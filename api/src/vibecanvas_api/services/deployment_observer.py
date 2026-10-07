"""Detach HTTP observation from the lifetime of an admitted sandbox execution."""

import asyncio
from datetime import datetime, timezone
import uuid

from sqlalchemy import text

from vibecanvas_api.services.deployment_results import accepted_response, sync_result_response
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES, WorkflowHistoryRepo

_dispatches: set[asyncio.Task] = set()


def own_dispatch(coroutine) -> asyncio.Task:
    """Keep a strong owner after the HTTP observer returns or disconnects."""
    task = asyncio.create_task(coroutine)
    _dispatches.add(task)

    def finished(done):
        _dispatches.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)
    return task


async def observe_invocation(*, tenant_id: str, slug: str, invocation_id: str, dispatch: asyncio.Task | None = None):
    from vibecanvas_api.services.execution_notifications import execution_changes

    async with execution_changes(invocation_id) as changed:
        # Observation duration never changes the invocation's sync/async contract.
        # sandboxd owns cancellation and terminal persistence after process exit.
        while True:
            changed.clear()
            wait_seconds = None
            async with session_scope(tenant_id=tenant_id) as session:
                history = WorkflowHistoryRepo(session)
                run = await history.get(invocation_id)
                if run is None:
                    raise RuntimeError("admitted invocation history missing")
                encountered_approval = await session.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM workflow_execution_approvals WHERE execution_id=:id)"),
                    {"id": uuid.UUID(invocation_id)},
                )
                if encountered_approval:
                    return accepted_response(slug=slug, invocation_id=invocation_id, state=run["status"], async_reason="human_approval")
                invocation = (await session.execute(text("SELECT * FROM deployment_invocations WHERE id=:id"),
                    {"id": uuid.UUID(invocation_id)})).mappings().one_or_none()
                if run["status"] in TERMINAL_STATUSES:
                    # History is committed before RPC ACK/slot release. Do not tell
                    # a synchronous caller to start its next request while the old
                    # call still owns capacity. The runtime publishes its terminal
                    # invocation summary only after release and workspace writeback.
                    if invocation is None or invocation["status"] in TERMINAL_STATUSES:
                        return sync_result_response(await history.result_detail(invocation_id))
                elif invocation and invocation["timeout_seconds"] is not None:
                    elapsed = (datetime.now(timezone.utc) - invocation["submitted_at"]).total_seconds()
                    remaining = invocation["timeout_seconds"] - elapsed
                    if remaining > 0 and run["timeout_requested_at"] is None:
                        wait_seconds = remaining
                    if remaining <= 0 and run["timeout_requested_at"] is None:
                        # Only mutation takes locks. Recheck under the same history
                        # row lock used by runtime events before fencing a dispatcher.
                        run = await history.get(invocation_id, lock=True)
                        if run["status"] in TERMINAL_STATUSES:
                            continue
                        if await session.scalar(
                            text("SELECT EXISTS (SELECT 1 FROM workflow_execution_approvals WHERE execution_id=:id)"),
                            {"id": uuid.UUID(invocation_id)},
                        ):
                            return accepted_response(slug=slug, invocation_id=invocation_id,
                                state=run["status"], async_reason="human_approval")
                        invocation = (await session.execute(
                            text("SELECT * FROM deployment_invocations WHERE id=:id FOR UPDATE"),
                            {"id": uuid.UUID(invocation_id)},
                        )).mappings().one()
                        await session.execute(text("UPDATE workflow_execution_runs SET timeout_requested_at=COALESCE(timeout_requested_at,now()) WHERE id=:id"), {"id": uuid.UUID(invocation_id)})
                        if invocation["runtime_claim"] is None:
                            # Fence a late dispatcher before it can claim or start.
                            from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
                            await history.confirm_timed_out(invocation_id)
                            await DeploymentInvocationsRepo(session).mark_terminal(uuid.UUID(invocation_id),
                                status="timed_out", latency_ms=elapsed * 1000, error="execution_timeout")
                            return sync_result_response(await history.result_detail(invocation_id))
            # A finished/failed dispatch RPC is not evidence of execution completion.
            # Keep observing the same owned call; never return a fallback 202.
            try:
                async with asyncio.timeout(wait_seconds):
                    await changed.wait()
            except TimeoutError:
                pass  # The deadline prompts one locked timeout transition.
