from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import io

import pytest
from vibecanvas_api.routes import storage as route
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity


@pytest.mark.asyncio
async def test_storage_read_uses_live_owner_file(tmp_path, monkeypatch):
    monkeypatch.setattr(route.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(route.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(route, '_authorize_logical_path', AsyncMock(return_value=None))
    monkeypatch.setattr(route, 'context_for_auth', lambda *args: SimpleNamespace(admitted_resource_organization_id='owner'))
    scope = project_workspace_scope_id('project')
    monkeypatch.setattr(route, '_resolve_path', AsyncMock(return_value=SimpleNamespace(
        root='project', scope_id=scope, vfs_path='/data/file.txt')))
    monkeypatch.setattr(route, 'get_object_store', Mock(side_effect=AssertionError('no object read')))
    PosixWorkspaceStorage(str(tmp_path)).write_file(WorkspaceIdentity('owner', 'project', scope), 'data/file.txt', io.BytesIO(b'new content'))
    result = await route.read_storage_content(request=object(), path='/project/project/data/file.txt',
        auth=SimpleNamespace(tenant_id='viewer', user_id='viewer'), session=object(), service=object())
    assert result.content == 'new content'
    assert result.size_bytes == 11


@pytest.mark.asyncio
async def test_storage_upload_persistence_is_visible_without_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(route.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(route.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(route, 'context_for_auth', lambda *args: SimpleNamespace(admitted_resource_organization_id='owner'))
    monkeypatch.setattr(route, 'get_object_store', Mock(side_effect=AssertionError('no copy')))
    monkeypatch.setattr(route, 'get_sandbox_manager', Mock(side_effect=AssertionError('no mirror')))
    scope = project_workspace_scope_id('project')
    result = await route._persist_storage_file(request=object(), auth=SimpleNamespace(tenant_id='viewer', user_id='viewer'),
        session=object(), scope_id=scope, path='/data/file.txt', data=b'uploaded', content_type='text/plain')
    assert not result
    assert b''.join(PosixWorkspaceStorage(str(tmp_path)).iter_bytes(WorkspaceIdentity('owner', 'project', scope), 'data/file.txt')) == b'uploaded'


@pytest.mark.asyncio
async def test_storage_rename_delete_and_readonly_boundary(tmp_path, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(route.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(route.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(route, '_authorize_logical_path', AsyncMock(return_value=None))
    monkeypatch.setattr(route, 'context_for_auth', lambda *args: SimpleNamespace(admitted_resource_organization_id='owner'))
    monkeypatch.setattr(route, 'get_object_store', Mock(side_effect=AssertionError('no object mutation')))
    monkeypatch.setattr(route, 'get_sandbox_manager', Mock(side_effect=AssertionError('no mirror')))
    scope = project_workspace_scope_id('project')
    identity = WorkspaceIdentity('owner', 'project', scope)
    storage = PosixWorkspaceStorage(str(tmp_path))
    storage.write_file(identity, 'data/old', io.BytesIO(b'content'))
    old = SimpleNamespace(writable=True, scope_id=scope, vfs_path='/data/old')
    new = SimpleNamespace(writable=True, scope_id=scope, vfs_path='/data/new')
    resolve = AsyncMock(side_effect=[old, new])
    monkeypatch.setattr(route, '_resolve_path', resolve)
    args = dict(request=object(), auth=SimpleNamespace(tenant_id='viewer', user_id='viewer'), session=object(), service=object())
    result = await route.rename_storage(route.StorageRenameIn(old_path='/project/project/data/old', new_path='/project/project/data/new'), **args)
    assert result.path.endswith('/new')
    assert b''.join(storage.iter_bytes(identity, 'data/new')) == b'content'
    monkeypatch.setattr(route, '_resolve_path', AsyncMock(return_value=new))
    new.writable = False
    with pytest.raises(HTTPException) as error:
        await route.delete_storage(path='/project/project/data/new', **args)
    assert error.value.status_code == 403
    assert b''.join(storage.iter_bytes(identity, 'data/new')) == b'content'
    new.writable = True
    deleted = await route.delete_storage(path='/project/project/data/new', **args)
    assert deleted.deleted == 1
