import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from vibecanvas_api.routes import previews
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity


@pytest.mark.asyncio
async def test_preview_reads_live_mount_file_and_rejects_stale_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(previews.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(previews.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(previews, 'get_object_store', Mock(side_effect=AssertionError('must read filesystem')))
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', 'user_mount', 'user')
    storage.write_file(identity, 'note.md', io.BytesIO(b'# Original'))
    ref = previews.MountFileRefV1(schema_version=1, scope='mount', path='/mount/note.md')
    resolved = await previews._resolve_file(file_ref=ref, auth=SimpleNamespace(tenant_id='tenant', user_id='user'), session=Mock())
    assert previews._source_prefix(resolved, 5) == b'# Ori'
    assert resolved.row.size_bytes == 10
    storage.write_file(identity, 'note.md', io.BytesIO(b'# Updated'))
    with pytest.raises(HTTPException) as error:
        previews._source_prefix(resolved, 100)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_project_preview_requires_owned_project_before_opening_files(monkeypatch):
    monkeypatch.setattr(previews.config, 'workspace_storage_backend', 'posix')
    session = Mock()
    session.execute = AsyncMock(return_value=Mock(one_or_none=Mock(return_value=None)))
    ref = previews.ProjectFileRefV1(schema_version=1, scope='project', project_id='project', path='/data/file.txt')
    with pytest.raises(HTTPException) as error:
        await previews._resolve_file(file_ref=ref, auth=SimpleNamespace(tenant_id='tenant', user_id='user'), session=session)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_preview_save_updates_file_and_rejects_old_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(previews.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(previews.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(previews, '_authorize_file_ref', AsyncMock())
    monkeypatch.setattr(previews, 'get_object_store', Mock(side_effect=AssertionError('no object copy')))
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', 'user_mount', 'user')
    storage.write_file(identity, 'note.txt', io.BytesIO(b'original\r\n'))
    auth = SimpleNamespace(tenant_id='tenant', user_id='user')
    session = Mock(execute=AsyncMock(), commit=AsyncMock())
    ref = previews.MountFileRefV1(schema_version=1, scope='mount', path='/mount/note.txt')
    resolved = await previews._resolve_file(file_ref=ref, auth=auth, session=session)
    revision = previews.vfs_row_revision(resolved.row)
    body = previews.PreviewFileWriteV1(fileRef=ref, expectedRevision=revision, contentType='text/plain', content='updated\n')
    output = await previews.write_preview_file(body, request=object(), auth=auth, session=session, service=object())
    assert output.revision != revision
    assert b''.join(storage.iter_bytes(identity, 'note.txt')) == b'updated\r\n'
    with pytest.raises(HTTPException) as error:
        await previews.write_preview_file(body, request=object(), auth=auth, session=session, service=object())
    assert error.value.status_code == 409
