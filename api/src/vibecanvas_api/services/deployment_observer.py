"""Detach HTTP observation from the lifetime of an admitted sandbox execution."""

import asyncio
import time
import uuid

from sqlalchemy import text

from vibecanvas_api.services.deployment_results import accepted_response, sync_result_response
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES, WorkflowHistoryRepo

SYNC_WAIT_SECONDS = 30.0
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


async def observe_invocation(*, tenant_id: str, slug: str, invocation_id: str, dispatch: asyncio.Task):
    deadline = time.monotonic() + SYNC_WAIT_SECONDS
    while True:
        dispatch_finished_before_read = dispatch.done()
        async with short_session_scope(tenant_id=tenant_id) as session:
            history = WorkflowHistoryRepo(session)
            run = await history.get(invocation_id)
            if run is None:
                raise RuntimeError("admitted invocation history missing")
            encountered_approval = await session.scalar(
                text("""SELECT EXISTS (
                SELECT 1 FROM workflow_execution_approvals WHERE execution_id=:id)"""),
                {"id": uuid.UUID(invocation_id)},
            )
            # Even an immediately resolved approval switches this particular
            # call to the asynchronous contract. Other calls stay synchronous.
            if encountered_approval:
                return accepted_response(slug=slug, invocation_id=invocation_id, state=run["status"])
            if run["status"] in TERMINAL_STATUSES:
                return sync_result_response(await history.result_detail(invocation_id))
            state = run["status"]
        remaining = deadline - time.monotonic()
        if remaining <= 0 or dispatch_finished_before_read:
            # A lost transport is not proof of execution loss. The sandbox owner
            # continues finalization; the same ticket remains queryable.
            return accepted_response(slug=slug, invocation_id=invocation_id, state=state)
        await asyncio.wait({dispatch}, timeout=min(0.1, remaining))
