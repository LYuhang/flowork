"""Opt-in real bubblewrap + cgroup + PostgreSQL resident execution test.

Run in a dedicated delegated systemd unit with FLOWORK_TEST_RESIDENT_CGROUP=1.
The normal test fixtures create a disposable PostgreSQL database; all files use
tmp_path. No live deployment or account is used.
"""
import asyncio
import base64
import os
import uuid
from pathlib import Path

import pytest

from tests.test_deployment_rollout import setup_rollout
from vibecanvas_api.config import config
from vibecanvas_api.services.sandbox.manager import SandboxManager
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.repo_deployment_invocations import DeploymentInvocationsRepo


def numbers(value):
    if isinstance(value, dict):
        return set().union(*(numbers(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(numbers(child) for child in value))
    return {value} if isinstance(value, (int, float)) else set()


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('FLOWORK_TEST_RESIDENT_CGROUP') != '1', reason='requires a dedicated delegated Linux test unit')
async def test_resident_workflows_reuse_instance_and_isolate_concurrent_requests(pg_engine, app_engine, monkeypatch, tmp_path):
    assert os.environ.get('SANDBOX_CGROUP_ROOT'), 'delegated cgroup root required'
    monkeypatch.setattr(config, 'sandbox_runtime', 'bubblewrap')
    monkeypatch.setattr(config, 'sandbox_network', 'none')
    monkeypatch.setattr(config, 'sandbox_fileop_workers', 2)
    monkeypatch.setattr(config.object_store, 'provider', 'filesystem')
    monkeypatch.setattr(config.object_store, 'fs_root', str(tmp_path / 'objects'))
    monkeypatch.setattr(config.object_store, 'fs_materialized_root', str(tmp_path / 'plaintext'))
    for name in ('agent_runtime_root', 'agent_overlay_root', 'vfs_volume_root'):
        monkeypatch.setattr(config, name, str(tmp_path / name))
    monkeypatch.setattr(config, 'kms_provider', 'local')
    monkeypatch.setattr(config, 'kms_local_master_key', base64.urlsafe_b64encode(b't' * 32).decode())
    monkeypatch.setattr(config, 'kms_local_master_key_file', '')
    fixture, dep, spec = await setup_rollout(pg_engine, app_engine)
    workflow = await fixture.graph(spec)
    tenant, revision = str(dep['tenant_id']), str(dep['active_revision_id'])
    manager = SandboxManager(max_resident=1, idle_ttl_s=60)
    runtime = manager.deployments
    async def invoke(value):
        invocation = uuid.uuid4()
        async with short_session_scope(tenant_id=tenant) as db:
            await DeploymentInvocationsRepo(db).create(invocation_id=invocation,
                tenant_id=dep['tenant_id'], deployment_id=dep['id'], wf_id=dep['wf_id'],
                trigger_type='api', source='sync_api', status='running', revision_id=dep['active_revision_id'])
        result = await runtime.run(tenant_id=tenant, deployment_id=str(dep['id']),
            revision_id=revision, workflow=workflow, inputs={'x': value}, run_id=str(invocation))
        assert not result['error_dict'], result
        assert value in numbers(result['final_outputs']), result
        assert not (Path(session.pool_runs_root) / 'requests' / invocation.hex).exists()
        return result
    try:
        session = await asyncio.wait_for(runtime.prepare(tenant_id=tenant, revision_id=revision,
            spec=spec, workflow=workflow), 90)
        assert session.workspace_profile == "execution"
        assert not any((Path(session.run_dir) / name).exists() for name in ("chats", "data", "logs", "memory"))
        assert not any(dest in {"/chats", "/data", "/logs", "/memory"} for dest, _ in session._rw_binds)
        pid = session._fileop_pool._handles[0].proc.pid
        terminal_id = str(uuid.uuid4())
        async def terminal(action, **fields):
            response = await manager.deployment_terminal(tenant_id=tenant, deployment_id=str(dep['id']),
                revision_id=revision, user_id=str(dep['user_id']), terminal_id=terminal_id,
                action=action, **fields)
            assert response.get('ok'), response
            return response
        await terminal('open', columns=80, rows=24)
        for field in ('tenant_id', 'user_id', 'deployment_id', 'revision_id'):
            scope = dict(tenant_id=tenant, deployment_id=str(dep['id']), revision_id=revision, user_id=str(dep['user_id']))
            scope[field] = str(uuid.uuid4())
            with pytest.raises(RuntimeError, match='terminal_scope_mismatch'):
                await manager.deployment_terminal(**scope, terminal_id=terminal_id, action='read')
        await terminal('resize', columns=101, rows=29)
        await terminal('write', data=base64.b64encode(b"test -t 0 && stty size; printf proof > /run/terminal-proof\r").decode())
        output = ''
        for _ in range(100):
            response = await terminal('read')
            output += base64.b64decode(response['data']).decode(errors='replace')
            if '29 101' in output and (Path(session.run_dir) / 'terminal-proof').exists():
                break
            await asyncio.sleep(0.05)
        assert '29 101' in output, output
        assert (Path(session.run_dir) / 'terminal-proof').read_text() == 'proof'
        await asyncio.wait_for(invoke(117), 60)
        second, third = await asyncio.wait_for(asyncio.gather(invoke(223), invoke(337)), 60)
        assert 337 not in numbers(second['final_outputs'])
        assert 223 not in numbers(third['final_outputs'])
        assert session._fileop_pool._handles[0].proc.pid == pid
        assert await runtime.prepare(tenant_id=tenant, revision_id=revision, spec=spec, workflow=workflow) is session
        metrics = runtime.metrics(tenant, revision)
        assert metrics and metrics['memory_bytes'] > 0
        assert metrics['memory_mb'] == 256
        await terminal('write', data=base64.b64encode(b'sleep 30\r').decode())
        await asyncio.sleep(0.2)
        await terminal('write', data=base64.b64encode(b'\x03').decode())
        await terminal('write', data=base64.b64encode(b"printf '%s%s\\n' '__AFTER_' 'INTERRUPT__'\r").decode())
        output = ''
        for _ in range(100):
            output += base64.b64decode((await terminal('read'))['data']).decode(errors='replace')
            if '__AFTER_INTERRUPT__' in output:
                break
            await asyncio.sleep(0.05)
        assert '__AFTER_INTERRUPT__' in output, output
        await terminal('close')
        await asyncio.wait_for(invoke(449), 60)
        assert session._fileop_pool._handles[0].proc.pid == pid
        # Namespace descendants may take another scheduling turn to exit after
        # the supervisor. A refused retirement keeps the row draining; retry
        # exactly as the controller does, bounded here for leak detection.
        retired = False
        for _ in range(30):
            if await runtime.retire(tenant, revision):
                retired = True
                break
            await asyncio.sleep(0.1)
        assert retired, (Path(os.environ['SANDBOX_CGROUP_ROOT']) / ('deployment-' + uuid.UUID(revision).hex) / 'cgroup.procs').read_text()
        assert not (Path(os.environ['SANDBOX_CGROUP_ROOT']) / ('deployment-' + uuid.UUID(revision).hex)).exists()
    finally:
        await manager.shutdown()
        if runtime._resources:
            runtime._resources.release(revision)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('FLOWORK_TEST_RESIDENT_CGROUP') != '1', reason='requires a dedicated delegated Linux test unit')
