"""Workflow-page producers with real history and RPC workers.

Identity/resource preparation and the legacy VFS preview cache are fixtures;
the current-run projection, engine execution and approval history are real.
"""

import asyncio
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi import HTTPException
import pytest

from tests.storage.test_workflow_history import approval_graph
from tests.test_scheduled_runs import _seed_tenant_user_workflow
from vibecanvas_api.routes import executions as routes
from vibecanvas_api.schemas.execution import ExecutionRequest
from vibecanvas_api.services import workflow_resources, workflow_history_runner
from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
from vibecanvas_api.services.sandbox.manager import SandboxSession
from vibecanvas_api.storage import stop_registry
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.execution_repo import ExecutionRepo
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
@pytest.mark.parametrize("node_only", [False, True])
@pytest.mark.parametrize("outcome", ["approve", "reject", "timeout", "cancel", "process_loss", "revoked", "notification_failure"])
async def test_canvas_approval_projects_progress_and_survives_viewer_refresh(
    pg_engine, monkeypatch, node_only, outcome
):
    if outcome == "notification_failure":
        notification = AsyncMock(side_effect=RuntimeError("notification adapter unavailable"))
        monkeypatch.setattr(
            "vibecanvas_api.services.sandbox.workflow_execution_driver.notify_approval_requested",
            notification,
        )
    if outcome == "revoked":
        monkeypatch.setattr(
            "vibecanvas_api.services.sandbox.workflow_execution_driver.refresh_execution_context",
            AsyncMock(side_effect=PermissionError("original actor revoked")),
        )
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required")
    tenant, actor, wf_id = await _seed_tenant_user_workflow(pg_engine)
    tenant, actor = str(tenant), str(actor)
    graph = approval_graph()
    if outcome == "timeout":
        graph["node_2"]["node_config"]["timeout_seconds"] = 2
    execution_id = str(uuid4())
    session = SimpleNamespace(
        tenant_id=tenant,
        user_id=actor,
        provider=BubblewrapProvider(shutil.which("bwrap")),
        workspace_folders=(),
        _rw_binds=[],
        skills_dir=None,
        _sync_mount_folder=AsyncMock(),
        _begin_activity=Mock(),
        _end_activity=Mock(),
        clear_workflow_run=AsyncMock(),
    )

    async def execute(**kwargs):
        return await SandboxSession.execute_workflow_job(session, **kwargs)

    async def close_pool(**kwargs):
        return await SandboxSession.close_workflow_pool(session, **kwargs)

    session.execute_workflow_job, session.close_workflow_pool = execute, close_pool
    monkeypatch.setattr(
        routes, "get_sandbox_manager", lambda: SimpleNamespace(get_session=AsyncMock(return_value=session))
    )
    monkeypatch.setattr(routes, "compute_allow_hosts_async", AsyncMock(return_value=set()))
    monkeypatch.setattr(routes, "write_node_result", AsyncMock())
    monkeypatch.setattr(routes, "prepare_code_pythonpath", AsyncMock(return_value=None))
    monkeypatch.setattr(workflow_history_runner, "prepare_code_pythonpath", AsyncMock(return_value=None))
    monkeypatch.setattr(workflow_resources, "prepare_execution_resources", AsyncMock(return_value=None))
    stop = asyncio.Event()
    stream = (
        routes._produce_node_execution(
            stop,
            execution_id,
            graph["node_2"],
            {},
            tenant,
            wf_id,
            actor,
            graph,
        )
        if node_only
        else routes._produce_execution(
            stop,
            wf_id,
            execution_id,
            ExecutionRequest(input={}),
            graph,
            actor,
            tenant,
        )
    )
    frames = []

    async def consume():
        async for name, frame in stream:
            assert name == "EXEC_UPDATE"
            frames.append(frame)

    work = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(20):
            while True:
                async with session_scope(tenant_id=tenant) as db:
                    detail = await WorkflowHistoryRepo(db).detail(execution_id)
                if detail and detail["status"] == "waiting_approval":
                    break
                if work.done():
                    await work
                    pytest.fail(f"Producer ended before approval: {frames}")
                await asyncio.sleep(0.05)
        # A fresh viewer can discover the exact history ID from current state.
        async with session_scope(tenant_id=tenant) as db:
            record = await ExecutionRepo(db, actor).latest_execution(wf_id)
            current = await routes._history_status(record, db)
        assert current.history_id == execution_id and current.status == "running"
        with pytest.raises(HTTPException) as conflict:
            await routes._reject_live_execution(tenant, wf_id, record)
        assert conflict.value.status_code == 409
        if outcome in {"approve", "reject", "revoked", "notification_failure"}:
            async with session_scope(tenant_id=tenant) as db:
                await WorkflowHistoryRepo(db).request_decision(
                    execution_id,
                    detail["approvals"][0]["id"],
                    actor_user_id=actor,
                    approved=outcome in {"approve", "notification_failure"},
                )
        elif outcome == "cancel":
            # Durable cancellation also works with no API-local turn handle.
            async with session_scope(tenant_id=tenant) as db:
                await routes._request_history_cancel(ExecutionRepo(db, actor), execution_id)
        elif outcome == "process_loss":
            group = next(iter(session._history_executions.groups.values()))
            await next(iter(group.pool._slots.values())).close()
        await asyncio.wait_for(work, 20)
        async with session_scope(tenant_id=tenant) as db:
            history = WorkflowHistoryRepo(db)
            detail = await history.detail(execution_id)
            record = await ExecutionRepo(db, actor).latest_execution(wf_id)
            current = await routes._history_status(record, db)
            status = (
                "cancelled"
                if outcome == "cancel"
                else "failed"
                if outcome in {"process_loss", "revoked"}
                else "succeeded"
            )
            assert detail["status"] == status
            assert record["status"] == (
                "stopped" if outcome == "cancel" else "error" if outcome in {"process_loss", "revoked"} else "success"
            )
            assert current.history_id == execution_id
            if status == "succeeded":
                result_key = "node_2" if node_only else "__end__"
                assert detail["result"]["final_outputs"][result_key] == {"approved": outcome in {"approve", "notification_failure"}}
                assert any(frame.get("node_id") == "node_2" and frame.get("status") == "completed" for frame in frames)
            events = await history.events(execution_id)
            if outcome == "notification_failure":
                notification.assert_awaited_once()
                assert sum(e["type"] == "approval_resolved" for e in events) == 1
            observed_nodes = {e["node_id"] for e in events if e["type"] == "node_event"}
            if outcome == "revoked":
                assert "node_3" not in observed_nodes
                assert detail["result"]["error_dict"] == {"__engine__": "execution_resume_failed"}
            if node_only:
                assert observed_nodes == {"node_2"}
        assert not session._history_executions.groups
        assert session._begin_activity.call_count == session._end_activity.call_count
    finally:
        stop.set()
        if not work.done():
            await asyncio.wait_for(work, 15)
        await stream.aclose()
        stop_registry.discard(execution_id)
        if hasattr(session, "_history_executions"):
            await session._history_executions.shutdown()
