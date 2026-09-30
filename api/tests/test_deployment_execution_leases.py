"""Durable fencing and expiry, using real PostgreSQL and controlled workers."""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from tests.test_deployment_rollout import setup_rollout
from vibecanvas_api.storage import db as db_module
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
from vibecanvas_api.services.sandbox.deployment_runtime import DeploymentRuntime


async def create_invocation(dep, source='sync_api', status='running'):
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        return await DeploymentInvocationsRepo(db).create(
            tenant_id=dep['tenant_id'], deployment_id=dep['id'], wf_id=dep['wf_id'],
            trigger_type='api', source=source, status=status, revision_id=dep['active_revision_id'])


@pytest.mark.asyncio
async def test_expiry_preserves_queue_and_live_claims(pg_engine, app_engine, monkeypatch):
    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    controller, dep, _ = await setup_rollout(pg_engine, app_engine)
    dispatch = await create_invocation(dep)
    dead = await create_invocation(dep)
    live = await create_invocation(dep)
    queued = await create_invocation(dep, 'async_api', 'queued')
    tenant = str(dep['tenant_id'])
    async with short_session_scope(tenant_id=tenant) as db:
        await db.execute(text("UPDATE deployment_invocations SET dispatch_deadline=now()-interval '1 second' WHERE id=:id"), {'id': dispatch})
        for invocation, seconds in [(dead, -1), (live, 60)]:
            await db.execute(text("""UPDATE deployment_invocations SET runtime_claim=:claim,
                execution_lease_until=now()+:seconds*interval '1 second', dispatch_deadline=NULL WHERE id=:id"""),
                {'id': invocation, 'claim': uuid.uuid4(), 'seconds': seconds})
        await db.execute(text("UPDATE deployment_invocations SET submitted_at=now()-interval '1 day' WHERE id=:id"), {'id': queued})
    await controller.expire_invocations()
    async with short_session_scope(tenant_id=tenant) as db:
        rows = {row['id']: row for row in (await db.execute(text('SELECT id,status,error FROM deployment_invocations WHERE deployment_id=:id'), {'id': dep['id']})).mappings()}
    assert (rows[dispatch]['status'], rows[dispatch]['error']) == ('failed', 'dispatch_expired')
    assert (rows[dead]['status'], rows[dead]['error']) == ('failed', 'execution_lease_expired')
    assert rows[live]['status'] == 'running'
    assert rows[queued]['status'] == 'queued'


@pytest.mark.asyncio
async def test_claim_fences_duplicates_and_lost_lease_stops_owner(pg_engine, app_engine, monkeypatch):
    from vibecanvas_api.services.sandbox import deployment_runtime as module
    monkeypatch.setattr(module, 'LEASE_HEARTBEAT_SECONDS', 0.02)
    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    controller, dep, _ = await setup_rollout(pg_engine, app_engine)
    invocation = await create_invocation(dep)
    runtime = DeploymentRuntime(SimpleNamespace(close_session=AsyncMock()))
    monkeypatch.setattr(runtime, 'prepare', AsyncMock(return_value=SimpleNamespace()))
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def execute(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    executor = AsyncMock(side_effect=execute)
    monkeypatch.setattr(runtime, '_execute_request', executor)
    arguments = dict(tenant_id=str(dep['tenant_id']), deployment_id=str(dep['id']),
        revision_id=str(dep['active_revision_id']), workflow={}, inputs={}, run_id=str(invocation))
    task = asyncio.create_task(runtime.run(**arguments))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with short_session_scope(tenant_id=arguments['tenant_id']) as db:
            await db.execute(text("UPDATE deployment_invocations SET execution_lease_until=now()+interval '2 seconds' WHERE id=:id"), {'id': invocation})
        renewed = False
        for _ in range(50):
            await asyncio.sleep(0.02)
            async with short_session_scope(tenant_id=arguments['tenant_id']) as db:
                renewed = (await db.execute(text("SELECT execution_lease_until > now()+interval '40 seconds' FROM deployment_invocations WHERE id=:id"), {'id': invocation})).scalar_one()
            if renewed:
                break
        assert renewed, 'live execution did not extend its lease'
        with pytest.raises(RuntimeError, match='not_admitted'):
            await runtime.run(**arguments)
        async with short_session_scope(tenant_id=arguments['tenant_id']) as db:
            repo = DeploymentInvocationsRepo(db)
            assert not await repo.mark_running(invocation)
            await repo.mark_terminal(invocation, status='failed', latency_ms=1)
            assert (await db.execute(text('SELECT status FROM deployment_invocations WHERE id=:id'), {'id': invocation})).scalar_one() == 'running'
            await db.execute(text("UPDATE deployment_invocations SET execution_lease_until=now()-interval '1 second' WHERE id=:id"), {'id': invocation})
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert stopped.is_set()
        assert executor.await_count == 1
        assert not runtime._active_requests
        await controller.expire_invocations()
        async with short_session_scope(tenant_id=arguments['tenant_id']) as db:
            assert (await db.execute(text('SELECT status FROM deployment_invocations WHERE id=:id'), {'id': invocation})).scalar_one() == 'failed'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_resident_invocations_prepare_fresh_resources(pg_engine, app_engine, monkeypatch):
    from vibecanvas_api.services import workflow_resources
    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    _, dep, _ = await setup_rollout(pg_engine, app_engine)
    runtime = DeploymentRuntime(SimpleNamespace(close_session=AsyncMock()))
    resident_session = SimpleNamespace()
    monkeypatch.setattr(runtime, 'prepare', AsyncMock(return_value=resident_session))
    snapshots = [{'nodes': {'worker': {'version': version}}} for version in (1, 2)]
    prepare = AsyncMock(side_effect=snapshots)
    monkeypatch.setattr(workflow_resources, 'prepare_execution_resources', prepare)
    execute = AsyncMock(return_value={'status': 'success', 'outputs': {}})
    monkeypatch.setattr(runtime, '_execute_request', execute)
    graph = {'worker': {'node_type': 'SubAgentNode', 'node_config': {
        'skills': [{'id': str(uuid.uuid4()), 'name': 'Latest Skill'}]}}}
    for index in range(2):
        invocation = await create_invocation(dep)
        claims = dict(tenant_id=str(dep['tenant_id']), user_id=str(dep['user_id']),
            workflow_id=dep['wf_id'], execution_id=str(invocation),
            execution_resource_type='deployment_invocation')
        await runtime.run(tenant_id=claims['tenant_id'], deployment_id=str(dep['id']),
            revision_id=str(dep['active_revision_id']), workflow=graph, inputs={},
            run_id=str(invocation), resource_claims=claims,
            extra={'workflow_resources': {'untrusted': True}})
        assert execute.await_args.kwargs['extra']['workflow_resources'] == snapshots[index]
        assert prepare.await_args.kwargs == dict(sandbox_session=resident_session, workflow=graph, **claims)
    assert prepare.await_count == 2
