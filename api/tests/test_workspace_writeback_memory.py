"""Workspace writeback must not retain the whole dataset in memory."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import tracemalloc
from types import SimpleNamespace

import pytest
from vibecanvas_api.services.sandbox import manager
from vibecanvas_api.services import workspace_files


@pytest.mark.asyncio
async def test_hydration_bounds_payload_memory_and_restores_empty_directories(tmp_path, monkeypatch):
    size = 2 * 1024 * 1024

    @asynccontextmanager
    async def session_scope(*, tenant_id):
        assert tenant_id == 'tenant'
        yield object()

    class Repo:
        def __init__(self, *args, **kwargs):
            pass

        async def ls(self, *, wf_id, prefix):
            assert wf_id == 'project'
            if prefix != '/data/':
                return []
            return [SimpleNamespace(path=f'/data/{i}.bin') for i in range(12)] + [
                SimpleNamespace(path='/data/empty/.vibekeep'),
            ]

        async def read_bytes(self, *, wf_id, path):
            return b'' if path.endswith('.vibekeep') else b'x' * size

    monkeypatch.setattr(workspace_files, 'session_scope', session_scope)
    monkeypatch.setattr(workspace_files, 'VfsRepo', Repo)
    monkeypatch.setattr(workspace_files, 'get_object_store', lambda: None)
    tracemalloc.start()
    try:
        assert await workspace_files.hydrate_workspace_files(str(tmp_path), 'project', 'tenant') == 13
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, peak
    expected = hashlib.sha256(b'x' * size).hexdigest()
    for i in range(12):
        assert hashlib.sha256((tmp_path / f'data/{i}.bin').read_bytes()).hexdigest() == expected
    assert (tmp_path / 'data/empty/.vibekeep').read_bytes() == b''


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_bytes', 'write_error'])
async def test_hydration_propagates_file_failures_without_reading_later_files(tmp_path, monkeypatch, failure):
    reads = []

    @asynccontextmanager
    async def session_scope(**kwargs):
        yield object()

    class Repo:
        def __init__(self, *args, **kwargs):
            pass

        async def ls(self, **kwargs):
            return [SimpleNamespace(path=f'/data/{name}') for name in ['first', 'blocked/result', 'last']]

        async def read_bytes(self, *, path, **kwargs):
            reads.append(path)
            return None if failure == 'missing_bytes' and path.endswith('result') else b'contents'

    monkeypatch.setattr(workspace_files, 'session_scope', session_scope)
    monkeypatch.setattr(workspace_files, 'VfsRepo', Repo)
    monkeypatch.setattr(workspace_files, 'get_object_store', lambda: None)
    if failure == 'write_error':
        (tmp_path / 'data').mkdir()
        (tmp_path / 'data/blocked').write_text('not a directory')
    with pytest.raises(RuntimeError if failure == 'missing_bytes' else OSError):
        await workspace_files.hydrate_workspace_files(str(tmp_path), 'project', 'tenant')
    assert reads == ['/data/first', '/data/blocked/result']
    assert (tmp_path / 'data/first').read_bytes() == b'contents'
    assert not (tmp_path / 'data/last').exists()


@pytest.mark.asyncio
async def test_writeback_bounds_payload_memory_and_preserves_artifacts(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir()
    payload = b'x' * (2 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    for i in range(12):
        (root / f'{i}.bin').write_bytes(payload)
    del payload
    (root / 'empty').mkdir()
    (root / 'fenced.bin').write_bytes(b'keep external revision')
    written = {}

    @asynccontextmanager
    async def session_scope(**kwargs):
        yield object()

    class Repo:
        def __init__(self, *args, **kwargs):
            pass

        async def upsert_artifact_bytes(self, *, path, data, **kwargs):
            written[path] = (len(data), hashlib.sha256(data).hexdigest())
            await asyncio.sleep(0)

    monkeypatch.setattr(manager, 'short_session_scope', session_scope)
    monkeypatch.setattr(manager, 'VfsRepo', Repo)
    monkeypatch.setattr(manager, 'get_object_store', lambda: None)
    sandbox = manager.SandboxSession.__new__(manager.SandboxSession)
    sandbox.run_dir = str(tmp_path)
    sandbox.wf_id = 'test'
    sandbox.tenant_id = 'test'
    sandbox._external_vfs_lock = asyncio.Lock()
    sandbox._external_vfs_fenced_paths = {'/data/fenced.bin'}
    tracemalloc.start()
    try:
        assert await sandbox._sync_run_folder('data') == 13
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, peak
    assert '/data/fenced.bin' not in written
    for i in range(12):
        assert written[f'/data/{i}.bin'] == (2 * 1024 * 1024, digest)
    assert written[f'/data/empty/{workspace_files.DIR_KEEP_SENTINEL}'][0] == 0


@pytest.mark.asyncio
async def test_persistent_file_acknowledgement_streams_without_object_write(tmp_path, monkeypatch):
    from pathlib import Path
    from unittest.mock import Mock
    from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity

    monkeypatch.setattr(manager.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(manager, 'VfsRepo', Mock(side_effect=AssertionError('no object write')))
    storage = PosixWorkspaceStorage(str(tmp_path))
    binding = storage.acquire(WorkspaceIdentity('tenant', 'project', 'project'))
    root = Path(binding.directory)
    (root / 'data').mkdir()
    payload = b'x' * (1024 * 1024)
    digest = hashlib.sha256()
    with (root / 'data/file.bin').open('wb') as handle:
        for _ in range(24):
            handle.write(payload)
            digest.update(payload)
    sandbox = manager.SandboxSession.__new__(manager.SandboxSession)
    sandbox.closed = False
    sandbox.run_dir = binding.directory
    sandbox.workspace_folders = ('data',)
    sandbox.persistent_workspace_binding = binding
    tracemalloc.start()
    try:
        assert await sandbox.sync_workspace_path('/data/file.bin', expected_bytes=24 * 1024 * 1024,
                                                 expected_sha256=digest.hexdigest())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5 * 1024 * 1024
    assert not await sandbox.sync_workspace_path('/data/file.bin', expected_bytes=1)
    assert not await sandbox.sync_workspace_path('/data/file.bin', expected_sha256='wrong')
    assert not await sandbox.sync_workspace_path('/data/missing.bin')
    (root / 'data/link.bin').symlink_to('/etc/passwd')
    assert not await sandbox.sync_workspace_path('/data/link.bin')
