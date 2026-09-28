from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import text

from vibecanvas_api.background_tasks import task_recovery
from vibecanvas_api.services import task_worker
from vibecanvas_api.services.sandbox.manager import SandboxManager
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models_tasks import Task
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from vibecanvas_api.storage.sync_session import current_sync_tenant_id, run_in_short_session


async def seed(pg_engine, kind):
    tenant, user, task_id, schedule_id, execution_id = [uuid4() for _ in range(5)]
    workflow = f"recovery-{uuid4().hex[:12]}"
    async with pg_engine.begin() as connection:
        await connection.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:id,'recovery')"), {"id": tenant})
        await connection.execute(text("INSERT INTO users(user_id,tenant_id,email) VALUES (:id,:tenant,:email)"),
            {"id": user, "tenant": tenant, "email": f"{user}@example.com"})
    async with session_scope(tenant_id=str(tenant)) as session:
        await WorkflowRepo(session, str(user)).create_workflow(wf_id=workflow, name="Recovery test")
        repo = TasksRepo(session)
        if kind == "batch":
            await repo.create(task_id=task_id, tenant_id=tenant, user_id=user, workflow_id=workflow,
                task_type="batch_exec", payload={"data_source": {"rows": [{"x": 1}]}}, background_job_id="delivery-one")
        else:
            await repo.create_schedule(task_id=task_id, schedule_id=schedule_id, tenant_id=tenant,
                user_id=user, workflow_id=workflow, name="Recovery schedule", enabled=False,
                schedule_type="interval", cron_expr=None, interval_seconds=3600, timezone="UTC",
                input_preset={}, mount_enabled=False, notification_policy={}, next_run_at=None,
                workflow_selector={"major": "v1"})
            await repo.create_scheduled_execution(execution_id=execution_id, tenant_id=tenant,
                schedule_id=schedule_id, workflow_id=workflow, run_key="one", trigger_type="manual",
                input_snapshot={})
    return tenant, task_id, task_id if kind == "batch" else execution_id


@pytest.mark.parametrize("kind", ["batch", "schedule"])
async def test_only_one_concurrent_claim_and_writes_are_fenced(pg_engine, kind):
    tenant, _, resource_id = await seed(pg_engine, kind)

    async def claim():
        async with session_scope(tenant_id=str(tenant)) as session:
            return await task_worker.claim_worker(session, kind, resource_id)

    claims = await asyncio.gather(claim(), claim())
    assert sum(item is not None for item in claims) == 1
    owner = next(item for item in claims if item is not None)
    context = task_worker.current_claim.set(owner)
    try:
        async with session_scope(tenant_id=str(tenant)) as session:
            await task_worker.assert_worker_owner(session)
            assert await task_worker.heartbeat_worker(session, owner)
        async with session_scope(tenant_id=str(tenant)) as session:
            row = await session.get(owner.model, resource_id)
            row.worker_recovery_pending = True
        async with session_scope(tenant_id=str(tenant)) as session:
            assert not await task_worker.heartbeat_worker(session, owner)
            with pytest.raises(task_worker.WorkerOwnershipLost):
                await task_worker.assert_worker_owner(session)
        tenant_context = current_sync_tenant_id.set(str(tenant))
        try:
            # Scheduled workers call this sync bridge from an active loop.
            # Its dedicated thread must retain ownership, not only tenant RLS.
            with pytest.raises(task_worker.WorkerOwnershipLost):
                run_in_short_session(task_worker.assert_worker_owner)
        finally:
            current_sync_tenant_id.reset(tenant_context)
    finally:
        task_worker.current_claim.reset(context)


async def test_old_delivery_cannot_claim_a_resumed_batch(pg_engine):
    tenant, task_id, _ = await seed(pg_engine, "batch")
    async with session_scope(tenant_id=str(tenant)) as session:
        row = await session.get(Task, task_id)
        row.status = "resuming"
        row.background_job_id = "delivery-two"
    async with session_scope(tenant_id=str(tenant)) as session:
        assert await task_worker.claim_worker(session, "batch", task_id, delivery_id="delivery-one") is None
        owner = await task_worker.claim_worker(session, "batch", task_id, delivery_id="delivery-two")
        assert owner is not None


