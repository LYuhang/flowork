"""Single-attempt batch submission and durable failure state."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_tasks import TasksRepo


def test_batch_submission_is_not_automatically_republished():
    from vibecanvas_api.background_workflows import RETIRED_SCHEDULES, SCHEDULES, SCHEDULE_WORKFLOWS
    assert not any(item["schedule_name"] == "flowork-queued-reconciler" for item in SCHEDULES)
    assert "flowork-queued-reconciler" in RETIRED_SCHEDULES
    assert "background.reconcile_queued" not in SCHEDULE_WORKFLOWS


def test_submit_body_silently_drops_smuggled_fields():
    """Pydantic config: smuggled tenant_id/user_id/background_job_id ignored without 422."""
    from vibecanvas_api.routes.workflows import BatchSubmitBody
    body = BatchSubmitBody.model_validate({
        "data_source": {"rows": []},
        "column_mapping": {},
        "tenant_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "background_job_id": "evil",
    })
    dumped = body.model_dump()
    # tenant_id/user_id/background_job_id smuggles are dropped; `output` + `concurrency`
    # are the legit optional fields (defaults None / 1).
    assert dumped == {
        "notification_policy": {},
        "evaluation": {"enabled": False, "script": ""},
        "data_source": {"rows": []},
        "column_mapping": {},
        "output": None,
        "output_columns": None,
        "concurrency": 1,
        "mount_enabled": False,
        "major": None,
        "version": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_submission_error_preserves_actual_worker_state(monkeypatch, pg_engine, started):
    from fastapi import HTTPException
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.routes import workflows
    from vibecanvas_api.services.task_worker import claim_worker

    tenant_id, user_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with pg_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"), {"t": tenant_id})
        await c.execute(text("INSERT INTO users(user_id, tenant_id, email) VALUES (:u, :t, :e)"),
            {"u": user_id, "t": tenant_id, "e": f"submit-{user_id}@example.com"})
    async with session_scope(tenant_id=str(tenant_id)) as session:
        await TasksRepo(session).create(task_id=task_id, tenant_id=tenant_id, user_id=user_id,
            workflow_id=None, task_type="batch_exec", payload={}, background_job_id=str(task_id))
        if started:
            assert await claim_worker(session, "batch", task_id) is not None
    monkeypatch.setattr(workflows, "_rebind_request_organization", AsyncMock())
    async with session_scope(tenant_id=str(tenant_id)) as session:
        with pytest.raises(HTTPException) as caught:
            await workflows._batch_submission_failed(session, SimpleNamespace(), task_id,
                "task_dispatch_failed", TimeoutError("queue acknowledgement unavailable"))
        assert caught.value.status_code == 503
        assert caught.value.detail["task_id"] == str(task_id)
        assert caught.value.detail["status"] == ("running" if started else "failed")
    async with session_scope(tenant_id=str(tenant_id)) as session:
        task = await TasksRepo(session).get(task_id)
        assert task.status == ("running" if started else "failed")
        if started:
            assert task.error is None and task.finished_at is None
        else:
            assert "queue acknowledgement unavailable" in task.error
            assert task.finished_at is not None
        # Late arrival or duplicate delivery cannot start this task again.
        assert await claim_worker(session, "batch", task_id) is None
