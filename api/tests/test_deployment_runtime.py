"""Cancellation must not release a resident instance before its worker exits."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vibecanvas_api.services.sandbox.deployment_runtime import DeploymentRuntime


@pytest.mark.asyncio
async def test_resident_rpc_calls_reuse_process_and_persist_history(pg_engine, app_engine, monkeypatch):
    import shutil
    import uuid
    from tests.test_deployment_rollout import setup_rollout
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    from vibecanvas_api.services.sandbox.workflow_rpc_pool import WorkflowRpcPool
    from vibecanvas_api.storage.db import short_session_scope
    from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
    from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required")
    controller, dep, spec = await setup_rollout(pg_engine, app_engine)
    graph = await controller.graph(spec)
    tenant, revision = str(dep["tenant_id"]), str(dep["active_revision_id"])
    session = SimpleNamespace(
        provider=BubblewrapProvider(shutil.which("bwrap")), workspace_folders=(), _rw_binds=[],
        skills_dir=None, _begin_activity=Mock(), _end_activity=Mock(), _sync_mount_folder=AsyncMock(),
    )
    pool = WorkflowRpcPool.for_session(session=session, revision=revision, workflow=graph, capacity=1)
    session._workflow_rpc_pool = pool
    runtime = DeploymentRuntime(SimpleNamespace())
    monkeypatch.setattr(runtime, "prepare", AsyncMock(return_value=session))
    try:
        await pool.prewarm()
        slot = pool._slots[0]
        pid = slot.handle.proc.pid
        for value in (117, 223):
            invocation = uuid.uuid4()
            async with short_session_scope(tenant_id=tenant) as db:
                await DeploymentInvocationsRepo(db).create(
                    invocation_id=invocation, tenant_id=dep["tenant_id"], deployment_id=dep["id"],
                    wf_id=dep["wf_id"], trigger_type="api", source="sync_api", status="queued",
                    revision_id=dep["active_revision_id"],
                )
            result = await runtime.run(
                tenant_id=tenant, deployment_id=str(dep["id"]), revision_id=revision,
                workflow=graph, inputs={"x": value}, run_id=str(invocation),
            )
            assert not result["error_dict"], result
            assert result["final_outputs"]["__end__"]["y"] == value
            assert slot.handle.proc.pid == pid and slot.alive
            assert list((slot.root / "artifacts").iterdir()) == []
            assert {path.name for path in (slot.root / "control").iterdir()} == {"rpc.sock"}
            async with short_session_scope(tenant_id=tenant) as db:
                detail = await WorkflowHistoryRepo(db).detail(str(invocation))
                assert detail["status"] == "succeeded"
                assert detail["result"] == result
                assert detail["inputs"] == {"x": value}
        assert not pool.busy
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_rpc_worker_without_interrupting_sibling(monkeypatch):
    from vibecanvas_api.services.deployment_completion import complete_before_cancelling
    from vibecanvas_api.services.sandbox import workflow_execution_driver
    from vibecanvas_api.services.sandbox.workflow_rpc_pool import WorkflowRpcPool, WorkflowPoolFull

    runtime = DeploymentRuntime(SimpleNamespace())
    entered = {key: asyncio.Event() for key in ("first", "second")}
    finish = {key: asyncio.Event() for key in entered}
    stop_requested, stop_confirmed = asyncio.Event(), asyncio.Event()

    class Slot:
        invocation_id = None
        alive = False

        async def start(self):
            self.alive = True

        @complete_before_cancelling
        async def close(self):
            stop_requested.set()
            await stop_confirmed.wait()
            self.alive = False

    class Driver:
        def __init__(self, *, execution_id, slot, **kwargs):
            self.key, self.slot = execution_id, slot

        async def run(self, **kwargs):
            self.slot.invocation_id = self.key
            entered[self.key].set()
            await finish[self.key].wait()
            self.slot.invocation_id = None
            return {"outputs": self.key}

    monkeypatch.setattr(workflow_execution_driver, "WorkflowExecutionDriver", Driver)
    pool = WorkflowRpcPool(capacity=2, factory=lambda _: Slot())
    session = SimpleNamespace(_workflow_rpc_pool=pool, _begin_activity=Mock(), _end_activity=Mock())
    tasks = {key: asyncio.create_task(runtime._execute_request(session,
        workflow={}, inputs={}, extra={}, tenant_id="tenant", run_id=key,
        wf_id="wf")) for key in entered}
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        assert await pool.retire() is False
        with pytest.raises(WorkflowPoolFull):
            async with pool.acquire("overflow"):
                pytest.fail("a full pool must not admit or queue work")
        tasks["first"].cancel()
        await asyncio.wait_for(stop_requested.wait(), 2)
        tasks["first"].cancel()
        assert not tasks["first"].done()
        assert not tasks["second"].done()
        stop_confirmed.set()
        with pytest.raises(asyncio.CancelledError):
            await tasks["first"]
        assert pool.busy
        finish["second"].set()
        assert (await tasks["second"])["outputs"] == "second"
        assert not pool.busy
        assert session._begin_activity.call_count == session._end_activity.call_count == 2
    finally:
        stop_confirmed.set()
        for event in finish.values():
            event.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await pool.close()


@pytest.mark.asyncio
async def test_local_request_lease_blocks_instance_retirement(monkeypatch):
    manager = SimpleNamespace(close_session=AsyncMock())
    runtime = DeploymentRuntime(manager)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def run(**kwargs):
        entered.set()
        await finish.wait()
        return {}
    monkeypatch.setattr(runtime, '_run_admitted', run)
    revision = '00000000-0000-0000-0000-000000000001'
    task = asyncio.create_task(runtime.run(tenant_id='tenant', revision_id=revision,
        deployment_id='deployment', workflow={}, inputs={}, run_id='request'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await runtime.retire('tenant', revision) is False
        manager.close_session.assert_not_awaited()
        finish.set()
        await task
        assert await runtime.retire('tenant', revision) is True
        manager.close_session.assert_awaited_once()
        assert not runtime._active_requests
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


def test_resident_invocation_cannot_fall_back_to_embedded_execution(monkeypatch):
    from vibecanvas_api.config import config
    from vibecanvas_api.services.workflow_runner import run_workflow_sandboxed_sync
    monkeypatch.setattr(config, 'sandbox_service_mode', 'embedded')
    with pytest.raises(RuntimeError, match='SANDBOX_SERVICE_MODE=service'):
        run_workflow_sandboxed_sync(workflow_id='wf', inputs={}, tenant_id='tenant', user_id='user',
            deployment_id='dep', revision_id='revision', workflow_dict={})
    for identity in ({'deployment_id': 'dep'}, {'revision_id': 'revision'}):
        with pytest.raises(ValueError, match='both deployment_id and revision_id'):
            run_workflow_sandboxed_sync(workflow_id='wf', inputs={}, tenant_id='tenant', user_id='user',
                workflow_dict={}, **identity)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'node_failure', 'runtime_failure'])
async def test_executor_commits_completion_without_api_caller(pg_engine, app_engine, monkeypatch, outcome):
    """Execution completion survives losing the API that admitted the call."""
    from sqlalchemy import text
    from tests.test_deployment_rollout import setup_rollout
    from vibecanvas_api.storage.db import short_session_scope
    from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
    from vibecanvas_api.storage import db as db_module
    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)

    controller, dep, _ = await setup_rollout(pg_engine, app_engine)
    tenant = str(dep['tenant_id'])
    revision = str(dep['active_revision_id'])
    async with short_session_scope(tenant_id=tenant) as db:
        invocation = await DeploymentInvocationsRepo(db).create(
            tenant_id=dep['tenant_id'], deployment_id=dep['id'], wf_id=dep['wf_id'],
            trigger_type='api', source='sync_api', status='running', revision_id=dep['active_revision_id'])
        await db.execute(text("UPDATE deployment_runtime_revisions SET state='draining' WHERE id=:id"), {'id': dep['active_revision_id']})

    runtime = DeploymentRuntime(SimpleNamespace())
    monkeypatch.setattr(runtime, 'prepare', AsyncMock(return_value=SimpleNamespace()))
    result = {'final_outputs': {'answer': 'private-output'}, 'error_dict': {}, 'execution_time': 0.1}
    if outcome == 'node_failure':
        result['error_dict'] = {'node': 'private-error'}
    async def execute(session, **kwargs):
        if outcome == "runtime_failure":
            raise RuntimeError("worker crashed")
        from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
        async with short_session_scope(tenant_id=tenant) as db:
            history = WorkflowHistoryRepo(db)
            await history.bind_runtime(str(invocation), "test-generation")
            await history.persist_events(str(invocation), "test-generation", [{
                "invocation_id": str(invocation), "generation": "test-generation", "seq": 1,
                "type": "result", "status": "failed" if result["error_dict"] else "succeeded", **result,
            }])
        return result
    monkeypatch.setattr(runtime, '_execute_request', execute)
    call = runtime.run(tenant_id=tenant, deployment_id=str(dep['id']), revision_id=revision,
                       workflow={}, inputs={}, run_id=str(invocation))
    if outcome == 'runtime_failure':
        with pytest.raises(RuntimeError, match='worker crashed'):
            await call
    else:
        assert await call == result
    # No HTTP route or queue finalizer runs in this test.
    async with short_session_scope(tenant_id=tenant) as db:
        repo = DeploymentInvocationsRepo(db)
        row = (await db.execute(text('SELECT status, finished_at, result_summary FROM deployment_invocations WHERE id=:id'), {'id': invocation})).mappings().one()
        expected = 'succeeded' if outcome == 'success' else 'failed'
        assert row['status'] == expected
        assert row['finished_at'] is not None
        assert 'private' not in str(row['result_summary'])
        assert not await repo.mark_running(invocation), 'retry resurrected a terminal invocation'
        await repo.mark_terminal(invocation, status='succeeded' if expected == 'failed' else 'failed', latency_ms=9999)
        assert (await db.execute(text('SELECT status FROM deployment_invocations WHERE id=:id'), {'id': invocation})).scalar_one() == expected
    await controller.drain()
    controller.manager.deployments.retire.assert_awaited_once_with(tenant, revision)


@pytest.mark.asyncio
@pytest.mark.parametrize('disconnect_before_ack', [False, True])
async def test_detached_start_owns_one_execution_after_caller_disconnect(
    pg_engine, app_engine, monkeypatch, disconnect_before_ack,
):
    import uuid
    from tests.test_deployment_rollout import setup_rollout
    from vibecanvas_api.storage.db import short_session_scope
    from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo

    controller, dep, spec = await setup_rollout(pg_engine, app_engine)
    graph = await controller.graph(spec)
    tenant, revision, invocation = str(dep['tenant_id']), str(dep['active_revision_id']), uuid.uuid4()
    async with short_session_scope(tenant_id=tenant) as db:
        await DeploymentInvocationsRepo(db).create(
            invocation_id=invocation, tenant_id=dep['tenant_id'], deployment_id=dep['id'],
            wf_id=dep['wf_id'], trigger_type='api', source='async_api', status='queued',
            revision_id=dep['active_revision_id'],
        )
    runtime = DeploymentRuntime(SimpleNamespace())
    admitted, may_claim, executing, finish = (asyncio.Event() for _ in range(4))
    original_run = runtime.run

    async def gated_run(**kwargs):
        admitted.set()
        await may_claim.wait()
        return await original_run(**kwargs)

    async def execute(*args, **kwargs):
        executing.set()
        await finish.wait()
        return {'final_outputs': {}, 'error_dict': {}, 'execution_time': 1}

    execution = AsyncMock(side_effect=execute)
    monkeypatch.setattr(runtime, 'run', gated_run)
    monkeypatch.setattr(runtime, 'prepare', AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(runtime, '_execute_request', execution)
    kwargs = dict(tenant_id=tenant, deployment_id=str(dep['id']), revision_id=revision,
                  workflow=graph, inputs={'x': 1}, run_id=str(invocation))
    caller = asyncio.create_task(runtime.start(**kwargs))
    try:
        await asyncio.wait_for(admitted.wait(), 2)
        owner = runtime._dispatches[(tenant, str(invocation))]
        if disconnect_before_ack:
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert not owner.done()
        may_claim.set()
        await asyncio.wait_for(executing.wait(), 3)
        receipt = await asyncio.wait_for(runtime.start(**kwargs), 2)
        assert receipt == {'invocation_id': str(invocation), 'accepted': True}
        assert not owner.done()
        assert execution.await_count == 1
        if not disconnect_before_ack:
            assert await caller == receipt
        finish.set()
        await asyncio.wait_for(owner, 3)
        assert await runtime.start(**kwargs) == receipt
        assert execution.await_count == 1
    finally:
        may_claim.set()
        finish.set()
        await asyncio.gather(caller, *runtime._dispatches.values(), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('sibling_running', [False, True])
async def test_dead_worker_replaced_without_rebuilding_deployment(sibling_running):
    from vibecanvas_api.services.sandbox.workflow_rpc_pool import WorkflowRpcPool

    class Slot:
        invocation_id = None
        alive = False

        async def start(self):
            self.alive = True

        async def close(self):
            self.alive = False

    pool = WorkflowRpcPool(capacity=2, factory=lambda _: Slot())
    manager = SimpleNamespace(close_session=AsyncMock(), get_session=AsyncMock())
    runtime = DeploymentRuntime(manager)
    session = SimpleNamespace(
        closed=False,
        _fileop_pool=SimpleNamespace(_handles=[SimpleNamespace(proc=SimpleNamespace(poll=lambda: None))]),
        _workflow_rpc_pool=pool,
    )
    runtime._ready['tenant:revision'] = session
    try:
        await pool.prewarm()
        dead = pool._slots[0]
        async with pool.acquire('timed-out') as worker:
            assert worker is dead
            await worker.close()
        assert not pool.ready and pool.accepting

        async def check_recovery(sibling=None):
            prepared = await runtime.prepare(tenant_id='tenant', revision_id='revision', spec={}, workflow={})
            assert prepared is session
            async with pool.acquire('next-request') as replacement:
                assert replacement.alive and replacement is not dead
                if sibling is not None:
                    assert sibling.alive
            manager.close_session.assert_not_awaited()
            manager.get_session.assert_not_awaited()

        if sibling_running:
            async with pool.acquire('sibling') as sibling:
                await check_recovery(sibling)
        else:
            await check_recovery()
    finally:
        await pool.close()
    assert not pool.accepting
