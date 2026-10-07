import asyncio
import io

import pytest

from vibecanvas_api.services.file_revision import vfs_row_revision
from vibecanvas_api.services.preview_workspace_events import workspace_changes
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity
from vibecanvas_api.services.workspace_vfs import workspace_file_metadata


@pytest.mark.asyncio
async def test_posix_preview_changes_deletion_recreation_and_revocation(tmp_path):
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('preview-test', 'project', 'owned')
    storage.acquire(identity)
    storage.write_file(identity, 'data/test.md', io.BytesIO(b'before'))
    initial = vfs_row_revision(workspace_file_metadata(storage, identity, 'data/test.md'))
    allowed = True

    async def authorized():
        return allowed

    changes = workspace_changes(storage, identity, 'data/test.md', initial,
                                authorized, interval=0.01, heartbeat_interval=0.05)
    try:
        assert await asyncio.wait_for(anext(changes), 1) == ('heartbeat', initial)
        # A direct storage write stands in for sandbox tools, without VFS DB rows.
        storage.write_file(identity, 'data/test.md', io.BytesIO(b'after'))
        kind, updated = await asyncio.wait_for(anext(changes), 1)
        assert kind == 'changed' and updated != initial
        storage.remove_path(identity, 'data/test.md')
        assert await asyncio.wait_for(anext(changes), 1) == ('changed', None)
        assert await asyncio.wait_for(anext(changes), 1) == ('heartbeat', None)
        storage.write_file(identity, 'data/test.md', io.BytesIO(b'recreated'))
        kind, recreated = await asyncio.wait_for(anext(changes), 1)
        assert kind == 'changed' and recreated not in (None, initial, updated)
        allowed = False
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(changes), 1)
    finally:
        await changes.aclose()


@pytest.mark.asyncio
async def test_posix_preview_cancellation_releases_wait(tmp_path):
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('preview-test', 'project', 'owned')

    async def authorized():
        raise AssertionError('cancelled observer must not read storage')

    changes = workspace_changes(storage, identity, 'missing', None, authorized)
    pending = asyncio.create_task(anext(changes))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await changes.aclose()
