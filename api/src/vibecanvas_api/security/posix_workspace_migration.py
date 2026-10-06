"""Offline, bounded-memory object migration. Call only with writers stopped.

The caller resolves authorized resource identities to destination paths and
supplies decrypted ObjectStore.iter_bytes(). No database identities are changed.
Source objects are never deleted and differing destination files are rejected.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class MigratedFile:
    size_bytes: int
    sha256: str
    already_present: bool


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def migrate_file(chunks: Iterable[bytes], *, root: Path, relative: str,
                 expected_size: int | None = None) -> MigratedFile:
    """Publish only complete files, then verify bytes read from the new volume.

    root must be an offline, host-controlled destination. This is a migration
    utility, not a runtime API for concurrently sandbox-writable directories.
    """
    parts = relative.split('/')
    if any(part in {'', '.', '..'} for part in parts) or '\0' in relative:
        raise ValueError('Invalid migration destination')
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError('Migration root must be an existing absolute directory')
    parent = root
    for part in parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError('Migration destination cannot contain links')
        parent.mkdir(mode=0o700, exist_ok=True)
    target = parent / parts[-1]
    if target.is_symlink():
        raise ValueError('Migration destination cannot be a link')
    descriptor, temporary = tempfile.mkstemp(prefix='.migration-', dir=parent)
    staged = Path(temporary)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, 'wb') as output:
            for chunk in chunks:
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            if expected_size is not None and size != expected_size:
                raise ValueError('Migrated source size does not match metadata')
            output.flush()
            os.fsync(output.fileno())
        expected = (size, digest.hexdigest())
        already_present = False
        try:
            os.link(staged, target)
        except FileExistsError:
            already_present = True
        if not target.is_file() or target.is_symlink() or _digest_file(target) != expected:
            raise ValueError('Migration destination content conflict')
        staged.unlink()
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return MigratedFile(size, expected[1], already_present)
    finally:
        staged.unlink(missing_ok=True)


def migrate_encrypted_task_result(source_store, target_store, key: str) -> MigratedFile:
    """Copy the existing ciphertext key, retaining fs:// URIs and encryption.

    Both filesystem stores must use the same persistent master key. The
    plaintext is authenticated on both sides but never written to a temp file.
    """
    from vibecanvas_api.services.object_store import FilesystemObjectStore
    if not isinstance(source_store, FilesystemObjectStore) or not isinstance(target_store, FilesystemObjectStore):
        raise ValueError('Ciphertext relocation requires filesystem stores')
    if not key.startswith('tasks/'):
        raise ValueError('Not a Task result key')

    def fingerprint(store):
        digest = hashlib.sha256()
        size = 0
        for chunk in store.iter_bytes(key):
            digest.update(chunk)
            size += len(chunk)
        return size, digest.hexdigest()

    before = fingerprint(source_store)
    source = Path(source_store._path(key))
    with source.open('rb') as handle:
        result = migrate_file(iter(lambda: handle.read(1024 * 1024), b''),
                              root=Path(target_store.root), relative=key,
                              expected_size=os.fstat(handle.fileno()).st_size)
    if fingerprint(target_store) != before:
        raise ValueError('Task result verification failed')
    return result
