"""Synchronous ownership of one admitted history-backed sandbox execution.

Workflow pages and scheduled jobs use an isolated pool for the invocation.
Batch and CLI groups reuse their own bounded pools through the same session
primitive. A stop signal must close the pool, not just cancel the RPC waiter.
"""

import asyncio
from uuid import UUID

from vibecanvas_api.services.workflow_execution_history import observe_execution
from vibecanvas_api.services.workflow_sandbox_runner import prepare_code_pythonpath


async def execute_history_workflow(
    *,
    session,
    tenant_id,
    execution_id,
    workflow,
    inputs,
    context,
    stop,
    on_state,
    on_event=None,
    node_id=None,
):
    pool_id = UUID(execution_id).hex
    closing = None

    async def close_pool():
        nonlocal closing
        if closing is None:
            closing = asyncio.create_task(session.close_workflow_pool(tenant=tenant_id, pool_id=pool_id, history=True))
        result = await asyncio.wait_for(asyncio.shield(closing), timeout=20)
        if not isinstance(result, dict) or result.get("closed") is not True:
            raise RuntimeError("Workflow process shutdown was not confirmed.")

    async def watch_stop():
        await stop.wait()
        await close_pool()

    watcher = asyncio.create_task(watch_stop())
    try:
        return await observe_execution(
            tenant_id=tenant_id,
            execution_id=execution_id,
            execute=session.execute_workflow_job(
                workflow=workflow,
                inputs=inputs,
                extra=context,
                tenant=tenant_id,
                run_id=execution_id,
                run_subpath=f"history/{execution_id}",
                execution_pool_id=pool_id,
                history_id=execution_id,
                execution_capacity=1,
                node_id=node_id,
            ),
            on_state=on_state,
            on_event=on_event,
            on_failure=close_pool,
        )
    finally:
        try:
            await close_pool()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


async def stream_history_workflow(
    *,
    session,
    tenant_id,
    execution_id,
    workflow,
    inputs,
    context,
    stop,
):
    """Project committed node events to an existing SSE consumer.

    The graph and results travel over RPC. This bounded queue is only a live
    projection; durable events remain in the history database when a viewer leaves.
    """
    frames = asyncio.Queue(maxsize=32)

    async def on_state(state):
        return None

    async def on_event(frame):
        if frame["type"] == "node_event":
            await frames.put(frame)

    extra = dict(context or {})
    pythonpath = await prepare_code_pythonpath(workflow, session=session)
    if pythonpath:
        extra["code_pythonpath"] = pythonpath
    execution = asyncio.create_task(
        execute_history_workflow(
            session=session,
            tenant_id=tenant_id,
            execution_id=execution_id,
            workflow=workflow,
            inputs=inputs,
            context=extra,
            stop=stop,
            on_state=on_state,
            on_event=on_event,
        )
    )
    pending = None
    try:
        while not execution.done() or not frames.empty():
            pending = asyncio.create_task(frames.get())
            await asyncio.wait({pending, execution}, return_when=asyncio.FIRST_COMPLETED)
            if pending.done():
                yield pending.result()
                pending = None
            elif execution.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
                break
        response = await execution
        if (response.get("status") or {}).get("status") == "cancelled":
            stop.set()
            return
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Execution ended without a result.")
        yield {"type": "result", **result}
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if not execution.done():
            stop.set()
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
