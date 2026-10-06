"""Project-scoped Runtime volumes exposed through the VFS control plane.

The durable copy lives in the configured encrypted Object Store.  A sandbox
gets a private 0700 POSIX materialization while it is active, so SQLite, JSONL,
instructions, atomic renames, and file locking keep normal filesystem
semantics.  Quiescent turns sync the directory back, and session release removes
the plaintext projection.

The provider deliberately knows nothing about Codex.  Any filesystem-backed
Runtime can use the same Project-scoped contract.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vibecanvas_api.config import config
from vibecanvas_api.services.object_store import FilesystemObjectStore, ObjectStore, get_object_store

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+")


def _identity_component(value: str, *, field: str) -> str:
    normalized = str(value or "")
    if (
        not normalized
        or normalized in {".", ".."}
        or _SAFE_COMPONENT.fullmatch(normalized) is None
    ):
        raise ValueError(f"invalid {field} for Project Runtime Volume")
    return normalized


def _volume_scope(tenant_id: str, user_id: str, project_scope_id: str) -> str:
    return hashlib.sha256(
        (
            "vibecanvas:project-runtime-volume:v1\0"
            f"{tenant_id}\0{user_id}\0{project_scope_id}"
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProjectRuntimeVolume:
    """One private, directly mountable Runtime directory for exactly one Project."""

    volume_id: str
    path: str
    storage_prefix: str | None = None


class ProjectRuntimeVolumeProvider(Protocol):
    """Storage lifecycle; release detaches use, delete destroys resource data."""

    def ensure(self, *, tenant_id: str, user_id: str,
               project_scope_id: str) -> ProjectRuntimeVolume: ...

    def sync(self, volume: ProjectRuntimeVolume) -> int: ...

    def release(self, volume: ProjectRuntimeVolume) -> int: ...

    def delete(self, *, tenant_id: str, user_id: str,
               project_scope_id: str) -> bool: ...


class LocalPosixProjectRuntimeVolumeProvider:
    """Directory-backed provider for local disks and CSI/POSIX mount roots.

    ``root`` may itself be an encrypted local filesystem, a mounted RWO block
    volume, or a storage-system mount owned by sandboxd.  The application never
    copies the directory on turn or close; persistence is the provider's normal
    filesystem durability.
    """

    def __init__(self, root: str) -> None:
        if not root:
            raise ValueError("Project Runtime Volume root is required")
        self.root = os.path.realpath(root)

    def _coordinates(
        self, *, tenant_id: str, user_id: str, project_scope_id: str
    ) -> tuple[str, str, str, str]:
        tenant = _identity_component(tenant_id, field="tenant_id")
        user = _identity_component(user_id, field="user_id")
        if not project_scope_id or "\0" in str(project_scope_id):
            raise ValueError("invalid project_scope_id for Project Runtime Volume")
        volume_id = _volume_scope(tenant, user, str(project_scope_id))
        path = os.path.join(
            self.root,
            tenant,
            user,
            "project-runtime-v1",
            volume_id,
        )
        return tenant, user, volume_id, path

    def resolve(
        self, *, tenant_id: str, user_id: str, project_scope_id: str
    ) -> ProjectRuntimeVolume:
        _tenant, _user, volume_id, path = self._coordinates(
            tenant_id=tenant_id,
            user_id=user_id,
            project_scope_id=project_scope_id,
        )
        return ProjectRuntimeVolume(volume_id=volume_id, path=path)

    @staticmethod
    def _secure_directory(path: str, *, mode: int = 0o700) -> None:
        os.makedirs(path, mode=mode, exist_ok=True)
        if os.stat(path).st_mode & 0o7777 != mode:
            os.chmod(path, mode)

    def ensure(
        self, *, tenant_id: str, user_id: str, project_scope_id: str
    ) -> ProjectRuntimeVolume:
        tenant, user, volume_id, path = self._coordinates(
            tenant_id=tenant_id,
            user_id=user_id,
            project_scope_id=project_scope_id,
        )
        # The root also hosts API/worker-accessible workspace namespaces.
        # Keep service-group access here; only the private Runtime subtree
        # belongs exclusively to sandboxd.
        self._secure_directory(self.root, mode=0o2770)
        self._secure_directory(os.path.join(self.root, tenant))
        self._secure_directory(os.path.join(self.root, tenant, user))
        self._secure_directory(os.path.join(self.root, tenant, user, "project-runtime-v1"))
        self._secure_directory(path)
        return ProjectRuntimeVolume(volume_id=volume_id, path=path)

    def sync(self, volume: ProjectRuntimeVolume) -> int:
        # Files are already on the persistent filesystem. This does not imply
        # fsync of arbitrary application-owned open handles.
        return 0

    def release(self, volume: ProjectRuntimeVolume) -> int:
        # Session lifetime must never own the durable resource directory.
        return 0

    def delete(
        self, *, tenant_id: str, user_id: str, project_scope_id: str
    ) -> bool:
        volume = self.resolve(
            tenant_id=tenant_id,
            user_id=user_id,
            project_scope_id=project_scope_id,
        )
        target = Path(volume.path)
        try:
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
        except FileNotFoundError:
            return False
        return True


class EncryptedObjectStoreProjectRuntimeVolumeProvider:
    """Project Runtime volume with encrypted durability and ephemeral POSIX use.

    Codex still receives a normal directory, so SQLite locking, JSONL appends
    and atomic renames retain native POSIX semantics.  The directory is only a
    process-private 0700 materialization.  Every quiescent Turn is synced to the
    configured encrypted Object Store, and release removes the plaintext tree.
    """

    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def _coordinates(
        self, *, tenant_id: str, user_id: str, project_scope_id: str
    ) -> tuple[str, str, str, str]:
        tenant = _identity_component(tenant_id, field="tenant_id")
        user = _identity_component(user_id, field="user_id")
        if not project_scope_id or "\0" in str(project_scope_id):
            raise ValueError("invalid project_scope_id for Project Runtime Volume")
        volume_id = _volume_scope(tenant, user, str(project_scope_id))
        prefix = f"project-runtime-v1/{tenant}/{user}/{volume_id}"
        return tenant, user, volume_id, prefix

    @staticmethod
    def _regular_files(root: str) -> dict[str, str]:
        if os.path.islink(root):
            raise ValueError("Project Runtime Volume cannot be a symlink")
        files: dict[str, str] = {}
        for directory, subdirs, names in os.walk(root, followlinks=False):
            # Runtime processes legitimately create disposable launch-helper
            # symlinks below their state root. Object-store snapshots persist
            # durable regular files only: never follow or serialize links,
            # sockets, FIFOs, or device nodes, and never let them fail the Turn.
            subdirs[:] = [
                name
                for name in subdirs
                if not os.path.islink(os.path.join(directory, name))
            ]
            for name in names:
                path = os.path.join(directory, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                if relative.startswith("../") or relative in {"", ".", ".."}:
                    raise ValueError("Project Runtime Volume path escaped its root")
                # Account auth is staged into the private Runtime so Codex can
                # atomically refresh it, but its durable authority remains the
                # user-scoped account cache. Never duplicate credentials into
                # every Project Runtime snapshot.
                if relative == ".codex/auth.json":
                    continue
                files[relative] = path
        return files

    def sync(self, volume: ProjectRuntimeVolume) -> int:
        prefix = str(volume.storage_prefix or "").strip("/")
        if not prefix:
            raise ValueError("encrypted Project Runtime Volume has no storage prefix")
        if not os.path.isdir(volume.path) or os.path.islink(volume.path):
            raise ValueError("Project Runtime Volume materialization is unavailable")
        files = self._regular_files(volume.path)
        current_keys: set[str] = set()
        for relative, path in sorted(files.items()):
            key = f"{prefix}/{relative}"
            try:
                handle = open(path, "rb")
            except (FileNotFoundError, NotADirectoryError):
                # Only a vanished source is disposable. Storage failures must
                # propagate so release retains the only remaining source.
                continue
            with handle:
                # Never replace the still-mounted source inode during sync.
                if isinstance(self.store, FilesystemObjectStore):
                    self.store.persist_materialized_stream(
                        key, handle, size=os.fstat(handle.fileno()).st_size,
                    )
                else:
                    self.store.put_bytes(key, handle.read())
            current_keys.add(key)
        stale = set(self.store.list_keys(f"{prefix}/")) - current_keys
        for key in sorted(stale):
            self.store.delete_bytes(key)
        return len(current_keys)

    def ensure(
        self, *, tenant_id: str, user_id: str, project_scope_id: str
    ) -> ProjectRuntimeVolume:
        _tenant, _user, volume_id, prefix = self._coordinates(
            tenant_id=tenant_id,
            user_id=user_id,
            project_scope_id=project_scope_id,
        )
        path = self.store.materialize_prefix(prefix)
        os.chmod(path, 0o700)
        return ProjectRuntimeVolume(
            volume_id=volume_id,
            path=path,
            storage_prefix=prefix,
        )

    def release(self, volume: ProjectRuntimeVolume) -> int:
        # Never discard the only remaining copy when durable storage fails.
        # Keep the private projection available for a later sync/recovery.
        count = self.sync(volume)
        assert volume.storage_prefix is not None
        self.store.release_materialized_prefix(volume.storage_prefix, volume.path)
        return count

    def delete(
        self, *, tenant_id: str, user_id: str, project_scope_id: str
    ) -> bool:
        _tenant, _user, _volume_id, prefix = self._coordinates(
            tenant_id=tenant_id,
            user_id=user_id,
            project_scope_id=project_scope_id,
        )
        existed = bool(self.store.list_keys(f"{prefix}/"))
        self.store.delete_prefix(prefix)
        return existed


def get_project_runtime_volume_provider(
) -> ProjectRuntimeVolumeProvider:
    """Select Runtime persistence without leaking backend checks to callers.

    POSIX roots require volume-level encryption and a completed data migration;
    selecting the backend does not implicitly move existing encrypted objects.
    """
    if config.workspace_storage_backend == "posix":
        return LocalPosixProjectRuntimeVolumeProvider(config.workspace_storage_root)
    return EncryptedObjectStoreProjectRuntimeVolumeProvider(
        get_object_store(),
    )


__all__ = [
    "ProjectRuntimeVolume",
    "ProjectRuntimeVolumeProvider",
    "EncryptedObjectStoreProjectRuntimeVolumeProvider",
    "LocalPosixProjectRuntimeVolumeProvider",
    "get_project_runtime_volume_provider",
]
