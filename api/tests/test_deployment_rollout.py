"""Real PostgreSQL state transitions; sandbox startup is controlled by the test."""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from tests.test_deployment_invoke_sync import _seed_full_deployment
from vibecanvas_api.services.deployment_revisions import execution_spec
from vibecanvas_api.services.deployment_rollout import DeploymentRollouts
from vibecanvas_api.services.deployment_snapshots import resolve_workflow
from vibecanvas_api.storage.db import short_session_scope


async def setup_rollout(pg_engine, app_engine):
    tenant, _, _, dep_id = await _seed_full_deployment(pg_engine, app_engine)
    async with short_session_scope(tenant_id=str(tenant)) as db:
        dep = dict((await db.execute(text('SELECT * FROM deployments WHERE id=:id'), {'id': dep_id})).mappings().one())
        workflow = await resolve_workflow(db, dep['user_id'], dep)
    dep['runtime_user_id'] = dep['user_id']
    runtime = SimpleNamespace(prepare=AsyncMock(), retire=AsyncMock())
    controller = DeploymentRollouts(SimpleNamespace(deployments=runtime))
    controller.graph = AsyncMock(return_value=workflow)
    spec = execution_spec(dep, workflow, str(dep['user_id']))
    active = uuid.uuid4()
    async with short_session_scope(tenant_id=str(tenant)) as db:
        await db.execute(text("""INSERT INTO deployment_runtime_revisions(id,tenant_id,deployment_id,spec,state)
            VALUES (:id,:tenant,:dep,CAST(:spec AS jsonb),'active')"""),
            {'id': active, 'tenant': tenant, 'dep': dep_id, 'spec': json.dumps(spec)})
        await db.execute(text('UPDATE deployments SET active_revision_id=:rev WHERE id=:id'), {'rev': active, 'id': dep_id})
    dep['active_revision_id'] = active
    return controller, dep, spec


@pytest.mark.asyncio
async def test_reverting_to_active_configuration_retires_candidate(pg_engine, app_engine):
    controller, dep, spec = await setup_rollout(pg_engine, app_engine)
    stale = uuid.uuid4()
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        await db.execute(text("""INSERT INTO deployment_runtime_revisions(id,tenant_id,deployment_id,spec)
            VALUES (:id,:tenant,:dep,CAST(:spec AS jsonb))"""),
            {'id': stale, 'tenant': dep['tenant_id'], 'dep': dep['id'], 'spec': json.dumps({**spec, 'mount_enabled': not spec['mount_enabled']})})
    await controller.reconcile(dep)
    controller.manager.deployments.retire.assert_awaited_once_with(str(dep['tenant_id']), str(stale))
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        state = (await db.execute(text('SELECT state FROM deployment_runtime_revisions WHERE id=:id'), {'id': stale})).scalar_one()
        current = (await db.execute(text('SELECT active_revision_id, rollout_status FROM deployments WHERE id=:id'), {'id': dep['id']})).one()
    assert state == 'retired'
    assert current == (dep['active_revision_id'], 'ready')


@pytest.mark.asyncio
async def test_stale_instance_metrics_are_unknown_not_zero(pg_engine, app_engine):
    from vibecanvas_api.services.deployment_revisions import runtime_summary
    _, dep, _ = await setup_rollout(pg_engine, app_engine)
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        await db.execute(text("UPDATE deployment_runtime_revisions SET runtime_metrics=CAST(:metrics AS jsonb), observed_at=now() WHERE id=:id"),
            {'id': dep['active_revision_id'], 'metrics': json.dumps({'memory_bytes': 12345, 'cpu_percent': 0})})
        assert (await runtime_summary(db, dep['id']))['instances'][0]['metrics']['memory_bytes'] == 12345
        await db.execute(text("UPDATE deployment_runtime_revisions SET observed_at=now()-interval '31 seconds' WHERE id=:id"), {'id': dep['active_revision_id']})
        assert (await runtime_summary(db, dep['id']))['instances'][0]['metrics'] is None


