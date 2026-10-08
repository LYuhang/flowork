# -*- coding: utf-8 -*-
"""Build the engine filesystem context for one workflow run.

``/run`` is execution-local scratch space. A real host directory is used as the
sandbox IPC boundary, but it has no workflow-persistent VFS semantics. Durable
cross-run files belong to the user's ``/mount`` namespace.
"""
from __future__ import annotations

import asyncio
import os

import structlog

from vibecanvas_api.services.file_format import content_type_for
from vibecanvas_api.services.workspace_files import WORKSPACE_FOLDERS
from sqlalchemy import delete

from vibecanvas_api.services.object_store import FilesystemObjectStore, get_object_store
from vibecanvas_api.storage.models import VfsRun
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.vfs_run_repo import PostgresVfsRunStore, VfsRunRepo

logger = structlog.get_logger(__name__)

# These are durable Chat workspace bind sources, not execution output.
PERSISTENT_WORKSPACE_FOLDERS = frozenset(WORKSPACE_FOLDERS)

def _guess_ct(rel: str, data: bytes) -> str:
    """Determine the content type of a temporary run output."""
    return content_type_for(rel, data)


def _collect_run_files(run_dir: str, run_id: str) -> list[tuple[str, bytes]]:
    collected: list[tuple[str, bytes]] = []
    for root, directories, files in os.walk(run_dir):
        if os.path.abspath(root) == os.path.abspath(run_dir):
            directories[:] = [name for name in directories if name not in PERSISTENT_WORKSPACE_FOLDERS]
        for name in files:
            file_path = os.path.join(root, name)
            relative_path = os.path.relpath(file_path, run_dir)
            try:
                with open(file_path, "rb") as file:
                    collected.append((relative_path, file.read()))
            except OSError:
                logger.warning(
                    "run_writeback_read_failed",
                    run_id=run_id,
                    rel=relative_path,
                    exc_info=True,
                )
    return collected


async def sync_run_back(run_id: str, tenant_id: str, run_dir: str | None,
                        wf_id: str | None = None) -> int:
    """Persist the run-tier ``/run`` files a workflow run wrote back into the
    ``vfs_run`` metadata table, so the WORKFLOW_SANDBOX Explorer
    (``GET /api/v1/vfs/runs/{run_id}``) can list + open them after a debug run.

    Why this is needed: the in-process execution path runs each node against the
    real ``run_dir`` host directory (``open('/run/<name>')`` resolves there), but
    nothing was flushing those files into ``vfs_run`` — that write-back was the
    deferred RE-6 seam. For the debug-execute experience (``retain=True``) the
    user expects the run folder's files in the Explorer, so we close the seam
    here for the in-process path: walk ``run_dir`` and ``write_bytes`` every file
    into the run-tier repo (idempotent upsert by ``(run_id, path)``).

    Each file's logical path is ``/run/<rel>`` where ``<rel>`` is its path under
    ``run_dir`` (POSIX-normalised). Fail-soft per file — a single unreadable file
    never aborts the rest or the run. Returns the count synced. No-op when
    ``run_dir`` is falsy or absent (a no-/run run)."""
    if not run_dir or not os.path.isdir(run_dir):
        return 0

    collected = await asyncio.to_thread(_collect_run_files, run_dir, run_id)
    if not collected:
        logger.info("run_writeback", run_id=run_id, synced=0)
        return 0

    synced = 0
    async with short_session_scope(tenant_id=tenant_id) as s:
        repo = VfsRunRepo(s, get_object_store(), tenant_id)
        for rel, data in collected:
            vfs_path = "/run/" + rel.replace(os.sep, "/")
            try:
                await repo.write_bytes(
                    run_id=run_id, path=vfs_path, data=data,
                    content_type=_guess_ct(rel, data), wf_id=wf_id,
                    from_materialized=True)
                synced += 1
            except Exception:  # fail-soft per file
                logger.warning("run_writeback_file_failed", run_id=run_id,
                               path=vfs_path, exc_info=True)
    logger.info("run_writeback", run_id=run_id, synced=synced)
    return synced


def sync_run_back_sync(
    run_id: str,
    tenant_id: str,
    run_dir: str | None,
    wf_id: str | None = None,
) -> int:
    """Synchronous run projection for worker threads.

    Uses the NullPool facade instead of creating an event loop around the API's
    shared async engine.
    """
    if not run_dir or not os.path.isdir(run_dir):
        return 0
    collected = _collect_run_files(run_dir, run_id)
    if not collected:
        return 0

    token = current_sync_tenant_id.set(tenant_id)
    try:
        return PostgresVfsRunStore().write_many_sync(
            run_id=run_id,
            wf_id=wf_id,
            from_materialized=True,
            items=[
                (
                    "/run/" + relative_path.replace(os.sep, "/"),
                    data,
                    _guess_ct(relative_path, data),
                )
                for relative_path, data in collected
            ],
        )
    finally:
        current_sync_tenant_id.reset(token)


