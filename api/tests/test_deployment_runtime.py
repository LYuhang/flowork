"""Cancellation must not release a resident instance before its worker exits."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.sandbox.deployment_runtime import DeploymentRuntime


@pytest.mark.asyncio
async def test_cancel_waits_for_worker_without_interrupting_sibling(tmp_path):
    runtime = DeploymentRuntime(SimpleNamespace())
    entered = {key: asyncio.Event() for key in ('first', 'second')}
    finish = {key: asyncio.Event() for key in entered}
    stop_requested = asyncio.Event()
    async def execute(**kwargs):
        key = kwargs['run_id']
        assert kwargs['kill_individually'] is True
        root = tmp_path / 'requests' / key
        root.mkdir(parents=True)
        (root / 'credential').write_text('test-only')
        entered[key].set()
        await finish[key].wait()
        assert root.exists(), 'request files deleted while worker was alive'
        return {'result': {'outputs': key}}
    async def kill(**kwargs):
        assert kwargs['run_id'] == 'first'
        stop_requested.set()
    session = SimpleNamespace(
        workflow_run_dir=str(tmp_path / 'run'), execute_workflow_job=execute,
        kill_workflow_job=AsyncMock(side_effect=kill), _sync_mount_folder=AsyncMock())
    tasks = {key: asyncio.create_task(runtime._execute_request(session,
        workflow={}, inputs={}, extra={}, tenant_id='tenant', run_id=key,
        subpath='requests/' + key)) for key in entered}
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        tasks['first'].cancel()
        await asyncio.wait_for(stop_requested.wait(), 2)
        assert not tasks['first'].done()
        assert not tasks['second'].done()
        assert (tmp_path / 'requests/first/credential').exists()
        # Repeated disconnect cancellation must not bypass the exit wait.
        tasks['first'].cancel()
        finish['first'].set()
        with pytest.raises(asyncio.CancelledError):
            await tasks['first']
        assert not (tmp_path / 'requests/first').exists()
        assert (tmp_path / 'requests/second/credential').exists()
        finish['second'].set()
        assert (await tasks['second'])['outputs'] == 'second'
        assert not (tmp_path / 'requests/second').exists()
        session.kill_workflow_job.assert_awaited_once()
    finally:
        for event in finish.values():
            event.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


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
    execute = AsyncMock(return_value=result)
    if outcome == 'runtime_failure':
        execute.side_effect = RuntimeError('worker crashed')
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
