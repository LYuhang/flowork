"""Materialize and persist the user-level ``/mount`` workspace.

``/mount`` is the single durable, cross-chat and cross-workflow filesystem
surface.  Sandboxes need a real host directory to bind at ``/mount``; this
module owns the translation between that directory and the durable VFS rows.
It deliberately knows nothing about Chat, Workflow, or a concrete sandbox
provider so every execution path can share the same lifecycle.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import overload

import structlog

from vibecanvas_api.config import config
from vibecanvas_api.security.upload_scanner import scan_upload
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.vfs_run_context import _guess_ct
from vibecanvas_api.storage.db import session_scope, short_session_scope
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.vfs_store import PostgresVfsStore, VfsRepo

_MOUNT_PREFIX = "/mount/"
_DIR_KEEP_SENTINEL = ".vibekeep"
logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _MirrorFileState:
    host_mtime_ns: int
    host_size: int
    vfs_revision: str


@dataclass(slots=True)
class _MirrorRegistration:
    tenant_id: str
    user_id: str
    directory: str
    files: dict[str, _MirrorFileState] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _canonical_uuid(value: str, *, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid {label}") from exc


def _host_mount_directory(*, user_id: str) -> str | None:
    """Resolve one stable user directory below the configured root.

    The user never supplies this path. UUID-only child names are intentionally
    stable across profile/username renames and cannot contain traversal.
    """
    root = config.storage.mount_path
    if root is None:
        return None
    canonical_user_id = _canonical_uuid(user_id, label="user_id")
    users_root = root / "users"
    if users_root.exists() and users_root.is_symlink():
        raise ValueError("MOUNT_PATH/users must not be a symbolic link")
    users_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(users_root, 0o700)
    directory = users_root / canonical_user_id
    if directory.exists() and directory.is_symlink():
        raise ValueError("user host mount must not be a symbolic link")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    resolved = directory.resolve(strict=True)
    root_resolved = root.resolve(strict=True)
    if root_resolved not in resolved.parents:
        raise ValueError("user host mount escapes MOUNT_PATH")
    return str(resolved)


def _collect_host_file_meta(source: str) -> dict[str, os.stat_result]:
    """Collect regular files without following a single symbolic link."""
    result: dict[str, os.stat_result] = {}
    for root, directories, files in os.walk(source, followlinks=False):
        safe_directories = []
        for name in directories:
            path = os.path.join(root, name)
            if os.path.islink(path):
                logger.warning("host_mount_symlink_skipped", entry_type="directory")
                continue
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                logger.warning("host_mount_symlink_skipped", entry_type="file")
                continue
            try:
                stat = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not os.path.isfile(path):
                continue
            relative = os.path.relpath(path, source).replace(os.sep, "/")
            try:
                from vibecanvas_api.storage.vfs_store import _validate_artifact_path

                _validate_artifact_path(_MOUNT_PREFIX + relative)
            except ValueError:
                logger.warning("host_mount_invalid_path_skipped")
                continue
            result[relative] = stat
    return result


def _write_host_file_atomic(directory: str, relative: str, data: bytes) -> None:
    target = Path(directory).joinpath(*relative.split("/"))
    root = Path(directory).resolve(strict=True)
    parent = target.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    if resolved_parent != root and root not in resolved_parent.parents:
        raise ValueError("host mount write escapes user directory")
    fd, temporary = tempfile.mkstemp(prefix=".vcmount-", dir=resolved_parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class HostMountBridge:
    """Near-real-time, conflict-aware host directory ↔ encrypted VFS mirror."""

    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], _MirrorRegistration] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return config.storage.mount_path is not None

    def register(self, *, tenant_id: str, user_id: str) -> str | None:
        directory = _host_mount_directory(user_id=user_id)
        if directory is None:
            return None
        tenant = _canonical_uuid(tenant_id, label="tenant_id")
        user = _canonical_uuid(user_id, label="user_id")
        self._registrations.setdefault(
            (tenant, user),
            _MirrorRegistration(tenant_id=tenant, user_id=user, directory=directory),
        )
        return directory

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="host-mount-bridge")

    async def shutdown(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            for registration in list(self._registrations.values()):
                try:
                    await self.sync(registration)
                except Exception as exc:
                    # Keep logs actionable without serializing paths, file
                    # names, database messages, or decrypted content. The
                    # exception body remains redacted by the global logger.
                    logger.exception(
                        "host_mount_sync_failed",
                        tenant_id=registration.tenant_id,
                        user_id=registration.user_id,
                        error_type=type(exc).__name__,
                    )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=config.storage.mount_sync_interval_seconds,
                )
            except TimeoutError:
                pass

    async def sync(self, registration: _MirrorRegistration) -> int:
        async with registration.lock:
            return await self._sync_locked(registration)

    async def sync_user(self, *, tenant_id: str, user_id: str) -> int:
        directory = self.register(tenant_id=tenant_id, user_id=user_id)
        if directory is None:
            return 0
        registration = self._registrations[
            (_canonical_uuid(tenant_id, label="tenant_id"), _canonical_uuid(user_id, label="user_id"))
        ]
        return await self.sync(registration)

    async def unregister_user(self, *, user_id: str) -> tuple[str, ...]:
        """Stop every mirror for a user before permanent account erasure.

        Removing registrations first prevents the periodic loop from starting a
        new sync. Acquiring each lock then waits for any already-running sync to
        leave its transaction before the purge deletes VFS and host state.
        """
        user = _canonical_uuid(user_id, label="user_id")
        registrations: list[_MirrorRegistration] = []
        for key, registration in list(self._registrations.items()):
            if key[1] != user:
                continue
            self._registrations.pop(key, None)
            registrations.append(registration)
        for registration in registrations:
            async with registration.lock:
                pass
        return tuple(registration.directory for registration in registrations)

    async def _sync_locked(self, registration: _MirrorRegistration) -> int:
        scope_id = mount_scope_id(registration.user_id)
        if scope_id is None:
            return 0
        host_meta = await asyncio.to_thread(
            _collect_host_file_meta, registration.directory
        )
        changed = 0
        async with short_session_scope(tenant_id=registration.tenant_id) as session:
            repo = VfsRepo(session, object_store=get_object_store())
            vfs_entries = {
                entry.path[len(_MOUNT_PREFIX):]: entry
                for entry in await repo.ls_meta(
                    wf_id=scope_id,
                    prefix=_MOUNT_PREFIX,
                )
                if entry.kind == "artifact" and entry.path.startswith(_MOUNT_PREFIX)
            }
            all_paths = set(host_meta) | set(vfs_entries) | set(registration.files)
            for relative in sorted(all_paths):
                host = host_meta.get(relative)
                vfs = vfs_entries.get(relative)
                previous = registration.files.get(relative)
                host_changed = host is not None and (
                    previous is None
                    or host.st_mtime_ns != previous.host_mtime_ns
                    or host.st_size != previous.host_size
                )
                vfs_changed = vfs is not None and (
                    previous is None or vfs.content_revision != previous.vfs_revision
                )

                if host is not None and host.st_size > config.storage.vfs_upload_max_bytes:
                    logger.warning("host_mount_file_too_large_skipped", size_bytes=host.st_size)
                    continue

                if host is not None and vfs is None:
                    # First discovery or a deliberate host recreation wins. If
                    # only VFS was deleted since the prior snapshot, propagate
                    # that deletion back to the unchanged host file.
                    if previous is not None and not host_changed:
                        try:
                            os.remove(os.path.join(registration.directory, *relative.split("/")))
                        except FileNotFoundError:
                            pass
                    else:
                        data = await asyncio.to_thread(
                            Path(registration.directory, *relative.split("/")).read_bytes
                        )
                        await scan_upload(data)
                        await repo.upsert_artifact_bytes(
                            wf_id=scope_id,
                            tenant=registration.tenant_id,
                            path=_MOUNT_PREFIX + relative,
                            data=data,
                            content_type=_guess_ct(relative, data),
                        )
                    changed += 1
                    continue

                if host is None and vfs is not None:
                    # Existing host disappearance is a deletion. On the first
                    # registration, hydrate the already-encrypted VFS instead.
                    if previous is not None and not vfs_changed:
                        await repo.delete_artifact(
                            wf_id=scope_id,
                            tenant=registration.tenant_id,
                            path=_MOUNT_PREFIX + relative,
                        )
                    else:
                        data = await repo.read_bytes(
                            wf_id=scope_id,
                            path=_MOUNT_PREFIX + relative,
                        )
                        if data is not None:
                            await asyncio.to_thread(
                                _write_host_file_atomic,
                                registration.directory,
                                relative,
                                data,
                            )
                    changed += 1
                    continue

                if host is None or vfs is None:
                    registration.files.pop(relative, None)
                    continue
                if host_changed:
                    # Host wins a true simultaneous conflict because setting
                    # MOUNT_PATH is an explicit operator-controlled bridge.
                    data = await asyncio.to_thread(
                        Path(registration.directory, *relative.split("/")).read_bytes
                    )
                    await scan_upload(data)
                    await repo.upsert_artifact_bytes(
                        wf_id=scope_id,
                        tenant=registration.tenant_id,
                        path=_MOUNT_PREFIX + relative,
                        data=data,
                        content_type=_guess_ct(relative, data),
                    )
                    changed += 1
                elif vfs_changed:
                    data = await repo.read_bytes(
                        wf_id=scope_id,
                        path=_MOUNT_PREFIX + relative,
                    )
                    if data is not None:
                        await asyncio.to_thread(
                            _write_host_file_atomic,
                            registration.directory,
                            relative,
                            data,
                        )
                        changed += 1

        # Re-read both sides after the transaction so stored revisions and
        # atomic-write mtimes become the next loop's exact baseline.
        host_meta = await asyncio.to_thread(
            _collect_host_file_meta, registration.directory
        )
        async with short_session_scope(tenant_id=registration.tenant_id) as session:
            entries = await VfsRepo(
                session, object_store=get_object_store()
            ).ls_meta(wf_id=scope_id, prefix=_MOUNT_PREFIX)
        revisions = {
            entry.path[len(_MOUNT_PREFIX):]: entry.content_revision
            for entry in entries
            if entry.kind == "artifact" and entry.path.startswith(_MOUNT_PREFIX)
        }
        registration.files = {
            relative: _MirrorFileState(
                host_mtime_ns=stat.st_mtime_ns,
                host_size=stat.st_size,
                vfs_revision=revisions.get(relative, ""),
            )
            for relative, stat in host_meta.items()
            if relative in revisions
        }
        if changed:
            logger.info(
                "host_mount_synced",
                tenant_id=registration.tenant_id,
                user_id=registration.user_id,
                changed=changed,
            )
        return changed


host_mount_bridge = HostMountBridge()


@overload
def mount_scope_id(user_id: str) -> str: ...


@overload
def mount_scope_id(user_id: None) -> None: ...


def mount_scope_id(user_id: str | None) -> str | None:
    """Return the internal VFS scope backing one user's ``/mount``.

    The identifier is an implementation detail and is never exposed as an
    agent-visible path or frontend contract.
    """
    if not user_id:
        return None
    return f"__mount_{user_id.replace('-', '')[:24]}"


def _flush_files(
    items: list[tuple[str, bytes]], *, overwrite: bool = True
) -> int:
    written = 0
    for destination, data in items:
        if not overwrite and os.path.exists(destination):
            continue
        parent = os.path.dirname(destination)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.chmod(destination, 0o600)
        written += 1
    return written


async def hydrate_user_mount(
    *, destination: str, user_id: str, tenant_id: str, overwrite: bool = True
) -> int:
    """Materialize the durable user mount into ``destination``."""
    scope_id = mount_scope_id(user_id)
    if scope_id is None:
        raise ValueError("user_id is required to materialize /mount")
    os.makedirs(destination, mode=0o700, exist_ok=True)
    os.chmod(destination, 0o700)

    async with session_scope(tenant_id=tenant_id) as session:
        repo = VfsRepo(session, object_store=get_object_store())
        entries = await repo.ls_meta(wf_id=scope_id, prefix=_MOUNT_PREFIX)
        payloads: list[tuple[str, bytes]] = []
        for entry in entries:
            if entry.kind != "artifact" or not entry.path.startswith(_MOUNT_PREFIX):
                continue
            relative_path = entry.path[len(_MOUNT_PREFIX):]
            if not relative_path:
                continue
            data = await repo.read_bytes(wf_id=scope_id, path=entry.path)
            if data is None:
                continue
            payloads.append(
                (os.path.join(destination, *relative_path.split("/")), data)
            )

    if not payloads:
        return 0
    return await asyncio.to_thread(_flush_files, payloads, overwrite=overwrite)


def hydrate_user_mount_sync(
    *, destination: str, user_id: str, tenant_id: str, overwrite: bool = True
) -> int:
    """Synchronous mount hydration for background worker/deployment worker threads.

    This uses the NullPool-backed sync VFS facade. It must not drive the API's
    shared async SQLAlchemy engine from a new ``asyncio.run`` event loop.
    """
    scope_id = mount_scope_id(user_id)
    if scope_id is None:
        raise ValueError("user_id is required to materialize /mount")
    os.makedirs(destination, mode=0o700, exist_ok=True)
    os.chmod(destination, 0o700)
    token = current_sync_tenant_id.set(tenant_id)
    try:
        payloads: list[tuple[str, bytes]] = []
        for entry, data in PostgresVfsStore().read_prefix_bytes(
            wf_id=scope_id, prefix=_MOUNT_PREFIX
        ):
            if entry.kind != "artifact" or not entry.path.startswith(_MOUNT_PREFIX):
                continue
            relative_path = entry.path[len(_MOUNT_PREFIX):]
            if not relative_path:
                continue
            payloads.append(
                (os.path.join(destination, *relative_path.split("/")), data)
            )
        return _flush_files(payloads, overwrite=overwrite)
    finally:
        current_sync_tenant_id.reset(token)


def _collect_files(source: str) -> list[tuple[str, bytes]]:
    collected: list[tuple[str, bytes]] = []
    for root, directories, files in os.walk(source, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if not os.path.islink(os.path.join(root, name))
        ]
        for name in files:
            file_path = os.path.join(root, name)
            if os.path.islink(file_path) or not os.path.isfile(file_path):
                continue
            relative_path = os.path.relpath(file_path, source).replace(os.sep, "/")
            with open(file_path, "rb") as file:
                data = file.read(config.storage.vfs_upload_max_bytes + 1)
            if len(data) > config.storage.vfs_upload_max_bytes:
                logger.warning(
                    "user_mount_file_too_large_skipped",
                    size_limit=config.storage.vfs_upload_max_bytes,
                )
                continue
            collected.append((relative_path, data))
        if root != source and not directories and not files:
            relative_dir = os.path.relpath(root, source).replace(os.sep, "/")
            collected.append((f"{relative_dir}/{_DIR_KEEP_SENTINEL}", b""))
    return collected


async def persist_user_mount(*, source: str, user_id: str, tenant_id: str) -> int:
    """Write the materialized mount back to durable VFS.

    Writes are exact-path, last-writer-wins upserts, matching the resident Chat
    sandbox contract.  Empty leaf directories are represented by a zero-byte
    sentinel because the VFS stores files rather than directory rows.
    """
    scope_id = mount_scope_id(user_id)
    if scope_id is None:
        raise ValueError("user_id is required to persist /mount")
    if not os.path.isdir(source):
        return 0

    collected = await asyncio.to_thread(_collect_files, source)
    if not collected:
        return 0
    if config.storage.mount_path is not None:
        for _relative_path, data in collected:
            await scan_upload(data)

    synced = 0
    async with short_session_scope(tenant_id=tenant_id) as session:
        repo = VfsRepo(session, object_store=get_object_store())
        for relative_path, data in collected:
            await repo.upsert_artifact_bytes(
                wf_id=scope_id,
                tenant=tenant_id,
                path=_MOUNT_PREFIX + relative_path,
                data=data,
                content_type=_guess_ct(relative_path, data),
            )
            synced += 1
    return synced


def persist_user_mount_sync(*, source: str, user_id: str, tenant_id: str) -> int:
    """Synchronous writeback through the NullPool-backed VFS facade."""
    scope_id = mount_scope_id(user_id)
    if scope_id is None:
        raise ValueError("user_id is required to persist /mount")
    if not os.path.isdir(source):
        return 0

    collected = _collect_files(source)
    if not collected:
        return 0
    if config.storage.mount_path is not None:
        for _relative_path, data in collected:
            asyncio.run(scan_upload(data))

    token = current_sync_tenant_id.set(tenant_id)
    try:
        return PostgresVfsStore().upsert_artifact_bytes_many(
            wf_id=scope_id,
            items=[
                (
                    _MOUNT_PREFIX + relative_path,
                    data,
                    _guess_ct(relative_path, data),
                )
                for relative_path, data in collected
            ],
        )
    finally:
        current_sync_tenant_id.reset(token)


async def create_user_mount(*, user_id: str, tenant_id: str) -> str:
    """Create and hydrate a temporary host directory for one execution."""
    host_directory = host_mount_bridge.register(
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if host_directory is not None:
        await host_mount_bridge.sync_user(tenant_id=tenant_id, user_id=user_id)
        return host_directory
    destination = tempfile.mkdtemp(prefix="vc-user-mount-")
    try:
        await hydrate_user_mount(
            destination=destination, user_id=user_id, tenant_id=tenant_id
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def create_user_mount_sync(*, user_id: str, tenant_id: str) -> str:
    """Create and hydrate a mount without crossing async event loops."""
    host_directory = host_mount_bridge.register(
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if host_directory is not None:
        # Host files are operator/user-authored and authoritative on a direct
        # bridge. Hydrate only missing VFS files, then persist the merged tree.
        hydrate_user_mount_sync(
            destination=host_directory,
            user_id=user_id,
            tenant_id=tenant_id,
            overwrite=False,
        )
        persist_user_mount_sync(
            source=host_directory,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        return host_directory
    destination = tempfile.mkdtemp(prefix="vc-user-mount-")
    try:
        hydrate_user_mount_sync(
            destination=destination, user_id=user_id, tenant_id=tenant_id
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def remove_user_mount(path: str | None) -> None:
    if not path:
        return
    configured_root = config.storage.mount_path
    if configured_root is not None:
        resolved = Path(path).resolve(strict=False)
        root = configured_root.resolve(strict=True)
        if root in resolved.parents:
            # Persistent host bridge directories outlive a sandbox/turn.
            return
    shutil.rmtree(path, ignore_errors=True)
