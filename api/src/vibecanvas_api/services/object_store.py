"""Pluggable object store for batch and knowledge-base results.

The background worker ``batch_exec`` task uploads the per-row results CSV here.
Production uses S3 via ``boto3``; the sandbox / test process can't run
Docker (and thus no LocalStack), so ``InMemoryObjectStore`` is the
fallback so the sandbox can still exercise the upload code path.

Design:

* :class:`ObjectStore` is a ``Protocol`` — anything with ``put_bytes``
  and ``signed_url`` satisfies it. Tests can inject their own.
* :class:`InMemoryObjectStore` keeps a process-global dict — the
  module-level ``_global_inmemory_store`` is exposed for tests so
  they can introspect what was written. Single-process only: a blob
  written by one process is invisible to another.
* :class:`FilesystemObjectStore` writes blobs to a
  directory shared between API + background worker (a Docker
  named volume / shared dir). The filesystem IS the shared state, so it
  spans the process boundary — fixing KB indexing (api puts → worker
  fetches) and cross-container batch download. LangFlow/Dify-style local
  backend before S3.
* :class:`S3ObjectStore` lazy-imports ``boto3`` inside ``__init__`` so
  the dependency is optional (only imported when ``provider=="s3"``).
  This is the ONE permitted local import in the codebase: a
  prod-only optional dep that should not be loaded at module import
  in test / dev environments. Every other import stays at file top.
* :func:`get_object_store` reads :class:`ObjectStoreConfig` and picks
  the implementation. In-memory returns the process singleton; S3
  builds a fresh wrapper each call (cheap; ``boto3.client`` is itself
  cheap to construct, and any pooling is internal to botocore).
"""
from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from functools import lru_cache
import shutil
import tempfile
import threading
from typing import Protocol

from vibecanvas_api.config import config
from vibecanvas_api.security.crypto_core import local_master_key_from_config
from vibecanvas_api.security.object_cipher import LocalObjectCipher


