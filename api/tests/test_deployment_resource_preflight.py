from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from vibecanvas_api.services import deployment_resource_preflight as preflight
from tests.test_deployment_rollout import setup_rollout


@pytest.mark.asyncio
@pytest.mark.parametrize('resident', [False, True])
@pytest.mark.parametrize('failure', ['permission', 'package', 'identity', 'projection', None])
async def test_persisted_deployment_preflight_and_cleanup(pg_engine, app_engine, monkeypatch, failure, resident):
    _, dep, spec = await setup_rollout(pg_engine, app_engine)
    import json
    from sqlalchemy import text
    from uuid import UUID
    from vibecanvas_api.storage.db import short_session_scope
    from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
    account = uuid4()
    spec = {**spec, 'service_account_id': str(account)}
    async with short_session_scope(tenant_id=str(dep['tenant_id'])) as db:
        await ServiceAccountsRepo(db).create_for_owner(service_account_id=account,
            tenant_id=dep['tenant_id'], name='preflight-test', kind='deployment',
            owner_resource_type='deployment', owner_resource_id=str(dep['id']),
            created_by=UUID(spec['user_id']))
        await db.execute(text('UPDATE deployments SET service_account_id=:account WHERE id=:id'),
                         {'account': account, 'id': dep['id']})
        await db.execute(text('UPDATE deployment_runtime_revisions SET spec=CAST(:spec AS jsonb) WHERE id=:id'),
                         {'spec': json.dumps(spec), 'id': dep['active_revision_id']})
    graph = {'worker': {'node_type': 'SubAgentNode', 'node_config': {
        'skills': [{'id': str(uuid4()), 'name': 'test'}]}}}
    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(preflight, 'openfga_client_from_config', lambda: client)
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=failure != 'permission')))
    monkeypatch.setattr(preflight, 'authz_service_for_session', lambda **kw: service)
    resolve = AsyncMock(return_value={'skills': [], 'nodes': {}, 'mcp_servers': []})
    monkeypatch.setattr(preflight, 'resolve_workflow_resources', resolve)
    delegate = AsyncMock(return_value=('intent',))
    project = AsyncMock(side_effect=RuntimeError('projection unavailable') if failure == 'projection' else None)
    monkeypatch.setattr(preflight, 'delegate_new_resources', delegate)
    monkeypatch.setattr(preflight, 'apply_committed_structural_mutations', project)
    roots = []
    async def materialize(**kwargs):
        from pathlib import Path
        root = Path(kwargs['root'])
        assert root.is_dir()
        roots.append(root)
        (root / 'prepared').write_text('decrypted test package')
        if failure == 'package':
            raise ValueError('package invalid')
    monkeypatch.setattr(preflight, 'materialize_workflow_skills', materialize)
    sandbox = SimpleNamespace(prepare_workflow_skills=AsyncMock(
        side_effect=ValueError('package invalid') if failure == 'package' else None))
    if failure == 'identity':
        spec = {**spec, 'mount_enabled': not spec['mount_enabled']}
    args = dict(tenant_id=str(dep['tenant_id']), revision_id=str(dep['active_revision_id']),
                spec=spec, workflow=graph, sandbox_session=sandbox if resident else None)
    if failure:
        with pytest.raises((PermissionError, ValueError, RuntimeError), match={
                'permission': 'deployment_resource_execution_access_revoked',
                'identity': 'deployment_resource_identity_unavailable',
                'package': 'package invalid', 'projection': 'projection unavailable'}[failure]):
            await preflight.validate_deployment_resources(**args)
    else:
        await preflight.validate_deployment_resources(**args)
        resolve.assert_awaited_once()
        if resident:
            prepared = sandbox.prepare_workflow_skills.await_args.args[0]
            assert prepared['execution']['execution_resource_type'] == 'deployment_preparation'
            assert prepared['execution']['spec'] == spec
            assert prepared['lease_id'] == 'deployment-preparation'
            assert not roots
        else:
            assert len(roots) == 1
    assert all(not root.exists() for root in roots)
    client.close.assert_awaited_once()
    if failure in ('identity', 'permission', 'projection'):
        resolve.assert_not_awaited()
        sandbox.prepare_workflow_skills.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_resource_workflow_does_not_need_preflight_client(monkeypatch):
    factory = AsyncMock()
    monkeypatch.setattr(preflight, 'openfga_client_from_config', factory)
    await preflight.validate_deployment_resources(tenant_id='unused', revision_id='unused', spec={}, workflow={})
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_new_delegation_excludes_existing_and_revoked_resources(monkeypatch):
    from vibecanvas_api.services import service_account_resources as delegation
    from vibecanvas_api.authorization import projection
    existing, revoked, added = (str(uuid4()) for _ in range(3))
    repo = SimpleNamespace(resource_refs=AsyncMock(return_value=[
        ('skill_installation', existing), ('skill_installation', revoked)]))
    monkeypatch.setattr(delegation, 'ServiceAccountsRepo', lambda session: repo)
    bind = AsyncMock(return_value=[('skill_installation', existing), ('skill_installation', added)])
    monkeypatch.setattr(delegation, 'bind_workflow_resources', bind)
    enqueue = AsyncMock(return_value=('intent',))
    monkeypatch.setattr(projection, 'enqueue_structural_delta', enqueue)
    graph = {'worker': {'node_type': 'SubAgentNode', 'node_config': {'skills': [
        {'id': identifier, 'name': 'skill'} for identifier in (existing, revoked, added)]}}}
    kwargs = dict(session=object(), coordinator=object(), tenant_id=str(uuid4()),
                  service_account_id=str(uuid4()), created_by=str(uuid4()), workflow=graph)
    assert await delegation.delegate_new_resources(**kwargs) == ('intent',)
    passed = bind.await_args.kwargs['workflow']['worker']['node_config']['skills']
    assert passed == [{'id': added, 'name': 'skill'}]
    assert len(enqueue.await_args.kwargs['after']) == 1
    repo.resource_refs.assert_awaited_once()
    assert repo.resource_refs.await_args.kwargs['include_revoked'] is True
    graph['worker']['node_config']['skills'].pop()
    bind.reset_mock()
    enqueue.reset_mock()
    assert await delegation.delegate_new_resources(**kwargs) == ()
    bind.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['valid', 'rotated', 'revoked', 'outage'])
async def test_preparation_view_rechecks_identity_without_delegating(monkeypatch, state):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def scope(**kwargs):
        yield object()
    monkeypatch.setattr(preflight, 'short_session_scope', scope)
    authorize = AsyncMock(return_value=(object(), object(), SimpleNamespace(authz_generation=2 if state == 'rotated' else 1)))
    if state == 'revoked':
        authorize.side_effect = PermissionError('revoked')
    if state == 'outage':
        authorize.side_effect = RuntimeError('authorization unavailable')
    monkeypatch.setattr(preflight, '_authorize_revision', authorize)
    delegate = AsyncMock()
    monkeypatch.setattr(preflight, 'delegate_new_resources', delegate)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(openfga_client=object())))
    claims = {'organization_id': str(uuid4()), 'revision_id': str(uuid4()), 'spec': {},
              'principal_generation': 1}
    resolve = AsyncMock(return_value=['allowed'])
    if state == 'outage':
        with pytest.raises(RuntimeError, match='authorization unavailable'):
            await preflight.authorize_prepared_skills(request, claims, resolve=resolve)
    else:
        result = await preflight.authorize_prepared_skills(request, claims, resolve=resolve)
        assert result == (['allowed'] if state == 'valid' else [])
    if state != 'valid':
        resolve.assert_not_awaited()
    delegate.assert_not_awaited()
