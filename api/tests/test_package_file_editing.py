"""File edits preserve publication boundaries and reject stale or unsafe writes."""
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from vibecanvas_api.routes import skills, kb
from vibecanvas_api.services.knowledge_packages import PackageFile
from vibecanvas_api.services import knowledge_versions

SKILL_MD = b'---\nname: example\ndescription: Example\nversion: 1\n---\nInstructions'


@pytest.fixture
def skill_repo(monkeypatch):
    owner = str(uuid4())
    repo = AsyncMock()
    repo.get.return_value = {'source':'custom','user_id':owner,'revision_hash':'published'}
    repo.get_draft.return_value = {'draft_hash':'draft'}
    repo.read_draft_files.return_value = [('SKILL.md','text/markdown',SKILL_MD),('notes.txt','text/plain',b'old')]
    monkeypatch.setattr(skills,'SkillsRepo',lambda session:repo)
    monkeypatch.setattr(skills,'_authorize_skill',AsyncMock())
    monkeypatch.setattr(skills,'require_clean_upload',AsyncMock())
    monkeypatch.setattr(skills,'get_custom_skill_draft',AsyncMock(return_value={'saved':True}))
    return repo, SimpleNamespace(user_id=owner,tenant_id=owner)


@pytest.mark.asyncio
@pytest.mark.parametrize(('path','create','token','code'),[
    ('../escape',True,'draft',422), ('/absolute',True,'draft',422),
    ('notes.txt',True,'draft',409), ('missing.txt',False,'draft',409),
    ('notes.txt/child',True,'draft',409), ('notes.txt',False,'stale',409),
])
async def test_skill_rejects_unsafe_conflicting_or_stale_edits(skill_repo,path,create,token,code):
    repo,ctx=skill_repo
    with pytest.raises(HTTPException) as exc:
        await skills.put_skill_draft_file(str(uuid4()),path,None,
            UploadFile(filename='text',file=io.BytesIO(b'new')),token,create,ctx,None,None)
    assert exc.value.status_code == code
    repo.save_draft.assert_not_awaited()
    repo.publish_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_skill_file_save_only_updates_draft(skill_repo):
    repo,ctx=skill_repo
    await skills.put_skill_draft_file(str(uuid4()),'notes.txt',None,
        UploadFile(filename='notes.txt',file=io.BytesIO(b'new')),'draft',False,ctx,None,None)
    assert dict((p,data) for p,_,data in repo.save_draft.await_args.kwargs['files']) == {'SKILL.md':SKILL_MD,'notes.txt':b'new'}
    repo.publish_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_skill_root_cannot_be_deleted(skill_repo):
    repo,ctx=skill_repo
    with pytest.raises(HTTPException) as exc:
        await skills.delete_skill_draft_file(str(uuid4()),'SKILL.md',None,'draft',ctx,None,None)
    assert exc.value.status_code == 409
    repo.save_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_skill_edit_requires_live_update_authorization(monkeypatch,skill_repo):
    repo,ctx=skill_repo
    monkeypatch.setattr(skills,'_authorize_skill',AsyncMock(side_effect=HTTPException(403)))
    with pytest.raises(HTTPException) as exc:
        await skills.delete_skill_draft_file(str(uuid4()),'notes.txt',None,'draft',ctx,None,None)
    assert exc.value.status_code == 403
    repo.save_draft.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('token',['old','new'])
async def test_knowledge_draft_checks_revision_and_publication(monkeypatch,token):
    current = SimpleNamespace(id=uuid4(),package_version=4)
    monkeypatch.setattr(knowledge_versions,'snapshot_row',AsyncMock(return_value={'content_hash':'new','base_version':4,'version':0}))
    monkeypatch.setattr(knowledge_versions,'read_snapshot',AsyncMock(return_value=[PackageFile('README.md',b'draft','text/markdown')]))
    if token == 'old':
        with pytest.raises(HTTPException) as exc:
            await kb._knowledge_draft_state(None,current,token)
        assert exc.value.status_code == 409
    else:
        row,files = await kb._knowledge_draft_state(None,current,token)
        assert row['version'] == 0 and files[0].data == b'draft'
    assert current.package_version == 4