@pytest.mark.parametrize("kind", ["batch", "schedule"])
async def test_recovery_waits_for_stop_then_records_unknown_without_reexecution(pg_engine, monkeypatch, kind):
    tenant, task_id, resource_id = await seed(pg_engine, kind)
    monkeypatch.setattr(task_recovery, "short_admin_session", lambda: session_scope(tenant_id=str(tenant)))
    manager = SimpleNamespace(terminate_task_scope=AsyncMock(return_value={"stopped": False}))
    monkeypatch.setattr(task_recovery, "get_sandbox_manager", lambda: manager)
    async with session_scope(tenant_id=str(tenant)) as session:
        owner = await task_worker.claim_worker(session, kind, resource_id)
    await task_recovery.reconcile_task_workers()
    manager.terminate_task_scope.assert_not_awaited()  # Healthy silent work is not timed out.
    async with session_scope(tenant_id=str(tenant)) as session:
        row = await session.get(owner.model, resource_id)
        row.worker_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=180)
    await task_recovery.reconcile_task_workers()
    async with session_scope(tenant_id=str(tenant)) as session:
        row = await session.get(owner.model, resource_id)
        assert row.status == "running" and row.worker_recovery_pending
        assert not await task_worker.heartbeat_worker(session, owner)
    manager.terminate_task_scope.assert_awaited_once_with(str(tenant), owner.scope_id)
    manager.terminate_task_scope.return_value = {"stopped": True}
    await task_recovery.reconcile_task_workers()
    await task_recovery.reconcile_task_workers()  # Idempotent; no re-execution or duplicate event.
    assert manager.terminate_task_scope.await_count == 2
    async with session_scope(tenant_id=str(tenant)) as session:
        repo = TasksRepo(session)
        row = await (repo.get(resource_id) if kind == "batch" else repo.get_scheduled_execution(resource_id))
        assert row.status == "failed"
        assert row.worker_token is None and not row.worker_recovery_pending
        assert row.result["outcome_unknown"] is True
        if kind == "batch":
            assert row.result["can_resume"] is False
        else:
            assert (await repo.get(task_id)).status == "paused"
        events = await repo.events_for_task(task_id=task_id)
        assert sum(e.payload.get("action") == "task.worker_lost" for e in events) == 1
        context = task_worker.current_claim.set(owner)
        try:
            with pytest.raises(task_worker.WorkerOwnershipLost):
                await task_worker.assert_worker_owner(session)
        finally:
            task_worker.current_claim.reset(context)


async def test_failed_heartbeat_stops_local_worker(monkeypatch):
    stop = asyncio.Event()
    monkeypatch.setattr(task_worker, "run_in_short_session", lambda fn: False)
    with pytest.raises(task_worker.WorkerOwnershipLost):
        await task_worker.watch_worker(task_worker.WorkerClaim("batch", uuid4(), uuid4()), stop)
    assert stop.is_set()


async def test_recovery_stop_is_strict_and_revokes_scope(monkeypatch):
    manager = SandboxManager(max_resident=2, idle_ttl_s=60)
    scope = task_worker.WorkerClaim("batch", uuid4(), uuid4()).scope_id
    with pytest.raises(ValueError):
        await manager.terminate_task_scope("tenant", "chat-owned-scope")
    handle = SimpleNamespace(proc=SimpleNamespace(poll=Mock(return_value=None)))
    pool = SimpleNamespace(_handles=[handle], stop=Mock())
    session = SimpleNamespace(tenant_id="tenant", wf_id=scope,
        closed=False, _lifecycle_state="warm", _fileop_pool=pool,
        _transition_lifecycle=Mock())
    manager._sessions[("tenant", scope)] = session
    with pytest.raises(RuntimeError, match="not confirmed"):
        await manager.terminate_task_scope("tenant", scope)
    assert manager._sessions[("tenant", scope)] is session
    with pytest.raises(RuntimeError, match="revoked"):
        await manager.get_session("tenant", scope)
    handle.proc.poll.return_value = 0
    monkeypatch.setattr(manager, "_close_session_best_effort", AsyncMock())
    assert (await manager.terminate_task_scope("tenant", scope))["stopped"] is True
    assert ("tenant", scope) not in manager._sessions
    assert (await manager.terminate_task_scope("tenant", scope))["already_absent"] is True
