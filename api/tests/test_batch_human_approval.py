"""Real RPC workers and database history behind the shared batch entrypoint."""

import asyncio
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from tests.storage.test_workflow_history import approval_graph, owner
from vibecanvas_api.services import batch_runtime
from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
from vibecanvas_api.services.sandbox.manager import SandboxSession
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo


@pytest.fixture(autouse=True)
def resume_authorization_fixture(monkeypatch):
    # These tests isolate entrypoint ownership. Live identity fences are covered
    # separately in test_workflow_resume; the driver and RPC gate remain real.
    async def refreshed(**kwargs):
        assert kwargs["context"]["_host_execution_identity"]["organization_id"] == kwargs["tenant_id"]
        return {"llm_credentials": {}, "workflow_resources": {}}

    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.workflow_execution_driver.refresh_execution_context",
        refreshed,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_waiting_row_holds_worker_and_each_input_has_its_own_history(pg_engine, monkeypatch, cancel):
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required")
    tenant, actor, _ = await owner()
    task_id = str(uuid4())
    graph = approval_graph()
    from vibecanvas_api.services.workflow_resume import IDENTITY_KEY, execution_identity

    session = SimpleNamespace(
        tenant_id=tenant,
        user_id=actor,
        provider=BubblewrapProvider(shutil.which("bwrap")),
        workspace_folders=(),
        workflow_run_source=None,
        persistent_run_binding=None,
        _rw_binds=[],
        skills_dir=None,
        _sync_mount_folder=AsyncMock(),
        _begin_activity=Mock(),
        _end_activity=Mock(),
    )

    async def execute(**kwargs):
        return await SandboxSession.execute_workflow_job(session, **kwargs)

    async def close_pool(**kwargs):
        return await SandboxSession.close_workflow_pool(session, **kwargs)

    async def close_session(*args):
        if hasattr(session, "_history_executions"):
            await session._history_executions.shutdown()

    session.execute_workflow_job = execute
    session.close_workflow_pool = close_pool
    coordinator = SimpleNamespace(get_session=AsyncMock(return_value=session), close_session=close_session)
    monkeypatch.setattr(batch_runtime, "get_sandbox_coordinator", lambda: coordinator)
    stop = threading.Event()
    progress = []

    async def on_progress(value):
        progress.append((value.index, value.status, value.row.get("execution_id")))

    task = asyncio.create_task(
        batch_runtime.run_batch_workflow(
            task_id=task_id,
            tenant_id=tenant,
            user_id=actor,
            workflow_id="wf-batch-human",
            workflow=graph,
            workflow_version="v2.sv7",
            rows=[{}, {}],
            column_mapping={},
            concurrency=1,
            mount_enabled=False,
            prepared_run_extra={
                IDENTITY_KEY: execution_identity(
                    tenant_id=tenant,
                    user_id=actor,
                    workflow_id="wf-batch-human",
                    execution_id=task_id,
                    execution_resource_type="task",
                )
            },
            stop_event=stop,
            on_progress=on_progress,
        )
    )

    async def waiting(count):
        async with asyncio.timeout(15):
            while True:
                async with session_scope(tenant_id=tenant) as db:
                    history = WorkflowHistoryRepo(db)
                    items = (await history.history(source_type="task", source_id=task_id))["items"]
                    pending = [item for item in items if item["status"] == "waiting_approval"]
                    if len(items) == count and len(pending) == 1:
                        return await history.detail(pending[0]["id"])
                if task.done():
                    await task
                    pytest.fail("batch ended before approval")
                await asyncio.sleep(0.05)

    try:
        first = await waiting(1)
        assert first["workflow_version"] == "v2.sv7"
        assert first["input_index"] == 0
        # Only one input is admitted while the sole worker is awaiting review.
        await asyncio.sleep(0.3)
        async with session_scope(tenant_id=tenant) as db:
            assert len((await WorkflowHistoryRepo(db).history(source_type="task", source_id=task_id))["items"]) == 1
        if cancel:
            stop.set()
        else:
            async with session_scope(tenant_id=tenant) as db:
                await WorkflowHistoryRepo(db).request_decision(
                    first["id"],
                    first["approvals"][0]["id"],
                    actor_user_id=actor,
                    approved=True,
                )
            second = await waiting(2)
            assert second["workflow_version"] == "v2.sv7"
            assert first["id"] != second["id"]
            assert second["input_index"] == 1
            assert first["generation"] == second["generation"]
            async with session_scope(tenant_id=tenant) as db:
                await WorkflowHistoryRepo(db).request_decision(
                    second["id"],
                    second["approvals"][0]["id"],
                    actor_user_id=actor,
                    approved=False,
                )
        result = await asyncio.wait_for(task, 15)
        if cancel:
            assert [row["status"] for row in result.rows] == ["cancelled", "cancelled"]
            async with session_scope(tenant_id=tenant) as db:
                detail = await WorkflowHistoryRepo(db).detail(first["id"])
                assert detail["status"] == "cancelled"
                assert detail["approvals"][0]["status"] == "cancelled"
        else:
            assert [row["execution_id"] for row in result.rows] == [first["id"], second["id"]]
            assert all(row["status"] == "success" for row in result.rows)
        assert (0, "waiting_approval", first["id"]) in progress
        assert session._begin_activity.call_count == session._end_activity.call_count
    finally:
        stop.set()
        if not task.done():
            await asyncio.wait_for(task, 15)
        await close_session()
