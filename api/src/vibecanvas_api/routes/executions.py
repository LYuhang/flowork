"""Workflow execution endpoints.

Interactive workflow-page execution has a small DB control plane
(``workflow_run_state`` + ``workflow_run_events``) and a VFS data plane
(``/run/__exec__/nodes/{node_id}.json``). Agent tools do not write this DB
state; batch execution is tracked by the task system.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from vibecanvas_engine import Workflow

from ..auth.deps import AuthContext, current_user
from ..authorization.dependencies import (
    authorize_resource,
    get_authz_service,
)
from ..authorization.service import AuthzService
from ..authorization.stream_guard import authorization_lease_is_valid
from ..authorization.types import Action, ResourceRef, ResourceType
from ..schemas.execution import (
    ExecutionListItem, ExecutionRequest, ExecutionStatusOut,
    NodeExecutionRequest, ResumeCheckRequest,
)
from ..services.node_results import (
    persist_node_frame_payload, write_node_result,
)
from ..schemas.pagination import Page, PageRequest
from ..services.vfs_run_context import clear_run_contents
from ..services.workflow_sandbox_runner import (
    prepare_code_pythonpath,
)
from ..services.workflow_execution_history import create_execution
from ..services.workflow_retry import latest_workflow_run, retry_reason
from ..services.workflow_history_runner import execute_history_workflow, stream_history_workflow
from ..storage.workflow_history_repo import WorkflowHistoryRepo, TERMINAL_STATUSES
from ..services.llm_credentials_inject import inject_into_run_context_async
from ..services.exec_events import to_exec_update
from ..services.sandbox.manager import get_sandbox_manager
from ..services.sandbox.workflow_guard import classify_workflow
from ..services.sandbox.egress_policy import compute_allow_hosts_async
from ..services.sandbox.gvisor import EngineNeedsHostNode
from ..storage import stop_registry
from ..storage.db import session_scope
from ..storage.execution_repo import ExecutionRepo
from ..storage.sync_session import current_sync_tenant_id
from ..storage.workflow_repo import WorkflowRepo
from ..streaming.sse import format_event
from ..streaming.turn_runtime import (
    TURN_BUFFERS, TURN_TASKS, register_turn, request_cancel,
    run_turn,
)
from .deps import get_execution_repo, get_workflow_repo

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["executions"])
_LIVE_EXEC_PAYLOAD_MAX_CHARS = 8_000

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _authorize_workflow_action(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    wf_id: str,
    action: Action,
):
    return await authorize_resource(
        request=request,
        auth=auth,
        service=service,
        resource=ResourceRef(
            ResourceType.WORKFLOW,
            wf_id,
            auth.active_organization_id,
        ),
        action=action,
    )


async def _authorize_execution_action(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    exec_id: str,
    action: Action,
):
    return await authorize_resource(
        request=request,
        auth=auth,
        service=service,
        resource=ResourceRef(
            ResourceType.WORKFLOW_EXECUTION,
            exec_id,
            auth.active_organization_id,
        ),
        action=action,
    )


async def _sse_from_turn(
    turn_key: str,
    *,
    authorization_guard: Callable[[], Awaitable[bool]] | None = None,
):
    """Subscribe to a workflow execution buffer and yield SSE-encoded bytes.

    The SSE pumper is inlined here; ``run_turn`` remains the
    producer that fences the stream with ``started`` / ``done`` /
    ``error``. The route is the consumer.
    """
    buf = TURN_BUFFERS.get(turn_key)
    if buf is None:
        return
    next_authorization_check = 0.0
    async for seq, event in buf.subscribe_with_ids(15.0, after_seq=0):
        now = asyncio.get_running_loop().time()
        if authorization_guard is not None and now >= next_authorization_check:
            if not await authorization_guard():
                return
            next_authorization_check = now + 5.0
        event_name, payload = event
        if event_name == "EXEC_UPDATE":
            logger.warning(
                "workflow_execution_sse_send",
                turn_key=turn_key,
                seq=seq,
                wf_id=(
                    payload.get("wf_id") if isinstance(payload, dict) else None
                ),
                node_id=(
                    payload.get("node_id") if isinstance(payload, dict) else None
                ),
                status=(
                    payload.get("status") if isinstance(payload, dict) else None
                ),
            )
        elif event_name in {"started", "done", "error"}:
            logger.warning(
                "workflow_execution_sse_control_send",
                turn_key=turn_key,
                seq=seq,
                event_name=event_name,
            )
        yield format_event(event_name, payload, event_id=seq)


# DB storage vocabulary is ``running|success|error|stopped|pending``;
# the API/frontend vocabulary is ``running|completed|stopped|error``.
_DB_STATUS_TO_API = {
    "success": "completed",
    "pending": "running",
    "running": "running",
    "completed": "completed",
    "stopped": "stopped",
    "error": "error",
}


def _record_to_status(record: dict) -> ExecutionStatusOut:
    db_status = record.get("status", "running")
    return ExecutionStatusOut(
        exec_id=record["exec_id"],
        history_id=record.get("history_id"),
        wf_id=record.get("wf_id", ""),
        status=_DB_STATUS_TO_API.get(db_status, "running"),
        started_at=record.get("started_at") or 0.0,
        finished_at=record.get("ended_at"),
        result=record.get("per_node"),
        error=record.get("error"),
    )


def _record_to_list_item(record: dict) -> ExecutionListItem:
    return ExecutionListItem(
        exec_id=record["exec_id"],
        wf_id=record.get("wf_id", ""),
        status=_DB_STATUS_TO_API.get(record.get("status", "running"), "running"),
        started_at=record.get("started_at") or 0.0,
        finished_at=record.get("ended_at"),
    )


async def _with_execution_repo(tenant_id: str, user_id: str, fn):
    """Run one execution-state write in its own tenant-bound transaction."""
    async with session_scope(tenant_id=tenant_id) as s:
        return await fn(ExecutionRepo(s, user_id))


async def _fail_history(tenant_id: str, exec_id: str):
    async with session_scope(tenant_id=tenant_id) as session:
        repo = WorkflowHistoryRepo(session)
        if await repo.get(exec_id) is not None:
            await repo.fail(exec_id, error_code="execution_dispatch_failed")


def _history_id(exec_id: str) -> str | None:
    try:
        return str(uuid.UUID(exec_id))
    except (ValueError, TypeError):
        return None


async def _history_status(record, session):
    """Only advertise a history link after its admission transaction commits."""
    history_id = _history_id(record["exec_id"])
    history = await WorkflowHistoryRepo(session).get(history_id) if history_id else None
    if history is not None:
        record = {**record, "history_id": history_id}
        record["status"] = {
            "succeeded": "success", "failed": "error", "timed_out": "error", "cancelled": "stopped",
        }.get(history["status"], "running")
    return _record_to_status(record)


async def _request_history_cancel(exec_repo, exec_id: str):
    history_id = _history_id(exec_id)
    if history_id is not None:
        repo = WorkflowHistoryRepo(exec_repo._session)
        run = await repo.get(history_id)
        if run is not None and run["status"] not in TERMINAL_STATUSES:
            await repo.request_cancel(history_id)


async def _reject_live_execution(tenant_id: str, wf_id: str, record):
    active = TURN_TASKS.get(record["exec_id"]) or TURN_TASKS.get(wf_id)
    if active is not None and not active.done():
        raise HTTPException(409, f"workflow {wf_id} already has a running execution")
    history_id = _history_id(record["exec_id"])
    if history_id is not None:
        async with session_scope(tenant_id=tenant_id) as session:
            run = await WorkflowHistoryRepo(session).get(history_id)
            if run is not None and run["status"] not in TERMINAL_STATUSES:
                raise HTTPException(409, f"workflow {wf_id} already has a running execution")


def _stringify_engine_error(value) -> "str | None":
    """Coerce an ``error_dict`` value to the VARCHAR ``error`` column's str.

    A NODE error during a run yields a rich dict ({error_message, status, output,
    args, kwargs:{extra:{...id2node...}}}); an engine crash yields a plain string.
    Prefer the dict's ``error_message``; fall back to a bounded json dump; pass
    strings through. ``None`` stays ``None`` (no error)."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        msg = value.get("error_message")
        if isinstance(msg, str) and msg:
            return msg
        return json.dumps(value, default=str)[:2000]
    return str(value)


