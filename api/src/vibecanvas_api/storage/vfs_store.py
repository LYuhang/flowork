"""VFS store (2b-1) — durable artifact/scratch files in Postgres.

VfsRepo = async session-bound repo (mirrors RefRepo). PostgresVfsStore = sync
facade (mirrors SyncRefRepo): one short NullPool session per call via
run_in_short_session, which sets app.tenant_id from current_sync_tenant_id for
RLS. The VfsStore Protocol lets the future Agent Sandbox add a materialize-to-FS
impl without touching callers.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from vibecanvas_api.security.vfs_protection import (
    protect_vfs_abstract,
    unprotect_vfs_abstract,
)
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.file_revision import vfs_content_revision, vfs_row_revision
from vibecanvas_api.storage.models import VfsArtifact, VfsScratch
from vibecanvas_api.storage.sync_session import current_sync_tenant_id, run_in_short_session

# User-writable upload prefixes (the explicit-path ingress allowlist). They map
# to the durable `VfsArtifact` table. `/mount` is the user-level shared area;
# `/data` is one Chat's working area. Anything outside the allowlist is
# rejected by `_validate_artifact_path` (the traversal/prefix security boundary).
_USER_WRITABLE_PREFIXES = ("/data/", "/chats/", "/mount/")

_EXT = {
    "table/jsonl": "jsonl", "table/csv": "csv", "table/tsv": "tsv", "table/xlsx": "xlsx",
    "text/plain": "txt", "text/python": "py", "text/html": "html", "application/json": "json",
    # legacy 2b-1 spellings — tolerated so no alembic migration is needed
    "json": "json", "text": "txt",
    # binary content types (2b binary VFS)
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
    "audio/mpeg": "mp3", "audio/wav": "wav", "application/pdf": "pdf",
    "application/vnd.jgraph.mxfile": "drawio",
    "application/octet-stream": "bin",
}
_SEQ_RE = re.compile(r"_(\d+)\.[^.]+$")
_MAX_SEQ_RETRY = 8


def _validate_artifact_path(path: str) -> str:
    """Validate an EXPLICIT user-writable artifact path (the user/agent-supplied
    write surface). The security boundary against traversal into other prefixes.

    Rules: must start with one of the user-writable prefixes (`/data/`,
    `/mount/`), hold ≥1 non-empty basename-style segment, reject `..`/`.`-only
    segments, absolute-escape, control chars, and a trailing-slash (folder)
    path. Returns the path unchanged on success; raises ValueError otherwise.
    Implicit folders (`/mount/a/b.png`) are allowed — folders are S3-style path
    prefixes, no explicit dir rows. A path under a NON-allowlisted prefix (e.g.
    `/memory/x`, `/etc/passwd`) is rejected.
    """
    prefix = next((p for p in _USER_WRITABLE_PREFIXES if path.startswith(p)), None)
    if not path or prefix is None:
        raise ValueError(
            f"path must start with one of {_USER_WRITABLE_PREFIXES!r}: {path!r}")
    rel = path[len(prefix):]
    if not rel:
        raise ValueError(f"empty filename: {path!r}")
    segs = rel.split("/")
    for seg in segs:
        if seg in ("", ".", ".."):
            raise ValueError(f"invalid path segment in {path!r}")
        if any(ord(ch) < 0x20 or ch == "\x7f" for ch in seg):
            raise ValueError(f"control char in path {path!r}")
    return path


def _is_text_ct(ct: str) -> bool:
    """A content_type whose bytes are UTF-8 text (decode-as-text on read).
    Mirrors tools.read_path._is_text_ct; kept local to avoid a tools→storage
    import cycle. Includes the legacy 2b-1 spellings ("json"/"text")."""
    ct = ct or ""
    return (ct.startswith("text/") or ct in {
                "application/json",
                "application/vnd.jgraph.mxfile",
            }
            or ct.startswith("table/") or ct in ("json", "text"))


@dataclass(slots=True)
class VfsEntry:
    path: str
    kind: str            # 'artifact' | 'scratch'
    content: str
    content_type: str
    abstract: str
    size_bytes: int
    wf_version: Optional[str]
    last_access: float


@dataclass(slots=True)
class VfsEntryMeta:
    """Listing row — no `content` (the listing path never loads large blobs)."""
    path: str
    kind: str            # 'artifact' | 'scratch'
    content_type: str
    abstract: str
    size_bytes: int
    wf_version: Optional[str]
    last_access: float
    content_revision: str = ""


@dataclass(frozen=True, slots=True)
class VfsCasResult:
    """Result of an exact-revision durable artifact update."""

    committed: bool
    revision: str | None
    current_revision: str | None
    content_revision: str | None = None


def _now():
    return datetime.now(timezone.utc)


class VfsRepo:
    def __init__(self, session: AsyncSession, *,
                 object_store=None,
                 max_entries_per_scope: int = 64,
                 max_bytes_per_scope: int = 16 * 1024 * 1024):
        self._s = session
        self._os = object_store
        self._max_entries = max_entries_per_scope
        self._max_bytes = max_bytes_per_scope

    async def write_artifact(self, *, wf_id, tenant, category, basename, content,
                             content_type="text/plain", wf_version=None,
                             abstract="") -> str:
        # Text write delegates to the bytes writer: encode → ObjectStore,
        # object_key set (Postgres = pure metadata index). Resolve the
        # ext with the TEXT default ("txt" for an unknown content_type, not
        # "bin") so a text write keeps its .txt path even for an unknown ct.
        ext = _EXT.get(content_type, "txt")
        return await self.write_artifact_bytes(
            wf_id=wf_id, tenant=tenant, category=category, basename=basename,
            data=content.encode(), content_type=content_type, ext=ext,
            wf_version=wf_version, abstract=abstract)

    async def _next_seq(self, wf_id, category) -> int:
        prefix = f"/{category}/"
        rows = (await self._s.execute(
            select(VfsArtifact.path).where(
                VfsArtifact.scope_id == wf_id,
                VfsArtifact.path.like(prefix + "%")))).scalars().all()
        mx = 0
        for p in rows:
            m = _SEQ_RE.search(p)
            if m:
                mx = max(mx, int(m.group(1)))
        return mx + 1

    async def write_scratch(self, *, wf_id, tenant, path, content,
                            content_type="text/plain", abstract="") -> None:
        # Text write delegates to the bytes writer (object-backed).
        await self.write_scratch_bytes(
            wf_id=wf_id, tenant=tenant, path=path, data=content.encode(),
            content_type=content_type, abstract=abstract)

    def _require_store(self):
        if self._os is None:
            raise RuntimeError("vfs: object_store required for binary operations")
        return self._os

    async def write_artifact_bytes(self, *, wf_id, tenant, category, basename, data,
                                   content_type, ext=None, wf_version=None, abstract="") -> str:
        store = self._require_store()
        # Binary default is "bin"; a text content_type still resolves via _EXT
        # so a text byte-write keeps .txt. The text writer (write_artifact)
        # passes an explicit `ext` to preserve the .txt fallback for an unknown
        # text content_type.
        if ext is None:
            ext = _EXT.get(content_type, "txt" if _is_text_ct(content_type) else "bin")
        size = len(data)
        for _ in range(_MAX_SEQ_RETRY):
            seq = await self._next_seq(wf_id, category)
            path = f"/{category}/{basename}_{seq}.{ext}"
            key = f"artifacts/{tenant}/{wf_id}{path}"
            abstract_values = await protect_vfs_abstract(
                self._s,
                tenant_id=str(tenant),
                kind="artifact",
                resource_id=str(wf_id),
                path=path,
                abstract=abstract,
            )
            res = await self._s.execute(
                pg_insert(VfsArtifact).values(
                    scope_id=wf_id, path=path, object_key=key,
                    content_type=content_type, **abstract_values,
                    size_bytes=size, wf_version=wf_version,
                ).on_conflict_do_nothing(index_elements=["scope_id", "path"]))
            await self._s.flush()
            if res.rowcount == 1:
                store.put_bytes(key, data, content_type)
                await self._evict(VfsArtifact, VfsArtifact.scope_id, wf_id)
                return path
        raise RuntimeError(f"vfs: could not allocate a path for /{category}/{basename}")

    async def upsert_artifact_bytes(self, *, wf_id, tenant, path, data,
                                    content_type, abstract="") -> bool:
        """Explicit-path durable artifact upsert.

        Unlike `write_artifact_bytes` (auto-seq under a category), the caller
        supplies the exact path (for example `/mount/sales.csv`) and it is preserved
        verbatim — last-writer-wins on a re-write of the same path. The object
        key is deterministic from the path, so a same-path overwrite reuses the
        SAME key (overwrite-in-place, no orphan blob). Returns whether an
        existing row was REPLACED (for the route's
        `replaced` field) — checked BEFORE the upsert.
        """
        store = self._require_store()
        key = f"artifacts/{tenant}/{wf_id}{path}"
        current = (
            await self._s.execute(
                select(VfsArtifact)
                .where(
                    VfsArtifact.scope_id == wf_id,
                    VfsArtifact.path == path,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        replaced = current is not None
        if current is not None and current.content_type == content_type:
            try:
                current_data = store.fetch_bytes(current.object_key)
            except (KeyError, OSError, ValueError):
                current_data = None
            if current_data == data:
                # Workspace write-through is followed by a turn-end safety
                # sweep. Rewriting identical bytes here would create a new
                # revision and immediately make an exact contentHash returned
                # by the sandbox Diagram MCP stale even though bytes did not change.
                return True
        content_revision = str(uuid.uuid4())
        abstract_values = await protect_vfs_abstract(
            self._s,
            tenant_id=str(tenant),
            kind="artifact",
            resource_id=str(wf_id),
            path=path,
            abstract=abstract,
        )
        await self._s.execute(
            pg_insert(VfsArtifact).values(
                scope_id=wf_id, path=path, object_key=key,
                content_type=content_type, **abstract_values,
                size_bytes=len(data), content_revision=content_revision,
            ).on_conflict_do_update(
                index_elements=["scope_id", "path"],
                set_=dict(object_key=key, content_type=content_type,
                          **abstract_values, size_bytes=len(data),
                          content_revision=content_revision, last_access=_now())))
        await self._s.flush()
        # put_bytes only AFTER the row flush — a rejected insert (RLS) leaves no
        # orphan blob (symmetric with write_scratch_bytes). Same key for the same
        # Explicit-path writes overwrite in place. The caller controls the
        # durable namespace and therefore does not invoke tool-output LRU here.
        store.put_bytes(key, data, content_type)
        return replaced

    async def compare_and_swap_artifact_bytes(
        self,
        *,
        wf_id,
        tenant,
        path,
        expected_revision: str | None,
        data: bytes,
        content_type: str,
        abstract: str = "",
    ) -> VfsCasResult:
        """Atomically replace one durable path only at ``expected_revision``.

        Unlike the general-purpose last-writer-wins upsert, semantic commit
        surfaces use this method to prevent a stale Agent check from replacing
        a newer user-visible revision. A revision-specific object key also
        prevents a losing writer from changing the bytes referenced by the
        winning database row.
        """
        _validate_artifact_path(path)
        store = self._require_store()
        current = (
            await self._s.execute(
                select(VfsArtifact)
                .where(
                    VfsArtifact.scope_id == wf_id,
                    VfsArtifact.path == path,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        current_revision = vfs_row_revision(current) if current is not None else None
        if current_revision != expected_revision:
            return VfsCasResult(
                committed=False,
                revision=None,
                current_revision=current_revision,
            )

        content_revision = str(uuid.uuid4())
        revision = vfs_content_revision(content_revision)
        key = (
            f"artifacts/{tenant}/{wf_id}{path}.revision-"
            f"{content_revision}"
        )
        abstract_values = await protect_vfs_abstract(
            self._s,
            tenant_id=str(tenant),
            kind="artifact",
            resource_id=str(wf_id),
            path=path,
            abstract=abstract,
        )
        values = dict(
            object_key=key,
            content_type=content_type,
            **abstract_values,
            size_bytes=len(data),
            content_revision=content_revision,
            last_access=_now(),
        )
        if current is None:
            result = await self._s.execute(
                pg_insert(VfsArtifact)
                .values(scope_id=wf_id, path=path, **values)
                .on_conflict_do_nothing(index_elements=["scope_id", "path"])
            )
        else:
            result = await self._s.execute(
                update(VfsArtifact)
                .where(
                    VfsArtifact.scope_id == wf_id,
                    VfsArtifact.path == path,
                    VfsArtifact.content_revision == current.content_revision,
                )
                .values(**values)
            )
        if result.rowcount != 1:
            refreshed = (
                await self._s.execute(
                    select(VfsArtifact).where(
                        VfsArtifact.scope_id == wf_id,
                        VfsArtifact.path == path,
                    )
                )
            ).scalar_one_or_none()
            return VfsCasResult(
                committed=False,
                revision=None,
                current_revision=(
                    vfs_row_revision(refreshed) if refreshed is not None else None
                ),
            )
        await self._s.flush()
        store.put_bytes(key, data, content_type)
        return VfsCasResult(
            committed=True,
            revision=revision,
            current_revision=revision,
            content_revision=content_revision,
        )

    async def upsert_artifact(self, *, wf_id, tenant, path, content,
                              content_type="text/plain", abstract="") -> bool:
        """Explicit-path TEXT artifact upsert — post-unification, delegates to
        the bytes writer (object-backed at the same key → overwrite-in-place,
        no object→text orphan). Returns whether a row was REPLACED."""
        return await self.upsert_artifact_bytes(
            wf_id=wf_id, tenant=tenant, path=path, data=content.encode(),
            content_type=content_type, abstract=abstract)

    async def upsert_internal_artifact_bytes(self, *, wf_id, tenant, path, data,
                                             content_type, abstract="") -> bool:
        """Internal exact-path artifact upsert.

        This bypasses the user-write allowlist used by ``upsert_artifact_bytes``.
        It is for backend-owned workspace artifacts that
        must be readable through the normal VFS surface but are not user ingress.
        Callers must pass absolute, traversal-free paths.
        """
        if not path or not path.startswith("/") or "\x00" in path:
            raise ValueError(f"invalid internal vfs path: {path!r}")
        for seg in path.split("/"):
            if seg in (".", ".."):
                raise ValueError(f"invalid internal vfs path: {path!r}")
        store = self._require_store()
        key = f"artifacts/{tenant}/{wf_id}{path}"
        replaced = await self._s.get(VfsArtifact, (wf_id, path)) is not None
        content_revision = str(uuid.uuid4())
        abstract_values = await protect_vfs_abstract(
            self._s,
            tenant_id=str(tenant),
            kind="artifact",
            resource_id=str(wf_id),
            path=path,
            abstract=abstract,
        )
        await self._s.execute(
            pg_insert(VfsArtifact).values(
                scope_id=wf_id, path=path, object_key=key,
                content_type=content_type, **abstract_values,
                size_bytes=len(data), content_revision=content_revision,
            ).on_conflict_do_update(
                index_elements=["scope_id", "path"],
                set_=dict(object_key=key, content_type=content_type,
                          **abstract_values, size_bytes=len(data),
                          content_revision=content_revision, last_access=_now())))
        await self._s.flush()
        store.put_bytes(key, data, content_type)
        return replaced

    async def upsert_internal_artifact(self, *, wf_id, tenant, path, content,
                                       content_type="text/plain", abstract="") -> bool:
        return await self.upsert_internal_artifact_bytes(
            wf_id=wf_id, tenant=tenant, path=path, data=content.encode(),
            content_type=content_type, abstract=abstract)

    async def write_scratch_bytes(self, *, wf_id, tenant, path, data,
                                  content_type, abstract="") -> None:
        store = self._require_store()
        size = len(data)
        key = f"scratch/{tenant}/{wf_id}{path}"
        abstract_values = await protect_vfs_abstract(
            self._s,
            tenant_id=str(tenant),
            kind="scratch",
            resource_id=str(wf_id),
            path=path,
            abstract=abstract,
        )
        await self._s.execute(
            pg_insert(VfsScratch).values(
                scope_id=wf_id, path=path, object_key=key,
                content_type=content_type, **abstract_values, size_bytes=size,
            ).on_conflict_do_update(
                index_elements=["scope_id", "path"],
                set_=dict(object_key=key, content_type=content_type,
                          **abstract_values, size_bytes=size, last_access=_now())))
        await self._s.flush()
        # put_bytes only AFTER the row commits, so a rejected insert (e.g. RLS /
        # tenantless CV) leaves no orphaned blob — symmetric with write_artifact_bytes.
        store.put_bytes(key, data, content_type)
        await self._evict(VfsScratch, VfsScratch.scope_id, wf_id)

    async def delete_artifact(self, *, wf_id, tenant, path) -> int:
        """Delete artifact row(s) at `path` (RLS-scoped) + best-effort blob delete.

        `path` may be an EXACT file row (e.g. `/data/x.csv`) or a FOLDER prefix
        (e.g. `/data/sub`) whose children (`/data/sub/...`) are all removed. We
        delete the exact row AND every row under `path + "/"` in one scope, drop
        each backing blob, and return the count of rows deleted.
        """
        like_prefix = path.rstrip("/") + "/"
        rows = (await self._s.execute(
            select(VfsArtifact).where(
                VfsArtifact.scope_id == wf_id,
                (VfsArtifact.path == path) | (VfsArtifact.path.like(like_prefix + "%")),
            ))).scalars().all()
        deleted = 0
        for r in rows:
            if r.object_key and self._os is not None:
                try:
                    self._os.delete_bytes(r.object_key)
                except Exception:
                    pass  # best-effort: a missing blob must not block the row delete
            await self._s.delete(r)
            deleted += 1
        if deleted:
            await self._s.flush()
        return deleted

    async def delete_scope_prefixes(self, *, wf_id, prefixes: list[str]) -> int:
        """Delete artifact and scratch rows under the given absolute prefixes.

        Backend-owned cleanup path for deleting an entire chat workspace. This is
        intentionally not exposed to user VFS routes, so it can remove agent-owned
        `/memory`, `/logs`, and `/__runtime` data as well as user-visible `/data`.
        """
        deleted = 0
        safe_prefixes: list[str] = []
        for prefix in prefixes:
            if not prefix or not prefix.startswith("/") or "\x00" in prefix:
                raise ValueError(f"invalid vfs prefix: {prefix!r}")
            if any(seg in (".", "..") for seg in prefix.split("/")):
                raise ValueError(f"invalid vfs prefix: {prefix!r}")
            safe_prefixes.append(prefix.rstrip("/"))

        for model in (VfsArtifact, VfsScratch):
            clauses = []
            for prefix in safe_prefixes:
                like_prefix = prefix + "/"
                clauses.append((model.path == prefix) | (model.path.like(like_prefix + "%")))
            if not clauses:
                continue
            criterion = clauses[0]
            for clause in clauses[1:]:
                criterion = criterion | clause
            rows = (await self._s.execute(
                select(model).where(model.scope_id == wf_id, criterion)
            )).scalars().all()
            for row in rows:
                if row.object_key and self._os is not None:
                    try:
                        self._os.delete_bytes(row.object_key)
                    except Exception:
                        pass
                await self._s.delete(row)
                deleted += 1
        if deleted:
            await self._s.flush()
        return deleted

    async def rename_artifact(self, *, wf_id, tenant, old_path, new_path) -> bool:
        """Rename a file OR folder within the durable VFS.

        For a single file row at `old_path`: read its bytes, re-write at
        `new_path` (validated; new deterministic object_key), then delete the
        old row+blob. For a FOLDER prefix (rows under `old_path + "/"`): each
        child is re-keyed by swapping the `old_path` prefix for `new_path`. Both
        the destination root and every re-keyed child path are validated with
        `_validate_artifact_path` (the same allowlist/traversal boundary the
        upload route uses). Returns True on success; raises ValueError on a bad
        `new_path` or a missing source.
        """
        _validate_artifact_path(new_path)

        exact = await self._s.get(VfsArtifact, (wf_id, old_path))
        old_prefix = old_path.rstrip("/") + "/"
        children = (await self._s.execute(
            select(VfsArtifact).where(
                VfsArtifact.scope_id == wf_id,
                VfsArtifact.path.like(old_prefix + "%")))).scalars().all()

        if exact is None and not children:
            raise ValueError(f"rename source not found: {old_path!r}")

        # A same-path rename is an idempotent no-op.  Without this guard the
        # copy-then-delete algorithm upserts the existing ORM row and then
        # deletes that very row, which turns an unchanged filename submission
        # into data loss.
        if old_path == new_path:
            return True

        new_prefix = new_path.rstrip("/") + "/"
        # Plan every (src_row, dst_path) move, validating each destination BEFORE
        # touching anything (so a bad child path aborts the whole rename).
        moves: list[tuple[VfsArtifact, str]] = []
        if exact is not None:
            moves.append((exact, new_path))
        for r in children:
            dst = new_prefix + r.path[len(old_prefix):]
            _validate_artifact_path(dst)
            moves.append((r, dst))

        for r, dst in moves:
            data = self._require_store().fetch_bytes(r.object_key) if r.object_key else b""
            abstract = await unprotect_vfs_abstract(
                self._s,
                tenant_id=str(r.tenant_id),
                kind="artifact",
                resource_id=str(r.scope_id),
                path=r.path,
                abstract=r.abstract,
                ciphertext=r.abstract_ciphertext,
                nonce=r.abstract_nonce,
                key_id=r.abstract_key_id,
            )
            await self.upsert_artifact_bytes(
                wf_id=wf_id, tenant=tenant, path=dst, data=data,
                content_type=r.content_type, abstract=abstract)
        # Delete the originals only after every destination is written.
        for r, _dst in moves:
            if r.object_key and self._os is not None:
                try:
                    self._os.delete_bytes(r.object_key)
                except Exception:
                    pass
            await self._s.delete(r)
        await self._s.flush()
        return True

    async def _read_row(self, *, wf_id, path):
        # Paths describe locations, not storage kinds. Workspace writeback and
        # internal artifacts can persist /memory files as durable artifacts.
        # A durable file takes precedence over a scratch entry at the same path;
        # ls/ls_meta use this same rule.
        row = await self._s.get(VfsArtifact, (wf_id, path))
        if row is not None:
            return row, "artifact"
        return await self._s.get(VfsScratch, (wf_id, path)), "scratch"

    async def read_bytes(self, *, wf_id, path) -> bytes | None:
        r, _ = await self._read_row(wf_id=wf_id, path=path)
        if not r:
            return None
        # Post-unification every row is object-backed (Postgres = pure metadata
        # index); a NULL object_key is corruption, not a legacy text row.
        if not r.object_key:
            return None
        return self._require_store().fetch_bytes(r.object_key)

    async def read(self, *, wf_id, path, touch: bool = True) -> VfsEntry | None:
        r, kind = await self._read_row(wf_id=wf_id, path=path)
        wf_version = r.wf_version if r is not None and kind == "artifact" else None
        if not r:
            return None
        if touch:
            r.last_access = _now()
            await self._s.flush()
        # Every row is object-backed: fetch+decode text; binary stays ""
        # (descriptor). A NULL object_key would be corruption.
        if _is_text_ct(r.content_type):
            content = self._require_store().fetch_bytes(
                r.object_key).decode("utf-8", "replace") if r.object_key else ""
        else:
            content = ""
        abstract = await unprotect_vfs_abstract(
            self._s,
            tenant_id=str(r.tenant_id),
            kind=kind,
            resource_id=str(r.scope_id),
            path=r.path,
            abstract=r.abstract,
            ciphertext=r.abstract_ciphertext,
            nonce=r.abstract_nonce,
            key_id=r.abstract_key_id,
        )
        return VfsEntry(path=path, kind=kind, content=content,
                        content_type=r.content_type, abstract=abstract,
                        size_bytes=r.size_bytes, wf_version=wf_version,
                        last_access=r.last_access.timestamp())

    async def ls(self, *, wf_id, prefix) -> list[VfsEntry]:
        out: list[VfsEntry] = []
        if wf_id:
            for r in (await self._s.execute(
                select(VfsArtifact).where(
                    VfsArtifact.scope_id == wf_id,
                    VfsArtifact.path.like(prefix + "%")))).scalars().all():
                abstract = await unprotect_vfs_abstract(
                    self._s, tenant_id=str(r.tenant_id), kind="artifact",
                    resource_id=str(r.scope_id), path=r.path,
                    abstract=r.abstract, ciphertext=r.abstract_ciphertext,
                    nonce=r.abstract_nonce, key_id=r.abstract_key_id,
                )
                out.append(VfsEntry(r.path, "artifact", "", r.content_type,
                                    abstract, r.size_bytes, r.wf_version,
                                    r.last_access.timestamp()))
            durable_paths = {entry.path for entry in out}
            for r in (await self._s.execute(
                select(VfsScratch).where(
                    VfsScratch.scope_id == wf_id,
                    VfsScratch.path.like(prefix + "%")))).scalars().all():
                if r.path in durable_paths:
                    continue
                abstract = await unprotect_vfs_abstract(
                    self._s, tenant_id=str(r.tenant_id), kind="scratch",
                    resource_id=str(r.scope_id), path=r.path,
                    abstract=r.abstract, ciphertext=r.abstract_ciphertext,
                    nonce=r.abstract_nonce, key_id=r.abstract_key_id,
                )
                out.append(VfsEntry(r.path, "scratch", "", r.content_type,
                                    abstract, r.size_bytes, None,
                                    r.last_access.timestamp()))
        return out

    async def ls_meta(self, *, wf_id, prefix) -> list[VfsEntryMeta]:
        out: list[VfsEntryMeta] = []
        if wf_id:
            rows = (await self._s.execute(
                select(VfsArtifact).options(load_only(
                    VfsArtifact.path, VfsArtifact.content_type, VfsArtifact.abstract,
                    VfsArtifact.abstract_ciphertext, VfsArtifact.abstract_nonce,
                    VfsArtifact.abstract_key_id, VfsArtifact.tenant_id,
                    VfsArtifact.size_bytes, VfsArtifact.wf_version,
                    VfsArtifact.last_access, VfsArtifact.content_revision))
                .where(VfsArtifact.scope_id == wf_id,
                       VfsArtifact.path.like(prefix + "%")))).scalars().all()
            for r in rows:
                abstract = await unprotect_vfs_abstract(
                    self._s, tenant_id=str(r.tenant_id), kind="artifact",
                    resource_id=str(wf_id), path=r.path, abstract=r.abstract,
                    ciphertext=r.abstract_ciphertext, nonce=r.abstract_nonce,
                    key_id=r.abstract_key_id,
                )
                out.append(VfsEntryMeta(r.path, "artifact", r.content_type, abstract,
                                        r.size_bytes, r.wf_version,
                                        r.last_access.timestamp(), r.content_revision))
            durable_paths = {entry.path for entry in out}
            rows = (await self._s.execute(
                select(VfsScratch).options(load_only(
                    VfsScratch.path, VfsScratch.content_type, VfsScratch.abstract,
                    VfsScratch.abstract_ciphertext, VfsScratch.abstract_nonce,
                    VfsScratch.abstract_key_id, VfsScratch.tenant_id,
                    VfsScratch.size_bytes, VfsScratch.last_access))
                .where(VfsScratch.scope_id == wf_id,
                       VfsScratch.path.like(prefix + "%")))).scalars().all()
            for r in rows:
                if r.path in durable_paths:
                    continue
                abstract = await unprotect_vfs_abstract(
                    self._s, tenant_id=str(r.tenant_id), kind="scratch",
                    resource_id=str(wf_id), path=r.path, abstract=r.abstract,
                    ciphertext=r.abstract_ciphertext, nonce=r.abstract_nonce,
                    key_id=r.abstract_key_id,
                )
                out.append(VfsEntryMeta(r.path, "scratch", r.content_type, abstract,
                                        r.size_bytes, None, r.last_access.timestamp()))
        return out

    async def _evict(self, model, scope_col, scope_val) -> None:
        q = select(model).where(scope_col == scope_val)
        rows = (await self._s.execute(
            q.order_by(model.last_access, model.path))).scalars().all()
        total = sum(r.size_bytes for r in rows)
        i = 0
        while (len(rows) - i) > self._max_entries or \
              (total > self._max_bytes and (len(rows) - i) > 1):
            total -= rows[i].size_bytes
            if rows[i].object_key and self._os is not None:
                self._os.delete_bytes(rows[i].object_key)
            await self._s.delete(rows[i])
            i += 1
        if i:
            await self._s.flush()


class VfsStore(Protocol):
    def read(self, *, wf_id, path) -> VfsEntry | None: ...
    # NB: the FACADE (PostgresVfsStore) injects `tenant` from current_sync_tenant_id;
    # callers (ctx.vfs) never pass it. The async VfsRepo methods DO take `tenant`.
    def write_artifact(self, *, wf_id, category, basename, content,
                       content_type="text/plain", wf_version=None, abstract="") -> str: ...
    def write_scratch(self, *, wf_id, path, content,
                      content_type="text/plain", abstract="") -> None: ...
    def write_artifact_bytes(self, *, wf_id, category, basename, data,
                            content_type, wf_version=None, abstract="") -> str: ...
    def write_scratch_bytes(self, *, wf_id, path, data, content_type, abstract="") -> None: ...
    # Explicit-path durable writers — the FACADE injects `tenant`.
    def upsert_artifact(self, *, wf_id, path, content,
                        content_type="text/plain", abstract="") -> bool: ...
    def upsert_artifact_bytes(self, *, wf_id, path, data,
                              content_type, abstract="") -> bool: ...
    def compare_and_swap_artifact_bytes(
        self,
        *,
        wf_id,
        path,
        expected_revision,
        data,
        content_type,
        abstract="",
    ) -> VfsCasResult: ...
    def upsert_internal_artifact(self, *, wf_id, path, content,
                                 content_type="text/plain", abstract="") -> bool: ...
    def upsert_internal_artifact_bytes(self, *, wf_id, path, data,
                                       content_type, abstract="") -> bool: ...
    def read_bytes(self, *, wf_id, path) -> bytes | None: ...
    def delete_artifact(self, *, wf_id, path) -> int: ...
    def rename_artifact(self, *, wf_id, old_path, new_path) -> bool: ...
    def ls(self, *, wf_id, prefix) -> list[VfsEntry]: ...
    def read_prefix_bytes(self, *, wf_id, prefix) -> list[tuple[VfsEntry, bytes]]: ...
    def upsert_artifact_bytes_many(
        self, *, wf_id, items: list[tuple[str, bytes, str]]
    ) -> int: ...


class PostgresVfsStore:
    def __init__(self, *, max_entries_per_scope: int = 64,
                 max_bytes_per_scope: int = 16 * 1024 * 1024):
        self._me = max_entries_per_scope
        self._mb = max_bytes_per_scope

    def _run(self, fn):
        return run_in_short_session(
            lambda s: fn(VfsRepo(s, object_store=get_object_store(),
                                 max_entries_per_scope=self._me,
                                 max_bytes_per_scope=self._mb)))

    def read(self, *, wf_id, path):
        return self._run(lambda r: r.read(wf_id=wf_id, path=path))

    def write_artifact(self, *, wf_id, category, basename, content,
                       content_type="text/plain", wf_version=None, abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.write_artifact(
            wf_id=wf_id, tenant=tenant, category=category, basename=basename,
            content=content, content_type=content_type, wf_version=wf_version,
            abstract=abstract))

    def write_scratch(self, *, wf_id, path, content, content_type="text/plain", abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.write_scratch(
            wf_id=wf_id, tenant=tenant, path=path, content=content,
            content_type=content_type, abstract=abstract))

    def write_artifact_bytes(self, *, wf_id, category, basename, data,
                             content_type, wf_version=None, abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.write_artifact_bytes(
            wf_id=wf_id, tenant=tenant, category=category, basename=basename,
            data=data, content_type=content_type, wf_version=wf_version, abstract=abstract))

    def write_scratch_bytes(self, *, wf_id, path, data, content_type, abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.write_scratch_bytes(
            wf_id=wf_id, tenant=tenant, path=path, data=data,
            content_type=content_type, abstract=abstract))

    def upsert_artifact(self, *, wf_id, path, content,
                        content_type="text/plain", abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.upsert_artifact(
            wf_id=wf_id, tenant=tenant, path=path, content=content,
            content_type=content_type, abstract=abstract))

    def upsert_artifact_bytes(self, *, wf_id, path, data,
                              content_type, abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.upsert_artifact_bytes(
            wf_id=wf_id, tenant=tenant, path=path, data=data,
            content_type=content_type, abstract=abstract))

    def compare_and_swap_artifact_bytes(
        self,
        *,
        wf_id,
        path,
        expected_revision,
        data,
        content_type,
        abstract="",
    ):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.compare_and_swap_artifact_bytes(
            wf_id=wf_id,
            tenant=tenant,
            path=path,
            expected_revision=expected_revision,
            data=data,
            content_type=content_type,
            abstract=abstract,
        ))

    def upsert_internal_artifact(self, *, wf_id, path, content,
                                 content_type="text/plain", abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.upsert_internal_artifact(
            wf_id=wf_id, tenant=tenant, path=path, content=content,
            content_type=content_type, abstract=abstract))

    def upsert_internal_artifact_bytes(self, *, wf_id, path, data,
                                       content_type, abstract=""):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.upsert_internal_artifact_bytes(
            wf_id=wf_id, tenant=tenant, path=path, data=data,
            content_type=content_type, abstract=abstract))

    def read_bytes(self, *, wf_id, path):
        return self._run(lambda r: r.read_bytes(wf_id=wf_id, path=path))

    def delete_artifact(self, *, wf_id, path):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.delete_artifact(wf_id=wf_id, tenant=tenant, path=path))

    def rename_artifact(self, *, wf_id, old_path, new_path):
        tenant = current_sync_tenant_id.get() or ""
        return self._run(lambda r: r.rename_artifact(
            wf_id=wf_id, tenant=tenant, old_path=old_path, new_path=new_path))

    def ls(self, *, wf_id, prefix):
        return self._run(lambda r: r.ls(wf_id=wf_id, prefix=prefix))

    def read_prefix_bytes(self, *, wf_id, prefix):
        async def _read(repo):
            result = []
            for entry in await repo.ls(wf_id=wf_id, prefix=prefix):
                data = await repo.read_bytes(wf_id=wf_id, path=entry.path)
                if data is not None:
                    result.append((entry, data))
            return result

        return self._run(_read)

    def upsert_artifact_bytes_many(self, *, wf_id, items):
        tenant = current_sync_tenant_id.get() or ""

        async def _write(repo):
            for path, data, content_type in items:
                await repo.upsert_artifact_bytes(
                    wf_id=wf_id,
                    tenant=tenant,
                    path=path,
                    data=data,
                    content_type=content_type,
                )
            return len(items)

        return self._run(_write)
