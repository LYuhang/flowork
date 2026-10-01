"""Per-input history admission and observation shared by synchronous callers."""

import asyncio
from uuid import uuid4

from vibecanvas_api.services.workflow_approvers import resolve_workflow_approvers
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


async def create_execution(
    *,
    tenant_id,
    source_type,
    source_id,
    user_id,
    workflow_id,
    workflow,
    inputs,
    node_id=None,
    execution_id=None,
    input_index=None,
) -> str:
    execution_id = execution_id or str(uuid4())
    async with short_session_scope(tenant_id=tenant_id) as session:
        approvers = await resolve_workflow_approvers(
            session,
            tenant_id=tenant_id,
            workflow=workflow,
            initiator_user_id=user_id,
            require_explicit=False,
        )
        await WorkflowHistoryRepo(session).create(
            execution_id=execution_id,
            tenant_id=tenant_id,
            wf_id=workflow_id,
            source_type=source_type,
            source_id=source_id,
            initiator_user_id=user_id,
            workflow=workflow,
            inputs=inputs,
            approvers=approvers,
            node_id=node_id,
            input_index=input_index,
        )
    return execution_id


async def observe_execution(*, tenant_id: str, execution_id: str, execute, on_state, on_failure, on_event=None):
    task = asyncio.create_task(execute)
    previous = None
    after = 0
    try:
        while True:
            # If completion races this read, read once more before declaring
            # the event stream drained; the terminal commit may be newer.
            finished_before_read = task.done()
            async with short_session_scope(tenant_id=tenant_id) as session:
                repo = WorkflowHistoryRepo(session)
                row = await repo.get(execution_id)
                frames = await repo.events(execution_id, after=after) if on_event else []
            if row["status"] != previous:
                previous = row["status"]
                if previous in {"queued", "running", "waiting_approval"}:
                    await on_state(previous)
            for frame in frames:
                await on_event(frame)
                after = frame["seq"]
            if finished_before_read and (on_event is None or after >= row["last_seq"]):
                return await task
            await asyncio.wait({task}, timeout=0.25)
    except BaseException:
        # A cancelled RPC is not proof that its process stopped. In embedded
        # mode the RPC deliberately waits for completion even on cancellation;
        # closing the owning pool first also prevents an approval-wait deadlock
        # if history polling or the progress callback itself fails.
        await on_failure()
        raise
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