def _accumulate_per_node(per_node: dict[str, dict], payload: dict) -> None:
    """Coalesce ONE mapped EXEC_UPDATE payload into the per-node accumulator (C1).

    Shared by the live stream and status endpoint so both expose the same
    ``per_node`` shape (``{status, execution_result, error, inputs}``).
    A later frame (completed/error) overwrites the earlier ``running``
    for the same node. ``inputs`` is round-tripped through ``json.dumps(default=
    str)`` so a non-JSON-native resolved input can't crash the asyncpg write at
    terminal (mirrors M3 / the mapper's ``result`` handling)."""
    node_id = payload.get("node_id")
    if not node_id:
        return
    slot = dict(per_node.get(node_id) or {})
    slot["status"] = payload.get("status", slot.get("status"))
    if "result" in payload:
        slot["execution_result"] = payload["result"]
    if "error" in payload:
        slot["error"] = payload["error"]
    if payload.get("duration") is not None:
        # Per-node wall-clock seconds (the mapper folds the engine's
        # ``execution_time`` here). Persist it so a reloaded run / GET
        # /executions/{id} can show the node's duration too, not just live.
        slot["duration"] = payload["duration"]
    if payload.get("inputs") is not None:
        slot["inputs"] = json.loads(json.dumps(payload["inputs"], default=str))
    per_node[node_id] = slot


def _workflow_public_payload(payload: dict, wf_id: str) -> dict:
    """Workflow SSE payloads are workflow-scoped; hide internal row ids."""
    out = {k: v for k, v in payload.items() if k != "exec_id"}
    if payload.get("exec_id"):
        out["history_id"] = payload["exec_id"]
    out["wf_id"] = wf_id
    return out


def _workflow_live_payload(payload: dict) -> dict:
    """Keep workflow-page SSE/DB state as a progress stream, not a data plane.

    Full node inputs/outputs are persisted to ``/run/__exec__/nodes`` before a
    frame is emitted. Sending the same large StartNode list/object through SSE
    can stall the browser/proxy stream after the ``running`` frame, while the
    backend keeps executing correctly. The live channel therefore carries
    status, timing, errors, and small previews only; durable detail stays in VFS.
    """
    out = dict(payload)
    if "inputs" in out:
        out["inputs_omitted"] = True
        out.pop("inputs", None)
    for key in ("result", "outputs"):
        if key not in out:
            continue
        try:
            size = len(json.dumps(out[key], ensure_ascii=False, default=str))
        except Exception:
            size = len(str(out[key]))
        if size > _LIVE_EXEC_PAYLOAD_MAX_CHARS:
            out.pop(key, None)
            out[f"{key}_omitted"] = True
            out[f"{key}_chars"] = size
    return out


def _valid_workflow_node_ids(wf_dict: dict) -> set[str]:
    return {
        node_id
        for node_id, node in (wf_dict or {}).items()
        if node_id != "__meta__" and isinstance(node_id, str)
        and isinstance(node, dict)
    }


def _normalize_node_event(wf_dict: dict, ev: dict) -> dict:
    """Return a node event whose progress key is a canonical workflow node_id.

    Workflow execution progress is keyed ONLY by ``node_id``. ``node_name`` is
    display metadata and can repeat legitimately (for example multiple EndNode
    objects all use ``__end__``), so it must never be used to merge frontend
    state. If the event has no valid node_id, leave it unchanged so the mapper
    either treats it as engine-level terminal error or drops it.
    """
    node_id = ev.get("node_id")
    if isinstance(node_id, str) and node_id in _valid_workflow_node_ids(wf_dict):
        return ev
    return ev


def _terminal_node_updates_from_result(
    wf_dict: dict,
    final_outputs: dict,
    error_dict: dict,
    exec_id: str,
    per_node: dict[str, dict],
) -> list[dict]:
    """Backfill terminal per-node frames from the authoritative result bundle.

    Live sandbox streaming tails ``events.ndjson`` while the worker runs. A fast
    or buffered run can still finish with a complete ``result.json`` while some
    per-node event frames were not observed by the HTTP client. The terminal
    result is authoritative, so before emitting the workflow-level completed
    frame, synthesize any missing completed/error node frames from
    ``final_outputs`` and ``error_dict``. Only true node-id keys are accepted:
    node names can repeat, so they are not safe execution-progress keys.
    """
    updates: list[dict] = []

    def _seen_terminal(node_id: str) -> bool:
        return per_node.get(node_id, {}).get("status") in {"completed", "error"}

    if isinstance(final_outputs, dict):
        for key, output in final_outputs.items():
            node_id = str(key)
            if node_id not in wf_dict or _seen_terminal(node_id):
                continue
            updates.append({
                "exec_id": exec_id,
                "node_id": node_id,
                "node_name": wf_dict[node_id].get("node_name"),
                "node_type": wf_dict[node_id].get("node_type"),
                "status": "completed",
                "result": json.dumps(output, default=str, ensure_ascii=False),
            })

    if isinstance(error_dict, dict):
        for key, raw in error_dict.items():
            node_id = str(key)
            if node_id not in wf_dict or _seen_terminal(node_id):
                continue
            updates.append({
                "exec_id": exec_id,
                "node_id": node_id,
                "node_name": wf_dict[node_id].get("node_name"),
                "node_type": wf_dict[node_id].get("node_type"),
                "status": "error",
                "error": _stringify_engine_error(raw) or "error",
            })
    return updates