async def test_http_calls_continue_during_real_instance_replacement(pg_engine, app_engine, monkeypatch, tmp_path):
    import hashlib
    import json
    import httpx
    from fastapi import FastAPI
    from sqlalchemy import text
    from vibecanvas_api.routes import deployment_invoke as routes
    from vibecanvas_api.services.deployment_revisions import execution_spec, admit_revision
    from vibecanvas_api.services.deployment_rollout import DeploymentRollouts
    from vibecanvas_api.storage import db as db_module

    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    monkeypatch.setattr(config, 'sandbox_runtime', 'bubblewrap')
    monkeypatch.setattr(config, 'sandbox_network', 'none')
    monkeypatch.setattr(config, 'sandbox_fileop_workers', 1)
    monkeypatch.setattr(config.object_store, 'provider', 'filesystem')
    monkeypatch.setattr(config.object_store, 'fs_root', str(tmp_path / 'objects'))
    monkeypatch.setattr(config.object_store, 'fs_materialized_root', str(tmp_path / 'plaintext'))
    for name in ('agent_runtime_root', 'agent_overlay_root', 'vfs_volume_root'):
        monkeypatch.setattr(config, name, str(tmp_path / name))
    monkeypatch.setattr(config, 'kms_provider', 'local')
    monkeypatch.setattr(config, 'kms_local_master_key', base64.urlsafe_b64encode(b't' * 32).decode())
    monkeypatch.setattr(config, 'kms_local_master_key_file', '')
    fixture, dep, _ = await setup_rollout(pg_engine, app_engine)
    workflow = await fixture.graph(dep)
    dep['memory_mb'] = 256
    spec = execution_spec(dep, workflow, str(dep['user_id']))
    tenant, original = str(dep['tenant_id']), str(dep['active_revision_id'])
    async with short_session_scope(tenant_id=tenant) as db:
        await db.execute(text('UPDATE deployments SET memory_mb=256, rate_limit_qps=0, api_key_hash=:key WHERE id=:id'),
            {'id': dep['id'], 'key': hashlib.sha256(b'isolated-rollout-test').hexdigest()})
        await db.execute(text('UPDATE deployment_runtime_revisions SET spec=CAST(:spec AS jsonb) WHERE id=:id'),
            {'id': dep['active_revision_id'], 'spec': json.dumps(spec)})
    manager = SandboxManager(max_resident=3, idle_ttl_s=60)
    runtime = manager.deployments
    controller = DeploymentRollouts(manager)
    seen = []
    # Use the production HTTP handler and actual durable admission. Only the
    # process transport is adapted to this test's event loop; execution itself
    # runs in the real bubblewrap instance with real engine workers.
    async def bridge(**kwargs):
        seen.append(kwargs['revision_id'])
        outcome = await runtime.run(
            tenant_id=kwargs['tenant_id'], deployment_id=kwargs['deployment_id'],
            revision_id=kwargs['revision_id'], workflow=kwargs['workflow_dict'],
            inputs=kwargs['inputs'], run_id=kwargs['run_id'])
        return outcome['final_outputs'], outcome['error_dict'], outcome['execution_time']
    from vibecanvas_api.services import deployment_dispatch
    monkeypatch.setattr(deployment_dispatch, 'run_workflow_sandboxed_async', bridge)
    app = FastAPI()
    app.include_router(routes.router)
    warming, release = asyncio.Event(), asyncio.Event()
    prepare = runtime.prepare
    async def gated_prepare(**kwargs):
        if kwargs['revision_id'] != original:
            warming.set()
            await release.wait()
        return await prepare(**kwargs)
    update = None
    try:
        old = await runtime.prepare(tenant_id=tenant, revision_id=original, spec=spec, workflow=workflow)
        old_pid = old._fileop_pool._handles[0].proc.pid
        async with short_session_scope(tenant_id=tenant) as db:
            _, admitted = await admit_revision(db, dep['id'])
            queued = await DeploymentInvocationsRepo(db).create(
                tenant_id=dep['tenant_id'], deployment_id=dep['id'], wf_id=dep['wf_id'],
                trigger_type='api', source='async_api', status='queued', revision_id=admitted['id'])
            dep['mount_enabled'] = not dep['mount_enabled']
            await db.execute(text('UPDATE deployments SET mount_enabled=:mount WHERE id=:id'),
                {'id': dep['id'], 'mount': dep['mount_enabled']})
        monkeypatch.setattr(runtime, 'prepare', gated_prepare)
        update = asyncio.create_task(controller.reconcile(dep))
        await asyncio.wait_for(warming.wait(), 10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            async def request(value):
                response = await client.post(f'/api/v1/deployments/{dep["slug"]}/invoke',
                    json={'x': value}, headers={'Authorization': 'Bearer isolated-rollout-test'})
                assert response.status_code == 200, response.text
                assert value in numbers(response.json()['outputs']), response.text
            await request(501)
            assert seen[-1] == original
            assert old._fileop_pool._handles[0].proc.pid == old_pid
            release.set()
            await asyncio.wait_for(update, 60)
            await request(502)
            replacement = seen[-1]
            assert replacement != original
            await controller.drain()
            assert not old.closed, 'queued request lost its accepted instance'
            result = await runtime.run(tenant_id=tenant, deployment_id=str(dep['id']),
                revision_id=original, workflow=workflow, inputs={'x': 503}, run_id=str(queued))
            assert 503 in numbers(result['final_outputs'])
            async with short_session_scope(tenant_id=tenant) as db:
                await DeploymentInvocationsRepo(db).mark_terminal(queued, status='succeeded', latency_ms=0)
            for _ in range(30):
                await controller.drain()
                if f'{tenant}:{original}' not in runtime._ready:
                    break
                await asyncio.sleep(.1)
            assert f'{tenant}:{original}' not in runtime._ready
            assert not (Path(os.environ['SANDBOX_CGROUP_ROOT']) / ('deployment-' + uuid.UUID(original).hex)).exists()
            await request(504)
            assert seen[-1] == replacement
            # Recreate the daemon owner, then restore the durable serving
            # revision before admitting another HTTP request.
            await manager.shutdown()
            manager = SandboxManager(max_resident=3, idle_ttl_s=60)
            runtime = manager.deployments
            controller = DeploymentRollouts(manager)
            async with short_session_scope(tenant_id=tenant) as db:
                restored = dict((await db.execute(text('SELECT * FROM deployments WHERE id=:id'), {'id': dep['id']})).mappings().one())
            restored['runtime_user_id'] = restored['user_id']
            await controller.reconcile(restored)
            assert f'{tenant}:{replacement}' in runtime._ready
            await request(505)
            assert seen[-1] == replacement
            # HTTP cancellation cannot leave a durable running row behind or
            # release the instance while its offloaded worker still owns it.
            request_started, allow_finish = asyncio.Event(), asyncio.Event()
            actual_run = runtime.run
            async def delayed_run(**kwargs):
                request_started.set()
                await allow_finish.wait()
                return await actual_run(**kwargs)
            monkeypatch.setattr(runtime, 'run', delayed_run)
            cancelled_request = asyncio.create_task(request(506))
            try:
                await asyncio.wait_for(request_started.wait(), 5)
                cancelled_request.cancel()
                await asyncio.sleep(0)
                cancelled_request.cancel()
                await asyncio.sleep(0)
                assert not cancelled_request.done()
                allow_finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(cancelled_request, 30)
                async with short_session_scope(tenant_id=tenant) as db:
                    pending = (await db.execute(text("SELECT count(*) FROM deployment_invocations WHERE deployment_id=:id AND status IN ('queued','running')"), {'id': dep['id']})).scalar_one()
                assert pending == 0
            finally:
                allow_finish.set()
                await asyncio.gather(cancelled_request, return_exceptions=True)
    finally:
        release.set()
        if update is not None:
            await asyncio.gather(update, return_exceptions=True)
        await manager.shutdown()
        if runtime._resources:
            for key in list(runtime._ready):
                runtime._resources.release(key.split(':', 1)[1])


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('FLOWORK_TEST_RESIDENT_CGROUP') != '1', reason='requires a dedicated delegated Linux test unit')
async def test_killed_daemon_leases_expire_and_orphan_instance_is_reaped(pg_engine, app_engine, monkeypatch, tmp_path):
    import json
    import sys
    from sqlalchemy import text
    from vibecanvas_api.services.deployment_rollout import DeploymentRollouts
    from vibecanvas_api.storage import db as db_module
    monkeypatch.setattr(db_module, '_admin_engine', pg_engine)
    monkeypatch.setattr(config, 'kms_provider', 'local')
    monkeypatch.setattr(config, 'kms_local_master_key', base64.urlsafe_b64encode(b't' * 32).decode())
    monkeypatch.setattr(config, 'kms_local_master_key_file', '')
    fixture, dep, spec = await setup_rollout(pg_engine, app_engine)
    tenant, revision = str(dep['tenant_id']), str(dep['active_revision_id'])
    async with short_session_scope(tenant_id=tenant) as db:
        invocation = await DeploymentInvocationsRepo(db).create(
            tenant_id=dep['tenant_id'], deployment_id=dep['id'], wf_id=dep['wf_id'],
            trigger_type='api', source='sync_api', status='running', revision_id=dep['active_revision_id'])
    ready = tmp_path / 'ready'
    group = Path(os.environ['SANDBOX_CGROUP_ROOT']) / ('deployment-' + uuid.UUID(revision).hex)
    manager = SandboxManager(max_resident=1, idle_ttl_s=60)
    child = None
    with (tmp_path / 'child.log').open('wb') as log:
        try:
            child = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name('deployment_crash_probe.py')),
                stdin=asyncio.subprocess.PIPE, stdout=log, stderr=log)
            child.stdin.write(json.dumps({'tenant': tenant, 'deployment': str(dep['id']), 'revision': revision,
                'invocation': str(invocation), 'workflow': await fixture.graph(spec), 'root': str(tmp_path / 'child'),
                'ready': str(ready)}).encode())
            await child.stdin.drain()
            child.stdin.close()
            for _ in range(300):
                if ready.exists():
                    break
                if child.returncode is not None:
                    pytest.fail((tmp_path / 'child.log').read_text()[-3000:])
                await asyncio.sleep(0.1)
            assert ready.exists(), (tmp_path / 'child.log').read_text()[-3000:]
            assert group.exists()
            child.kill()  # real SIGKILL: no finalizer, no graceful shutdown
            await asyncio.wait_for(child.wait(), 5)
            for _ in range(100):
                if 'populated 0' in (group / 'cgroup.events').read_text():
                    break
                await asyncio.sleep(0.1)
            assert 'populated 0' in (group / 'cgroup.events').read_text()
            async with short_session_scope(tenant_id=tenant) as db:
                state = (await db.execute(text('SELECT status,runtime_claim FROM deployment_invocations WHERE id=:id'), {'id': invocation})).one()
                assert state[0] == 'running' and state[1] is not None
                # Advance the database lease clock rather than sleeping a minute.
                await db.execute(text("UPDATE deployment_invocations SET execution_lease_until=now()-interval '1 second' WHERE id=:id"), {'id': invocation})
                await db.execute(text("UPDATE deployment_runtime_revisions SET state='draining' WHERE id=:id"), {'id': dep['active_revision_id']})
            controller = DeploymentRollouts(manager)
            await controller.expire_invocations()
            await controller.drain()
            async with short_session_scope(tenant_id=tenant) as db:
                assert (await db.execute(text('SELECT status,error FROM deployment_invocations WHERE id=:id'), {'id': invocation})).one() == ('failed', 'execution_lease_expired')
                assert (await db.execute(text('SELECT state FROM deployment_runtime_revisions WHERE id=:id'), {'id': dep['active_revision_id']})).scalar_one() == 'retired'
            assert not group.exists()
        finally:
            if child is not None and child.returncode is None:
                child.kill()
                await child.wait()
            await manager.shutdown()
