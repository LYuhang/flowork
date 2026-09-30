# -*- coding: utf-8 -*-
"""Per-node run results in the VFS run-tier (``/run/__exec__/nodes/{node_id}.json``).

Each node-run's ``{inputs, output, status, error}`` is persisted as a small JSON
file under the EXECUTING run's run-tier so the Run-node sider, the human Explorer,
and the coding-agent's file tools all read ONE substrate (and a bulky tool output
can be compacted to a reference to its path). See
``docs/superpowers/specs/2026-06-15-run-results-in-vfs-design.md``.

A results-write failure MUST NOT break a run, so ``write_node_result`` is
FAIL-SOFT (logs a warning, never raises).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import structlog

from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.vfs_run_repo import PostgresVfsRunStore, VfsRunRepo

logger = structlog.get_logger(__name__)

# Filename-safe node ids: a normal ``node_3`` OR a reserved channel like
# ``__end__`` / ``__start__``. Anything else (path separators, ``..``, etc.) is
# rejected so it can never escape ``/run/__exec__/nodes/``.
_NODE_ID = re.compile(r"^node_\d+$|^__\w+__$")

# Frame statuses that are TERMINAL (worth persisting). A ``running`` frame is
# skipped — only the final result of a node-run becomes a file.
_TERMINAL_STATUSES = {"completed", "error", "cancelled"}


def node_result_path(node_id: str) -> str:
    """The per-node result VFS path (default ``/run/__exec__/nodes/{node_id}.json``;
    the ``run_prefix`` + subdir come from ``config.vfs_paths``) for a filename-safe
    ``node_id``.

    Raises ``ValueError`` on an id that doesn't match ``^node_\\d+$`` or a
    reserved ``^__\\w+__$`` channel (e.g. ``__end__``)."""
    if not isinstance(node_id, str) or not _NODE_ID.match(node_id):
        raise ValueError(f"invalid node_id for result path: {node_id!r}")
    from vibecanvas_api.config import config
    return config.vfs_paths.node_result_path(node_id)


def build_node_payload(
    *,
    node_id: str,
    node_name: str | None = None,
    node_type: str | None = None,
    status: str,
    inputs=None,
    output=None,
    error: str | None = None,
    execution_time: float | None = None,
    ts: str | None = None,
    resource_audit: dict | None = None,
) -> dict:
    """The per-node result JSON dict (spec shape). ``ts`` is stamped with the
    current UTC ISO timestamp when not supplied (plain ``datetime`` is fine in
    the api layer — the engine forbids nondeterministic time, the host writer
    does not)."""
    return {
        "node_id": node_id,
        "node_name": node_name,
        "node_type": node_type,
        "status": status,
        "inputs": inputs,
        "output": output,
        "error": error,
        "execution_time": execution_time,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        **({'resource_audit': resource_audit} if resource_audit is not None else {}),
    }


def persist_node_frame_payload(frame: dict) -> dict | None:
    """Map a frontend EXEC_UPDATE-style frame → a ``build_node_payload`` dict.

    The frame is the per-node frame ``exec_events.to_exec_update`` produces:
    ``{node_id, status, inputs?, result?(JSON string), error?, duration?,
    node_name?, node_type?}``. ``result`` is a JSON STRING → parsed into
    ``output`` (on parse failure the raw string is kept as the output).

    Returns ``None`` when there is no ``node_id`` or the status is not terminal
    (e.g. ``running``) — only terminal frames produce a durable file."""
    node_id = frame.get("node_id")
    status = frame.get("status")
    if not node_id or status not in _TERMINAL_STATUSES:
        return None

    output = None
    raw = frame.get("result")
    if raw is not None:
        try:
            output = json.loads(raw)
        except (TypeError, ValueError):
            output = raw  # keep the raw string if it isn't valid JSON

    return build_node_payload(
        node_id=node_id,
        node_name=frame.get("node_name"),
        node_type=frame.get("node_type"),
        status=status,
        inputs=frame.get("inputs"),
        output=output,
        error=frame.get("error"),
        execution_time=frame.get("duration"),
        resource_audit=frame.get('resource_audit'),
    )


async def write_node_result(run_id: str, tenant_id: str, payload: dict) -> None:
    """Write ``payload`` to ``/run/__exec__/nodes/{node_id}.json`` in ``run_id``'s
    run-tier. FAIL-SOFT: a write failure logs a warning and NEVER raises (a
    results-write must not break a run). Opens its own short tenant-bound
    ``session_scope`` (the producer outlives the request session)."""
    try:
        path = node_result_path(payload["node_id"])
        data = json.dumps(payload, default=str, ensure_ascii=False).encode()
        async with session_scope(tenant_id=tenant_id) as s:
            repo = VfsRunRepo(s, get_object_store(), tenant_id)
            await repo.write_bytes(
                run_id=run_id, path=path, data=data,
                content_type="application/json")
    except Exception:  # fail-soft — a results-write failure must not break a run
        logger.warning(
            "node_result_write_failed", run_id=run_id,
            node_id=(payload or {}).get("node_id"), exc_info=True)


async def persist_node_debug_result(
    wf_id: str, tenant_id: str, terminal_frame: dict,
) -> str | None:
    """Persist a single-node DEBUG run's terminal frame into fixed workflow /run.

    Node and workflow execution share ``run_id == wf_id``. A node-debug run
    overwrites only ``nodes/{node_id}.json``; every other node file is untouched.
    """
    payload = persist_node_frame_payload(terminal_frame)
    if payload is None:
        return None
    await write_node_result(wf_id, tenant_id, payload)
    return wf_id


def write_node_result_sync(run_id: str, payload: dict) -> None:
    """Sync counterpart of :func:`write_node_result` for genuinely-SYNC callers
    (e.g. ``canvas_tools._sync_run_workflow``, which runs under
    ``asyncio.to_thread`` and CANNOT ``await``). Writes via the
    :class:`PostgresVfsRunStore` sync facade, which opens its own short
    NullPool session and reads the tenant from ``current_sync_tenant_id`` (set
    by the agent turn) for RLS. FAIL-SOFT: a write failure logs + never raises."""
    try:
        path = node_result_path(payload["node_id"])
        data = json.dumps(payload, default=str, ensure_ascii=False).encode()
        PostgresVfsRunStore().write_bytes_sync(
            run_id=run_id, path=path, data=data,
            content_type="application/json")
    except Exception:  # fail-soft — a results-write failure must not break a run
        logger.warning(
            "node_result_write_sync_failed", run_id=run_id,
            node_id=(payload or {}).get("node_id"), exc_info=True)


async def read_node_result(run_id: str, tenant_id: str, node_id: str) -> dict | None:
    """Read + parse ``/run/__exec__/nodes/{node_id}.json`` from ``run_id``'s
    run-tier. Returns ``None`` when the file is absent or unparseable."""
    try:
        path = node_result_path(node_id)
        async with session_scope(tenant_id=tenant_id) as s:
            repo = VfsRunRepo(s, get_object_store(), tenant_id)
            data = await repo.read_bytes(run_id=run_id, path=path)
        return json.loads(data)
    except (KeyError, FileNotFoundError, ValueError, TypeError):
        return None