async def _persist_node_progress(
    exec_id: str,
    creator_user_id: str,
    tenant_id: str,
    payload: dict,
) -> None:
    node_id = payload.get("node_id")
    if not node_id:
        return
    await _with_execution_repo(
        tenant_id,
        creator_user_id,
        lambda repo: repo.update_node_execution(
            exec_id,
            node_id,
            status=payload.get("status"),
            inputs=payload.get("inputs") if "inputs" in payload else None,
            result_overwrite=(
                payload.get("result") if isinstance(payload.get("result"), str) else None
            ),
            error=payload.get("error") if isinstance(payload.get("error"), str) else None,
            duration=(
                payload.get("duration")
                if isinstance(payload.get("duration"), (int, float))
                else None
            ),
        ),
    )


async def _produce_execution_sandbox(
    stop: asyncio.Event, wf_id: str, exec_id: str, body: ExecutionRequest,
    wf_dict: dict, creator_user_id: str, tenant_id: str,
) -> AsyncIterator[tuple[str, dict]]:
    """Run a workflow execution through the workflow's resident sandbox session.

    Each execution has an isolated artifact directory and immutable history.
    Ordinary runs clear the workflow's debug projection; explicit retries also
    hydrate the new slot from the authorized previous execution's artifacts.
    """
    # Validate buildability on the host FIRST (a malformed wf shouldn't reach the
    # sandbox launch) — mirrors the in-process ``Workflow(...)`` construction.
    Workflow(wf_dict, max_workers=4)
    per_node: dict[str, dict] = {}
    engine_error: str | None = None
    result_seen = False
    finalized = False
    total_started = time.perf_counter()
    current_sync_tenant_id.set(tenant_id)
    try:
        await create_execution(
            execution_id=exec_id, tenant_id=tenant_id, source_type="workflow", source_id=wf_id,
            user_id=creator_user_id, workflow_id=wf_id, workflow=wf_dict, inputs=body.input,
        )
        workflow_run_id = wf_id
        stage_started = time.perf_counter()
        session = await get_sandbox_manager().get_session(
            tenant_id,
            workflow_run_id,
            user_id=creator_user_id,
            expose_run=True,
            expose_runtime=True,
        )
        # The sandbox service deliberately keeps daemon host paths private.
        # Remote sessions therefore expose only the logical workflow run id;
        # staging and mount resolution happen inside sandboxd over the session
        # RPC.  Do not couple an API worker to sandboxd's local filesystem.
        clear_owned_run = getattr(session, "clear_workflow_run", None)
        if body.resume_from is None:
            if clear_owned_run is not None:
                await clear_owned_run()
            else:
                await clear_run_contents(workflow_run_id, tenant_id)
        logger.warning(
            "workflow_execution_stage",
            stage="workflow_run_prepare_done",
            wf_id=wf_id,
            exec_id=exec_id,
            workflow_run_id=workflow_run_id,
            clear_run=body.resume_from is None,
            elapsed_ms=int((time.perf_counter() - stage_started) * 1000),
        )
        # Inject the saved-credential mapping into the fixed workflow run tier so
        # the in-sandbox engine sees ``extra['llm_credentials']``.
        stage_started = time.perf_counter()
        _cred_rc = await inject_into_run_context_async(
            {},
            wf_dict,
            tenant_id,
            user_id=creator_user_id,
            workflow_id=wf_id,
            execution_id=exec_id,
            execution_resource_type=ResourceType.WORKFLOW_EXECUTION.value,
        )
        from vibecanvas_api.services.workflow_resources import prepare_execution_resources
        resources = await prepare_execution_resources(
            sandbox_session=session, workflow=wf_dict, tenant_id=tenant_id,
            user_id=creator_user_id, workflow_id=wf_id, execution_id=exec_id,
            execution_resource_type=ResourceType.WORKFLOW_EXECUTION.value,
        )
        if resources:
            _cred_rc["workflow_resources"] = resources
        if body.resume_from is not None:
            _cred_rc["_workflow_resume_from"] = str(body.resume_from)
        _creds = _cred_rc.get("llm_credentials")
        logger.warning(
            "workflow_execution_stage",
            stage="credentials_inject_done",
            wf_id=wf_id,
            exec_id=exec_id,
            has_credentials=bool(_creds),
            elapsed_ms=int((time.perf_counter() - stage_started) * 1000),
        )
        # Plan-B B6: per-run egress allowlist (auto-derived LLM + MCP hosts ∪
        # user-declared). Only consumed in ``proxy`` mode (ignored in the default
        # host-network mode), so it is always safe to compute + pass.
        stage_started = time.perf_counter()
        try:
            async with session_scope(tenant_id=tenant_id) as _s:
                _allow_hosts = await compute_allow_hosts_async(
                    wf_dict,
                    session=_s,
                    user_id=creator_user_id,
                    creds_mapping=_creds or {},
                )
        except Exception:  # defensive; the wrapper is itself fail-soft
            logger.warning("egress_allowlist_compute_failed", exc_info=True)
            _allow_hosts = set()
        logger.warning(
            "workflow_execution_stage",
            stage="egress_allowlist_done",
            wf_id=wf_id,
            exec_id=exec_id,
            allow_host_count=len(_allow_hosts or set()),
            elapsed_ms=int((time.perf_counter() - stage_started) * 1000),
        )
        logger.warning(
            "workflow_execution_stage",
            stage="sandbox_stream_start",
            wf_id=wf_id,
            exec_id=exec_id,
            workflow_run_id=workflow_run_id,
            backend="resident_session",
        )
        first_msg_seen = False
        live_node_event_count = 0
        backfilled_node_event_count = 0
        stream_started = time.perf_counter()
        async for msg in stream_history_workflow(
            stop=stop,
            workflow=wf_dict,
            inputs=body.input,
            tenant_id=tenant_id,
            session=session,
            execution_id=exec_id,
            context=_cred_rc or None,
        ):
            if not first_msg_seen:
                first_msg_seen = True
                logger.warning(
                    "workflow_execution_stage",
                    stage="sandbox_first_message",
                    wf_id=wf_id,
                    exec_id=exec_id,
                    workflow_run_id=workflow_run_id,
                    backend="resident_session",
                    elapsed_ms=int((time.perf_counter() - stream_started) * 1000),
                )

            mtype = msg.get("type")
            if mtype == "node_event":
                raw_ev = {k: v for k, v in msg.items() if k != "type"}
                ev = _normalize_node_event(wf_dict, raw_ev)
                mapped = to_exec_update(ev, exec_id)
                if mapped is None:
                    logger.warning(
                        "workflow_execution_node_event_dropped",
                        wf_id=wf_id,
                        exec_id=exec_id,
                        status=raw_ev.get("status"),
                        node_id=raw_ev.get("node_id"),
                        node_name=raw_ev.get("node_name"),
                    )
                    continue
                name, payload = mapped
                payload_node_id = payload.get("node_id")
                if payload_node_id and payload_node_id in wf_dict:
                    public_payload = _workflow_public_payload(payload, wf_id)
                    live_payload = _workflow_live_payload(public_payload)
                    live_node_event_count += 1
                    logger.debug(
                        "workflow_execution_node_frame_mapped",
                        wf_id=wf_id,
                        exec_id=exec_id,
                        node_id=payload_node_id,
                        node_name=raw_ev.get("node_name"),
                        status=payload.get("status"),
                        live_node_events=live_node_event_count,
                    )
                    _accumulate_per_node(per_node, live_payload)
                    # Persist the full terminal node result to VFS before
                    # trimming the live frame for DB/SSE.
                    np = persist_node_frame_payload(payload)
                    if np is not None:
                        await write_node_result(workflow_run_id, tenant_id, np)
                    await _persist_node_progress(
                        exec_id, creator_user_id, tenant_id, live_payload)
                elif payload_node_id:
                    logger.warning(
                        "workflow_execution_node_event_unknown_node",
                        wf_id=wf_id,
                        exec_id=exec_id,
                        status=payload.get("status"),
                        node_id=payload_node_id,
                        node_name=raw_ev.get("node_name"),
                    )
                elif payload.get("status") == "error":
                    engine_error = payload.get("error", "")
                    public_payload = _workflow_public_payload(payload, wf_id)
                    live_payload = _workflow_live_payload(public_payload)
                else:
                    public_payload = _workflow_public_payload(payload, wf_id)
                    live_payload = _workflow_live_payload(public_payload)
                yield name, live_payload
            elif mtype == "result":
                result_seen = True
                finished_ev = {
                    "status": "finished",
                    "final_outputs": msg.get("final_outputs") or {},
                    "error_dict": msg.get("error_dict") or {},
                    "execution_time": msg.get("execution_time"),
                }
                err_dict = finished_ev["error_dict"]
                logger.warning(
                    "workflow_execution_result_frame_received",
                    wf_id=wf_id,
                    exec_id=exec_id,
                    final_output_keys=(
                        list(finished_ev["final_outputs"].keys())
                        if isinstance(finished_ev["final_outputs"], dict)
                        else []
                    ),
                    error_keys=(
                        list(err_dict.keys()) if isinstance(err_dict, dict) else []
                    ),
                )
                if isinstance(err_dict, dict) and err_dict:
                    engine_error = _stringify_engine_error(
                        next(iter(err_dict.values()), None))
                for payload in _terminal_node_updates_from_result(
                    wf_dict,
                    finished_ev["final_outputs"],
                    err_dict,
                    exec_id,
                    per_node,
                ):
                    public_payload = _workflow_public_payload(payload, wf_id)
                    live_payload = _workflow_live_payload(public_payload)
                    backfilled_node_event_count += 1
                    _accumulate_per_node(per_node, live_payload)
                    np = persist_node_frame_payload(payload)
                    if np is not None:
                        await write_node_result(workflow_run_id, tenant_id, np)
                    await _persist_node_progress(
                        exec_id, creator_user_id, tenant_id, live_payload)
                    logger.warning(
                        "workflow_execution_backfill_node_frame_mapped",
                        wf_id=wf_id,
                        exec_id=exec_id,
                        node_id=payload.get("node_id"),
                        status=payload.get("status"),
                        backfilled_node_events=backfilled_node_event_count,
                    )
                    yield "EXEC_UPDATE", live_payload
                mapped = to_exec_update(finished_ev, exec_id)
                if mapped is not None:
                    name, payload = mapped
                    public_payload = _workflow_live_payload(
                        _workflow_public_payload(payload, wf_id))
                    await _with_execution_repo(
                        tenant_id,
                        creator_user_id,
                        lambda repo: repo.finish_execution(
                            exec_id,
                            status="error" if engine_error else "success",
                            error=engine_error,
                            per_node=per_node,
                            terminal_payload=public_payload,
                        ),
                    )
                    finalized = True
                    logger.warning(
                        "workflow_execution_terminal_frame_mapped",
                        wf_id=wf_id,
                        exec_id=exec_id,
                        status=payload.get("status"),
                        live_node_events=live_node_event_count,
                        backfilled_node_events=backfilled_node_event_count,
                    )
                    yield name, public_payload
                logger.warning(
                    "workflow_execution_stage",
                    stage="sandbox_stream_result",
                    wf_id=wf_id,
                    exec_id=exec_id,
                    live_node_events=live_node_event_count,
                    backfilled_node_events=backfilled_node_event_count,
                    final_output_count=(
                        len(finished_ev["final_outputs"])
                        if isinstance(finished_ev["final_outputs"], dict)
                        else 0
                    ),
                    error_count=(
                        len(err_dict) if isinstance(err_dict, dict) else 0
                    ),
                )
                break
            elif mtype == "timeout":
                engine_error = msg.get("message", "sandbox run timed out")
                public_payload = {
                    "wf_id": wf_id, "status": "error", "error": engine_error,
                }
                await _with_execution_repo(
                    tenant_id,
                    creator_user_id,
                    lambda repo: repo.finish_execution(
                        exec_id,
                        status="error",
                        error=engine_error,
                        per_node=per_node,
                        terminal_payload=public_payload,
                    ),
                )
                finalized = True
                yield "EXEC_UPDATE", public_payload
                break
            # Unknown type → ignore (forward-compat).

        # ---- terminal funnel (mirrors the in-process path) ----
        if stop.is_set():
            await _with_execution_repo(
                tenant_id,
                creator_user_id,
                lambda repo: repo.stop_execution(exec_id, per_node=per_node),
            )
            logger.warning(
                "workflow_execution_stage",
                stage="execution_stopped",
                wf_id=wf_id,
                exec_id=exec_id,
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
            )
            return
        if finalized:
            logger.warning(
                "workflow_execution_stage",
                stage="execution_error" if engine_error else "execution_success",
                wf_id=wf_id,
                exec_id=exec_id,
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
            )
            return
        if engine_error:
            await _with_execution_repo(
                tenant_id,
                creator_user_id,
                lambda repo: repo.finish_execution(
                    exec_id, status="error", error=engine_error, per_node=per_node),
            )
            logger.warning(
                "workflow_execution_stage",
                stage="execution_error",
                wf_id=wf_id,
                exec_id=exec_id,
                elapsed_ms=int((time.perf_counter() - total_started) * 1000),
            )
            return
        if not result_seen:
            # The sandbox stream closed without a terminal ``result``.
            err = "sandbox exited without a result frame"
            await _with_execution_repo(
                tenant_id,
                creator_user_id,
                lambda repo: repo.finish_execution(
                    exec_id, status="error", error=err, per_node=per_node),
            )
            yield "EXEC_UPDATE", {
                "wf_id": wf_id, "status": "error", "error": err,
            }
            return
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.finish_execution(
                exec_id, status="success", per_node=per_node),
        )
        logger.warning(
            "workflow_execution_stage",
            stage="execution_success",
            wf_id=wf_id,
            exec_id=exec_id,
            elapsed_ms=int((time.perf_counter() - total_started) * 1000),
        )
    except Exception as e:
        error = str(e)
        await _fail_history(tenant_id, exec_id)
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.finish_execution(
                exec_id, status="error", error=error, per_node=per_node),
        )
        yield "EXEC_UPDATE", {
            "wf_id": wf_id, "status": "error", "error": error,
        }


