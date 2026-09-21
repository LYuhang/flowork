"""Batch background implementation with ``TasksRepo`` and an object store.

The sandbox has no Docker → no Redis broker, no LocalStack S3. So:

* :class:`InMemoryObjectStore` is the default ``object_store.provider``,
  exposed via the module-level ``_global_inmemory_store`` singleton so
  tests can introspect bytes uploaded by the task body.
* The best-effort ``_publish`` swallows any Redis error, so even without
  a broker the task body finishes cleanly.

Coverage in this file:

* ``test_batch_exec_is_runtime_neutral`` — business code is a plain callable.
* ``test_inmemory_object_store_roundtrip`` — :func:`get_object_store`
  honours ``provider="inmemory"`` and ``put_bytes`` / ``get_bytes``
  roundtrip.
* ``test_tasks_repo_create_update_event`` — :class:`TasksRepo` CRUD
  smoke against the real Postgres test DB.
* ``test_batch_exec_eager_end_to_end`` — full task run: seed a tenant
  + user + workflow row, invoke ``batch_exec`` synchronously,
  assert the ``tasks`` row finishes, ``task_events`` accumulates the
  expected events, and the CSV lands in the in-memory store.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest
from sqlalchemy import text
from vibecanvas_api.background_tasks.batch_exec import (
    _watch_durable_cancel,
    batch_exec,
)
from vibecanvas_api.services.batch_runtime import BatchProgress, BatchRunResult
from vibecanvas_api.services.object_store import (
    _global_inmemory_store,
    get_object_store,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.sync_session import (
    current_sync_tenant_id,
    run_in_short_session,
)
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


def test_batch_exec_is_runtime_neutral():
    assert callable(batch_exec)
    assert not hasattr(batch_exec, "delay")


async def test_durable_cancel_watcher_sets_worker_event(monkeypatch):
    """A running worker observes a soft-cancel persisted by the API."""
    snapshots = iter(({"status": "running"}, {"status": "cancelling"}))
    calls = 0

    def _snapshot(_task_id):
        nonlocal calls
        calls += 1
        return next(snapshots)

    monkeypatch.setattr(
        "vibecanvas_api.background_tasks.batch_exec._task_snapshot",
        _snapshot,
    )
    stop_event = threading.Event()

    await _watch_durable_cancel(
        uuid.uuid4(),
        stop_event,
        poll_seconds=0.001,
    )

    assert stop_event.is_set()
    assert calls == 2


def test_inmemory_object_store_roundtrip():
    """Default provider is in-memory; put/get roundtrips via the URI."""
    store = get_object_store()
    uri = store.put_bytes("test/key.csv", b"hello,world\n", content_type="text/csv")
    assert uri == "memory://test/key.csv"
    # The singleton lets tests read back without going through a signed URL.
    assert _global_inmemory_store.get_bytes(uri) == b"hello,world\n"
    # signed_url on the in-memory store is a passthrough.
    assert store.signed_url(uri) == uri


def test_inmemory_store_rejects_non_memory_uri():
    """Defensive: ``get_bytes`` only accepts ``memory://`` URIs."""
    with pytest.raises(ValueError, match="Not an in-memory URI"):
        _global_inmemory_store.get_bytes("s3://bucket/key")


async def test_tasks_repo_create_update_event(app_engine):
    """``TasksRepo`` CRUD smoke against the real test DB.

    Seeds a tenant + user (auth tables — no RLS), then runs all repo
    methods under the tenant's GUC and verifies persistence.
    """
    t_a = uuid.uuid4()
    u_a = uuid.uuid4()
    async with app_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": t_a},
        )
        await c.execute(
            text("INSERT INTO users(user_id, tenant_id, email) "
                 "VALUES (:u, :t, 't9-repo@example.com')"),
            {"u": u_a, "t": t_a},
        )

    task_id = uuid.uuid4()

    async def _do(s):
        # Short sessions set app.tenant_id from the tenant ContextVar.
        repo = TasksRepo(s)
        task = await repo.create(
            task_id=task_id, tenant_id=t_a, user_id=u_a,
            workflow_id=None, task_type="batch_exec",
            payload={"hello": "world"}, background_job_id="dbos-abc",
        )
        assert task.id == task_id
        assert task.status == "queued"
        # Update — status + progress + a JSONB result.
        await repo.update_status(
            task_id, status="running", progress=0.25,
            result={"partial": True},
        )
        # Re-fetch via .get — should reflect the update.
        refreshed = await repo.get(task_id)
        assert refreshed.status == "running"
        assert refreshed.progress == pytest.approx(0.25)
        assert refreshed.result == {"partial": True}
        # Event insert returns the autogenerated id.
        ev_id = await repo.insert_event(
            task_id, "progress", {"done": 1, "total": 4}, t_a,
        )
        assert isinstance(ev_id, int) and ev_id > 0
        # list_for_tenant honours RLS (only the seeded row).
        rows, total = await repo.list_for_tenant(task_type=["batch_exec"], limit=10)
        assert total == 1
        assert len(rows) == 1
        assert rows[0].id == task_id

    token = current_sync_tenant_id.set(str(t_a))
    try:
        await asyncio.to_thread(run_in_short_session, _do)
    finally:
        current_sync_tenant_id.reset(token)


