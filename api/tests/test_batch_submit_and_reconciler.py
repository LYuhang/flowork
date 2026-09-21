"""Atomic submission and reconciler unit tests.

Covers:

* The DBOS schedule includes the queued-row reconciler.
* The reconciler implementation remains runtime-neutral.
* The submit body silently drops smuggled tenant/user/dbos fields —
  defence in depth: those are derived from the authenticated context,
  never from the request body.
* The reconciler re-publishes a stuck ``queued`` row whose
  ``submitted_at`` exceeds the §6.3 threshold.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_tasks import TasksRepo


def test_dbos_schedule_has_reconciler():
    from vibecanvas_api.background_workflows import SCHEDULES

    entry = next(
        item for item in SCHEDULES
        if item["schedule_name"] == "flowork-queued-reconciler"
    )
    assert entry["schedule"] == "0 */5 * * * *"
    assert entry["queue_name"] == "control"

    purge = next(
        item for item in SCHEDULES
        if item["schedule_name"] == "flowork-data-purge"
    )
    assert purge["schedule"] == "0 */5 * * * *"


def test_reconciler_is_runtime_neutral():
    from vibecanvas_api.background_tasks.reconciler import resubmit_stuck_queued
    assert callable(resubmit_stuck_queued)
    assert not hasattr(resubmit_stuck_queued, "delay")


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
        "data_source": {"rows": []},
        "column_mapping": {},
        "output": None,
        "output_columns": None,
        "concurrency": 1,
    }


@pytest.mark.asyncio
async def test_reconciler_resubmits_stuck_queued_rows(monkeypatch, pg_engine):
    """Seed a stuck queued row via pg_engine (superuser, RLS-bypassing);
    point the admin engine at it; call _resubmit(); assert enqueue fired."""
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", pg_engine)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with pg_engine.begin() as c:
        await c.execute(text(
            "INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"
        ), {"t": tenant_id})
        await c.execute(text(
            "INSERT INTO users(user_id, tenant_id, email) VALUES (:u, :t, :e)"
        ), {"u": user_id, "t": tenant_id, "e": f"recon-{uuid.uuid4().hex[:6]}@example.com"})
    async with session_scope(tenant_id=str(tenant_id)) as session:
        await TasksRepo(session).create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_id),
        )
        await session.execute(
            text(
                "UPDATE tasks SET submitted_at=now() - interval '120 seconds' "
                "WHERE id=:id"
            ),
            {"id": task_id},
        )

    sent: list[dict] = []
    import vibecanvas_api.background_tasks.reconciler as recon
    monkeypatch.setattr(
        recon,
        "enqueue_background_job",
        lambda name, **kw: sent.append({"name": name, **kw}),
    )

    await recon._resubmit()

    by_id = [s for s in sent if s.get("job_id") == str(task_id)]
    assert by_id, f"Expected re-submit for task {task_id}; got {sent}"
    assert by_id[0]["name"] == "batch_exec"
    assert by_id[0]["kwargs"] == {"task_id": str(task_id)}