async def _produce_execution(
    stop: asyncio.Event, wf_id: str, exec_id: str, body: ExecutionRequest,
    wf_dict: dict, creator_user_id: str, tenant_id: str,
) -> AsyncIterator[tuple[str, dict]]:
    """Run a workflow execution; yield EXEC_UPDATE events as it progresses.

    P2 (sandbox-only): this is the SSE single-execute producer (the canvas
    "Run"). It owns the per-execution lifecycle — in-memory start/status
    bookkeeping, the stop-registry FD-leak guard, and terminal
    persistence — and delegates the actual run to the workflow's resident
    sandbox session. There is NO in-process ``astream`` fallback.

    The live per-node frames (running → completed / error), the C1 per_node
    accumulation+persistence, and the cancel/error/success funnel live inside
    ``_produce_execution_sandbox``.

    ``wf_dict`` is resolved by the route handler with the per-request DI
    session *before* streaming starts — the producer outlives the request
    so it must not touch a request-scoped session.

    The producer outlives the request. It must not hold a request-scoped DB
    session; each status update opens a short tenant-bound transaction.
    """
    if not wf_dict:
        yield "EXEC_UPDATE", {
            "wf_id": wf_id, "status": "error",
            "error": f"workflow {wf_id} has no committed content",
        }
        return

    version = wf_dict.get("__meta__", {}).get("workflow_version", 1)
    try:
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.start_execution(
                wf_id, version, exec_id, is_single_node=False),
        )
    except RuntimeError as e:
        yield "EXEC_UPDATE", {
            "wf_id": wf_id, "status": "error", "error": str(e),
        }
        return
    # Single exit funnel for the stop-registry cleanup. ``start_execution``
    # added a stop Event to the process-global
    # ``_STOP_EVENTS`` registry (risk #3, module-scoped since T10); nothing
    # GC-cleans it anymore, so every terminal path MUST discard it exactly
    # once or the registry leaks Events/OS-semaphore FDs for the life of
    # the long-running API process. This ``finally`` runs on normal
    # completion, the error path, the stop/cancel return, and any
    # exception/GeneratorExit — i.e. every way this async generator can
    # terminate. ``stop_registry.discard`` is an
    # in-memory pop-with-default under the registry lock (no DB session,
    # idempotent, safe if never registered or already discarded), so the
    # cleanup never raises and changes no persistence/SSE semantics.
    # The OUTER ``try/finally`` owns ONLY ``stop_registry.discard`` — the
    # FD-leak guard, unrelated to the fixed workflow /run lifecycle.
    try:
        yield "EXEC_UPDATE", {"wf_id": wf_id, "status": "started", "history_id": exec_id}

        # The sandbox is the only execution path. The canvas "Run"
        # single-execute delegates to the resident workflow sandbox session.
        #
        # ``classify_workflow`` is the pre-launch routing guard. It raises for
        # a workflow the sandbox cannot run:
        #   * ``EngineNeedsHostNode`` — a host/API node type that must use a
        #     brokered platform path instead of receiving credentials in gVisor.
        # Previously these fell through to the in-process loop; now there is no
        # fallback, so they become an explicit, persisted terminal error rather
        # than a silent host run.
        try:
            classify_workflow(wf_dict)
        except EngineNeedsHostNode as e:
            # Not sandbox-runnable → CLEAR terminal error (no silent in-process
            # fallback). Persist the error so a reload / GET surfaces it.
            err = (
                "workflow cannot run in the sandbox: "
                f"{type(e).__name__}: {e}"
            )
            await _with_execution_repo(
                tenant_id,
                creator_user_id,
                lambda repo: repo.finish_execution(exec_id, status="error", error=err),
            )
            yield "EXEC_UPDATE", {
                "wf_id": wf_id, "status": "error", "error": err,
            }
            return

        async for ev in _produce_execution_sandbox(
            stop, wf_id, exec_id, body, wf_dict,
            creator_user_id, tenant_id,
        ):
            yield ev
    finally:
        # Idempotent in-memory cleanup; runs on EVERY terminal path.
        stop_registry.discard(exec_id)


