from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from vibecanvas_api.services import workflow_skill_cache as cache
from vibecanvas_api.services.workflow_resources import publish_skill_files


@pytest.mark.asyncio
async def test_live_snapshots_pin_old_versions_then_collect_after_completion(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    identifier = str(uuid4())
    alive = {'old': True, 'new': True}
    async def active(session, claims):
        return alive[claims.execution_id]
    monkeypatch.setattr(cache, '_workflow_execution_is_active', active)
    def snapshot(run, version, lease):
        publish_skill_files(str(root), skill_id=identifier, revision_hash=version * 64,
            files=[('SKILL.md', 'text/plain', version.encode())])
        return {'execution': {'execution_id': run}, 'lease_id': lease,
                'skills': [{'id': identifier, 'revision_hash': version * 64}]}
    old = snapshot('old', 'a', 'first')
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot=old)
    new = snapshot('new', 'b', 'second')
    inode = root.stat().st_ino
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot=new)
    assert (root / identifier / ('a' * 64)).exists()
    alive['old'] = False
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot=new)
    assert not (root / identifier / ('a' * 64)).exists()
    assert (root / identifier / ('b' * 64)).exists()
    assert root.stat().st_ino == inode
    assert not list(root.glob('*.json'))


@pytest.mark.asyncio
async def test_two_snapshots_in_same_agent_turn_are_both_retained(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    identifier = str(uuid4())
    monkeypatch.setattr(cache, '_workflow_execution_is_active', AsyncMock(return_value=True))
    for letter in ('a', 'b'):
        publish_skill_files(str(root), skill_id=identifier, revision_hash=letter * 64,
            files=[('SKILL.md', 'text/plain', letter.encode())])
        await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot={
            'execution': {'execution_id': 'same-agent-turn'}, 'lease_id': letter,
            'skills': [{'id': identifier, 'revision_hash': letter * 64}]})
    assert len(list((root / identifier).iterdir())) == 2


@pytest.mark.asyncio
async def test_database_failure_keeps_existing_files(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    identifier = str(uuid4())
    for version in ('a', 'b'):
        publish_skill_files(str(root), skill_id=identifier, revision_hash=version * 64,
            files=[('SKILL.md', 'text/plain', version.encode())])
    first = {'execution': {'execution_id': 'old'}, 'lease_id': 'a',
             'skills': [{'id': identifier, 'revision_hash': 'a' * 64}]}
    # The first execution pins both versions until its state can be checked.
    first['skills'].append({'id': identifier, 'revision_hash': 'b' * 64})
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot=first)
    monkeypatch.setattr(cache, '_workflow_execution_is_active', AsyncMock(side_effect=RuntimeError('database unavailable')))
    with pytest.raises(RuntimeError, match='database unavailable'):
        await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot={
            'execution': {'execution_id': 'new'}, 'lease_id': 'b',
            'skills': [{'id': identifier, 'revision_hash': 'b' * 64}]})
    assert len(list((root / identifier).iterdir())) == 2


@pytest.mark.asyncio
async def test_periodic_sweep_removes_revoked_skill_but_preserves_other_live_files(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    identifiers = [str(uuid4()), str(uuid4())]
    monkeypatch.setattr(cache, '_workflow_execution_is_active', AsyncMock(return_value=True))
    for identifier in identifiers:
        publish_skill_files(str(root), skill_id=identifier, revision_hash='a' * 64,
            files=[('SKILL.md', 'text/plain', b'content')])
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot={
        'execution': {'execution_id': 'running'}, 'lease_id': 'lease',
        'skills': [{'id': identifier, 'revision_hash': 'a' * 64} for identifier in identifiers]})
    authorize = AsyncMock(side_effect=lambda claims, skills: [s for s in skills if s['id'] == identifiers[1]])
    await cache.reconcile_skill_cache(session=object(), root=str(root), authorize=authorize)
    assert not (root / identifiers[0]).exists()
    assert (root / identifiers[1] / ('a' * 64) / 'SKILL.md').read_bytes() == b'content'
    authorize.assert_awaited_once()
    monkeypatch.setattr(cache, '_workflow_execution_is_active', AsyncMock(return_value=False))
    await cache.reconcile_skill_cache(session=object(), root=str(root), authorize=authorize)
    assert not list(root.iterdir())
    assert not list((tmp_path / '.skills-workflow-leases').iterdir())


