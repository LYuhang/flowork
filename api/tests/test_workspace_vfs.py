from pathlib import Path

import pytest

from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.user_mount_workspace import mount_scope_id
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity
from vibecanvas_api.services.workspace_vfs import list_workspace_files


@pytest.mark.parametrize('kind,scope,prefix', [
    ('workflow', 'workflow-id', '/'),
    ('project', project_workspace_scope_id('project-id'), '/'),
    ('user_mount', mount_scope_id('user-id'), '/mount/'),
])
def test_listing_maps_authorized_scope_to_live_directory(tmp_path, kind, scope, prefix):
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', kind, 'user-id' if kind == 'user_mount' else scope)
    root = Path(storage.acquire(identity).directory)
    (root / 'result.csv').write_text('a,b\n1,2')
    (root / 'empty').mkdir()
    rows = list_workspace_files(storage, tenant_id='tenant', scope_id=scope,
                                user_id='user-id', prefix=prefix, owned_chats=set())
    by_path = {row.path: row for row in rows}
    assert by_path[prefix + 'result.csv'].content_type == 'table/csv'
    assert by_path[prefix + 'result.csv'].size_bytes == 7
    assert prefix + 'empty/.vibekeep' in by_path


def test_listing_hides_other_chats_and_rejects_other_mount_scope(tmp_path):
    storage = PosixWorkspaceStorage(str(tmp_path))
    root = Path(storage.acquire(WorkspaceIdentity('tenant', 'workflow', 'workflow')).directory)
    for chat in ('own', 'other'):
        (root / 'chats' / chat).mkdir(parents=True)
        (root / 'chats' / chat / 'message').write_text('message')
    rows = list_workspace_files(storage, tenant_id='tenant', scope_id='workflow',
                                user_id='user-id', prefix='/', owned_chats={'own'})
    assert '/chats/own/message' in {row.path for row in rows}
    assert not any('/other' in row.path for row in rows)
    with pytest.raises(ValueError):
        list_workspace_files(storage, tenant_id='tenant', scope_id=mount_scope_id('other-user'),
                             user_id='user-id', prefix='/', owned_chats=set())


def test_bounded_read_uses_current_file_and_mount_namespace(tmp_path):
    from vibecanvas_api.services.workspace_vfs import read_workspace_file
    storage = PosixWorkspaceStorage(str(tmp_path))
    root = Path(storage.acquire(WorkspaceIdentity('tenant', 'user_mount', 'user-id')).directory)
    file = root / 'result.txt'
    file.write_bytes(b'0123456789')
    kwargs = dict(tenant_id='tenant', scope_id=mount_scope_id('user-id'), user_id='user-id',
                  path='/mount/result.txt')
    assert read_workspace_file(storage, **kwargs, max_bytes=4) == (b'0123', 10)
    assert read_workspace_file(storage, **kwargs, max_bytes=0) == (b'', 10)
    file.write_bytes(b'new')
    assert read_workspace_file(storage, **kwargs, max_bytes=4) == (b'new', 3)
    with pytest.raises(ValueError):
        read_workspace_file(storage, **{**kwargs, 'path': '/data/result.txt'}, max_bytes=4)
    with pytest.raises(ValueError):
        read_workspace_file(storage, **{**kwargs, 'path': '/mount/../result.txt'}, max_bytes=4)