@pytest.mark.asyncio
async def test_knowledge_encrypted_history_and_draft_are_independent(pg_engine,monkeypatch):
    from tests.routes.test_kb_routes import _seed_tenant_and_user
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_kb import KbRepo
    from vibecanvas_api.services.knowledge_packages import replace_package,package_snapshot
    tenant,user=uuid4(),uuid4()
    await _seed_tenant_and_user(pg_engine,tenant,user)
    published=[PackageFile('README.md',b'# Published content','text/markdown')]
    draft=[PackageFile('README.md',b'# Draft only','text/markdown'),PackageFile('new.txt',b'new','text/plain')]
    async with session_scope(tenant_id=str(tenant)) as session:
        resource=await KbRepo(session).create_kb(tenant_id=tenant,user_id=user,name='Versioned')
        identifier=resource.id
        await replace_package(session,kb_id=identifier,actor_user_id=user,expected_version=1,files=published,increment_version=False,derive_index=False)
        await knowledge_versions.save_snapshot(session,resource,draft,version=0,base_version=1)
        assert (await package_snapshot(session,identifier))[0].data.endswith(b'# Published content')
        assert (await knowledge_versions.read_snapshot(session,identifier,0))[0].data.endswith(b'# Draft only')
        stored=await knowledge_versions.snapshot_row(session,identifier,0)
        assert 'Draft only' not in stored['content_ciphertext']
        version,_=await replace_package(session,kb_id=identifier,actor_user_id=user,expected_version=1,files=draft,derive_index=False)
        assert version == 2
        assert await knowledge_versions.read_snapshot(session,identifier,0) is None
        assert (await knowledge_versions.read_snapshot(session,identifier,1))[0].data.endswith(b'# Published content')
        assert (await knowledge_versions.read_snapshot(session,identifier,2))[0].data.endswith(b'# Draft only')
        assert (await package_snapshot(session,identifier))[0].data.endswith((b'# Draft only', b'new'))
    async with session_scope(tenant_id=str(uuid4())) as session:
        assert await knowledge_versions.snapshot_row(session,identifier,1) is None


@pytest.mark.asyncio
async def test_runtime_refresh_replaces_only_target_skill_and_removes_old_files(tmp_path,monkeypatch):
    from contextlib import asynccontextmanager
    from vibecanvas_api.services import runtime_skills
    repo=AsyncMock()
    repo.read_revision_files.return_value=[('SKILL.md','text/markdown',SKILL_MD),('new.txt','text/plain',b'new')]
    @asynccontextmanager
    async def scope(**kwargs):
        yield None
    monkeypatch.setattr(runtime_skills,'session_scope',scope)
    monkeypatch.setattr(runtime_skills,'SkillsRepo',lambda session:repo)
    target=tmp_path/'scope'/'target';target.mkdir(parents=True)
    (target/'SKILL.md').write_text('old');(target/'removed.txt').write_text('old')
    sibling=tmp_path/'scope'/'sibling';sibling.mkdir();(sibling/'SKILL.md').write_text('keep')
    assert await runtime_skills.refresh_runtime_skill(destination=str(target),tenant_id=str(uuid4()),skill_id=str(uuid4()),revision_id=str(uuid4()),revision_hash='hash') == 2
    assert (target/'SKILL.md').read_bytes() == SKILL_MD
    assert not (target/'removed.txt').exists()
    assert (sibling/'SKILL.md').read_text() == 'keep'


@pytest.mark.asyncio
async def test_refresh_denied_skill_never_reaches_sandbox(monkeypatch):
    from contextlib import asynccontextmanager
    from vibecanvas_api.services.agent_runtime import cli_skills
    from vibecanvas_api.agents.tools import _session_fs
    @asynccontextmanager
    async def scope(**kwargs):
        yield None
    monkeypatch.setattr(cli_skills,'session_scope',scope)
    monkeypatch.setattr(cli_skills,'resource_route_params',lambda ctx,session:{'session':session})
    monkeypatch.setattr(cli_skills.skills,'_authorize_skill',AsyncMock(side_effect=HTTPException(403)))
    sandbox=AsyncMock();monkeypatch.setattr(_session_fs,'_require_session',sandbox)
    with pytest.raises(HTTPException):
        await cli_skills.refresh(SimpleNamespace(tenant_id='t',username='u'),None,uuid4())
    sandbox.assert_not_awaited()