class ObjectStore(Protocol):
    """Minimum surface ``batch_exec`` and the results download route need."""

    def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream",
    ) -> str:
        """Store ``data`` at ``key``. Returns a URI (e.g. ``s3://bucket/key``
        or ``memory://key``)."""
        ...

    def signed_url(self, uri: str, ttl_seconds: int = 600) -> str:
        """Generate a download URL valid for ``ttl_seconds``."""
        ...

    def fetch_bytes(self, key: str) -> bytes:
        """Server-side download for the same ``key`` originally passed to
        :meth:`put_bytes`. Added in T4 for the KB indexer (it parses blobs
        in-process; ``signed_url`` would force an HTTP round-trip).
        Implementations: ``InMemoryObjectStore`` reads ``self._data[key]``;
        ``S3ObjectStore`` issues ``get_object`` and returns the body."""
        ...

    def iter_bytes(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        """Stream an inclusive byte range without aggregating the object."""
        ...

    def delete_prefix(self, prefix: str) -> None:
        """Remove every blob whose key starts with ``prefix``.

        The knowledge-base garbage collector uses this hook when a
        ``knowledge_bases`` row is hard-deleted after the 30-day
        retention window, the matching ``kb/{tenant_id}/{kb_id}/``
        prefix is bulk-deleted in object storage so blobs do not
        accumulate. Best-effort by contract — callers swallow
        exceptions so a bad blob cannot block the DB DELETE."""
        ...

    def delete_bytes(self, key: str) -> None:
        """Delete exactly the single blob at ``key`` (no prefix semantics).

        The per-row eviction primitive for the persistent VFS: ``delete_prefix``
        is unsafe here (FS no-ops on a single-file key → orphan; InMemory/S3
        use startswith/Prefix). Missing key = silent no-op (best-effort)."""
        ...

    def materialize_prefix(self, prefix: str) -> str:
        """Return a REAL host directory whose tree mirrors every blob under
        ``prefix`` (keys become ``prefix``-relative paths). Lets a sandbox/shell
        mount + node tools share the same files (RE-1 §7). Filesystem: the prefix
        dir under root (zero-copy). S3: sync down to a temp dir. InMemory: raise
        (process-local, not a real FS)."""
        ...

    def list_keys(self, prefix: str) -> list[str]:
        """List durable object keys below one non-root prefix."""
        ...

    def release_materialized_prefix(self, prefix: str, path: str) -> None:
        """Remove one process-private plaintext materialization only."""
        ...


class InMemoryObjectStore:
    """Process-local in-memory backend — sandbox / test default.

    The CSV bytes live in ``self._data``. Test code reads them back via
    :meth:`get_bytes` (using the URI returned from :meth:`put_bytes`);
    production code never invokes ``get_bytes`` — it goes through
    :meth:`signed_url` which is a passthrough here.
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream",
    ) -> str:
        self._data[key] = data
        return f"memory://{key}"

    def signed_url(self, uri: str, ttl_seconds: int = 600) -> str:
        # In-memory store: the URI itself is the handle.
        return uri

    def get_bytes(self, uri: str) -> bytes:
        """Test-only helper — production code uses :meth:`signed_url`."""
        if not uri.startswith("memory://"):
            raise ValueError(f"Not an in-memory URI: {uri}")
        return self._data[uri[len("memory://"):]]

    def fetch_bytes(self, key: str) -> bytes:
        """KB indexer entry point — fetches by the raw key (no URI prefix)."""
        if key not in self._data:
            raise KeyError(f"key not found in in-memory store: {key}")
        return self._data[key]

    def iter_bytes(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        data = self.fetch_bytes(key)
        stop = len(data) if end is None else min(len(data), end + 1)
        for offset in range(start, stop, chunk_size):
            yield data[offset:min(stop, offset + chunk_size)]

    def delete_prefix(self, prefix: str) -> None:
        """KB GC sweeper entry — delete every key starting with ``prefix``.

        Builds the doomed-key list first (cannot mutate ``self._data``
        while iterating), then pops each. Silent no-op for the empty
        case (no keys with that prefix is expected after a clean GC)."""
        doomed = [k for k in self._data if k.startswith(prefix)]
        for k in doomed:
            del self._data[k]

    def delete_bytes(self, key: str) -> None:
        self._data.pop(key, None)

    def materialize_prefix(self, prefix: str) -> str:
        raise NotImplementedError("InMemoryObjectStore cannot materialize to a real FS")

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self._data if key.startswith(prefix))

    def release_materialized_prefix(self, prefix: str, path: str) -> None:
        del prefix, path


class FilesystemObjectStore:
    """Encrypted cross-process local Object Store.

    Durable files under ``root`` are always VCOBJ2 ciphertext containers.  A
    separate process-private 0700 tree holds plaintext only while a sandbox
    needs a mount.  This preserves the resident-sandbox fast path: file tools
    operate on a normal filesystem and encryption is paid once at hydration and
    once at durable writeback, not on every Agent filesystem operation.
    """

    def __init__(
        self,
        *,
        root: str,
        materialized_root: str | None = None,
        encryption_chunk_bytes: int = 256 * 1024,
        master_key: bytes | None = None,
    ) -> None:
        self.root = os.path.realpath(root)
        self._materialized_base = os.path.realpath(
            materialized_root or f"{self.root}.materialized"
        )
        if (
            self._materialized_base == self.root
            or self._materialized_base.startswith(self.root + os.sep)
            or self.root.startswith(self._materialized_base + os.sep)
        ):
            raise ValueError(
                "Object Store ciphertext and materialized roots must be disjoint"
            )
        self.materialized_root = os.path.join(
            self._materialized_base,
            f"process-{os.getpid()}",
        )
        # The durable tree is encrypted and shared by API/worker/sandboxd
        # processes.  In rootful deployments sandboxd runs as ``0:10001``
        # while the API runs as ``10001:10001``; owner-only permissions make
        # objects written by sandboxd unreadable to the API.  Keep access
        # restricted to the trusted service group, but allow every process in
        # that group to traverse, read and update the ciphertext tree.
        os.makedirs(self.root, mode=0o770, exist_ok=True)
        os.chmod(self.root, 0o770)
        self._repair_durable_permissions_once()
        os.makedirs(self.materialized_root, mode=0o700, exist_ok=True)
        os.chmod(self.materialized_root, 0o700)
        self._remove_stale_materializations()
        self._cipher = LocalObjectCipher(
            master_key or local_master_key_from_config(),
            chunk_size=encryption_chunk_bytes,
        )
        self._materialized_prefixes: set[str] = set()
        self._lock = threading.RLock()

    def _repair_durable_permissions_once(self) -> None:
        """Upgrade ciphertext written by older owner-only deployments.

        The marker avoids walking a potentially large store on every process
        start.  A non-owner process may encounter root-owned legacy objects;
        in that case it leaves no marker so the rootful sandbox service can
        complete the repair when it starts.  Symlinks are never followed.
        """
        marker = os.path.join(self.root, ".service-group-permissions-v1")
        if os.path.exists(marker):
            return
        complete = True

        def mark_incomplete(_error: OSError) -> None:
            nonlocal complete
            complete = False

        for directory, directories, files in os.walk(
            self.root, followlinks=False, onerror=mark_incomplete,
        ):
            directories[:] = [
                name for name in directories
                if not os.path.islink(os.path.join(directory, name))
            ]
            try:
                os.chmod(directory, 0o770)
            except PermissionError:
                complete = False
            for name in files:
                path = os.path.join(directory, name)
                if os.path.islink(path):
                    continue
                try:
                    os.chmod(path, 0o660)
                except PermissionError:
                    complete = False
        if complete:
            fd = os.open(marker, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o660)
            os.close(fd)
            os.chmod(marker, 0o660)

    def _remove_stale_materializations(self) -> None:
        """Remove plaintext trees left by processes that no longer exist.

        Only numeric ``process-<pid>`` siblings below the configured private
        materialization root are considered.  A live process is never touched.
        """
        try:
            entries = os.listdir(self._materialized_base)
        except FileNotFoundError:
            return
        for name in entries:
            if not name.startswith("process-") or not name[8:].isdigit():
                continue
            pid = int(name[8:])
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
                continue
            except PermissionError:
                continue
            except ProcessLookupError:
                pass
            candidate = os.path.join(self._materialized_base, name)
            if os.path.islink(candidate):
                continue
            self._remove_tree(candidate)

    @staticmethod
    def _safe_path(root: str, key: str) -> str:
        candidate = os.path.join(root, *str(key).split("/"))
        real_root = os.path.realpath(root)
        real_candidate = os.path.realpath(candidate)
        if real_candidate != real_root and not real_candidate.startswith(
            real_root + os.sep
        ):
            raise ValueError(
                f"object key escapes store root (path traversal): {key!r}"
            )
        return candidate

    def _path(self, key: str) -> str:
        return self._safe_path(self.root, key)

    def _materialized_path(self, key: str) -> str:
        return self._safe_path(self.materialized_root, key)

    @staticmethod
    def _normal_prefix(prefix: str) -> str:
        value = str(prefix).strip().strip("/")
        if value in {"", "."}:
            raise ValueError(
                f"refusing empty/root object prefix: {prefix!r}"
            )
        return value + "/"

    @staticmethod
    def _write_plaintext_atomic(path: str, data: bytes) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".vcmat-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as file:
                fd = -1
                file.write(data)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _ensure_durable_directory(self, directory: str) -> None:
        """Create every ciphertext path component with service-group access."""
        relative = os.path.relpath(directory, self.root)
        current = self.root
        if relative == ".":
            return
        for component in relative.split(os.sep):
            current = os.path.join(current, component)
            os.makedirs(current, mode=0o770, exist_ok=True)
            try:
                os.chmod(current, 0o770)
            except PermissionError:
                # A sibling service may own an already-correct directory.
                if stat.S_IMODE(os.stat(current).st_mode) != 0o770:
                    raise

    def _write_encrypted_atomic(self, path: str, *, key: str, data: bytes) -> None:
        directory = os.path.dirname(path)
        self._ensure_durable_directory(directory)
        fd, temporary = tempfile.mkstemp(prefix=".vcobj-", dir=directory)
        try:
            os.fchmod(fd, 0o660)
            with os.fdopen(fd, "wb") as file:
                fd = -1
                self._cipher.write(file, key=key, plaintext=data)
            os.replace(temporary, path)
            os.chmod(path, 0o660)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _active_materialized_path(self, key: str) -> str | None:
        for prefix in self._materialized_prefixes:
            if key.startswith(prefix):
                return self._materialized_path(key)
        return None

    def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream",
    ) -> str:
        del content_type
        with self._lock:
            self._write_encrypted_atomic(self._path(key), key=key, data=data)
            mirror = self._active_materialized_path(key)
            if mirror is not None:
                self._write_plaintext_atomic(mirror, data)
        return f"fs://{key}"

    def signed_url(self, uri: str, ttl_seconds: int = 600) -> str:
        raise NotImplementedError(
            "FilesystemObjectStore has no signed URL; stream via fetch_bytes"
        )

    def fetch_bytes(self, key: str) -> bytes:
        # A resident sandbox already operates on this process-private 0600
        # plaintext tree. Prefer that authoritative hot copy so Preview and
        # host file tools do not pay AEAD again for every read. Direct sandbox
        # writes also become visible before the next durable writeback.
        with self._lock:
            mirror = self._active_materialized_path(key)
        if mirror is not None:
            try:
                with open(mirror, "rb") as file:
                    return file.read()
            except FileNotFoundError:
                # A just-created key may not exist in the hydrated tree yet;
                # the durable encrypted object remains the strict fallback.
                pass
        try:
            with open(self._path(key), "rb") as file:
                return self._cipher.read(file, key=key)
        except FileNotFoundError as exc:
            raise KeyError(f"key not found in filesystem store: {key}") from exc

    def iter_bytes(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        with self._lock:
            mirror = self._active_materialized_path(key)
        if mirror is not None:
            try:
                with open(mirror, "rb") as file:
                    size = os.fstat(file.fileno()).st_size
                    if start < 0 or (end is not None and end < 0):
                        raise ValueError("object byte range must be non-negative")
                    if chunk_size <= 0:
                        raise ValueError("output chunk size must be positive")
                    stop = size if end is None else min(size, end + 1)
                    if start >= stop:
                        return
                    file.seek(start)
                    remaining = stop - start
                    while remaining > 0:
                        chunk = file.read(min(chunk_size, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
                return
            except FileNotFoundError:
                pass
        try:
            with open(self._path(key), "rb") as file:
                yield from self._cipher.iter_range(
                    file,
                    key=key,
                    start=start,
                    end=end,
                    output_chunk_size=chunk_size,
                )
        except FileNotFoundError as exc:
            raise KeyError(f"key not found in filesystem store: {key}") from exc

    @staticmethod
    def _remove_tree(path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def delete_persisted_prefix(self, prefix: str) -> None:
        """Delete ciphertext while preserving an already-bound mount path."""
        normalized = self._normal_prefix(prefix)
        base = self._path(normalized.rstrip("/"))
        if os.path.realpath(base) == self.root:
            raise ValueError("refusing to delete the Object Store root")
        self._remove_tree(base)

    def delete_prefix(self, prefix: str) -> None:
        normalized = self._normal_prefix(prefix)
        with self._lock:
            self.delete_persisted_prefix(normalized)
            self._remove_tree(
                self._materialized_path(normalized.rstrip("/"))
            )
            self._materialized_prefixes = {
                item
                for item in self._materialized_prefixes
                if not item.startswith(normalized)
            }

    def delete_bytes(self, key: str) -> None:
        with self._lock:
            try:
                os.remove(self._path(key))
            except FileNotFoundError:
                pass
            mirror = self._active_materialized_path(key)
            if mirror is not None:
                try:
                    os.remove(mirror)
                except FileNotFoundError:
                    pass

    def materialize_prefix(self, prefix: str) -> str:
        normalized = self._normal_prefix(prefix)
        destination = self._materialized_path(normalized.rstrip("/"))
        with self._lock:
            if any(
                normalized.startswith(active)
                for active in self._materialized_prefixes
            ):
                os.makedirs(destination, mode=0o700, exist_ok=True)
                return destination
            self._remove_tree(destination)
            os.makedirs(destination, mode=0o700, exist_ok=True)
            source = self._path(normalized.rstrip("/"))
            try:
                if os.path.isdir(source):
                    for directory, _subdirs, files in os.walk(source):
                        for name in files:
                            encrypted_path = os.path.join(directory, name)
                            key = os.path.relpath(
                                encrypted_path,
                                self.root,
                            ).replace(os.sep, "/")
                            data = self.fetch_bytes(key)
                            relative = os.path.relpath(encrypted_path, source)
                            self._write_plaintext_atomic(
                                os.path.join(destination, relative),
                                data,
                            )
                self._materialized_prefixes = {
                    active
                    for active in self._materialized_prefixes
                    if not active.startswith(normalized)
                }
                self._materialized_prefixes.add(normalized)
            except Exception:
                self._remove_tree(destination)
                raise
        return destination

    def list_keys(self, prefix: str) -> list[str]:
        normalized = self._normal_prefix(prefix)
        source = self._path(normalized.rstrip("/"))
        keys: list[str] = []
        if not os.path.isdir(source) or os.path.islink(source):
            return keys
        for directory, subdirs, files in os.walk(source, followlinks=False):
            subdirs[:] = [
                name
                for name in subdirs
                if not os.path.islink(os.path.join(directory, name))
            ]
            for name in files:
                path = os.path.join(directory, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                keys.append(
                    os.path.relpath(path, self.root).replace(os.sep, "/")
                )
        return sorted(keys)

    def release_materialized_prefix(self, prefix: str, path: str) -> None:
        normalized = self._normal_prefix(prefix)
        expected = self._materialized_path(normalized.rstrip("/"))
        if os.path.realpath(path) != os.path.realpath(expected):
            raise ValueError("materialized path does not match object prefix")
        with self._lock:
            self._materialized_prefixes = {
                active
                for active in self._materialized_prefixes
                if active != normalized and not active.startswith(normalized)
            }
            self._remove_tree(expected)


class S3ObjectStore:
    """Production S3 backend.

    ``boto3`` is lazy-imported in ``__init__`` so the dependency is only
    required at runtime when ``provider="s3"``. Sandbox / test installs
    don't need boto3 to work.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str,
        server_side_encryption: str = "",
        kms_key_id: str = "",
    ) -> None:
        import boto3  # noqa: PLC0415 — optional prod-only dep
        self.bucket = bucket
        if server_side_encryption not in {"", "AES256", "aws:kms"}:
            raise ValueError("unsupported S3 server-side encryption mode")
        if server_side_encryption == "aws:kms" and not kms_key_id:
            raise ValueError("S3 KMS key id is required for aws:kms")
        self.server_side_encryption = server_side_encryption
        self.kms_key_id = kms_key_id
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream",
    ) -> str:
        encryption: dict[str, str] = {}
        if self.server_side_encryption:
            encryption["ServerSideEncryption"] = self.server_side_encryption
        if self.server_side_encryption == "aws:kms":
            encryption["SSEKMSKeyId"] = self.kms_key_id
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            **encryption,
        )
        return f"s3://{self.bucket}/{key}"

    def signed_url(self, uri: str, ttl_seconds: int = 600) -> str:
        if not uri.startswith("s3://"):
            raise ValueError(f"Not an S3 URI: {uri}")
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            raise ValueError(f"Malformed S3 URI: {uri}")
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    def fetch_bytes(self, key: str) -> bytes:
        """KB indexer entry point — server-side download (used by background worker
        worker; ``signed_url`` is for client-side downloads only)."""
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def iter_bytes(
        self,
        key: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        range_header = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=key,
            Range=range_header,
        )
        body = response["Body"]
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    def delete_prefix(self, prefix: str) -> None:
        """KB GC sweeper entry — bulk-delete every key under ``prefix``.

        S3 has no native prefix-delete: we paginate ``list_objects_v2``
        and feed batches of up to 1000 keys to ``delete_objects``
        (the API's hard cap). Continues across pages until the prefix
        is fully drained. Caller swallows exceptions per the
        :class:`ObjectStore` contract."""
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            contents = page.get("Contents") or []
            if not contents:
                continue
            # ``delete_objects`` accepts at most 1000 keys per call,
            # which matches ``list_objects_v2``'s default page size —
            # one call per page is safe.
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
            )

    def delete_bytes(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def materialize_prefix(self, prefix: str) -> str:
        import os as _os  # noqa: PLC0415 — prod-only sync-down path
        import tempfile  # noqa: PLC0415 — prod-only sync-down path
        d = tempfile.mkdtemp(prefix="vfs-run-")
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix):].lstrip("/")
                dest = _os.path.join(d, *rel.split("/"))
                _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(self.fetch_bytes(obj["Key"]))
        return d

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(
                str(item["Key"])
                for item in page.get("Contents", [])
                if isinstance(item, dict) and item.get("Key")
            )
        return sorted(keys)

    def release_materialized_prefix(self, prefix: str, path: str) -> None:
        del prefix
        if os.path.islink(path):
            raise ValueError("refusing to release a symlinked materialization")
        shutil.rmtree(path, ignore_errors=True)