@router.post("/workflows/{wf_id}/execution/resume-check")
async def check_workflow_resume(
    wf_id: str, body: ResumeCheckRequest, request: Request,
    ctx: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow_action(request=request, auth=ctx, service=service,
                                   wf_id=wf_id, action=Action.EXECUTE)
    async with session_scope(tenant_id=ctx.tenant_id) as session:
        detail = await latest_workflow_run(session, wf_id=wf_id, user_id=ctx.user_id)
        reason = retry_reason(detail, wf_id=wf_id, user_id=ctx.user_id,
                              workflow=body.workflow, inputs=body.input)
    return {"eligible": reason is None, "resume_from": detail["id"] if reason is None else None,
            "reason": reason, "previous_inputs": detail["inputs"] if detail else None}


@router.post("/workflows/{wf_id}/executions")
async def start_execution(
    wf_id: str,
    body: ExecutionRequest,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    ctx: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> StreamingResponse:
    """Start an execution; respond with an SSE stream of EXEC_UPDATE events."""
    await _authorize_workflow_action(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.EXECUTE,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    # Resolve the snapshot now (request session); the producer outlives
    # the request and must not hold a request-scoped session.
    wf_dict = await repo.get_current_workflow(wf_id)

    if body.resume_from is not None:
        async with session_scope(tenant_id=ctx.tenant_id) as session:
            previous = await latest_workflow_run(session, wf_id=wf_id, user_id=ctx.user_id)
            reason = retry_reason(previous, wf_id=wf_id, user_id=ctx.user_id,
                                  workflow=wf_dict, inputs=body.input)
            if reason is not None or previous["id"] != str(body.resume_from):
                raise HTTPException(status_code=409, detail="workflow_resume_unavailable")

    execution_record_id = str(uuid.uuid4())
    workflow_turn_key = wf_id

    active = TURN_TASKS.get(workflow_turn_key)
    if active is not None and not active.done():
        raise HTTPException(
            status_code=409,
            detail=f"workflow {wf_id} already has a running execution",
        )
    stale_running = await _with_execution_repo(
        ctx.tenant_id,
        ctx.user_id,
        lambda exec_repo: exec_repo.latest_execution(wf_id),
    )
    if stale_running is not None:
        await _reject_live_execution(ctx.tenant_id, wf_id, stale_running)
        await _with_execution_repo(
            ctx.tenant_id,
            ctx.user_id,
            lambda exec_repo: exec_repo.stop_execution(stale_running["exec_id"]),
        )

    # The producer escapes the request transaction. Re-check immediately
    # before allocating its live turn and capturing the authorized identity.
    await _authorize_workflow_action(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.EXECUTE,
    )
    # M1: exec turns use a drop-oldest (ring) buffer — a high-frame run (a
    # large loop emitting running+success per node per iteration) evicts the
    # oldest frames at the cap instead of falsely failing the run. Exec is
    # not resumable like chat, so dropping early frames is acceptable.
    buf, stop = register_turn(workflow_turn_key, drop_oldest=True)

    async def producer(stop_ev: asyncio.Event):
        # ``ctx`` is a frozen dataclass of plain strings — safe to
        # capture in this closure that outlives the request.
        async for ev in _produce_execution(
            stop_ev, wf_id, execution_record_id, body, wf_dict,
            ctx.user_id, ctx.tenant_id,
        ):
            yield ev

    TURN_TASKS[workflow_turn_key] = asyncio.create_task(
        run_turn(workflow_turn_key, buf, stop, producer)
    )

    return StreamingResponse(
        _sse_from_turn(
            workflow_turn_key,
            authorization_guard=lambda: authorization_lease_is_valid(
                auth=ctx,
                openfga_client=getattr(
                    request.app.state, "openfga_client", None
                ),
                resource=ResourceRef(
                    ResourceType.WORKFLOW,
                    wf_id,
                    ctx.active_organization_id,
                ),
                action=Action.EXECUTE,
            ),
        ),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Turn-Id": workflow_turn_key},
    )


async def _produce_node_execution(
    stop: asyncio.Event, exec_id: str, node_dict: dict, inputs: dict,
    tenant_id: str | None = None, wf_id: str | None = None,
    creator_user_id: str | None = None,
    workflow: dict | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """Run ONE node (the draft node from the request body) and yield its
    synthesized ``running`` to ``completed`` or ``error`` frames.

    P2 (sandbox-only): the node is run INSIDE the selected OS sandbox via the
    provider's ``run_node``. There is NO in-process fallback any more — a
    non-sandbox-runnable node (``classify_workflow`` raises), a provider/
    admission failure, or an in-sandbox engine crash all surface a CLEAR
    terminal error frame instead of silently degrading to an unsandboxed
    host run. ``exec_id`` is stamped onto each frame so the frontend can
    correlate them (mirrors ``to_exec_update``).

    Unlike agent ``node_execute``, this workflow-page node-debug path persists
    lightweight run state so the inspector can recover after refresh.

    Task 3 (run results in VFS): on the node's TERMINAL frame we additionally
    OVERWRITE ``nodes/{node_id}.json`` in the workflow's fixed run-tier
    (``run_id == wf_id``) — so the Run-node sider / Explorer see the freshly
    debugged node. NO purge: only that one node's file is replaced. Requires
    ``wf_id`` because workflow run files are scoped by workflow id.

    Cancel: the producer races the sandbox job against the durable turn stop
    event. A stop request hard-cancels the resident sandbox job, persists a
    terminal ``cancelled`` node frame, and only then exposes that status to the
    SSE consumer. Closing the browser stream alone is never treated as a
    successful cancellation.
    """
    if not isinstance(node_dict, dict) or not node_dict.get("node_type"):
        yield "EXEC_UPDATE", {
            "exec_id": exec_id, "status": "error",
            "error": "request body 'node' is missing a node_type",
        }
        return
    # PHASE 1c: a single-node debug run needs the SAME saved-credential
    # resolution the full-workflow path does — otherwise a PromptNode whose
    # model_name is a saved API name falls through to a registry lookup and
    # fails with "Cannot find a class named '<name>'". Build the engine-only
    # ``llm_credentials`` mapping from this one node and pass it as ``extra``.
    extra: dict = {}
    nid = node_dict.get("node_id") or "node_x"
    if wf_id is None or tenant_id is None or creator_user_id is None:
        yield "EXEC_UPDATE", {
            "exec_id": exec_id, "node_id": nid, "status": "error",
            "error": "workflow id, tenant id, and user id are required",
        }
        return
    try:
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.start_execution(
                wf_id, (1, 0), exec_id, target_node_id=nid, is_single_node=True),
        )
    except RuntimeError as e:
        yield "EXEC_UPDATE", {
            "exec_id": exec_id, "node_id": nid, "status": "error", "error": str(e),
        }
        return
    selected = {nid: node_dict}
    if isinstance(workflow, dict) and "__meta__" in workflow:
        selected["__meta__"] = workflow["__meta__"]
    await create_execution(
        execution_id=exec_id, tenant_id=tenant_id, source_type="workflow", source_id=wf_id,
        user_id=creator_user_id, workflow_id=wf_id, workflow=selected, inputs=inputs, node_id=nid,
    )
    if tenant_id is not None:
        extra = await inject_into_run_context_async(
            {},
            {nid: node_dict},
            tenant_id,
            user_id=creator_user_id,
            workflow_id=wf_id,
            execution_id=exec_id,
            execution_resource_type=ResourceType.WORKFLOW_EXECUTION.value,
        )

    # P2 (sandbox-only): the node ALWAYS runs inside the selected OS sandbox via
    # the provider's ``run_node``. The legacy in-process node-synthesizer
    # fallback was REMOVED (#629) — the sandbox is the sole isolation
    # boundary, so a non-runnable node / provider-admission failure /
    # in-sandbox engine crash now surfaces a CLEAR terminal error frame
    # rather than silently re-running unsandboxed on the host.
    # Host/API node guard — not sandbox-runnable → clear error.
    try:
        classify_workflow({nid: node_dict})
    except EngineNeedsHostNode as e:
        err = f"node cannot run in the sandbox: {type(e).__name__}: {e}"
        await _fail_history(tenant_id, exec_id)
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.update_node_execution(
                exec_id, nid, status="error", error=err),
        )
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.finish_execution(exec_id, status="error", error=err),
        )
        yield "EXEC_UPDATE", {
            "exec_id": exec_id, "node_id": nid, "status": "error", "error": err,
        }
        return

    await _with_execution_repo(
        tenant_id,
        creator_user_id,
        lambda repo: repo.update_node_execution(exec_id, nid, status="running"),
    )
    yield "EXEC_UPDATE", {"exec_id": exec_id, "node_id": nid, "status": "running"}
    node_started = time.perf_counter()

    async def _record_cancelled() -> dict:
        async with session_scope(tenant_id=tenant_id) as history_session:
            history = WorkflowHistoryRepo(history_session)
            run = await history.get(exec_id)
            if run is not None and run["status"] not in TERMINAL_STATUSES:
                await history.request_cancel(exec_id)
                if run["generation"] is None:
                    await history.confirm_cancelled(exec_id)
        frame = {
            "exec_id": exec_id,
            "node_id": nid,
            "node_name": node_dict.get("node_name"),
            "node_type": node_dict.get("node_type"),
            "inputs": inputs,
            "duration": time.perf_counter() - node_started,
            "status": "cancelled",
            "error": "cancelled by user",
        }
        payload = persist_node_frame_payload(frame)
        if payload is not None:
            await write_node_result(wf_id, tenant_id, payload)
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.update_node_execution(
                exec_id,
                nid,
                status="cancelled",
                inputs=inputs,
                error="cancelled by user",
                duration=frame["duration"],
            ),
        )
        await _with_execution_repo(tenant_id, creator_user_id, lambda repo: repo.stop_execution(exec_id))
        return frame

    try:
        if stop.is_set():
            yield "EXEC_UPDATE", await _record_cancelled()
            return
        workflow_run_id = wf_id
        session = await get_sandbox_manager().get_session(
            tenant_id,
            workflow_run_id,
            user_id=creator_user_id,
            expose_run=True,
            expose_runtime=True,
        )
        if stop.is_set():
            yield "EXEC_UPDATE", await _record_cancelled()
            return

        from vibecanvas_api.services.workflow_resources import prepare_execution_resources
        resources = await prepare_execution_resources(
            sandbox_session=session, workflow={nid: node_dict}, tenant_id=tenant_id,
            user_id=creator_user_id, workflow_id=wf_id, execution_id=exec_id,
            execution_resource_type=ResourceType.WORKFLOW_EXECUTION.value,
        )
        if resources:
            extra["workflow_resources"] = resources
        pythonpath = await prepare_code_pythonpath(selected, session=session)
        if pythonpath:
            extra["code_pythonpath"] = pythonpath

        async def on_state(state):
            return None

        job = await execute_history_workflow(
            session=session, tenant_id=tenant_id, execution_id=exec_id,
            workflow=selected, inputs=inputs, context=extra, stop=stop,
            node_id=nid, on_state=on_state,
        )
        if stop.is_set() or (job.get("status") or {}).get("status") == "cancelled":
            yield "EXEC_UPDATE", await _record_cancelled()
            return
        rj = job.get("result")
        if not isinstance(rj, dict):
            raise RuntimeError("Execution ended without a result.")
    except Exception as e:
        # Provider/admission failure (e.g. SandboxUnavailable, at-capacity) →
        # CLEAR terminal error frame (NO silent in-process fallback).
        err = f"sandbox node run failed: {e}"
        await _fail_history(tenant_id, exec_id)
        logger.warning(
            "workflow_node_execute_sandbox_failure",
            exec_id=exec_id,
            wf_id=wf_id,
            node_id=nid,
            error=err,
            exc_info=True,
        )
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.update_node_execution(
                exec_id, nid, status="error", error=err),
        )
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.finish_execution(exec_id, status="error", error=err),
        )
        yield "EXEC_UPDATE", {
            "exec_id": exec_id, "node_id": nid, "status": "error", "error": err,
        }
        return

    error_dict = rj.get("error_dict") or {}
    final_outputs = rj.get("final_outputs") or {}
    if "__engine__" in error_dict:
        # The in-sandbox engine crashed (no per-node result) — surface it.
        err = _stringify_engine_error(error_dict.get("__engine__"))
        logger.warning(
            "workflow_node_execute_engine_error",
            exec_id=exec_id,
            wf_id=wf_id,
            node_id=nid,
            error=err,
        )
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.update_node_execution(
                exec_id, nid, status="error", error=err),
        )
        await _with_execution_repo(
            tenant_id,
            creator_user_id,
            lambda repo: repo.finish_execution(exec_id, status="error", error=err),
        )
        yield "EXEC_UPDATE", {
            "exec_id": exec_id, "node_id": nid, "status": "error", "error": err,
        }
        return
    execution_time = rj.get("execution_time")
    frame_metadata = {
        "exec_id": exec_id,
        "node_id": nid,
        "node_name": node_dict.get("node_name"),
        "node_type": node_dict.get("node_type"),
        "inputs": inputs,
        "duration": execution_time,
    }
    if error_dict.get(nid):
        logger.warning(
            "workflow_node_execute_node_error",
            exec_id=exec_id,
            wf_id=wf_id,
            node_id=nid,
            error=error_dict[nid],
        )
        _frame = {
            **frame_metadata,
            "status": "error",
            "error": _stringify_engine_error(error_dict[nid]) or "error",
        }
    else:
        _out = final_outputs.get(nid)
        _frame = {
            **frame_metadata,
            "status": "completed",
            "result": json.dumps(_out, default=str, ensure_ascii=False),
        }
    np = persist_node_frame_payload(_frame)
    if np is not None and tenant_id is not None and wf_id is not None:
        await write_node_result(wf_id, tenant_id, np)
    await _with_execution_repo(
        tenant_id,
        creator_user_id,
        lambda repo: repo.update_node_execution(
            exec_id,
            nid,
            status=_frame.get("status"),
            result_overwrite=(
                _frame.get("result") if isinstance(_frame.get("result"), str) else None
            ),
            error=(
                str(_frame.get("error")) if _frame.get("error") is not None else None
            ),
        ),
    )
    yield "EXEC_UPDATE", _frame
    await _with_execution_repo(
        tenant_id,
        creator_user_id,
        lambda repo: repo.finish_execution(
            exec_id,
            status="success" if _frame.get("status") == "completed" else "error",
            error=(
                str(_frame.get("error")) if _frame.get("error") is not None else None
            ),
        ),
    )


