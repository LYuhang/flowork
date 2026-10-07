"""Observe POSIX Preview revisions without copying content or indexing writes."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from vibecanvas_api.services.file_revision import vfs_row_revision
from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity
from vibecanvas_api.services.workspace_vfs import workspace_file_metadata


async def workspace_changes(
    storage: PosixWorkspaceStorage,
    identity: WorkspaceIdentity,
    relative_path: str,
    revision: str | None,
    authorized: Callable[[], Awaitable[bool]],
    *,
    interval: float = 2.0,
    heartbeat_interval: float = 15.0,
) -> AsyncIterator[tuple[str, str | None]]:
    """Yield current revisions on change; None represents a deleted file.

    The caller sends an authoritative snapshot on every connection. File changes
    may coalesce between checks; Preview needs current content, not an audit log.
    Metadata reads use the storage adapter's normal path/symlink restrictions.
    """
    last_frame = asyncio.get_running_loop().time()
    while True:
        await asyncio.sleep(interval)
        if not await authorized():
            return
        try:
            metadata = await asyncio.to_thread(
                workspace_file_metadata, storage, identity, relative_path,
            )
            current = vfs_row_revision(metadata)
        except (FileNotFoundError, NotADirectoryError):
            current = None
        if current != revision:
            revision = current
            last_frame = asyncio.get_running_loop().time()
            yield "changed", current
        elif asyncio.get_running_loop().time() - last_frame >= heartbeat_interval:
            last_frame = asyncio.get_running_loop().time()
            yield "heartbeat", current
