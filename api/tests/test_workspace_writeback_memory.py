"""Workspace writeback must not retain the whole dataset in memory."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import tracemalloc

import pytest
from vibecanvas_api.services.sandbox import manager


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
    assert written[f'/data/empty/{manager.DIR_KEEP_SENTINEL}'][0] == 0


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