@router.post("/workflows/{wf_id}/nodes/{node_id}/execute")
async def execute_node(
    wf_id: str,
    node_id: str,
    body: NodeExecutionRequest,
    request: Request,
    repo: WorkflowRepo = Depends(get_workflow_repo),
    ctx: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> StreamingResponse:
    """Execute one node for interactive debugging.

    Runs ONE node — the UNSAVED *draft* ``node_dict`` carried in the
    request body (NOT ``get_current_workflow``) — with the supplied
    ``input`` directly (no reference resolution). Streams the synthesized
    ``running`` → ``completed`` / ``error`` frames via the SAME SSE
    machinery (``run_turn`` / ``format_event``) so the frontend consumes
    them identically to a workflow run.

    Tenant-scoped: the wf-existence check runs on the request's
    tenant-bound ``WorkflowRepo`` (RLS isolates by tenant), so a node
    cannot be debug-run against a workflow the caller can't see. The node
    itself is supplied by the caller, so debug-execute is allowed even on
    a pinned/read-only version. Its terminal result is persisted in the
    workflow's fixed run-tier so debug inputs and output survive a refresh.
    """
    await _authorize_workflow_action(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.EXECUTE,
    )
    if not await repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    # The request intentionally carries the unsaved node body, but execution
    # settings are workflow-level state. Read the saved workflow only for
    # __meta__.settings (not to replace the draft node being debugged).
    workflow = await repo.get_current_workflow(wf_id)

    running = await _with_execution_repo(
        ctx.tenant_id,
        ctx.user_id,
        lambda exec_repo: exec_repo.latest_execution(wf_id),
    )
    if running is not None:
        await _reject_live_execution(ctx.tenant_id, wf_id, running)
        await _with_execution_repo(
            ctx.tenant_id,
            ctx.user_id,
            lambda exec_repo: exec_repo.stop_execution(running["exec_id"]),
        )

    await _authorize_workflow_action(
        request=request,
        auth=ctx,
        service=service,
        wf_id=wf_id,
        action=Action.EXECUTE,
    )
    exec_id = str(uuid.uuid4())
    buf, stop = register_turn(exec_id, drop_oldest=True)

    node_dict = body.node
    inputs = body.input

    async def producer(stop_ev: asyncio.Event):
        try:
            async for ev in _produce_node_execution(
                stop_ev, exec_id, node_dict, inputs, ctx.tenant_id, wf_id,
                ctx.user_id, workflow,
            ):
                yield ev
        except Exception as exc:
            message = str(exc)
            await _fail_history(ctx.tenant_id, exec_id)
            await _with_execution_repo(ctx.tenant_id, ctx.user_id,
                lambda execution_repo: execution_repo.finish_execution(exec_id, status="error", error=message))
            yield "EXEC_UPDATE", {"exec_id": exec_id, "node_id": node_id, "status": "error", "error": message}
        finally:
            stop_registry.discard(exec_id)

    TURN_TASKS[exec_id] = asyncio.create_task(
        run_turn(exec_id, buf, stop, producer)
    )

    return StreamingResponse(
        _sse_from_turn(
            exec_id,
            authorization_guard=lambda: authorization_lease_is_valid(
                auth=ctx,
                openfga_client=getattr(
                    request.app.state, "openfga_client", None
                ),
                resource=ResourceRef(
                    ResourceType.WORKFLOW,
                    wf_id,
                    ctx.active_organization_id,
                ),
                action=Action.EXECUTE,
            ),
        ),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Turn-Id": exec_id},
    )


@router.get("/executions/{exec_id}", response_model=ExecutionStatusOut)
async def get_execution_status(
    exec_id: str,
    request: Request,
    exec_repo: ExecutionRepo = Depends(get_execution_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_execution_action(
        request=request,
        auth=auth,
        service=service,
        exec_id=exec_id,
        action=Action.INSPECT_RUNS,
    )
    record = await exec_repo.get_execution(exec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"execution {exec_id} not found")
    return await _history_status(record, exec_repo._session)


@router.post("/executions/{exec_id}/cancel", status_code=202)
async def cancel_execution(
    exec_id: str,
    request: Request,
    exec_repo: ExecutionRepo = Depends(get_execution_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Request cancellation. Sets both the asyncio stop event (so the SSE
    producer can exit) and the process-shared threading stop event (so
    the engine's sync worker thread sees it via
    ExecutionRepo.get_stop_event — risk #3, shared module-level
    registry)."""
    await _authorize_execution_action(
        request=request,
        auth=auth,
        service=service,
        exec_id=exec_id,
        action=Action.CANCEL,
    )
    if await exec_repo.get_execution(exec_id) is None:
        raise HTTPException(status_code=404, detail=f"execution {exec_id} not running")
    # Do not publish an optimistic terminal frame here. The node producer keeps
    # SSE connected and emits ``cancelled`` only after sandbox hard-cancel and
    # terminal VFS persistence have completed.
    request_cancel(exec_id)
    await _authorize_execution_action(
        request=request,
        auth=auth,
        service=service,
        exec_id=exec_id,
        action=Action.CANCEL,
    )
    await _request_history_cancel(exec_repo, exec_id)
    await exec_repo.stop_execution(exec_id)
    return {"status": "cancel-requested"}


@router.get("/workflows/{wf_id}/execution/status",
            response_model=ExecutionStatusOut | None)
async def get_workflow_execution_status(
    wf_id: str,
    request: Request,
    exec_repo: ExecutionRepo = Depends(get_execution_repo),
    workflow_repo: WorkflowRepo = Depends(get_workflow_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Latest workflow execution state, keyed by workflow id.

    A known workflow with no runs has a successful, nullable projection rather
    than a noisy 404: absence is the normal initial state rendered by the Run
    inspector. An unknown workflow remains a real not-found error.
    """
    await _authorize_workflow_action(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.INSPECT_RUNS,
    )
    if not await workflow_repo.get_meta(wf_id):
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    record = await exec_repo.latest_execution(wf_id)
    return await _history_status(record, exec_repo._session) if record is not None else None


@router.post("/workflows/{wf_id}/execution/cancel", status_code=202)
async def cancel_workflow_execution(
    wf_id: str,
    request: Request,
    exec_repo: ExecutionRepo = Depends(get_execution_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Cancel the active workflow execution by workflow id."""
    await _authorize_workflow_action(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.CANCEL,
    )
    record = await exec_repo.latest_running_execution(wf_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"workflow {wf_id} has no running execution",
        )
    buf = TURN_BUFFERS.get(wf_id)
    if buf is not None and not buf.closed:
        try:
            await buf.put(("EXEC_UPDATE", {"status": "cancelled"}))
        except RuntimeError:
            pass
    request_cancel(wf_id)
    request_cancel(record["exec_id"])
    await _authorize_workflow_action(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.CANCEL,
    )
    await _request_history_cancel(exec_repo, record["exec_id"])
    await exec_repo.stop_execution(record["exec_id"])
    return {"status": "cancel-requested"}


@router.get("/workflows/{wf_id}/executions",
            response_model=Page[ExecutionListItem])
async def list_executions(
    wf_id: str,
    request: Request,
    page: PageRequest = Depends(PageRequest.as_query),
    exec_repo: ExecutionRepo = Depends(get_execution_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_workflow_action(
        request=request,
        auth=auth,
        service=service,
        wf_id=wf_id,
        action=Action.INSPECT_RUNS,
    )
    records = await exec_repo.list_executions(wf_id)
    sliced = records[page.offset:page.offset + page.limit]
    return Page[ExecutionListItem](
        items=[_record_to_list_item(r) for r in sliced],
        total=len(records), limit=page.limit, offset=page.offset,
    )
