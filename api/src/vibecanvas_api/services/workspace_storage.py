"""Persistent workspace coordinates, independent of workers and execution leases.

Callers authorize resource access before requesting a binding. This module owns
filesystem lifecycle only; it does not grant permissions or decide when a
Workflow should clear its run files.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import errno
import os
from pathlib import Path
import shutil
import stat
import uuid
from typing import BinaryIO, Callable, Iterator, Literal

WorkspaceKind = Literal['project', 'workflow', 'task', 'deployment', 'user_mount']
_KINDS = {'project', 'workflow', 'task', 'deployment', 'user_mount'}


@dataclass(frozen=True)
class WorkspaceIdentity:
    tenant_id: str
    kind: WorkspaceKind
    resource_id: str

    def __post_init__(self):
        if self.kind not in _KINDS:
            raise ValueError('Unsupported workspace kind')
        if not self.tenant_id or not self.resource_id or '\0' in self.tenant_id + self.resource_id:
            raise ValueError('Workspace identity must be nonempty and contain no NUL')


@dataclass(frozen=True)
class WorkspaceBinding:
    identity: WorkspaceIdentity
    directory: str


@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    is_directory: bool
    size_bytes: int
    modified_ns: int


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class PosixWorkspaceStorage:
    """One stable directory per resource on a local or mounted shared volume.

    The volume root and namespace parents are host-managed, never writable by
    sandbox processes. Only the resolved resource directory is bind-mounted.
    """

    def __init__(self, root: str):
        if not root or not os.path.isabs(root):
            raise ValueError('Persistent workspace root must be absolute')
        self.root = Path(os.path.realpath(root))

    def _path(self, identity: WorkspaceIdentity) -> Path:
        return self.root / 'workspaces-v1' / _digest(identity.tenant_id) / identity.kind / _digest(identity.resource_id)

    def _validate_parents(self, path: Path) -> None:
        for part in [path, *path.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise ValueError('Workspace namespace cannot contain symbolic links')

    def acquire(self, identity: WorkspaceIdentity) -> WorkspaceBinding:
        path = self._path(identity)
        self._validate_parents(path)
        self.root.mkdir(mode=0o2770, parents=True, exist_ok=True)
        # API/worker and rootful sandboxd share the trusted service group.
        # The setgid bit keeps new namespace directories in that group.
        if stat.S_IMODE(self.root.stat().st_mode) != 0o2770:
            self.root.chmod(0o2770)
        current = self.root
        for component in path.relative_to(self.root).parts:
            current = current / component
            try:
                current.mkdir(mode=0o2770)
            except FileExistsError:
                pass
            else:
                current.chmod(0o2770)
            if current.is_symlink():
                raise ValueError('Workspace namespace cannot contain symbolic links')
        return WorkspaceBinding(identity, str(path))

    @contextmanager
    def open_read(self, identity: WorkspaceIdentity, relative_path: str) -> Iterator[BinaryIO]:
        """Open an authorized resource-relative regular file, without symlinks.

        Walk using directory descriptors so a rename cannot redirect a later
        lookup outside the resource. Authorization stays with the caller.
        """
        parts = relative_path.split("/")
        if any(part in {"", ".", ".."} for part in parts) or "\0" in relative_path:
            raise ValueError("Invalid workspace relative path")
        root = self._path(identity)
        self._validate_parents(root)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            try:
                file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise ValueError("Workspace content cannot be a symbolic link") from error
                raise
            with os.fdopen(file_descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Workspace content must be a regular file")
                yield source
        finally:
            os.close(descriptor)

    def iter_bytes(self, identity: WorkspaceIdentity, relative_path: str, *,
                   start: int = 0, end: int | None = None,
                   chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Read an inclusive byte range with bounded memory."""
        if start < 0 or chunk_size <= 0 or (end is not None and end < start):
            raise ValueError("Invalid workspace byte range")
        with self.open_read(identity, relative_path) as source:
            source.seek(start)
            remaining = None if end is None else end - start + 1
            while remaining is None or remaining > 0:
                chunk = source.read(chunk_size if remaining is None else min(chunk_size, remaining))
                if not chunk:
                    break
                yield chunk
                if remaining is not None:
                    remaining -= len(chunk)

    def write_file(self, identity: WorkspaceIdentity, relative_path: str,
                   source: BinaryIO) -> bool:
        """Atomically publish one file; failed writes leave its old bytes intact.

        This provides filesystem replacement semantics, not optimistic locking
        against independent sandbox writers. Last successful rename wins.
        """
        parts = relative_path.split('/')
        if any(part in {'', '.', '..'} for part in parts) or '\0' in relative_path:
            raise ValueError('Invalid workspace relative path')
        binding = self.acquire(identity)
        descriptor = os.open(binding.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = None
        try:
            for part in parts[:-1]:
                created = False
                try:
                    os.mkdir(part, mode=0o2770, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                if created:
                    os.fchmod(child, 0o2770)
                os.close(descriptor)
                descriptor = child
            try:
                previous = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                previous = None
            if previous is not None and not stat.S_ISREG(previous.st_mode):
                raise ValueError('Workspace write target must be a regular file')
            temporary_name = '.workspace-write-' + uuid.uuid4().hex
            file_descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                      mode=0o660, dir_fd=descriptor)
            temporary = temporary_name
            with os.fdopen(file_descriptor, 'wb') as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=descriptor, dst_dir_fd=descriptor)
            temporary = None
            os.fsync(descriptor)
            return previous is not None
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=descriptor)
            os.close(descriptor)

    def release(self, binding: WorkspaceBinding) -> None:
        # Releasing compute resources never deletes persistent workspace data.
        if Path(binding.directory) != self._path(binding.identity):
            raise ValueError('Workspace binding does not belong to this storage')

    @contextmanager
    def _parent_directory(self, identity: WorkspaceIdentity, path: str):
        parts = path.split('/')
        if any(part in {'', '.', '..'} for part in parts) or '\0' in path:
            raise ValueError('Invalid workspace relative path')
        root = self._path(identity)
        self._validate_parents(root)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def remove_path(self, identity: WorkspaceIdentity, path: str) -> int:
        """Remove an exact file or subtree without following directory links."""
        def remove(parent: int, name: str) -> int:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(name, dir_fd=parent)
                return 1
            nested = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                count = 0
                for child in os.listdir(nested):
                    try:
                        count += remove(nested, child)
                    except FileNotFoundError:
                        continue
            finally:
                os.close(nested)
            os.rmdir(name, dir_fd=parent)
            return max(1, count)
        try:
            with self._parent_directory(identity, path) as (parent, name):
                return remove(parent, name)
        except FileNotFoundError:
            return 0

    def rename_path(self, identity: WorkspaceIdentity, source: str, destination: str) -> None:
        """Move a file or directory within one authorized resource."""
        with self._parent_directory(identity, source) as (source_parent, source_name):
            with self._parent_directory(identity, destination) as (target_parent, target_name):
                os.rename(source_name, target_name, src_dir_fd=source_parent, dst_dir_fd=target_parent)

    def entries(self, identity: WorkspaceIdentity, *,
                visible: Callable[[str], bool] = lambda path: True) -> Iterator[WorkspaceEntry]:
        """List live metadata without reading content or following sandbox links.

        The caller's visibility predicate also prunes directories, so private
        Chat folders can be excluded before any children are inspected.
        Concurrent removal is normal; unexpected filesystem errors propagate.
        """
        root = self._path(identity)
        self._validate_parents(root)
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return

        def walk(parent: int, prefix: str) -> Iterator[WorkspaceEntry]:
            with os.scandir(parent) as children:
                for child in children:
                    relative = prefix + child.name
                    if not visible(relative):
                        continue
                    try:
                        info = child.stat(follow_symlinks=False)
                        is_directory = stat.S_ISDIR(info.st_mode)
                        if not is_directory and not stat.S_ISREG(info.st_mode):
                            continue
                        yield WorkspaceEntry(relative, is_directory,
                                             0 if is_directory else info.st_size, info.st_mtime_ns)
                        if is_directory:
                            try:
                                nested = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                 dir_fd=parent)
                            except NotADirectoryError:
                                # A sandbox can replace a listed directory with a
                                # file or symlink before we open it. Never follow it.
                                continue
                            try:
                                yield from walk(nested, relative + '/')
                            finally:
                                os.close(nested)
                    except FileNotFoundError:
                        continue

        try:
            yield from walk(descriptor, '')
        finally:
            os.close(descriptor)

    def clear(self, identity: WorkspaceIdentity, *, preserve: frozenset[str] = frozenset()) -> None:
        """Clear resource contents in place; the caller defines retention policy."""
        path = self._path(identity)
        self._validate_parents(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in os.listdir(descriptor):
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if name not in preserve:
                            shutil.rmtree(name, dir_fd=descriptor)
                    else:
                        os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
        finally:
            os.close(descriptor)

    def delete(self, identity: WorkspaceIdentity) -> bool:
        path = self._path(identity)
        self._validate_parents(path)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return False
        return True

    def delete_tenant(self, tenant_id: str) -> bool:
        """Erase a whole tenant namespace after explicit account/tenant erasure."""
        if not tenant_id or '\0' in tenant_id:
            raise ValueError('Invalid tenant identity')
        path = self.root / 'workspaces-v1' / _digest(tenant_id)
        self._validate_parents(path)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return False
        return True