@pytest.mark.asyncio
async def test_failed_candidate_preserves_active_and_backs_off(pg_engine, app_engine):
    controller, dep, _ = await setup_rollout(pg_engine, app_engine)
    dep['mount_enabled'] = not dep['mount_enabled']
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        await db.execute(text('UPDATE deployments SET mount_enabled=:mount WHERE id=:id'), {'mount': dep['mount_enabled'], 'id': dep['id']})
    active_calls = []
    async def prepare(**kwargs):
        active_calls.append(kwargs['revision_id'])
        if kwargs['revision_id'] != str(dep['active_revision_id']):
            raise RuntimeError('capacity exhausted')
    controller.manager.deployments.prepare.side_effect = prepare
    await controller.reconcile(dep)
    await controller.reconcile(dep)
    assert sum(rev != str(dep['active_revision_id']) for rev in active_calls) == 1
    controller.manager.deployments.retire.assert_not_awaited()
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        current = (await db.execute(text('SELECT active_revision_id, rollout_status FROM deployments WHERE id=:id'), {'id': dep['id']})).one()
        count = (await db.execute(text("SELECT count(*) FROM deployment_runtime_revisions WHERE deployment_id=:id AND state='preparing'"), {'id': dep['id']})).scalar_one()
    assert current == (dep['active_revision_id'], 'waiting_capacity')
    assert count == 1


@pytest.mark.asyncio
async def test_settings_changed_during_prepare_cannot_switch_traffic(pg_engine, app_engine):
    controller, dep, _ = await setup_rollout(pg_engine, app_engine)
    original_mount = dep['mount_enabled']
    dep['mount_enabled'] = not original_mount
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        await db.execute(text('UPDATE deployments SET mount_enabled=:mount WHERE id=:id'), {'mount': dep['mount_enabled'], 'id': dep['id']})
    async def prepare(**kwargs):
        if kwargs['revision_id'] != str(dep['active_revision_id']):
            async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
                await db.execute(text("UPDATE deployments SET mount_enabled=:mount, rollout_status='pending' WHERE id=:id"), {'mount': original_mount, 'id': dep['id']})
    controller.manager.deployments.prepare.side_effect = prepare
    await controller.reconcile(dep)
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        current = (await db.execute(text('SELECT active_revision_id, rollout_status FROM deployments WHERE id=:id'), {'id': dep['id']})).one()
    assert current == (dep['active_revision_id'], 'pending')
    controller.manager.deployments.retire.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_keeps_accepted_queue_on_old_instance_until_finished(pg_engine, app_engine, monkeypatch):
    from vibecanvas_api.services.deployment_revisions import admit_revision, runtime_summary
    from vibecanvas_api.storage import db as db_module
    from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo
    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    controller, dep, _ = await setup_rollout(pg_engine, app_engine)
    tenant = str(dep['tenant_id'])
    # Admission creates its durable drain lease in the same transaction that
    # selects the revision. A queued request is as binding as a running one.
    async with short_session_scope(tenant_id=tenant) as db:
        _, revision = await admit_revision(db, dep['id'])
        invocation = await DeploymentInvocationsRepo(db).create(
            tenant_id=dep['tenant_id'], deployment_id=dep['id'], wf_id=dep['wf_id'],
            trigger_type='api', source='async_api', status='queued', revision_id=revision['id'])
        dep['mount_enabled'] = not dep['mount_enabled']
        await db.execute(text('UPDATE deployments SET mount_enabled=:mount WHERE id=:id'), {'mount': dep['mount_enabled'], 'id': dep['id']})
    await controller.reconcile(dep)
    await controller.drain()
    controller.manager.deployments.retire.assert_not_awaited()
    async with short_session_scope(tenant_id=tenant) as db:
        instances = (await runtime_summary(db, dep['id']))['instances']
        assert len(instances) == 2
        assert next(item for item in instances if item['id'] == str(revision['id']))['pending_requests'] == 1
        _, next_revision = await admit_revision(db, dep['id'])
        assert next_revision['id'] != revision['id']
        old = (await db.execute(text('SELECT state FROM deployment_runtime_revisions WHERE id=:id'), {'id': revision['id']})).scalar_one()
        assert old == 'draining'
        await db.execute(text("UPDATE deployment_invocations SET status='running' WHERE id=:id"), {'id': invocation})
    await controller.drain()
    controller.manager.deployments.retire.assert_not_awaited()
    async with short_session_scope(tenant_id=tenant) as db:
        await db.execute(text("UPDATE deployment_invocations SET status='succeeded' WHERE id=:id"), {'id': invocation})
    await controller.drain()
    controller.manager.deployments.retire.assert_awaited_once_with(tenant, str(revision['id']))
    async with short_session_scope(tenant_id=tenant) as db:
        instances = (await runtime_summary(db, dep['id']))['instances']
        assert len(instances) == 1
        assert instances[0]['state'] == 'active'
        assert instances[0]['activated_at'] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('deleted', [False, True])
