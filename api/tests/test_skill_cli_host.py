import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from vibecanvas_api.services.agent_runtime import cli_skills as host


def files(valid=True):
    content = '---\nname: example\ndescription: Example\n---\nInstructions' if valid else 'No frontmatter'
    return [{'path':'SKILL.md','data':base64.b64encode(content.encode()).decode()}]


def setup(monkeypatch, operation):
    ctx = SimpleNamespace(tenant_id=str(uuid4()),username=str(uuid4()))
    call = SimpleNamespace(operation=operation,capability=SimpleNamespace(approval_mode='always_allow'),call_id='call',durable_lease=False)
    monkeypatch.setattr(host.agent_context,'resolve_context',AsyncMock(return_value=ctx))
    session = AsyncMock()
    @asynccontextmanager
    async def scope(**kwargs):
        yield session
    monkeypatch.setattr(host,'session_scope',scope)
    monkeypatch.setattr(host,'resource_route_params',lambda ctx,session:{'session':session,'ctx':SimpleNamespace(user_id=ctx.username)})
    monkeypatch.setattr(host,'admitted_resource_route_params',AsyncMock(side_effect=lambda ctx,session,*args: host.resource_route_params(ctx,session)))
    return call, ctx, session


@pytest.mark.asyncio
@pytest.mark.parametrize('installed', [True, False])
async def test_personal_installation_uses_actor_route_not_package_delete(monkeypatch, installed):
    call, ctx, session = setup(monkeypatch, 'skill.install' if installed else 'skill.uninstall')
    guard = AsyncMock()
    monkeypatch.setattr(host, '_require_active_chat_write', guard)
    handler = AsyncMock()
    monkeypatch.setattr(host.skills, 'install_skill_for_user' if installed else 'uninstall_skill_for_user', handler)
    delete = AsyncMock()
    monkeypatch.setattr(host.skills, 'delete_skill', delete)
    identifier = str(uuid4())
    result = await host.execute(call, {'skill_id': identifier})
    assert result['status'] == 'succeeded'
    assert result['installed'] is installed
    guard.assert_awaited_once_with(session, ctx)
    assert handler.await_args.kwargs['skill_id'] == identifier
    session.commit.assert_awaited_once()
    delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('valid',[True,False])
async def test_check_validates_package_without_publication(monkeypatch,valid):
    call,_,session = setup(monkeypatch,'skill.check')
    scanner=AsyncMock()
    monkeypatch.setattr(host.skills,'require_clean_upload',scanner)
    result=await host.execute(call,{'files':files(valid)})
    if valid:
        assert result['valid'] and result['published'] is False
        scanner.assert_awaited_once()
    else:
        assert result['error']=='invalid_arguments'
        scanner.assert_not_awaited()
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_pins_authorized_revision(monkeypatch):
    call,_,_=setup(monkeypatch,'skill.download')
    identifier,revision=uuid4(),uuid4()
    auth=AsyncMock()
    revision_auth=AsyncMock()
    monkeypatch.setattr(host.skills,'_authorize_skill',auth)
    monkeypatch.setattr(host.skills,'_authorize_skill_revision',revision_auth)
    repo=AsyncMock()
    repo.get.return_value={'current_revision_id':revision,'name':'example','source':'custom','version':4,'revision_hash':'hash'}
    repo.read_revision_files.return_value=[('SKILL.md','text/markdown',b'instructions')]
    monkeypatch.setattr(host,'SkillsRepo',lambda session:repo)
    result=await host.execute(call,{'skill_id':str(identifier)})
    assert result['version']==4
    assert base64.b64decode(result['files'][0]['data'])==b'instructions'
    repo.read_revision_files.assert_awaited_once_with(identifier,revision)
    assert revision_auth.await_args.kwargs['revision_id']==revision
    assert auth.await_args.kwargs['action']==host.Action.USE


