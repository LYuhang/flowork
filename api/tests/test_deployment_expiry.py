"""Expiry cannot release capacity ahead of confirmed worker shutdown."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
import time

import pytest
from sqlalchemy import text

from tests.test_deployment_rollout import setup_rollout
from tests.storage.test_workflow_history import approval_graph
from vibecanvas_api.services import deployment_expiry as expiry
from vibecanvas_api.services.sandbox.deployment_runtime import DeploymentRuntime
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.storage.workflow_history_repo import HistoryConflict, WorkflowHistoryRepo


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed,cancel", [(False, False), (True, False), (True, True)])
async def test_expiry_finalizes_history_only_after_process_stop(pg_engine, app_engine, monkeypatch, claimed, cancel):
    _, dep, _ = await setup_rollout(pg_engine, app_engine)
    tenant, invocation, approval = str(dep["tenant_id"]), uuid4(), uuid4().hex

    @asynccontextmanager
    async def scoped_admin():
        # Keep the sweep isolated to this test tenant using real RLS and SQL.
        async with short_session_scope(tenant_id=tenant) as session:
            yield session

    monkeypatch.setattr(expiry, "session_scope_admin", scoped_admin)
    async with short_session_scope(tenant_id=tenant) as session:
        await DeploymentInvocationsRepo(session).create(
            invocation_id=invocation,
            tenant_id=dep["tenant_id"],
            deployment_id=dep["id"],
            wf_id=dep["wf_id"],
            trigger_type="api",
            source="sync_api",
            status="running",
            revision_id=dep["active_revision_id"],
        )
        history = WorkflowHistoryRepo(session)
        await history.create(
            execution_id=str(invocation),
            tenant_id=tenant,
            wf_id=dep["wf_id"],
            source_type="deployment",
            source_id=str(dep["id"]),
            initiator_user_id=str(dep["user_id"]),
            workflow=approval_graph(),
            inputs={},
            approvers={"node_2": str(dep["user_id"])},
        )
        if claimed:
            await history.bind_runtime(str(invocation), "lost-generation")
            await history.persist_events(
                str(invocation),
                "lost-generation",
                [
                    {
                        "type": "approval_requested",
                        "seq": 1,
                        "generation": "lost-generation",
                        "invocation_id": str(invocation),
                        "approval_id": approval,
                        "node_id": "node_2",
                        "deadline": time.time() + 60,
                    }
                ],
            )
        if cancel:
            await history.request_cancel(str(invocation))
        await session.execute(
            text("""UPDATE deployment_invocations
            SET status=:status,runtime_claim=:claim,
                execution_lease_until=CASE WHEN :claimed THEN now()-interval '1 second' ELSE NULL END,
                dispatch_deadline=CASE WHEN :claimed THEN NULL ELSE now()-interval '1 second' END
            WHERE id=:id"""),
            {
                "id": invocation,
                "claimed": claimed,
                "claim": uuid4() if claimed else None,
                "status": "waiting_approval" if claimed else "running",
            },
        )
    runtime = SimpleNamespace(stop_expired_invocation=AsyncMock(return_value=False))
    if claimed:
        await expiry.expire_deployment_invocations(runtime)
        async with short_session_scope(tenant_id=tenant) as session:
            detail = await WorkflowHistoryRepo(session).detail(str(invocation))
            assert detail["status"] == "waiting_approval"
            assert detail["approvals"][0]["status"] == "pending"
            assert (
                await session.scalar(text("SELECT status FROM deployment_invocations WHERE id=:id"), {"id": invocation})
                == "waiting_approval"
            )
    runtime.stop_expired_invocation.return_value = True
    await expiry.expire_deployment_invocations(runtime)
    async with short_session_scope(tenant_id=tenant) as session:
        history = WorkflowHistoryRepo(session)
        detail = await history.detail(str(invocation))
        expected = "cancelled" if cancel else "failed"
        assert detail["status"] == expected
        assert (
            await session.scalar(text("SELECT status FROM deployment_invocations WHERE id=:id"), {"id": invocation})
            == expected
        )
        if claimed:
            assert detail["approvals"][0]["status"] == ("cancelled" if cancel else "execution_lost")
            with pytest.raises(HistoryConflict):
                await history.request_decision(
                    str(invocation), approval, actor_user_id=str(dep["user_id"]), approved=True
                )
        else:
            assert detail["error_code"] == "execution_dispatch_failed"
            runtime.stop_expired_invocation.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_expiry_waits_for_cleanup_and_keeps_other_workers(monkeypatch):
    runtime = DeploymentRuntime(SimpleNamespace())
    started, stopping, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def run(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await finish.wait()

    monkeypatch.setattr(runtime, "_run_admitted", run)
    other = SimpleNamespace(invocation_id="other", alive=True, close=AsyncMock())
    runtime._ready["tenant:revision"] = SimpleNamespace(_workflow_rpc_pool=SimpleNamespace(_slots={1: other}))
    task = asyncio.create_task(
        runtime.run(
            tenant_id="tenant",
            revision_id="revision",
            deployment_id="deployment",
            workflow={},
            inputs={},
            run_id="expired",
        )
    )
    await started.wait()
    stopped = asyncio.create_task(
        runtime.stop_expired_invocation(tenant_id="tenant", revision_id="revision", invocation_id="expired")
    )
    try:
        await asyncio.wait_for(stopping.wait(), 2)
        assert not stopped.done()
        assert runtime._active_requests["tenant:revision"] == 1
        finish.set()
        assert await asyncio.wait_for(stopped, 2)
        await asyncio.gather(task, return_exceptions=True)
        assert not runtime._invocations and not runtime._active_requests
        other.close.assert_not_awaited()
    finally:
        finish.set()
        await asyncio.gather(task, stopped, return_exceptions=True)


@pytest.mark.asyncio
async def test_restarted_runtime_requires_empty_cgroup_before_expiry():
    runtime = DeploymentRuntime(SimpleNamespace())
    runtime._resources = SimpleNamespace(release=Mock(return_value=False))
    kwargs = dict(tenant_id="tenant", revision_id="revision", invocation_id="old-execution")
    assert not await runtime.stop_expired_invocation(**kwargs)
    runtime._resources.release.return_value = True
    assert await runtime.stop_expired_invocation(**kwargs)
