from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text


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