async def release_run(run_id: str, tenant_id: str, retain: bool = False,
                      keep_run: bool = False) -> None:
    """Release a run's run-tier from an async frame.

    RE-2 C1: ``PostgresVfsRunStore.release_sync`` drives ``asyncio.run`` and
    CRASHES on a running loop, so the async run-end sites (``invoke_sync``,
    ``executions._produce_execution``) must NOT use it. This opens its own
    short tenant-bound ``session_scope`` and awaits ``VfsRunRepo.release``
    directly, staying on the caller's loop. Idempotent (``release`` deletes by
    run_id); ``retain=True`` is a no-op (debug-execute keeps its files for the
    Explorer).

    ``keep_run`` retains the latest run metadata for existing result viewers;
    it does not imply a persistent workflow filesystem."""
    async with short_session_scope(tenant_id=tenant_id) as s:
        await VfsRunRepo(s, get_object_store(), tenant_id).release(
            run_id=run_id, retain=retain or keep_run)


async def purge_prior_workflow_runs(wf_id: str, tenant_id: str,
                                    except_run_id: str) -> int:
    """UX-10e0 "keep latest run per workflow" — at a new run's START, purge the
    PREVIOUS run's ``/run`` rows+blobs for this workflow so only the current run
    survives. Opens its own short tenant-bound ``session_scope`` (the producer
    outlives the request session) and delegates to
    ``VfsRunRepo.purge_workflow_runs`` (deletes rows where ``wf_id == wf_id`` AND
    ``run_id != except_run_id``, then best-effort deletes their blob prefixes).

    Fail-soft: a purge failure (DB/blob) MUST NOT block the run — the caller logs
    + continues. No-op when ``wf_id`` is falsy. Returns the count of run_ids
    purged (0 on a swallowed failure)."""
    if not wf_id:
        return 0
    async with short_session_scope(tenant_id=tenant_id) as s:
        return await VfsRunRepo(s, get_object_store(), tenant_id).purge_workflow_runs(
            wf_id=wf_id, except_run_id=except_run_id)


async def clear_run_contents(run_id: str, tenant_id: str) -> str | None:
    """Clear a fixed run VFS without changing the run identity.

    Interactive node/workflow execution reuses ``run_id == wf_id`` so the
    workflow has one stable ``/run`` mount. Before those interactive runs, clear
    the run rows and host files in place. Batch/background execution can skip
    this helper so concurrent rows/jobs do not erase each other.

    Returns the host run directory when the object store can materialize one.
    """
    async with short_session_scope(tenant_id=tenant_id) as s:
        await s.execute(delete(VfsRun).where(VfsRun.run_id == run_id))
        await s.flush()

    store = get_object_store()
    prefix = f"run/{tenant_id}/{run_id}/"
    if isinstance(store, FilesystemObjectStore):
        run_dir = store.materialize_prefix(prefix)
        # Clear the durable encrypted projection immediately, while preserving
        # the stable host directory already bound into a resident sandbox.
        store.delete_persisted_prefix(prefix)

        def _clear_path(path: str) -> None:
            if os.path.isdir(path) and not os.path.islink(path):
                for child in os.listdir(path):
                    _clear_path(os.path.join(path, child))
                try:
                    os.rmdir(path)
                except OSError:
                    pass
            else:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

        def _clear_dir() -> None:
            os.makedirs(run_dir, exist_ok=True)
            for name in os.listdir(run_dir):
                path = os.path.join(run_dir, name)
                # Preserve both the live bind inode AND its durable workspace
                # contents. Starting a run must not erase a concurrent Chat.
                if name in PERSISTENT_WORKSPACE_FOLDERS and os.path.isdir(path):
                    continue
                _clear_path(path)

        await asyncio.to_thread(_clear_dir)
        return run_dir

    store.delete_prefix(prefix)
    try:
        return store.materialize_prefix(prefix)
    except NotImplementedError:
        return None


def build_run_context(run_id: str, tenant_id: str) -> dict:
    """Return ``{"run_id", "run_dir"}`` for one run.

    ``run_dir`` is a real host directory rooted at ``run/{tenant_id}/{run_id}``
    in the object store (degrades to ``None`` under ``InMemoryObjectStore`` — the
    sandbox/test default — so a /run-only workflow still runs; one that touches a
    /run file fails loudly at the path, not here).

    NOTE — object-store materialization can block; async callers must wrap the
    call in ``asyncio.to_thread``."""
    try:
        run_dir = get_object_store().materialize_prefix(f"run/{tenant_id}/{run_id}")
    except NotImplementedError:
        logger.debug("run_dir_unavailable", run_id=run_id, reason="store cannot materialize")
        run_dir = None
    return {"run_id": run_id, "run_dir": run_dir}
