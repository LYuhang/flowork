"""Scheduled worker and dispatcher with real RPC execution and database history.

Workspace provisioning and external identity/resource brokers are fixtures.
The worker claim, schedule deduplication, history, approvals and results use
the real implementations; deployment acceptance remains a separate gate.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from tests.storage.test_workflow_history import approval_graph
from tests.test_scheduled_runs import _seed_tenant_user_workflow
from vibecanvas_api.background_tasks import scheduled_runs as worker
from vibecanvas_api.services import service_account_resources, workflow_resources
from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
from vibecanvas_api.services.sandbox.manager import SandboxSession
from vibecanvas_api.storage import db as db_mod
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


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
@pytest.mark.parametrize("outcome", ["approve", "reject", "timeout", "cancel", "history_cancel", "process_loss"])
async def test_scheduled_review_allows_overlap_and_persists_isolated_result(pg_engine, monkeypatch, outcome):
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required")
    tenant, actor, wf_id = await _seed_tenant_user_workflow(pg_engine)
    task_id, schedule_id, execution_id = uuid4(), uuid4(), uuid4()
    graph = approval_graph()
    if outcome == "timeout":
        graph["node_2"]["node_config"]["timeout_seconds"] = 3
    due_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    async with session_scope(tenant_id=str(tenant)) as db:
        await WorkflowRepo(db, str(actor)).commit(wf_id, graph, target_major=1)
        repo = TasksRepo(db)
        await repo.create_schedule(
            task_id=task_id,
            schedule_id=schedule_id,
            tenant_id=tenant,
            user_id=actor,
            workflow_id=wf_id,
            name="Review schedule",
            enabled=True,
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=300,
            timezone="UTC",
            input_preset={},
            mount_enabled=False,
            notification_policy={},
            next_run_at=due_at,
        )
        await repo.create_scheduled_execution(
            execution_id=execution_id,
            tenant_id=tenant,
            schedule_id=schedule_id,
            workflow_id=wf_id,
            run_key="first-review",
            trigger_type="manual",
            input_snapshot={},
        )
    session = SimpleNamespace(
        tenant_id=str(tenant),
        user_id=str(actor),
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

    session.execute_workflow_job, session.close_workflow_pool = execute, close_pool
    manager = SimpleNamespace(get_session=AsyncMock(return_value=session), close_session=close_session)
    monkeypatch.setattr(worker, "get_sandbox_manager", lambda: manager)
    monkeypatch.setattr(
        worker,
        "_scheduled_execution_lease",
        lambda **_: SimpleNamespace(
            created_by=actor,
            service_account_id=uuid4(),
            generation=1,
        ),
    )
    monkeypatch.setattr(worker, "prepare_code_pythonpath", AsyncMock(return_value=None))
    monkeypatch.setattr(service_account_resources, "refresh_scheduled_resources", AsyncMock())
    monkeypatch.setattr(workflow_resources, "prepare_execution_resources", AsyncMock(return_value=None))
    monkeypatch.setattr(worker, "_publish", lambda *args: None)
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)
    enqueue = AsyncMock()
    monkeypatch.setattr(worker, "enqueue_background_job_in_transaction", enqueue)
    token = current_sync_tenant_id.set(str(tenant))
    run = asyncio.create_task(
        worker._execute_scheduled_run(
            task_id=task_id,
            schedule_id=schedule_id,
            execution_id=execution_id,
            tenant_id=str(tenant),
            user_id=str(actor),
            workflow_id=wf_id,
        )
    )
    try:
        async with asyncio.timeout(20):
            while True:
                async with session_scope(tenant_id=str(tenant)) as db:
                    detail = await WorkflowHistoryRepo(db).detail(str(execution_id))
                if detail and detail["status"] == "waiting_approval":
                    break
                if run.done():
                    await run
                    async with session_scope(tenant_id=str(tenant)) as db:
                        ended = await TasksRepo(db).get_scheduled_execution(execution_id)
                        pytest.fail(f"scheduled worker ended before approval: {ended.error}")
                await asyncio.sleep(0.05)
        assert manager.get_session.call_args.kwargs["lease"] == "resident"
        assert detail["source_type"] == "task" and detail["source_id"] == str(task_id)
        assert detail["initiator_user_id"] == str(actor)
        assert detail["workflow_version"] == "v1.sv1"
        async with session_scope(tenant_id=str(tenant)) as db:
            assert (await TasksRepo(db).get_scheduled_execution(execution_id)).status == "running"
        await worker._dispatch_due_scheduled_runs()
        enqueue.assert_awaited_once()
        async with session_scope(tenant_id=str(tenant)) as db:
            repo = TasksRepo(db)
            rows, total = await repo.list_scheduled_executions(schedule_id=schedule_id)
            overlap = next(row for row in rows if row.id != execution_id)
            assert total == 2 and overlap.status == "queued"
            assert overlap.error is None and overlap.started_at is None and overlap.finished_at is None
            assert (await repo.get_schedule(schedule_id)).next_run_at > datetime.now(timezone.utc)
        # Repeating the tick does not queue or accumulate another execution.
        await worker._dispatch_due_scheduled_runs()
        enqueue.assert_awaited_once()
        if outcome in {"approve", "reject"}:
            async with session_scope(tenant_id=str(tenant)) as db:
                await WorkflowHistoryRepo(db).request_decision(
                    str(execution_id),
                    detail["approvals"][0]["id"],
                    actor_user_id=str(actor),
                    approved=outcome == "approve",
                )
        elif outcome == "cancel":
            async with session_scope(tenant_id=str(tenant)) as db:
                await TasksRepo(db).update_scheduled_execution(execution_id, status="cancelling")
        elif outcome == "history_cancel":
            async with session_scope(tenant_id=str(tenant)) as db:
                await WorkflowHistoryRepo(db).request_cancel(str(execution_id))
        elif outcome == "process_loss":
            group = session._history_executions.groups[execution_id.hex]
            await next(iter(group.pool._slots.values())).worker.close()
        await asyncio.wait_for(run, 20)
        async with session_scope(tenant_id=str(tenant)) as db:
            repo = TasksRepo(db)
            actual = await repo.get_scheduled_execution(execution_id)
            detail = await WorkflowHistoryRepo(db).detail(str(execution_id))
            status = (
                "cancelled"
                if outcome in {"cancel", "history_cancel"}
                else "failed"
                if outcome in {"process_loss", "timeout"}
                else "succeeded"
            )
            assert actual.status == status
            assert detail["status"] == ("timed_out" if outcome == "timeout" else status)
            if outcome == "timeout":
                assert "approval_timeout" in actual.error
                assert not actual.result["final_outputs"]
                assert detail["approvals"][0]["status"] == "timeout"
                events = await WorkflowHistoryRepo(db).events(str(execution_id))
                assert not any(e.get("node_id") == "node_3" for e in events)
            if outcome in {"approve", "reject"}:
                assert actual.result["final_outputs"]["__end__"] == {"approved": outcome == "approve"}
                assert actual.result["execution_id"] == str(execution_id)
                assert actual.result["execution_url"] == f"/workflow-executions/{execution_id}"
                events = await WorkflowHistoryRepo(db).events(str(execution_id))
                assert any(e.get("node_id") == "node_3" and e.get("status") == "success" for e in events)
            assert (await repo.get_schedule(schedule_id)).last_status == "queued"
            task = await repo.get(task_id)
            assert task.status == "queued" and task.payload["queued_count"] == 1
            assert task.result is None and task.error is None
            # A later due time creates one new invocation, independently of
            # the queued occurrence and the outcome of the first execution.
            await repo.update_schedule(schedule_id, next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        await worker._dispatch_due_scheduled_runs()
        assert enqueue.await_count == 2
        admitted = UUID(enqueue.await_args.kwargs["kwargs"]["execution_id"])
        assert admitted not in {execution_id, overlap.id}
        assert not session._history_executions.groups
        assert session._begin_activity.call_count == session._end_activity.call_count
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await close_session()
        current_sync_tenant_id.reset(token)
