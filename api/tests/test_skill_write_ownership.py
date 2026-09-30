"""Explicit custom-Skill ownership is stricter than a generic editor grant."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from vibecanvas_api.routes import skills
from vibecanvas_api.schemas.skills import SkillDraftSave, SkillVersionCreate


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['save', 'publish', 'bundle'])
@pytest.mark.parametrize(('source', 'owner', 'code'), [
    ('openai', 'caller', 'skill_read_only_source'),
    ('anthropic', 'caller', 'skill_read_only_source'),
    ('custom', 'another-user', 'skill_not_creator'),
])
async def test_editor_grant_cannot_write_catalog_or_another_creators_skill(monkeypatch, action, source, owner, code):
    repo = AsyncMock()
    repo.get.return_value = {'source': source, 'user_id': owner}
    monkeypatch.setattr(skills, 'SkillsRepo', lambda session: repo)
    # Simulate an allowed generic edit/publish grant. Domain ownership must
    # still reject the write before loading or modifying any draft files.
    monkeypatch.setattr(skills, '_authorize_skill', AsyncMock())
    route = {'save': skills.save_custom_skill_draft, 'publish': skills.publish_custom_skill_version, 'bundle': skills.update_custom_skill_bundle}[action]
    body = SkillDraftSave(skill_md='draft') if action == 'save' else SkillVersionCreate(version=2)
    with pytest.raises(HTTPException) as error:
        await route(skill_id='6f39f476-a371-449b-86f4-6c85640f7916', **({'bundle': None} if action == 'bundle' else {'body': body}),
                    request=None, ctx=SimpleNamespace(user_id='caller'), session=None, service=None)
    assert error.value.status_code == 403
    assert error.value.detail['code'] == code
    repo.read_draft_files.assert_not_awaited()
    repo.get_draft.assert_not_awaited()
    repo.save_draft.assert_not_awaited()
    repo.publish_draft.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('valid', [True, False])
async def test_bundle_validates_all_files_before_publishing_next_version(monkeypatch, valid):
    import io
    import zipfile
    from fastapi import UploadFile
    user_id = '9669344f-39b6-42c0-9100-8df9488908dc'
    current = {'source':'custom', 'user_id':user_id, 'version':3}
    repo = AsyncMock()
    repo.get.return_value = current
    repo._lock_custom_skill.return_value = current
    repo.save_draft.return_value = {'draft_hash':'validated'}
    repo.publish_draft.return_value = {'version':4}
    monkeypatch.setattr(skills, 'SkillsRepo', lambda session: repo)
    monkeypatch.setattr(skills, '_authorize_skill', AsyncMock(return_value=SimpleNamespace(decision=None)))
    monkeypatch.setattr(skills, '_row_to_out', AsyncMock(return_value={'version':4}))
    monkeypatch.setattr(skills, 'ResourceProvenanceBuilder', lambda session: None)
    monkeypatch.setattr(skills, 'require_clean_upload', AsyncMock())
    package = io.BytesIO()
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('SKILL.md', '---\nname: own-skill\ndescription: Example\nversion: 999\n---\nFollow these instructions.\n')
        archive.writestr('references/example.txt' if valid else '../escape.txt', 'reference')
    package.seek(0)
    invoke = lambda: skills.update_custom_skill_bundle(
        skill_id='6f39f476-a371-449b-86f4-6c85640f7916', request=None,
        bundle=UploadFile(filename='skill.zip', file=package),
        ctx=SimpleNamespace(user_id=user_id, tenant_id=user_id), session=AsyncMock(), service=None,
    )
    if not valid:
        with pytest.raises(HTTPException) as error:
            await invoke()
        assert error.value.status_code == 422
        repo.save_draft.assert_not_awaited()
        repo.publish_draft.assert_not_awaited()
        return
    assert await invoke() == {'version':4}
    published = repo.publish_draft.await_args.kwargs
    assert published['version'] == 4
    files = {path:data for path, _type, data in published['files']}
    assert b'version: 4' in files['SKILL.md']
    assert files['references/example.txt'] == b'reference'
    assert repo.save_draft.await_args.kwargs['files'] == published['files']


@pytest.mark.asyncio
@pytest.mark.parametrize(('source','owner','editable'), [
    ('custom','caller',True), ('custom','other',False),
    ('openai','caller',False), ('anthropic','caller',False),
])
async def test_effective_access_hides_forbidden_edit_actions(monkeypatch,source,owner,editable):
    from vibecanvas_api.schemas.access import ResourceAccessOut
    from vibecanvas_api.authorization.types import Action
    access=ResourceAccessOut(capabilities=[Action.VIEW,Action.USE,Action.UPDATE,Action.PUBLISH,Action.DELETE])
    monkeypatch.setattr(skills,'access_from_decision',lambda decision:access)
    provenance=SimpleNamespace(build=AsyncMock(return_value={
        'ownership_scope':'personal','origin_type':'created',
        'owner':{'type':'user','display_name':'Example'},
    }))
    result=await skills._row_to_out({
        'skill_id':'6f39f476-a371-449b-86f4-6c85640f7916','name':'example',
        'source':source,'user_id':owner,'version':1,
    },None,provenance,user_id='caller')
    assert (Action.UPDATE in result.access.capabilities)==editable
    assert (Action.PUBLISH in result.access.capabilities)==editable
    assert Action.VIEW in result.access.capabilities
    assert Action.USE in result.access.capabilities
    assert Action.DELETE in result.access.capabilities
    # Projection must not mutate the authorization service's original decision.
    assert Action.UPDATE in access.capabilities
