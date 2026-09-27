from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from sqlalchemy import text


@pytest.mark.parametrize("mount_enabled", [False, True])
@pytest.mark.parametrize("outcome", ["result", "empty", "error", "cleanup_error"])
async def test_worker_uses_frozen_mount_and_private_scope_and_always_finalizes(
    monkeypatch, mount_enabled, outcome,
):
    from vibecanvas_api.background_tasks import scheduled_runs as worker
    from vibecanvas_api.services.task_worker import WorkerClaim

    task_id, schedule_id, execution_id, tenant, user = [uuid.uuid4() for _ in range(5)]
    claim = WorkerClaim("schedule", execution_id, uuid.uuid4())
    scope = claim.scope_id
    sandbox = object()
    manager = SimpleNamespace(get_session=AsyncMock(return_value=sandbox), close_session=AsyncMock())
    monkeypatch.setattr(worker, "get_sandbox_manager", lambda: manager)
    monkeypatch.setattr(worker, "_claim_execution", AsyncMock(return_value=claim))
    monkeypatch.setattr(worker, "watch_worker", AsyncMock())
    monkeypatch.setattr(worker, "_execution_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "_watch_cancellation", AsyncMock())
    monkeypatch.setattr(worker, "_scheduled_execution_lease", lambda **kw: SimpleNamespace(
        created_by=user, service_account_id=uuid.uuid4(), generation=1))
    graph = {"__meta__": {"workflow_id": "wf-original"}}
    monkeypatch.setattr(worker, "run_in_short_session", lambda fn: ({"value": 0}, graph, mount_enabled))
    monkeypatch.setattr(worker, "inject_into_run_context_async", AsyncMock(return_value={}))
    monkeypatch.setattr(worker, "_snapshot_schedule", lambda _: {"enabled": False})
    updates = []
    monkeypatch.setattr(worker, "_update_execution", lambda eid, **kw: updates.append(kw))
    for name in ("_update_task", "_update_schedule", "_emit"):
        monkeypatch.setattr(worker, name, lambda *a, **kw: None)

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if outcome == "empty":
                raise StopAsyncIteration
            if outcome == "error":
                raise RuntimeError("Sandbox released.")
            return {"type": "result", "final_outputs": {"answer": 0}}

        async def aclose(self):
            if outcome == "cleanup_error":
                raise RuntimeError("Connection lost during cleanup.")

    def stream(**kw):
        assert kw["workflow"] is graph
        assert kw["workflow_run_id"] == scope
        assert kw["timeout"] is None
        return Stream()

    monkeypatch.setattr(worker, "stream_workflow_job", stream)
    await worker._execute_scheduled_run(task_id=task_id, schedule_id=schedule_id,
        execution_id=execution_id, tenant_id=str(tenant), user_id=str(user), workflow_id="wf-original")
    manager.get_session.assert_awaited_once_with(str(tenant), scope, user_id=str(user),
        expose_run=True, expose_mount=mount_enabled)
    manager.close_session.assert_awaited_once_with(str(tenant), scope)
    assert updates[-1]["status"] == ("failed" if outcome in {"empty", "error"} else "succeeded")
    assert bool(updates[-1]["error"]) is (outcome in {"empty", "error"})


async def _seed_tenant_user_workflow(pg_engine):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": tenant_id},
        )
        await c.execute(
            text("INSERT INTO users(user_id, tenant_id, email) VALUES (:u, :t, :e)"),
            {"u": user_id, "t": tenant_id, "e": f"sched-{uuid.uuid4().hex[:6]}@example.com"},
        )
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo

    async with session_scope(tenant_id=str(tenant_id)) as session:
        await WorkflowRepo(session, str(user_id)).create_workflow(
            wf_id=wf_id,
            name="Scheduled Test",
        )
    return tenant_id, user_id, wf_id


def test_compute_next_run_at_interval():
    from vibecanvas_api.services.scheduled_runs import compute_next_run_at

    base = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    out = compute_next_run_at(
        schedule_type="interval",
        timezone_name="UTC",
        interval_seconds=3600,
        base=base,
    )
    assert out == datetime(2026, 7, 10, 11, 0, tzinfo=timezone.utc)