@pytest.mark.asyncio
async def test_content_route_reads_resource_owner_storage_after_authorization(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.routes import vfs
    monkeypatch.setattr(vfs.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs.config, 'workspace_storage_root', str(tmp_path))
    storage = PosixWorkspaceStorage(str(tmp_path))
    for tenant, content in [('owner', 'shared-content'), ('viewer', 'wrong-content')]:
        root = Path(storage.acquire(WorkspaceIdentity(tenant, 'workflow', 'workflow')).directory)
        (root / 'data').mkdir()
        (root / 'data/result.txt').write_text(content)
    authorize = AsyncMock()
    monkeypatch.setattr(vfs, '_ensure_vfs_scope_access', authorize)
    monkeypatch.setattr(vfs, 'context_for_auth', lambda *args: SimpleNamespace(admitted_resource_organization_id='owner'))
    result = await vfs.read_vfs(
        request=object(), path='/data/result.txt', wf_id='workflow', run_id=None,
        ctx=SimpleNamespace(user_id='viewer', tenant_id='viewer'), session=object(), authz=object())
    authorize.assert_awaited_once()
    assert result.content == 'shared-content'


@pytest.mark.asyncio
async def test_content_route_rejects_private_chat_before_posix_read(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from fastapi import HTTPException
    from vibecanvas_api.routes import vfs
    from vibecanvas_api.services import workspace_vfs
    monkeypatch.setattr(vfs.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs, '_ensure_vfs_scope_access', AsyncMock())
    monkeypatch.setattr(vfs, 'owned_workspace_chats', AsyncMock(return_value={'mine'}))
    read = Mock(side_effect=AssertionError('must not read private directory'))
    monkeypatch.setattr(workspace_vfs, 'read_workspace_file', read)
    with pytest.raises(HTTPException) as error:
        await vfs.read_vfs(request=object(), path='/chats/other/message.txt', wf_id='workflow', run_id=None,
                           ctx=SimpleNamespace(user_id='viewer', tenant_id='viewer'), session=object(), authz=object())
    assert error.value.status_code == 404
    read.assert_not_called()


@pytest.mark.asyncio
async def test_signed_preview_binds_admitted_owner_not_viewer(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from urllib.parse import parse_qs, urlsplit
    from vibecanvas_api.routes import vfs
    from vibecanvas_api.schemas.vfs import VfsSignIn
    from vibecanvas_api.services.vfs_signing import verify_vfs_sig
    monkeypatch.setattr(vfs, '_ensure_vfs_scope_access', AsyncMock())
    monkeypatch.setattr(vfs, 'context_for_auth', lambda *args: SimpleNamespace(admitted_resource_organization_id='owner'))
    result = await vfs.sign_vfs(
        VfsSignIn(path='/data/plot.png', wf_id='workflow'), request=object(),
        ctx=SimpleNamespace(user_id='viewer', tenant_id='viewer'), session=object(), authz=object())
    args = {key: values[0] for key, values in parse_qs(urlsplit(result.url).query, keep_blank_values=True).items()}
    assert args['tenant'] == 'owner'
    args['exp'] = int(args['exp'])
    assert verify_vfs_sig(**args) == 'owner'
    args['tenant'] = 'viewer'
    assert verify_vfs_sig(**args) is None


@pytest.mark.asyncio
async def test_write_route_uses_posix_without_object_store_or_mirror(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from vibecanvas_api.routes import vfs
    from vibecanvas_api.schemas.vfs import VfsWriteIn
    monkeypatch.setattr(vfs.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(vfs, '_ensure_vfs_scope_access', AsyncMock())
    monkeypatch.setattr(vfs, '_ensure_writable_vfs_scope', AsyncMock())
    monkeypatch.setattr(vfs, 'context_for_auth', lambda *args: SimpleNamespace(admitted_resource_organization_id='owner'))
    monkeypatch.setattr(vfs, 'get_object_store', Mock(side_effect=AssertionError('no blob copy')))
    mirror = AsyncMock(side_effect=AssertionError('no mirror'))
    monkeypatch.setattr(vfs, '_mirror_live_sandbox_write', mirror)
    scope = project_workspace_scope_id('project')
    result = await vfs.write_vfs_content(
        VfsWriteIn(wf_id=scope, path='/data/result.txt', content='written'), request=object(),
        ctx=SimpleNamespace(user_id='viewer', tenant_id='viewer'), session=object(), authz=object())
    assert not result.replaced
    storage = PosixWorkspaceStorage(str(tmp_path))
    assert b''.join(storage.iter_bytes(WorkspaceIdentity('owner', 'project', scope), 'data/result.txt')) == b'written'
    mirror.assert_not_called()


@pytest.mark.asyncio
async def test_rename_delete_routes_modify_only_admitted_posix_resource(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from vibecanvas_api.routes import vfs
    from vibecanvas_api.schemas.vfs import VfsRenameIn
    monkeypatch.setattr(vfs.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(vfs, '_ensure_vfs_scope_access', AsyncMock())
    monkeypatch.setattr(vfs, '_ensure_writable_vfs_scope', AsyncMock())
    monkeypatch.setattr(vfs, 'context_for_auth', lambda *args: SimpleNamespace(admitted_resource_organization_id='owner'))
    monkeypatch.setattr(vfs, 'get_object_store', Mock(side_effect=AssertionError('no blob operation')))
    scope = project_workspace_scope_id('project')
    identity = WorkspaceIdentity('owner', 'project', scope)
    storage = PosixWorkspaceStorage(str(tmp_path))
    storage.write_file(identity, 'data/old', io.BytesIO(b'content'))
    args = dict(request=object(), ctx=SimpleNamespace(user_id='viewer', tenant_id='viewer'), session=object(), authz=object())
    renamed = await vfs.rename_vfs(VfsRenameIn(wf_id=scope, old_path='/data/old', new_path='/data/new'), **args)
    assert renamed.path == '/data/new'
    assert b''.join(storage.iter_bytes(identity, 'data/new')) == b'content'
    deleted = await vfs.delete_vfs(path='/data/new', wf_id=scope, **args)
    assert deleted.deleted == 1
    assert not any(e.path == 'data/new' for e in storage.entries(identity))


@pytest.mark.asyncio
@pytest.mark.parametrize('range_header,expected,status', [(None, b'0123456789', 200), ('bytes=2-5', b'2345', 206), ('bytes=-3', b'789', 206)])
async def test_posix_media_stream_ranges(tmp_path, monkeypatch, range_header, expected, status):
    import io
    from contextlib import asynccontextmanager
    from unittest.mock import Mock
    from vibecanvas_api.routes import vfs
    monkeypatch.setattr(vfs.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs.config, 'workspace_storage_root', str(tmp_path))
    @asynccontextmanager
    async def session_scope(**kwargs):
        assert kwargs['tenant_id'] == 'owner'
        yield object()
    monkeypatch.setattr(vfs, 'session_scope', session_scope)
    monkeypatch.setattr(vfs, 'get_object_store', Mock(side_effect=AssertionError('no object store')))
    storage = PosixWorkspaceStorage(str(tmp_path))
    storage.write_file(WorkspaceIdentity('owner', 'workflow', 'workflow'), 'data/plot.png', io.BytesIO(b'0123456789'))
    response = await vfs._serve_vfs_resource(tenant='owner', wf_id='workflow', run_id='', path='/data/plot.png', range_header=range_header)
    assert response.status_code == status
    assert response.headers['content-type'] == 'image/png'
    assert int(response.headers['content-length']) == len(expected)
    assert b''.join([chunk async for chunk in response.body_iterator]) == expected


@pytest.mark.asyncio
async def test_signed_mount_scope_resolves_registered_full_user_id():
    from unittest.mock import AsyncMock, Mock
    from uuid import uuid4
    from vibecanvas_api.services.workspace_vfs import signed_workspace_scope
    user = uuid4()
    session = Mock()
    session.execute = AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=user)))
    identity, prefix = await signed_workspace_scope(session, tenant_id='organization', scope_id=mount_scope_id(str(user)))
    assert identity == WorkspaceIdentity('organization', 'user_mount', str(user))
    assert prefix == '/mount/'


@pytest.mark.asyncio
async def test_media_response_keeps_open_file_when_path_is_replaced(tmp_path, monkeypatch):
    import io
    from contextlib import asynccontextmanager
    from vibecanvas_api.routes import vfs
    monkeypatch.setattr(vfs.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs.config, 'workspace_storage_root', str(tmp_path))
    @asynccontextmanager
    async def session_scope(**kwargs):
        yield object()
    monkeypatch.setattr(vfs, 'session_scope', session_scope)
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('owner', 'workflow', 'workflow')
    storage.write_file(identity, 'data/result.pdf', io.BytesIO(b'original'))
    response = await vfs._serve_vfs_resource(tenant='owner', wf_id='workflow', run_id='', path='/data/result.pdf')
    storage.write_file(identity, 'data/result.pdf', io.BytesIO(b'new longer version'))
    assert response.headers['content-length'] == '8'
    assert b''.join([chunk async for chunk in response.body_iterator]) == b'original'
    assert not response.resources._exit_callbacks


@pytest.mark.asyncio
async def test_stream_response_closes_resources_when_client_disconnects(monkeypatch):
    from contextlib import ExitStack
    from unittest.mock import Mock
    from vibecanvas_api.routes import vfs
    close = Mock()
    resources = ExitStack()
    resources.callback(close)
    async def disconnect(*args):
        raise OSError('client disconnected')
    monkeypatch.setattr(vfs.StreamingResponse, '__call__', disconnect)
    response = vfs._FileStreamingResponse(iter([b'data']), resources=resources)
    with pytest.raises(OSError, match='client disconnected'):
        await response({}, None, None)
    close.assert_called_once()


@pytest.mark.asyncio
async def test_run_text_reads_resolved_task_directory_with_size_limit(tmp_path, monkeypatch):
    import io
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, Mock
    from vibecanvas_api.routes import vfs
    from vibecanvas_api.services import run_workspace_resolution
    monkeypatch.setattr(vfs.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(vfs.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(vfs, 'VFS_HTTP_MAX_BYTES', 4)
    monkeypatch.setattr(vfs, 'get_object_store', Mock(side_effect=AssertionError('no snapshot read')))
    identity = WorkspaceIdentity('owner', 'task', 'task')
    PosixWorkspaceStorage(str(tmp_path)).write_file(identity, 'result.txt', io.BytesIO(b'0123456789'))
    @asynccontextmanager
    async def authorize(*args):
        yield 'owner'
    monkeypatch.setattr(vfs, '_authorized_run_scope', authorize)
    monkeypatch.setattr(run_workspace_resolution, 'resolve_run_workspace', AsyncMock(return_value=identity))
    result = await vfs.read_vfs(request=object(), path='/run/result.txt', wf_id='', run_id='execution',
                                ctx=object(), session=object(), authz=object())
    assert result.content == '0123'
    assert result.size_bytes == 10
    assert result.truncated
