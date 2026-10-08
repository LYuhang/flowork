"""File materialization for object-backed Project workspaces.

POSIX workspaces are mounted directly and do not use hydration. Session locks,
writeback fencing and runtime lifecycle remain owned by the sandbox manager.
"""
from __future__ import annotations

import asyncio
import os

import structlog

from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.vfs_store import VfsRepo

logger = structlog.get_logger(__name__)
WORKSPACE_FOLDERS = ("data", "memory", "logs", "chats")
DIR_KEEP_SENTINEL = ".vibekeep"


def collect_workspace_files(root: str) -> list[tuple[str, str | None]]:
    """Collect paths, including empty leaf directories, without loading bytes."""
    paths: list[tuple[str, str | None]] = []
    for current, directories, files in os.walk(root):
        for name in files:
            source = os.path.join(current, name)
            paths.append((os.path.relpath(source, root), source))
        if current != root and not directories and not files:
            relative = os.path.relpath(current, root)
            paths.append((os.path.join(relative, DIR_KEEP_SENTINEL), None))
    return paths


def _write_file(destination: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(data)


async def hydrate_workspace_files(run_dir: str, wf_id: str, tenant_id: str) -> int:
    """Restore durable workspace files, retaining only one file payload at a time.

    Reads remain on the database event loop; filesystem writes run off-loop.
    Missing bytes or failed writes propagate so the caller cannot start a
    partially restored workspace and overwrite durable data on writeback.
    Empty-directory sentinel files are restored like ordinary zero-byte files.
    """
    written = 0
    for folder in WORKSPACE_FOLDERS:
        prefix = f"/{folder}/"
        root = os.path.realpath(os.path.join(run_dir, folder))
        try:
            async with session_scope(tenant_id=tenant_id) as session:
                repo = VfsRepo(session, object_store=get_object_store())
                entries = await repo.ls(wf_id=wf_id, prefix=prefix)
                for entry in entries:
                    if not entry.path.startswith(prefix):
                        continue
                    relative = entry.path[len(prefix):]
                    parts = relative.split("/")
                    destination = os.path.join(root, *parts)
                    if (
                        not relative
                        or any(part in {"", ".", ".."} for part in parts)
                        or os.path.commonpath([root, os.path.realpath(destination)]) != root
                    ):
                        logger.warning(
                            "agent_hydrate_unsafe_path_skipped",
                            wf_id=wf_id, folder=folder, path=entry.path,
                        )
                        continue
                    data = await repo.read_bytes(wf_id=wf_id, path=entry.path)
                    if data is None:
                        raise RuntimeError("workspace_artifact_unavailable")
                    try:
                        await asyncio.to_thread(_write_file, destination, data)
                    except OSError:
                        logger.warning(
                            "agent_hydrate_file_write_failed",
                            wf_id=wf_id, folder=folder, dest=destination,
                            exc_info=True,
                        )
                        raise
                    del data
                    written += 1
        except Exception:
            logger.warning(
                "agent_hydrate_folder_failed",
                wf_id=wf_id, folder=folder, exc_info=True,
            )
            raise
    return written