# Module-level singleton — tests introspect via ``_global_inmemory_store``.
_global_inmemory_store = InMemoryObjectStore()


@lru_cache(maxsize=4)
def _filesystem_object_store(
    root: str,
    materialized_root: str,
    encryption_chunk_bytes: int,
) -> FilesystemObjectStore:
    """One store per process/config so active materializations stay coherent."""
    return FilesystemObjectStore(
        root=root,
        materialized_root=materialized_root,
        encryption_chunk_bytes=encryption_chunk_bytes,
        master_key=local_master_key_from_config(require_persistent=True),
    )


def safe_object_key_segment(name: str | None) -> str:
    """Reduce a user-controlled filename to ONE safe object-key segment.

    The knowledge-base upload route
    embeds ``file.filename`` in the object_store key. A filename like
    ``../../../../tmp/evil`` would, on the filesystem backend, escape the
    store root (arbitrary write/read). The display filename is preserved
    in the ``kb_files.name`` column; the key segment is purely an FS path
    component, so we strip it to a single safe name:

    * ``os.path.basename`` drops any directory portion (``a/b/c`` → ``c``),
    * a remaining ``.`` / ``..`` / empty / whitespace-only result (e.g.
      from ``"../.."`` or ``""``) collapses to ``"unnamed"`` — the
      ``file_id`` UUID already in the key guarantees uniqueness, so the
      segment only needs to be safe, not meaningful.

    Normal filenames (``report.csv``, ``my file (1).pdf``) pass through
    unchanged.
    """
    # ``os.path.basename`` only splits on the host separator; normalise
    # backslashes first so a Windows-style ``..\\..\\evil`` is also caught.
    raw = (name or "").replace("\\", "/")
    base = os.path.basename(raw).strip()
    if base in ("", ".", ".."):
        return "unnamed"
    return base


