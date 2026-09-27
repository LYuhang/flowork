"""Cancel the batch-owned pool without dispatching any further rows."""
from __future__ import annotations

import asyncio
import threading

import pytest

from vibecanvas_api.services import batch_runtime


@pytest.mark.parametrize("silent", [False, True])
@pytest.mark.parametrize("mount_enabled", [False, True])
async def test_cancel_kills_owned_pool_and_skips_waiting_rows(monkeypatch, silent, mount_enabled):
    stop_event = threading.Event()

    class FakeSession:
        calls = 0
        close_calls = 0
        killed = asyncio.Event()
        pool_id = None

        async def close_workflow_pool(self, *, tenant, pool_id):
            assert tenant == "tenant-1" and pool_id == self.pool_id
            self.close_calls += 1
            self.killed.set()
            return {"closed": True}

        async def execute_workflow_job(self, **_kwargs):
            assert _kwargs["timeout"] is None
            self.pool_id = _kwargs["execution_pool_id"]
            assert len(self.pool_id) == 32
            self.calls += 1
            stop_event.set()
            if silent:
                await asyncio.wait_for(self.killed.wait(), 2)
            return {
                "status": {
                    "status": "error",
                    "error_message": "late business error",
                },
                "result": None,
            }

    class FakeCoordinator:
        def __init__(self):
            self.session = FakeSession()
            self.closed = []

        async def get_session(self, *_args, **_kwargs):
            assert _kwargs["expose_mount"] is mount_enabled
            return self.session

        async def close_session(self, tenant_id, scope_id):
            self.closed.append((tenant_id, scope_id))

    coordinator = FakeCoordinator()
    monkeypatch.setattr(batch_runtime, "get_sandbox_coordinator", lambda: coordinator)

    async def _no_dependency_layer(_workflow):
        return None

    monkeypatch.setattr(batch_runtime, "ensure_code_pythonpath", _no_dependency_layer)

    result = await batch_runtime.run_batch_workflow(
        task_id="task-soft-cancel",
        tenant_id="tenant-1",
        user_id="user-1",
        workflow_id="workflow-1",
        workflow={"__meta__": {"workflow_id": "workflow-1"}},
        rows=[{"value": "first"}, {"value": "waiting"}],
        column_mapping={},
        concurrency=1,
        mount_enabled=mount_enabled,
        stop_event=stop_event,
        prepared_run_extra={},
    )

    assert coordinator.session.calls == 1
    assert coordinator.session.close_calls == 1
    assert coordinator.closed == [("tenant-1", "batch-task-soft-cancel")]
    assert result.status == "interrupted"
    assert result.summary["cancelled"] == 2
    assert result.summary["can_resume"] is True
    assert [row["status"] for row in result.rows] == ["cancelled", "cancelled"]
    assert all("late business error" not in str(row) for row in result.rows)
    store = batch_runtime.get_object_store()
    assert result.summary["artifact_sizes"] == {
        name: len(store.fetch_bytes(batch_runtime.uri_to_key(uri)))
        for name, uri in result.artifact_uris.items()
    }