def test_future_cron_start_is_a_lower_bound_not_an_off_cron_execution():
    from vibecanvas_api.services.scheduled_runs import compute_next_run_at
    assert compute_next_run_at(schedule_type="cron", timezone_name="UTC", cron_expr="0 * * * *",
        base=datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc),
        start_at=datetime(2026, 7, 10, 11, 30, tzinfo=timezone.utc),
    ) == datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_execution_snapshots_follow_only_selected_branch_and_survive_updates(pg_engine):
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo
    from vibecanvas_api.storage.repo_tasks import TasksRepo
    tenant, user, workflow = await _seed_tenant_user_workflow(pg_engine)
    task_id, schedule_id, first_id, second_id = [uuid.uuid4() for _ in range(4)]
    async with session_scope(tenant_id=str(tenant)) as session:
        graphs = WorkflowRepo(session, str(user))
        await graphs.commit(workflow, {"marker": "branch-one"}, target_major=1)
        await graphs.new_version(workflow, {"marker": "branch-two"})
        repo = TasksRepo(session)
        await repo.create_schedule(task_id=task_id, schedule_id=schedule_id, tenant_id=tenant,
            user_id=user, workflow_id=workflow, name="snapshot", enabled=False,
            schedule_type="interval", cron_expr=None, interval_seconds=60, timezone="UTC",
            input_preset={"number": 0}, mount_enabled=False, notification_policy={}, next_run_at=None,
            workflow_selector={"major": "v1"})
        first = await repo.create_scheduled_execution(execution_id=first_id, tenant_id=tenant,
            schedule_id=schedule_id, workflow_id=workflow, run_key="one", trigger_type="manual", input_snapshot={"number": 0})
        assert first.workflow_snapshot["version"] == "v1.sv1"
        assert first.workflow_snapshot["workflow"]["marker"] == "branch-one"
        assert first.workflow_snapshot["mount_enabled"] is False
        await repo.update_schedule(schedule_id, mount_enabled=True)
        await graphs.commit(workflow, {"marker": "branch-one-new"}, target_major=1)
        await repo.update_scheduled_execution(first_id, status="succeeded", result={"value": False})
        second = await repo.create_scheduled_execution(execution_id=second_id, tenant_id=tenant,
            schedule_id=schedule_id, workflow_id=workflow, run_key="two", trigger_type="manual", input_snapshot={"number": 2})
        assert second.workflow_snapshot["version"] == "v1.sv2"
        assert second.workflow_snapshot["mount_enabled"] is True
        await repo.update_schedule(schedule_id, workflow_selector={"version": "v1.sv1"}, start_at=None)
        fixed = await repo.create_scheduled_execution(execution_id=uuid.uuid4(), tenant_id=tenant,
            schedule_id=schedule_id, workflow_id=workflow, run_key="three", trigger_type="manual", input_snapshot={})
        assert fixed.workflow_snapshot["workflow"]["marker"] == "branch-one"
    async with session_scope(tenant_id=str(tenant)) as session:
        first = await TasksRepo(session).get_scheduled_execution(first_id)
        assert first.workflow_snapshot["version"] == "v1.sv1"
        assert first.workflow_snapshot["mount_enabled"] is False
        assert first.input_snapshot == {"number": 0}
        assert first.result == {"value": False}


def test_scheduled_run_routes_and_queue_are_registered():
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts
    from vibecanvas_api.services.queue_routing import route_for

    paths = {
        getattr(route, "path", "")
        for route in application_route_contexts(build_app())
    }
    assert "/api/v1/tasks/scheduled-runs" in paths
    assert "/api/v1/tasks/scheduled-runs/{task_id}" in paths
    assert "/api/v1/tasks/scheduled-runs/{task_id}/run-now" in paths
    assert route_for("scheduled_run") == "interactive"


def test_scheduled_execution_disposes_loop_bound_resources(monkeypatch):
    import vibecanvas_api.background_tasks.scheduled_runs as scheduled_runs

    run = AsyncMock()
    dispose_db = AsyncMock()
    dispose_rpc = AsyncMock()
    monkeypatch.setattr(scheduled_runs, "_execute_scheduled_run", run)
    monkeypatch.setattr(scheduled_runs, "dispose_engine", dispose_db)
    monkeypatch.setattr(
        scheduled_runs,
        "dispose_sandbox_rpc_client",
        dispose_rpc,
    )
    ids = [str(uuid.uuid4()) for _ in range(4)]

    for _ in range(2):
        scheduled_runs.execute_scheduled_run(
            task_id=ids[0],
            schedule_id=ids[1],
            execution_id=ids[2],
            tenant_id=ids[3],
            user_id=ids[3],
            workflow_id="wf-loop-safe",
        )

    assert run.await_count == 2
    assert dispose_rpc.await_count == 2
    assert dispose_db.await_count == 4
    assert [call.kwargs for call in dispose_db.await_args_list] == [
        {"close": False}, {}, {"close": False}, {},
    ]