@pytest.mark.asyncio
async def test_denied_update_never_creates_lease_or_publishes(monkeypatch):
    call,_,session=setup(monkeypatch,'skill.update')
    monkeypatch.setattr(host,'authorize',AsyncMock(side_effect=HTTPException(403,'denied')))
    publish=AsyncMock()
    monkeypatch.setattr(host.skills,'update_custom_skill_bundle',publish)
    result=await host.execute(call,{'skill_id':str(uuid4()),'files':files(),'expected_version':1})
    assert result['error']=='permission_denied'
    session.execute.assert_not_awaited()
    publish.assert_not_awaited()
    assert not call.durable_lease


@pytest.mark.asyncio
@pytest.mark.parametrize('source,own', [('custom',True),('catalog',True),('custom',False),('catalog',False)])
async def test_delete_obeys_resource_permission_instead_of_creator_identity(monkeypatch,source,own):
    user,identifier=uuid4(),uuid4()
    repo=AsyncMock()
    repo.get.return_value={'user_id':user if own else uuid4(),'source':source}
    monkeypatch.setattr(host,'SkillsRepo',lambda session:repo)
    auth=AsyncMock()
    monkeypatch.setattr(host.skills,'_authorize_skill',auth)
    params={'session':object(),'ctx':SimpleNamespace(user_id=user)}
    await host.authorize(params,identifier,deleting=True)
    assert auth.await_args.kwargs['action']==host.Action.DELETE


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,deny', [('always_allow',False),('always_ask',False),('agent',True)])
async def test_delete_approval_and_dispatch(monkeypatch,mode,deny):
    from vibecanvas_api.services.agent_runtime import cli_delete
    call,ctx,session=setup(monkeypatch,'skill.delete')
    call.capability=SimpleNamespace(approval_mode=mode,tenant_id=ctx.tenant_id,turn_id='run')
    session.execute.return_value=SimpleNamespace(first=lambda:(1,))
    request=SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(host,'resource_route_params',lambda *a:{'request':request})
    authorize=AsyncMock()
    monkeypatch.setattr(host,'authorize',authorize)
    monkeypatch.setattr(host,'_require_active_chat_write',AsyncMock())
    approve=AsyncMock(side_effect=host.ToolError('approval_denied','Declined') if deny else None)
    monkeypatch.setattr(cli_delete,'_approve',approve)
    delete=AsyncMock()
    monkeypatch.setattr(host.skills,'delete_skill',delete)
    identifier=str(uuid4())
    result=await host.execute(call,{'skill_id':identifier})
    assert approve.await_count==int(mode!='always_allow')
    if deny:
        assert result['error']=='approval_denied'
        delete.assert_not_awaited()
    else:
        assert result['deleted'] is True
        delete.assert_awaited_once_with(skill_id=identifier,request=request)
        assert authorize.await_count==2


@pytest.mark.asyncio
async def test_publication_rechecks_original_version_after_approval(monkeypatch):
    from vibecanvas_api.services.agent_runtime import cli_delete
    from vibecanvas_api.services.write_conflicts import WriteConflict
    call,ctx,session=setup(monkeypatch,'skill.update')
    call.capability=SimpleNamespace(approval_mode='always_ask',tenant_id=ctx.tenant_id,turn_id='turn')
    session.execute.return_value=SimpleNamespace(first=lambda: (1,))
    monkeypatch.setattr(host,'authorize',AsyncMock())
    monkeypatch.setattr(host,'_require_active_chat_write',AsyncMock())
    monkeypatch.setattr(host,'resource_route_params',lambda *a: {'request':SimpleNamespace(state=SimpleNamespace())})
    version=1
    async def approve(*a,**k):
        nonlocal version
        version=2  # Another editor publishes while the confirmation is open.
    monkeypatch.setattr(cli_delete,'_approve',approve)
    async def publish(*a,expected_version,**k):
        assert expected_version==1
        raise WriteConflict(expected_version=expected_version,current_version=version)
    monkeypatch.setattr(host.skills,'update_custom_skill_bundle',publish)
    result=await host.execute(call,{'skill_id':str(uuid4()),'files':files(),'expected_version':1})
    assert result['error']=='version_conflict'
    assert result['current_version']==2 and result['expected_version']==1
    session.commit.assert_not_awaited()