@pytest.mark.asyncio
async def test_periodic_authorization_failure_does_not_remove_files(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    identifier = str(uuid4())
    publish_skill_files(str(root), skill_id=identifier, revision_hash='a' * 64,
        files=[('SKILL.md', 'text/plain', b'content')])
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot={
        'execution': {'execution_id': 'running'}, 'skills': [{'id': identifier, 'revision_hash': 'a' * 64}]})
    monkeypatch.setattr(cache, '_workflow_execution_is_active', AsyncMock(return_value=True))
    with pytest.raises(RuntimeError, match='authorization unavailable'):
        await cache.reconcile_skill_cache(session=object(), root=str(root),
            authorize=AsyncMock(side_effect=RuntimeError('authorization unavailable')))
    assert (root / identifier / ('a' * 64) / 'SKILL.md').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 409, 503])
async def test_live_authorization_denial_is_distinct_from_outage(monkeypatch, status):
    from fastapi import HTTPException
    from vibecanvas_api.services import workflow_execution_authorization as execution_auth
    monkeypatch.setattr(execution_auth, 'authorize_workflow_execution',
        AsyncMock(side_effect=HTTPException(status, 'denied')))
    if status == 503:
        with pytest.raises(HTTPException):
            await cache.authorized_lease_skills(object(), {}, [])
    else:
        assert await cache.authorized_lease_skills(object(), {}, []) == []


@pytest.mark.asyncio
async def test_live_authorization_checks_discovered_pinned_revision(monkeypatch):
    from types import SimpleNamespace
    from vibecanvas_api.authorization.types import PrincipalType, ResourceType
    from vibecanvas_api.services import workflow_execution_authorization as execution_auth
    from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
    from vibecanvas_api.storage.repo_skills import SkillsRepo
    retained, revoked = str(uuid4()), str(uuid4())
    revision = str(uuid4())
    principal = SimpleNamespace(type=PrincipalType.SERVICE_ACCOUNT, id=str(uuid4()))
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    from vibecanvas_api.authorization.types import AuthzRequestContext
    organization = str(uuid4())
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: organization)))
    monkeypatch.setattr('vibecanvas_api.services.runtime_skills.authorized_skill_rows', AsyncMock(return_value=[
        {'skill_id': retained, 'tenant_id': organization}]))
    async def authorize(request, capability, *, resolve):
        return await resolve(session=session, service=service, principal=principal,
            authz_context=AuthzRequestContext(active_organization_id=organization), capability=SimpleNamespace(organization_id=organization))
    monkeypatch.setattr(execution_auth, 'authorize_workflow_execution', authorize)
    monkeypatch.setattr(ServiceAccountsRepo, 'resource_refs',
        AsyncMock(return_value=[('skill_installation', retained)]))
    monkeypatch.setattr(SkillsRepo, 'get', AsyncMock(return_value={'current_revision_id': 'new-publication'}))
    get_revision = AsyncMock(return_value={'revision_id': revision, 'revision_hash': 'a' * 64})
    monkeypatch.setattr(SkillsRepo, 'get_revision', get_revision)
    skills = [{'id': identifier, 'revision_id': revision, 'revision_hash': 'a' * 64}
              for identifier in (retained, revoked)]
    assert await cache.authorized_lease_skills(object(), {}, skills) == skills[:1]
    get_revision.assert_awaited_once_with(retained, revision)
    refs = [call.args[2] for call in service.check.await_args_list]
    assert len(refs) == 2
    assert refs[1].type == ResourceType.SKILL_REVISION


@pytest.mark.asyncio
async def test_preparation_lease_keeps_files_until_first_invocation(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    identifier = str(uuid4())
    publish_skill_files(str(root), skill_id=identifier, revision_hash='a' * 64,
                        files=[('SKILL.md', 'text/plain', b'prewarmed')])
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot={
        'execution': {'execution_resource_type': 'deployment_preparation'},
        'lease_id': 'deployment-preparation',
        'skills': [{'id': identifier, 'revision_hash': 'a' * 64}]})
    authorize = AsyncMock(side_effect=lambda claims, skills: skills)
    await cache.reconcile_skill_cache(session=object(), root=str(root), authorize=authorize)
    assert (root / identifier / ('a' * 64)).exists()
    authorize.assert_awaited_once()
    publish_skill_files(str(root), skill_id=identifier, revision_hash='b' * 64,
                        files=[('SKILL.md', 'text/plain', b'latest')])
    await cache.reconcile_skill_cache(session=object(), root=str(root), snapshot={
        'execution': {'execution_resource_type': 'deployment_invocation', 'execution_id': 'new'},
        'lease_id': 'new', 'skills': [{'id': identifier, 'revision_hash': 'b' * 64}]})
    assert not (root / identifier / ('a' * 64)).exists()
    assert (root / identifier / ('b' * 64)).exists()
    assert len(list((tmp_path / '.skills-workflow-leases').glob('*.json'))) == 1