@pytest.mark.asyncio
async def test_schedule_repo_create_and_dedupe_execution(pg_engine):
    from vibecanvas_api.services.scheduled_runs import compute_next_run_at
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id, wf_id = await _seed_tenant_user_workflow(pg_engine)
    task_id = uuid.uuid4()
    schedule_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    next_run = compute_next_run_at(
        schedule_type="interval",
        timezone_name="UTC",
        interval_seconds=3600,
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        task, schedule = await repo.create_schedule(
            task_id=task_id,
            schedule_id=schedule_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=wf_id,
            name="Daily report",
            enabled=True,
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=3600,
            timezone="UTC",
            input_preset={"source": "/mount/docs"},
            mount_enabled=False,
            notification_policy={"enabled": True, "on": ["failed"]},
            next_run_at=next_run,
        )
        assert task.status == "enabled"
        assert schedule.input_preset == {"source": "/mount/docs"}
        first = await repo.create_scheduled_execution(
            execution_id=execution_id,
            tenant_id=tenant_id,
            schedule_id=schedule_id,
            workflow_id=wf_id,
            run_key="rk",
            trigger_type="manual",
            input_snapshot=schedule.input_preset,
        )
        duplicate = await repo.create_scheduled_execution(
            execution_id=uuid.uuid4(),
            tenant_id=tenant_id,
            schedule_id=schedule_id,
            workflow_id=wf_id,
            run_key="rk",
            trigger_type="manual",
            input_snapshot=schedule.input_preset,
        )
        assert first is not None
        assert duplicate is None

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        rows, total = await repo.list_scheduled_executions(
            schedule_id=schedule_id,
        )
        assert total == 1
        assert rows[0].run_key == "rk"


@pytest.mark.asyncio
async def test_schedule_name_is_inside_strict_private_envelope(pg_engine):
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id, wf_id = await _seed_tenant_user_workflow(pg_engine)
    task_id = uuid.uuid4()
    schedule_id = uuid.uuid4()
    title = "private-schedule-title-sentinel"
    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = TasksRepo(session)
        _, schedule = await repo.create_schedule(
            task_id=task_id,
            schedule_id=schedule_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=wf_id,
            name=title,
            enabled=True,
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=300,
            timezone="UTC",
            input_preset={},
            mount_enabled=False,
            notification_policy={},
            next_run_at=None,
        )
        assert title not in schedule.private_ciphertext
        assert schedule.private_schema_version == 2
        columns = set((await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='task_schedules'"
        ))).scalars())
        assert "name" not in columns

    async with session_scope(tenant_id=str(tenant_id)) as session:
        restored = await TasksRepo(session).get_schedule(schedule_id)
        assert restored is not None
        assert restored.name == title


@pytest.mark.asyncio
async def test_due_dispatch_uses_encrypted_schedule_and_task_documents(
    pg_engine,
    monkeypatch,
):
    from datetime import timedelta

    import vibecanvas_api.background_tasks.scheduled_runs as scheduled_runs
    from vibecanvas_api.storage import db as db_mod
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id, user_id, wf_id = await _seed_tenant_user_workflow(pg_engine)
    task_id = uuid.uuid4()
    schedule_id = uuid.uuid4()
    due_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    async with session_scope(tenant_id=str(tenant_id)) as session:
        await TasksRepo(session).create_schedule(
            task_id=task_id,
            schedule_id=schedule_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=wf_id,
            name="Encrypted due schedule",
            enabled=True,
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=300,
            timezone="UTC",
            input_preset={"private": "input"},
            mount_enabled=False,
            notification_policy={},
            next_run_at=due_at,
        )

    sent: list[dict] = []
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)
    async def _capture_enqueue(_session, name, **kwargs):
        sent.append({"name": name, **kwargs})

    monkeypatch.setattr(
        scheduled_runs,
        "enqueue_background_job_in_transaction",
        _capture_enqueue,
    )
    await scheduled_runs._dispatch_due_scheduled_runs()

    async with session_scope(tenant_id=str(tenant_id)) as session:
        repo = TasksRepo(session)
        schedule = await repo.get_schedule(schedule_id)
        task = await repo.get(task_id)
        executions, total = await repo.list_scheduled_executions(
            schedule_id=schedule_id,
        )
    assert schedule is not None and schedule.next_run_at > due_at
    assert task is not None and task.payload["next_run_at"] is not None
    assert total == 1
    assert executions[0].status == "queued"
    assert executions[0].input_snapshot == {"private": "input"}
    assert sent and sent[0]["name"] == "scheduled_runs.execute"
    assert sent[0]["kwargs"] == {"execution_id": str(executions[0].id)}