def uri_to_key(uri: str) -> str:
    """Reverse :meth:`put_bytes` — recover the bare ``key`` from a stored URI.

    Used by the batch-download route to stream a blob server-side via
    :meth:`fetch_bytes` for the non-S3 providers (``memory://`` /
    ``fs://``), which have no signed URL. Schemes:

    * ``memory://<key>``    → ``<key>``
    * ``fs://<key>``        → ``<key>``
    * ``s3://<bucket>/<key>`` → ``<key>``  (bucket dropped — the configured
      :class:`S3ObjectStore` already knows its bucket)
    """
    for prefix in ("memory://", "fs://"):
        if uri.startswith(prefix):
            return uri[len(prefix):]
    if uri.startswith("s3://"):
        _bucket, _, key = uri[len("s3://"):].partition("/")
        return key
    # No recognised scheme — treat the whole string as the key.
    return uri


def get_object_store() -> ObjectStore:
    """Resolve the configured backend.

    ``inmemory`` returns the process singleton so tests can assert on
    written bytes. ``s3`` returns a fresh wrapper around boto3.
    """
    cfg = config.object_store
    if cfg.provider == "inmemory":
        return _global_inmemory_store
    if cfg.provider == "filesystem":
        return _filesystem_object_store(
            cfg.fs_root,
            cfg.fs_materialized_root,
            cfg.fs_encryption_chunk_bytes,
        )
    if cfg.provider == "s3":
        return S3ObjectStore(
            endpoint_url=cfg.s3_endpoint_url,
            bucket=cfg.s3_bucket,
            access_key=cfg.s3_access_key,
            secret_key=cfg.s3_secret_key,
            region=cfg.s3_region,
            server_side_encryption=cfg.s3_server_side_encryption,
            kms_key_id=cfg.s3_kms_key_id,
        )
    raise ValueError(f"Unknown object store provider: {cfg.provider!r}")
