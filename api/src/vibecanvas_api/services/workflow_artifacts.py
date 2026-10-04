"""Persist execution artifacts before the runtime releases its private /run.

Business inputs, workflow definitions, events and results never use this path.
Only regular files are collected; sandbox-created links cannot redirect a host
read into another execution or into host credentials.
"""

from __future__ import annotations

import asyncio
import os
import stat

from vibecanvas_api.services.file_format import content_type_for
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo


def _files(root: str, excluded_roots: frozenset[str] = frozenset()):
    for directory, _subdirs, filenames, directory_fd in os.fwalk(root, follow_symlinks=False):
        if os.path.normpath(directory) == os.path.normpath(root):
            _subdirs[:] = [name for name in _subdirs if name not in excluded_roots]
            filenames = [name for name in filenames if name not in excluded_roots]
        for name in filenames:
            # O_NOFOLLOW protects the final component; fwalk's directory fd
            # pins the already-open directory across rename/link races.
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                continue  # Shared workspaces may remove a file during enumeration.
            except OSError:
                if stat.S_ISLNK(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                    continue
                raise
            with os.fdopen(fd, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    continue
                relative = os.path.relpath(os.path.join(directory, name), root)
                yield relative, file.read()


async def persist_workflow_artifacts(*, root: str, tenant_id: str, execution_id: str, wf_id: str,
                                     excluded_roots: frozenset[str] = frozenset()) -> None:
    # One file per transaction keeps DB locks short. Errors propagate: a caller
    # must not publish a successful result with missing durable file references.
    iterator = _files(root, excluded_roots)
    sentinel = object()
    try:
        while True:
            item = await asyncio.to_thread(next, iterator, sentinel)
            if item is sentinel:
                break
            relative, data = item
            async with short_session_scope(tenant_id=tenant_id) as session:
                await VfsRunRepo(session, get_object_store(), tenant_id).write_bytes(
                    run_id=execution_id,
                    path="/run/" + relative.replace(os.sep, "/"),
                    data=data,
                    content_type=content_type_for(relative, data),
                    wf_id=wf_id,
                )
    finally:
        iterator.close()


async def restore_workflow_artifacts(*, root, tenant_id: str, execution_id: str) -> None:
    """Hydrate a new, unused slot before invoking any untrusted workflow code.

    The caller must authorize the historical execution before entering here.
    O_EXCL and O_NOFOLLOW also reject collisions and links; an incomplete copy
    fails admission instead of running with missing artifacts.
    """
    from pathlib import Path, PurePosixPath

    root = Path(root)
    async with short_session_scope(tenant_id=tenant_id) as session:
        files = await VfsRunRepo(session, get_object_store(), tenant_id).ls(run_id=execution_id)
    for entry in files:
        path = PurePosixPath(entry.path)
        if not entry.path.startswith("/run/") or ".." in path.parts or "\x00" in entry.path:
            raise ValueError("invalid_workflow_artifact_path")
        relative = path.relative_to("/run")
        parent = root
        for part in relative.parts[:-1]:
            parent = parent / part
            parent.mkdir(exist_ok=True, mode=0o700)
            if parent.is_symlink():
                raise ValueError("invalid_workflow_artifact_link")
        async with short_session_scope(tenant_id=tenant_id) as session:
            data = await VfsRunRepo(session, get_object_store(), tenant_id).read_bytes(
                run_id=execution_id, path=entry.path)
        fd = os.open(root / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
