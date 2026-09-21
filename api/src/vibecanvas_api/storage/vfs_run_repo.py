# -*- coding: utf-8 -*-
"""RE-1 — the run-scope VFS tier repo. Bytes in the ObjectStore (key
run/{tenant}/{run_id}/<path-after-/run/>); metadata in vfs_run. Keyed by an
explicit run_id (A0). Path validated store-independently (F0)."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vibecanvas_api.security.vfs_protection import protect_vfs_abstract
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.models import VfsRun
from vibecanvas_api.storage.sync_session import (
    current_sync_tenant_id, run_in_short_session,
)

_BAD = re.compile(r"(^|/)\.\.(/|$)")


def _validate(path: str) -> str:
    if not path.startswith("/run/") or _BAD.search(path) or "\x00" in path:
        raise ValueError(f"invalid run path: {path!r}")
    return path


@dataclass(slots=True)
class VfsRunEntry:
    path: str
    object_key: str
    content_type: str
    size_bytes: int


class VfsRunRepo:
    def __init__(self, session, object_store, tenant_id: str):
        self._s = session
        self._os = object_store
        self._t = str(tenant_id)

    def _key(self, run_id: str, path: str) -> str:
        # logical /run/n1/a.txt -> key run/{tenant}/{run_id}/n1/a.txt (strip /run/)
        rel = path[len("/run/"):]
        return f"run/{self._t}/{run_id}/{rel}"

    async def write_bytes(self, *, run_id: str, path: str, data: bytes,
                          content_type: str = "application/octet-stream", abstract: str = "",
                          wf_id: str | None = None) -> str:
        _validate(path)
        key = self._key(run_id, path)
        content_revision = str(uuid.uuid4())
        self._os.put_bytes(key, data, content_type)
        abstract_values = await protect_vfs_abstract(
            self._s,
            tenant_id=self._t,
            kind="run",
            resource_id=run_id,
            path=path,
            abstract=abstract,
        )
        await self._s.execute(pg_insert(VfsRun).values(
            run_id=run_id, path=path, object_key=key, content_type=content_type,
            size_bytes=len(data), **abstract_values, wf_id=wf_id,
            content_revision=content_revision,
        ).on_conflict_do_update(index_elements=["run_id", "path"],
            set_=dict(object_key=key, content_type=content_type, size_bytes=len(data),
                      **abstract_values, wf_id=wf_id,
                      content_revision=content_revision)))
        await self._s.flush()
        return path

    async def read_bytes(self, *, run_id: str, path: str) -> bytes:
        _validate(path)
        row = await self._s.get(VfsRun, (run_id, path))
        if row is None:
            raise KeyError(path)
        return self._os.fetch_bytes(row.object_key)

    async def read(self, *, run_id: str, path: str) -> VfsRunEntry | None:
        _validate(path)
        row = await self._s.get(VfsRun, (run_id, path))
        if row is None:
            return None
        return VfsRunEntry(row.path, row.object_key, row.content_type, row.size_bytes)

    async def ls(self, *, run_id: str, prefix: str = "/run/") -> list[VfsRunEntry]:
        rows = (await self._s.execute(select(VfsRun).where(
            VfsRun.run_id == run_id, VfsRun.path.like(prefix + "%")))).scalars().all()
        return [VfsRunEntry(r.path, r.object_key, r.content_type, r.size_bytes) for r in rows]

    async def release(self, *, run_id: str, retain: bool = False) -> None:
        """Auto-release at execution end (E0). Idempotent + crash-tolerant: delete
        the metadata rows first (RLS-bound), then best-effort delete the bytes (a
        crash between leaves at most orphaned blobs — an orphan sweeper is a
        follow-up). retain=True keeps everything (debug-execute)."""
        if retain:
            return
        await self._s.execute(delete(VfsRun).where(VfsRun.run_id == run_id))
        await self._s.flush()
        self._os.delete_prefix(f"run/{self._t}/{run_id}/")   # best-effort (swallows by contract)

    async def purge_workflow_runs(self, *, wf_id: str, except_run_id: str | None = None) -> int:
        """UX-10e0 "keep latest run per workflow": delete this workflow's prior
        ``vfs_run`` rows (+ best-effort ObjectStore blobs) so only the CURRENT
        run's ``/run`` survives. Deletes every row with ``wf_id == wf_id`` AND
        ``run_id != except_run_id`` (the current run is never purged; concurrent
        same-wf runs are out of scope — builder runs one at a time).

        Tenant-scoped via the repo's RLS-bound session (the FOR-ALL policy filters
        on ``app.tenant_id``). Idempotent + crash-tolerant, mirroring ``release``:
        find the distinct other run_ids FIRST, delete the metadata rows
        (RLS-bound), then best-effort delete each run's blob prefix (a crash
        between leaves at most orphaned blobs). Returns the count of run_ids
        purged. A falsy ``wf_id`` is a no-op (a /run-only run has nothing to
        scope-purge)."""
        if not wf_id:
            return 0
        criterion = VfsRun.wf_id == wf_id
        if except_run_id is not None:
            criterion = criterion & (VfsRun.run_id != except_run_id)
        other_runs = (await self._s.execute(
            select(VfsRun.run_id).distinct().where(criterion)
        )).scalars().all()
        if not other_runs:
            return 0
        await self._s.execute(delete(VfsRun).where(criterion))
        await self._s.flush()
        for rid in other_runs:
            self._os.delete_prefix(f"run/{self._t}/{rid}/")   # best-effort
        return len(other_runs)

    def materialize(self, *, run_id: str) -> str:
        """Real host dir mirroring the run's files (RE-6 mounts it). Sync, on the
        executing worker. FS = zero-copy; S3 = sync-down; InMemory = raises."""
        return self._os.materialize_prefix(f"run/{self._t}/{run_id}/")

    def release_materialized(self, *, run_id: str, local_dir: str, persist=None) -> None:
        """RE-1 builds the seam; NO producer writes into local_dir until RE-6's
        shell, so this is cleanup-only here (the real sync-back lands with RE-6).
        For the FS store local_dir IS the store subtree — do NOT delete it (release()
        owns byte deletion); for an S3 sync-down temp dir, remove it."""
        # RE-1: no-op cleanup placeholder; RE-6 adds sync-back + S3 temp-dir removal.
        return None


class PostgresVfsRunStore:
    """Sync facade over :class:`VfsRunRepo` (mirrors ``PostgresVfsStore``).

    Each method opens ONE short NullPool session via ``run_in_short_session``
    (which sets ``app.tenant_id`` from ``current_sync_tenant_id`` for RLS) and
    builds the repo with the configured ObjectStore + the current sync tenant.

    A0: no production agent path writes /run yet — this facade is reached from
    the agent tools via an injected ``ctx.vfs_run`` + ``ctx.run_id``. Wiring it
    onto the live run context is a downstream task (RE-6).
    """

    def _repo(self, s) -> "VfsRunRepo":
        return VfsRunRepo(s, get_object_store(),
                          current_sync_tenant_id.get() or "")

    def write_bytes_sync(self, *, run_id, path, data,
                         content_type="application/octet-stream", abstract="",
                         wf_id=None):
        return run_in_short_session(lambda s: self._repo(s).write_bytes(
            run_id=run_id, path=path, data=data,
            content_type=content_type, abstract=abstract, wf_id=wf_id))

    def write_many_sync(self, *, run_id, items, wf_id=None):
        async def _write(session):
            repo = self._repo(session)
            for path, data, content_type in items:
                await repo.write_bytes(
                    run_id=run_id,
                    path=path,
                    data=data,
                    content_type=content_type,
                    wf_id=wf_id,
                )
            return len(items)

        return run_in_short_session(_write)

    def read_sync(self, *, run_id, path):
        return run_in_short_session(
            lambda s: self._repo(s).read(run_id=run_id, path=path))

    def read_bytes_sync(self, *, run_id, path):
        return run_in_short_session(
            lambda s: self._repo(s).read_bytes(run_id=run_id, path=path))

    def ls_sync(self, *, run_id, prefix="/run/"):
        return run_in_short_session(
            lambda s: self._repo(s).ls(run_id=run_id, prefix=prefix))

    def release_sync(self, *, run_id, retain=False):
        """Auto-release the run's tier (E0) from a genuinely-SYNC frame.

        RE-2 C1: this drives ``run_in_short_session`` → ``asyncio.run``, which
        crashes if called from a running event loop. So it is ONLY legal at the
        two genuinely-sync run-end sites (``run_workflow_sync`` after its own
        ``asyncio.run`` returns, and the background worker ``deployment_invoke`` sync shell
        around ``asyncio.run(_run(...))``). The async sites (``invoke_sync``,
        ``executions._produce_execution``) must use the async ``release`` path
        (see ``services.vfs_run_context.release_run``)."""
        return run_in_short_session(lambda s: self._repo(s).release(
            run_id=run_id, retain=retain))
