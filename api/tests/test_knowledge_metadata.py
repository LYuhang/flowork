from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import base64

import pytest

from vibecanvas_api.services.knowledge_packages import PackageFile, replace_package, package_snapshot
from vibecanvas_api.services.knowledge_metadata import package_metadata, with_metadata
from vibecanvas_api.services import knowledge_versions
from vibecanvas_api.services.agent_runtime import cli_knowledge
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_kb import KbRepo


def package(name, description=''):
    return with_metadata([PackageFile('README.md', b'# Original body', 'text/markdown')], name=name, description=description)


@pytest.mark.parametrize('header', ['', '---\nname: A\n---\n', '---\nname: 123\ndescription: x\n---\n', '---\nname: A\ndescription: []\n---\n', '---\nname: A\ndescription: x\n'])
def test_invalid_frontmatter_is_rejected(header):
    with pytest.raises(ValueError):
        package_metadata([PackageFile('README.md', header.encode(), '')])


def test_unicode_metadata_and_legacy_copy_preserve_original():
    old = [PackageFile('README.md', b'# Original body', '')]
    new = with_metadata(old, name='资料库', description='产品资料')
    assert old[0].data == b'# Original body'
    assert new[0].data.endswith(old[0].data)
    assert package_metadata(new) == {'name':'资料库', 'description':'产品资料'}
    assert package_metadata(old, required=False) is None
    assert with_metadata(new, name='资料库', description='产品资料') is new
    legacy_yaml = [PackageFile('README.md', b'---\ntitle: Legacy\n---\n# Body', '')]
    assert with_metadata(legacy_yaml, name='Legacy', description='', only_if_missing=True)[0].data.endswith(b'# Body')


@pytest.mark.asyncio
async def test_check_validates_without_write_session_or_approval(monkeypatch):
    monkeypatch.setattr(cli_knowledge.agent_context, 'resolve_context', AsyncMock(return_value=object()))
    monkeypatch.setattr(cli_knowledge, 'session_scope', lambda **kwargs: pytest.fail('check must not open a write session'))
    scan = AsyncMock()
    monkeypatch.setattr(cli_knowledge, 'require_clean_upload', scan)
    files = [{'path':f.path,'data':base64.b64encode(f.data).decode()} for f in package('Check')]
    result = await cli_knowledge.execute(SimpleNamespace(operation='knowledge.check',capability=object()), {'files':files})
    assert result['status'] == 'succeeded' and result['name'] == 'Check'
    scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_metadata_draft_publication_and_conflict_are_atomic(pg_engine):
    from tests.routes.test_kb_routes import _seed_tenant_and_user
    from vibecanvas_api.routes import kb as routes
    tenant, user = uuid4(), uuid4()
    await _seed_tenant_and_user(pg_engine, tenant, user)
    async with session_scope(tenant_id=str(tenant)) as session:
        resource = await KbRepo(session).create_kb(tenant_id=tenant,user_id=user,name='Before')
        identifier = resource.id
        await replace_package(session,kb_id=identifier,actor_user_id=user,expected_version=1,
            files=package('Before','Old'),increment_version=False,derive_index=False)
        await knowledge_versions.save_snapshot(session,resource,package('After',''),version=0,base_version=1)
        current = await KbRepo(session).get_active(identifier)
        assert current.name == 'Before' and current.description == 'Old'
    with pytest.raises(RuntimeError, match='knowledge_version_conflict'):
        async with session_scope(tenant_id=str(tenant)) as session:
            await replace_package(session,kb_id=identifier,actor_user_id=user,expected_version=2,
                files=package('Wrong'),derive_index=False)
    async with session_scope(tenant_id=str(tenant)) as session:
        assert await knowledge_versions.snapshot_row(session,identifier,0) is not None
        assert package_metadata(await package_snapshot(session,identifier))['name'] == 'Before'
        version,_ = await replace_package(session,kb_id=identifier,actor_user_id=user,expected_version=1,
            files=await knowledge_versions.read_snapshot(session,identifier,0),derive_index=False)
        assert version == 2
        current = await KbRepo(session).get_active(identifier)
        assert current.name == 'After' and current.description == ''
        assert await knowledge_versions.snapshot_row(session,identifier,0) is None
        historical = await knowledge_versions.read_snapshot(session,identifier,1)
        row = await knowledge_versions.snapshot_row(session,identifier,1)
        assert routes._knowledge_snapshot_out(row,historical,2)['name'] == 'Before'
        assert package_metadata(await package_snapshot(session,identifier))['name'] == 'After'