def test_tasks_repo_update_status_rejects_unknown_field(app_engine):
    """The allowlist catches typos at the repo boundary."""
    async def _do(s):
        # No row needs to exist — the allowlist check fires first.
        with pytest.raises(ValueError, match="unknown columns"):
            await TasksRepo(s).update_status(uuid.uuid4(), totally_bogus=1)

    run_in_short_session(_do)


def _minimal_workflow_dict(wf_id: str) -> dict:
    """The smallest valid Start → End workflow with one passthrough field.

    Mirrors ``engine/tests/conftest.py::simple_workflow_dict``: no
    CodeNode (no sandbox / LLM dep), and the End node references
    ``__start__.x`` so ``Workflow.trigger`` produces a non-empty
    ``previous_outputs``.
    """
    return {
        "__meta__": {
            "workflow_id": wf_id,
            "workflow_name": "batch_smoke",
            "workflow_version": 1,
            "workflow_subversion": 0,
        },
        "node_1": {
            "node_id": "node_1",
            "node_name": "__start__",
            "node_type": "StartNode",
            "node_description": "start",
            "input_fields": {},
            "output_fields": {
                "x": {"type": "string", "description": "passthrough"},
            },
            "node_config": {},
            "children": ["node_2"],
        },
        "node_2": {
            "node_id": "node_2",
            "node_name": "__end__",
            "node_type": "EndNode",
            "node_description": "end",
            "input_fields": {
                "x": {"type": "string", "value": "", "reference": "__start__.x"},
            },
            "output_fields": {
                "x": {"type": "string", "description": "passthrough"},
            },
            "node_config": {},
            "children": [],
        },
    }


