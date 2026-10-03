from datetime import datetime, timedelta, timezone
import uuid

import pytest

from vibecanvas_api.services.scheduled_runs import compute_next_run_at


def test_once_preserves_seconds_and_converts_offset():
    base = datetime(2026, 10, 5, tzinfo=timezone.utc)
    selected = datetime(2026, 10, 5, 9, 30, 17, tzinfo=timezone(timedelta(hours=8)))
    assert compute_next_run_at(schedule_type="once", timezone_name="Asia/Shanghai",
                               run_at=selected, base=base) == datetime(2026, 10, 5, 1, 30, 17, tzinfo=timezone.utc)


@pytest.mark.parametrize("selected", [None, datetime(2026, 10, 6),
    datetime(2026, 10, 4, tzinfo=timezone.utc), datetime(2026, 10, 5, tzinfo=timezone.utc)])
def test_once_rejects_missing_naive_and_expired_time(selected):
    with pytest.raises(ValueError):
        compute_next_run_at(schedule_type="once", timezone_name="UTC", run_at=selected,
                            base=datetime(2026, 10, 5, tzinfo=timezone.utc))


def test_once_rejects_invalid_display_timezone():
    with pytest.raises(Exception):
        compute_next_run_at(schedule_type="once", timezone_name="Invalid/Zone",
            run_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
            base=datetime(2026, 10, 5, tzinfo=timezone.utc))


@pytest.mark.parametrize("schedule_type", ["once", "interval"])
async def test_dispatch_retains_occurrence_while_previous_execution_active(pg_engine, monkeypatch, schedule_type):
    from .test_scheduled_runs import _seed_tenant_user_workflow
    from vibecanvas_api.background_tasks import scheduled_runs
    from vibecanvas_api.storage import db
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant, user, workflow = await _seed_tenant_user_workflow(pg_engine)
    task_id, schedule_id = uuid.uuid4(), uuid.uuid4()
    due = datetime.now(timezone.utc) - timedelta(seconds=2)
    async with session_scope(tenant_id=str(tenant)) as session:
        repo = TasksRepo(session)
        await repo.create_schedule(task_id=task_id, schedule_id=schedule_id,
            tenant_id=tenant, user_id=user, workflow_id=workflow, name="Overlap",
            enabled=True, schedule_type=schedule_type, cron_expr=None,
            interval_seconds=60 if schedule_type == "interval" else None,
            run_at=due if schedule_type == "once" else None,
            timezone="UTC", input_preset={}, mount_enabled=False,
            notification_policy={}, next_run_at=due)
        await repo.create_scheduled_execution(execution_id=uuid.uuid4(), tenant_id=tenant,
            schedule_id=schedule_id, workflow_id=workflow, run_key="manual-existing",
            trigger_type="manual", input_snapshot={}, status="running")
    monkeypatch.setattr(db, "_admin_engine", pg_engine)
    queued = []
    async def enqueue(session, name, **kwargs):
        queued.append(kwargs)
    monkeypatch.setattr(scheduled_runs, "enqueue_background_job_in_transaction", enqueue)
    await scheduled_runs._dispatch_due_scheduled_runs()
    await scheduled_runs._dispatch_due_scheduled_runs()
    async with session_scope(tenant_id=str(tenant)) as session:
        repo = TasksRepo(session)
        schedule = await repo.get_schedule(schedule_id)
        executions, total = await repo.list_scheduled_executions(schedule_id=schedule_id)
        assert total == 2
        assert {e.status for e in executions} == {"running", "queued"}
        assert (schedule.next_run_at is None) == (schedule_type == "once")
        assert schedule.concurrency_policy == "allow"
    assert len(queued) == 1

    # Finishing the newer occurrence must not hide the older active execution.
    async with session_scope(tenant_id=str(tenant)) as session:
        repo = TasksRepo(session)
        newer = next(e for e in executions if e.status == "queued")
        older = next(e for e in executions if e.status == "running")
        await repo.update_scheduled_execution(newer.id, status="succeeded",
            result={"answer": "new"}, finished_at=datetime.now(timezone.utc))
        await repo.refresh_scheduled_task(task_id)
        task = await repo.get(task_id)
        assert task.status == "running"
        assert task.payload["running_count"] == 1
        assert task.payload["queued_count"] == 0
        await repo.update_scheduled_execution(older.id, status="failed",
            error="older failure", finished_at=datetime.now(timezone.utc))
        await repo.refresh_scheduled_task(task_id)
        task = await repo.get(task_id)
        assert task.status == ("finished" if schedule_type == "once" else "enabled")
        assert task.result is None
        assert (await repo.get_scheduled_execution(newer.id)).result == {"answer": "new"}
        assert (await repo.get_scheduled_execution(older.id)).error == "older failure"
        assert task.error is None


async def test_execution_logs_filter_before_limit_and_cursor(pg_engine):
    from .test_scheduled_runs import _seed_tenant_user_workflow
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant, user, workflow = await _seed_tenant_user_workflow(pg_engine)
    task, target, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with session_scope(tenant_id=str(tenant)) as session:
        repo = TasksRepo(session)
        await repo.create(task_id=task, tenant_id=tenant, user_id=user,
            workflow_id=workflow, task_type="scheduled_run", payload={}, background_job_id=str(task))
        expected = []
        for index in range(207):
            execution = target if index in {0, 202, 206} else other
            event = await repo.insert_event(task, "log", {
                "data": {"execution_id": str(execution)}, "message": f"event-{index}"}, tenant)
            if execution == target:
                expected.append(event)
        first = await repo.events_for_task(task_id=task, execution_id=target, limit=2)
        assert [row.id for row in first] == expected[:2]
        second = await repo.events_for_task(task_id=task, execution_id=target, after_seq=first[-1].id, limit=2)
        assert [row.id for row in second] == expected[2:]
        recent = await repo.events_for_task(task_id=task, execution_id=target, descending=True, limit=2)
        assert [row.id for row in recent] == list(reversed(expected))[:2]
        earlier = await repo.events_for_task(task_id=task, execution_id=target,
            descending=True, before_seq=recent[-1].id, limit=2)
        assert [row.id for row in earlier] == expected[:1]
