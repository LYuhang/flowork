"""CLI orchestration against real RPC processes and encrypted history.

Identity admission and workspace setup are fixtures. This is development
integration coverage, not a substitute for deployed CLI acceptance.
"""

import asyncio
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from tests.storage.test_workflow_history import approval_graph, owner
from vibecanvas_api.services.agent_runtime import cli_runs
from vibecanvas_api.services import workflow_resources
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
@pytest.mark.parametrize("mode", ["graph", "node", "batch"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_cli_waits_keeps_worker_and_records_each_execution(pg_engine, monkeypatch, mode, cancel):
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required")
    tenant, actor, _ = await owner()
    wf_id = "wf-cli-" + uuid4().hex
    graph = approval_graph()
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
        writeback_vfs=AsyncMock(),
    )

    async def execute(**kwargs):
        return await SandboxSession.execute_workflow_job(session, **kwargs)

    async def close(**kwargs):
        return await SandboxSession.close_workflow_pool(session, **kwargs)

    session.execute_workflow_job = execute
    session.close_workflow_pool = close
    capability = SimpleNamespace(tenant_id=tenant, user_id=actor, turn_id=uuid4().hex)
    ctx = SimpleNamespace(tenant_id=tenant, username=actor, turn_id=capability.turn_id)
    monkeypatch.setattr(cli_runs.agent_context, "resolve_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(cli_runs, "require_workflow_action", AsyncMock())
    monkeypatch.setattr(
        cli_runs,
        "read_workflow_snapshot",
        AsyncMock(
            return_value={
                "id": wf_id,
                "version": "v1.sv1",
                "workflow": graph,
            }
        ),
    )
    monkeypatch.setattr(cli_runs, "validate_workflow_for_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(cli_runs, "_require_session", AsyncMock(return_value=session))
    monkeypatch.setattr(cli_runs, "prepare_code_pythonpath", AsyncMock(return_value=None))
    monkeypatch.setattr(workflow_resources, "prepare_execution_resources", AsyncMock(return_value=None))
    for name in ("reserve", "renew", "release"):
        monkeypatch.setattr(cli_runs.cli_run_lease, name, AsyncMock(return_value=True))
    operation = "workflow.run-batch" if mode == "batch" else "workflow.run"
    run = cli_runs.Run(capability, uuid4().hex, operation, asyncio.Queue(64))
    args = {"workflow_id": wf_id, "major": "v1", "inputs": {}, "concurrency": 1}
    if mode == "node":
        args["node"] = "node_2"
    if mode == "batch":
        args["name"] = "approval inputs"
        monkeypatch.setattr(cli_runs, "_batch_rows", lambda _: [{}, {}])
    work = asyncio.create_task(cli_runs._work(run, args))

    async def waiting(count):
        async with asyncio.timeout(20):
            while True:
                async with session_scope(tenant_id=tenant) as db:
                    repo = WorkflowHistoryRepo(db)
                    items = (await repo.history(source_type="workflow", source_id=wf_id))["items"]
                    pending = [item for item in items if item["status"] == "waiting_approval"]
                    if len(items) == count and len(pending) == 1:
                        return await repo.detail(pending[0]["id"])
                if work.done():
                    await work
                    pytest.fail("CLI ended before human approval")
                await asyncio.sleep(0.05)

    ids = []
    try:
        first = await waiting(1)
        ids.append(first["id"])
        assert first["node_id"] == ("node_2" if mode == "node" else None)
        assert first["initiator_user_id"] == actor
        # Human waiting retains the sole worker; batch input 2 has not started.
        await asyncio.sleep(0.3)
        async with session_scope(tenant_id=tenant) as db:
            assert len((await WorkflowHistoryRepo(db).history(source_type="workflow", source_id=wf_id))["items"]) == 1
        if cancel:
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(work, 15)
        else:
            async with session_scope(tenant_id=tenant) as db:
                await WorkflowHistoryRepo(db).request_decision(
                    first["id"],
                    first["approvals"][0]["id"],
                    actor_user_id=actor,
                    approved=True,
                )
            if mode == "batch":
                second = await waiting(2)
                ids.append(second["id"])
                assert second["generation"] == first["generation"]
                async with session_scope(tenant_id=tenant) as db:
                    await WorkflowHistoryRepo(db).request_decision(
                        second["id"],
                        second["approvals"][0]["id"],
                        actor_user_id=actor,
                        approved=False,
                    )
            await asyncio.wait_for(work, 15)
        events = [run.queue.get_nowait() for _ in range(run.queue.qsize())]
        assert any(e.get("row_status") == "waiting_approval" and e["execution_id"] == first["id"] for e in events)
        async with session_scope(tenant_id=tenant) as db:
            detail = await WorkflowHistoryRepo(db).detail(first["id"])
            assert detail["status"] == ("cancelled" if cancel else "succeeded")
            if cancel:
                assert detail["approvals"][0]["status"] == "cancelled"
        if not cancel:
            assert events[-1]["status"] == "completed"
            records = [e["record"] if "record" in e else e["result"] for e in events if "record" in e or "result" in e]
            assert [r["execution_id"] for r in records] == ids
            assert all(r["execution_url"] == f"/workflow-executions/{r['execution_id']}" for r in records)
            assert records[0]["output"] == {"approved": True}
            if mode == "batch":
                assert records[1]["output"] == {"approved": False}
        assert not session._history_executions.groups
        assert session._begin_activity.call_count == session._end_activity.call_count
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        if hasattr(session, "_history_executions"):
            await session._history_executions.shutdown()
