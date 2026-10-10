import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from vibecanvas_api.routes import previews
from vibecanvas_api.services import chat_branch_files
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity


def arguments(attachments):
    return dict(repo=SimpleNamespace(list_message_page=AsyncMock(return_value=(
        [{'content': {'attachments': attachments}}], 1, 0))),
        source=SimpleNamespace(chat_id='chat', project_id='project'), turn_id='turn',
        request=object(), auth=SimpleNamespace(tenant_id='tenant', user_id='user'),
        session=Mock(), service=object())


@pytest.mark.asyncio
async def test_branch_checks_live_files_without_loading_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(previews.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(previews.config, 'workspace_storage_root', str(tmp_path))
    authorize = AsyncMock()
    monkeypatch.setattr(previews, '_authorize_file_ref', authorize)
    monkeypatch.setattr(chat_branch_files, 'get_object_store', Mock(side_effect=AssertionError('no object read')))
    storage = PosixWorkspaceStorage(str(tmp_path))
    storage.write_file(WorkspaceIdentity('tenant', 'user_mount', 'user'), 'present.txt', io.BytesIO(b'present'))
    present = {'type': 'file', 'name': 'Present', 'path': '/mount/present.txt'}
    missing = {'type': 'image', 'name': 'Missing', 'path': '/mount/missing.png'}
    result = await chat_branch_files.unavailable_branch_files(**arguments([present, missing, missing]))
    assert result == ['Missing']
    assert authorize.await_count == 2


@pytest.mark.asyncio
async def test_denied_reference_is_not_opened_and_service_errors_are_not_missing(monkeypatch):
    authorize = AsyncMock(side_effect=HTTPException(403))
    resolve = AsyncMock()
    monkeypatch.setattr(previews, '_authorize_file_ref', authorize)
    monkeypatch.setattr(previews, '_resolve_file', resolve)
    item = {'type': 'quote', 'label': 'Quoted file', 'source': {'kind': 'file',
        'file_ref': {'schemaVersion': 1, 'scope': 'mount', 'path': '/mount/private.txt'}}}
    assert await chat_branch_files.unavailable_branch_files(**arguments([item])) == ['Quoted file']
    resolve.assert_not_called()
    authorize.side_effect = HTTPException(503)
    with pytest.raises(HTTPException) as error:
        await chat_branch_files.unavailable_branch_files(**arguments([item]))
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_object_metadata_without_actual_object_is_unavailable(monkeypatch):
    monkeypatch.setattr(previews, '_authorize_file_ref', AsyncMock())
    monkeypatch.setattr(previews, '_resolve_file', AsyncMock(return_value=SimpleNamespace(
        workspace_identity=None, row=SimpleNamespace(object_key='private-object'))))
    store = Mock(revision=Mock(side_effect=KeyError('missing')))
    monkeypatch.setattr(chat_branch_files, 'get_object_store', lambda: store)
    item = {'type': 'file', 'name': 'Missing object', 'path': '/data/file.txt'}
    assert await chat_branch_files.unavailable_branch_files(**arguments([item])) == ['Missing object']
    store.revision.assert_called_once_with('private-object')
    store.fetch_bytes.assert_not_called()
