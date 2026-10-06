from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from vibecanvas_api.routes import chats
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/chats/chat/attachments/file.txt', '/data/interactive-result.json'])
async def test_chat_file_is_immediately_in_project_workspace(tmp_path, monkeypatch, path):
    monkeypatch.setattr(chats.app_config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(chats.app_config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(chats, 'get_object_store', Mock(side_effect=AssertionError('no object copy')))
    scope = project_workspace_scope_id('project')
    kwargs = dict(session=object(), auth=SimpleNamespace(tenant_id='tenant', user_id='user'), scope=scope,
                  path=path, data=b'uploaded', content_type='text/plain')
    assert not await chats._persist_chat_file(**kwargs)
    identity = WorkspaceIdentity('tenant', 'project', scope)
    storage = PosixWorkspaceStorage(str(tmp_path))
    assert b''.join(storage.iter_bytes(identity, path.lstrip('/'))) == b'uploaded'
    assert await chats._persist_chat_file(**{**kwargs, 'data': b'updated'})
    assert b''.join(storage.iter_bytes(identity, path.lstrip('/'))) == b'updated'


@pytest.mark.asyncio
async def test_html_source_reads_live_file_and_rejects_oversized_source(tmp_path, monkeypatch):
    monkeypatch.setattr(chats.app_config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(chats.app_config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(chats, 'get_object_store', Mock(side_effect=AssertionError('no object copy')))
    scope = project_workspace_scope_id('project')
    storage = PosixWorkspaceStorage(str(tmp_path))
    root = Path(storage.acquire(WorkspaceIdentity('tenant', 'project', scope)).directory)
    (root / 'data').mkdir()
    target = root / 'data/page.html'
    kwargs = dict(session=object(), auth=SimpleNamespace(tenant_id='tenant', user_id='user'),
                  scope=scope, path='/data/page.html')
    assert await chats._read_chat_html_source(**kwargs) is None
    target.write_bytes(b'<img src="image.png">')
    assert await chats._read_chat_html_source(**kwargs) == target.read_bytes()
    with target.open('wb') as stream:
        stream.truncate(2 * 1024 * 1024 + 1)
    assert await chats._read_chat_html_source(**kwargs) is None
