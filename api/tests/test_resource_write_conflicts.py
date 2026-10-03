"""Database serialization, protected drafts and explicit conflict contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import UploadFile
from sqlalchemy import text

from vibecanvas_api.services.write_conflicts import WriteConflict
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from vibecanvas_api.storage.repo_skills import SkillsRepo
from tests.storage.test_workflow_history import owner


@pytest.mark.asyncio
async def test_workflow_same_base_has_one_winner_and_other_major_is_independent(pg_engine):
    tenant, actor, _ = await owner()
    async with session_scope(tenant_id=tenant) as db:
        repo = WorkflowRepo(db, actor)
        wf = (await repo.create_workflow(name='Version protection'))['wf_id']
        meta = await repo.get_meta(wf)
        base = f"v{meta['active_major']}.sv{meta['active_sub']}"
    async def save(marker):
        async with session_scope(tenant_id=tenant) as db:
            return await WorkflowRepo(db, actor).commit(wf, {'marker': marker}, target_major=1, expected_version=base)
    outcomes = await asyncio.gather(save('first'), save('second'), return_exceptions=True)
    conflicts = [x for x in outcomes if isinstance(x, WriteConflict)]
    assert len(conflicts) == 1
    assert conflicts[0].detail == {
        'error': 'version_conflict', 'expected_version': base, 'current_version': 'v1.sv1',
        'message': conflicts[0].detail['message'],
    }
    async with session_scope(tenant_id=tenant) as db:
        repo = WorkflowRepo(db, actor)
        before = await repo.get_workflow_at(wf, 1, 1)
        await repo.new_version(wf, {'other': True}, source_version=(1, 1))
        result = await repo.commit(wf, {'merged': True}, target_major=1, expected_version='v1.sv1')
        assert result.sv == 2
        assert await repo.get_workflow_at(wf, 1, 1) == before
        assert await repo.get_workflow_at(wf, 2, 0) == {'other': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['stale', 'draft'])
async def test_skill_package_conflict_preserves_published_bytes_and_draft(pg_engine, monkeypatch, mode):
    from vibecanvas_api.routes import skills
    tenant, actor, _ = await owner()
    files=[('SKILL.md','text/markdown',b'---\nname: qa-protected\ndescription: Test\nversion: 1\n---\nOriginal')]
    async with session_scope(tenant_id=tenant) as db:
        repo=SkillsRepo(db)
        sid=await repo.insert(tenant_id=tenant,user_id=actor,name='qa-protected',source='custom',files=files)
        if mode == 'draft': await repo.save_draft(skill_id=sid,tenant_id=tenant,files=files+[('notes.txt','text/plain',b'user edits')])
    monkeypatch.setattr(skills,'_authorize_skill',AsyncMock())
    monkeypatch.setattr(skills,'_read_custom_bundle',AsyncMock(return_value=({},files)))
    with pytest.raises(WriteConflict) as raised:
        async with session_scope(tenant_id=tenant) as db:
            await skills.update_custom_skill_bundle(str(sid),None,bundle=UploadFile(filename='unused',file=None),
                expected_version=2 if mode=='stale' else 1,ctx=SimpleNamespace(user_id=actor,tenant_id=tenant),session=db,service=None)
    assert raised.value.status_code==409
    assert raised.value.detail['error']==('version_conflict' if mode=='stale' else 'draft_conflict')
    async with session_scope(tenant_id=tenant) as db:
        repo=SkillsRepo(db)
        assert (await repo.get(sid))['version']==1
        assert await repo.read_current_files(sid)==files
        assert bool(await repo.get_draft(sid)) == (mode=='draft')


@pytest.mark.asyncio
async def test_knowledge_draft_blocks_cli_and_legacy_file_mutations(pg_engine, monkeypatch):
    from vibecanvas_api.services.knowledge_packages import PackageFile, replace_package, package_snapshot
    from vibecanvas_api.services.knowledge_versions import save_snapshot, snapshot_row
    from vibecanvas_api.storage.repo_kb import KbRepo
    from vibecanvas_api.routes import kb as routes
    import io
    tenant, actor, _ = await owner()
    files=[PackageFile('README.md', b'---\nname: Protected\ndescription: Test\n---\nOriginal','text/markdown')]
    async with session_scope(tenant_id=tenant) as db:
        kb=await KbRepo(db).create_kb(tenant_id=tenant,user_id=actor,name='Protected')
        identifier=kb.id
        await replace_package(db,kb_id=identifier,actor_user_id=actor,expected_version=1,files=files,increment_version=False,derive_index=False)
        await save_snapshot(db,kb,files+[PackageFile('notes.txt',b'draft','text/plain')],version=0,base_version=1)
    monkeypatch.setattr(routes,'_authorize_knowledge_base',AsyncMock())
    for legacy in (False,True):
        with pytest.raises(WriteConflict) as raised:
            async with session_scope(tenant_id=tenant) as db:
                if legacy:
                    await routes._write_file(identifier,None,file=UploadFile(filename='test.txt',file=io.BytesIO(b'new')),
                        ctx=SimpleNamespace(tenant_id=tenant,user_id=actor),session=db,service=None,expected_version=1)
                else:
                    await replace_package(db,kb_id=identifier,actor_user_id=actor,expected_version=1,files=files,protect_draft=True,derive_index=False)
        assert raised.value.detail['error']=='draft_conflict'
    async with session_scope(tenant_id=tenant) as db:
        assert (await KbRepo(db).get_active(identifier)).package_version==1
        assert await snapshot_row(db,identifier,0) is not None
        assert [f.data for f in await package_snapshot(db,identifier)]==[files[0].data]


def test_full_replacements_require_explicit_base_versions():
    from vibecanvas_api.flowork_cli.cli import validate_arguments
    from pydantic import ValidationError
    from vibecanvas_api.schemas.workflow import GuardedCommitRequest
    for operation,arguments in [
        ('workflow.upload',{'workflow_id':'test','major':'v1','workflow':{}}),
        ('skill.update',{'skill_id':str(uuid4()),'files':[]}),
        ('knowledge.publish',{'knowledge_id':str(uuid4()),'files':[]}),
    ]:
        with pytest.raises(ValueError): validate_arguments(operation,arguments)
    with pytest.raises(ValidationError): GuardedCommitRequest(workflow={})