async def test_stopped_first_candidate_is_reclaimed_without_active_pointer(pg_engine, app_engine, monkeypatch, deleted):
    """Disable/delete during first boot must not hide the candidate from scanning."""
    from vibecanvas_api.storage import db as db_module
    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    controller, dep, _ = await setup_rollout(pg_engine, app_engine)
    controller.manager.deployments.metrics = lambda *_: None
    controller.manager.deployments.retire.return_value = True
    tenant = str(dep['tenant_id'])
    async with short_session_scope(tenant_id=tenant) as db:
        await db.execute(text("UPDATE deployment_runtime_revisions SET state='preparing' WHERE id=:id"), {'id': dep['active_revision_id']})
        await db.execute(text("""UPDATE deployments SET active_revision_id=NULL, enabled=false,
            deleted_at=CASE WHEN :deleted THEN now() ELSE NULL END WHERE id=:id"""),
            {'id': dep['id'], 'deleted': deleted})
    await controller.tick()
    controller.manager.deployments.prepare.assert_not_awaited()
    controller.manager.deployments.retire.assert_awaited_once_with(tenant, str(dep['active_revision_id']))
    async with short_session_scope(tenant_id=tenant) as db:
        state = (await db.execute(text('SELECT state FROM deployment_runtime_revisions WHERE id=:id'), {'id': dep['active_revision_id']})).scalar_one()
        status = (await db.execute(text('SELECT rollout_status FROM deployments WHERE id=:id'), {'id': dep['id']})).scalar_one()
    assert state == 'retired'
    assert status == 'stopped'


@pytest.mark.asyncio
async def test_slow_candidate_does_not_block_metrics_or_crash_recovery(monkeypatch):
    import asyncio
    controller = DeploymentRollouts(SimpleNamespace())
    preparing, finish, sampled, stop = (asyncio.Event() for _ in range(4))
    async def slow_tick(*, maintenance):
        assert maintenance is False
        preparing.set()
        await finish.wait()
    async def sample():
        await preparing.wait()
        sampled.set()
    monkeypatch.setattr(controller, 'tick', slow_tick)
    monkeypatch.setattr(controller, 'expire_invocations', AsyncMock())
    monkeypatch.setattr(controller, 'sample_metrics', AsyncMock(side_effect=sample))
    monkeypatch.setattr(controller, 'drain', AsyncMock())
    worker = asyncio.create_task(controller.serve(stop))
    try:
        await asyncio.wait_for(sampled.wait(), 2)
        assert not finish.is_set()
        controller.expire_invocations.assert_awaited_once()
    finally:
        stop.set()
        finish.set()
        await asyncio.wait_for(worker, 2)
    controller.drain.assert_awaited_once()