async def test_batch_exec_eager_end_to_end(app_engine, monkeypatch):
    """Full task run with the in-memory store.

    Seeds tenant/user/workflow rows, then invokes the runtime-neutral body.

    * the ``tasks`` row transitions queued → finished,
    * progress hits 1.0,
    * ``results_uri`` is set + the CSV is in the in-memory store,
    * ``task_events`` accumulates batch start + per-row progress + terminal.

    Orchestration test — MOCKS the per-row runner (a). This file exercises the
    batch task's rows/CSV/reconciler wiring, NOT engine output, so the
    sandbox runner is replaced with a canned echo. This decouples the test
    from the in-process host-fallback (removed in the sandbox-only cutover):
    the per-row run is canned, so no gVisor + no fallback is needed.
    """
    # MOCK the shared batch runtime at its point of use. It echoes each row as
    # the End node output, emits per-row progress, and uploads deterministic
    # CSV/JSONL artifacts. The engine/sandbox itself is never invoked here.
    async def _fake_batch_runtime(
        *,
        task_id,
        workflow_id,
        rows,
        on_progress=None,
        **kw,
    ):
        final_rows = []
        for i, row in enumerate(rows):
            out = {
                "schema_version": 1,
                "i": i,
                "index": i,
                "status": "success",
                "attempt": 1,
                "input": row,
                "mapped_input": row,
                "output": {"__end__": dict(row)},
                "error": None,
                "execution_time": 0.01,
                "ok": True,
            }
            final_rows.append(out)
            if on_progress is not None:
                maybe = on_progress(BatchProgress(
                    index=i,
                    status="success",
                    done=i + 1,
                    total=len(rows),
                    row=out,
                ))
                if asyncio.iscoroutine(maybe):
                    await maybe
        csv_body = (
            "index,status,attempt,input,output,error,execution_time\n"
            "0,success,1,\"{\"\"x\"\": \"\"alpha\"\"}\",\"{\"\"__end__\"\": {\"\"x\"\": \"\"alpha\"\"}}\",,0.0100\n"
            "1,success,1,\"{\"\"x\"\": \"\"beta\"\"}\",\"{\"\"__end__\"\": {\"\"x\"\": \"\"beta\"\"}}\",,0.0100\n"
        ).encode("utf-8")
        jsonl_body = "\n".join(json.dumps(r) for r in final_rows).encode("utf-8") + b"\n"
        store = get_object_store()
        csv_uri = store.put_bytes(f"tasks/{task_id}/results.csv", csv_body, content_type="table/csv")
        jsonl_uri = store.put_bytes(f"tasks/{task_id}/results.jsonl", jsonl_body, content_type="application/jsonl")
        summary = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "task_status": "finished",
            "rows_total": len(rows),
            "rows_ok": len(rows),
            "rows_failed": 0,
            "artifact_uris": {"csv": csv_uri, "jsonl": jsonl_uri},
        }
        return BatchRunResult(
            status="finished",
            summary=summary,
            results_uri=csv_uri,
            artifact_uris=summary["artifact_uris"],
            rows=final_rows,
        )

    monkeypatch.setattr(
        "vibecanvas_api.background_tasks.batch_exec.run_batch_workflow",
        _fake_batch_runtime,
    )
    t_a = uuid.uuid4()
    u_a = uuid.uuid4()
    wf_id = "wf_t9_batch"
    task_id = uuid.uuid4()

    # Seed tenant + user (auth tables, no RLS).
    async with app_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": t_a},
        )
        await c.execute(
            text("INSERT INTO users(user_id, tenant_id, email) "
                 "VALUES (:u, :t, 't9-batch@example.com')"),
            {"u": u_a, "t": t_a},
        )

    # Seed private content through the same strict encrypted boundaries used
    # by production.  Tests must never depend on retired plaintext columns.
    async with session_scope(tenant_id=str(t_a)) as session:
        workflows = WorkflowRepo(session, str(u_a))
        await workflows.create_workflow(wf_id=wf_id, name="batch_smoke")
        await workflows.commit(
            wf_id,
            _minimal_workflow_dict(wf_id),
            note="init",
        )
        service_account_id = uuid.uuid4()
        await ServiceAccountsRepo(session).create_for_owner(
            service_account_id=service_account_id,
            tenant_id=t_a,
            name="batch test",
            kind="task",
            owner_resource_type="task",
            owner_resource_id=str(task_id),
            created_by=u_a,
        )
        await TasksRepo(session).create(
            task_id=task_id,
            tenant_id=t_a,
            user_id=u_a,
            workflow_id=wf_id,
            task_type="batch_exec",
            payload={},
            service_account_id=service_account_id,
        )

    # Run eagerly — no broker, no worker. Surface any exception.
    # CV is set inside the task body itself, but we set it here too so
    # the optional pre-task sanity checks in this test process see it.
    token = current_sync_tenant_id.set(str(t_a))
    try:
        await asyncio.to_thread(
            batch_exec,
            **{
                "task_id": str(task_id),
                "tenant_id": str(t_a),
                "user_id": str(u_a),
                "workflow_id": wf_id,
                "data_source": {"rows": [{"x": "alpha"}, {"x": "beta"}]},
                "column_mapping": {},  # pass-through
                "concurrency": 2,  # exercise the parallel thread-pool path
            },
        )
    finally:
        current_sync_tenant_id.reset(token)

    # The task row should be finished + progress=1.0 + results_uri set.
    async with session_scope(tenant_id=str(t_a)) as session:
        task = await TasksRepo(session).get(task_id)
    assert task is not None
    assert task.status == "finished", (
        f"expected finished, got {task.status} (err={task.error})"
    )
    assert task.progress == pytest.approx(1.0)
    assert task.results_uri is not None and task.results_uri.startswith("memory://")
    assert task.result["rows_total"] == 2
    assert task.result["rows_ok"] == 2
    assert task.result["rows_failed"] == 0

    # The CSV is uploaded to the in-memory store.
    body = _global_inmemory_store.get_bytes(task.results_uri).decode("utf-8")
    assert body.splitlines()[0] == "index,status,attempt,input,output,error,execution_time"
    assert "alpha" in body and "beta" in body

    # task_events accumulates the expected event types.
    async with session_scope(tenant_id=str(t_a)) as session:
        events = await TasksRepo(session).events_for_task(task_id=task_id)
    ev_types = [event.event_type for event in events]
    assert ev_types[0] == "state"
    assert ev_types.count("progress") == 2
    assert ev_types[-1] == "terminal"
