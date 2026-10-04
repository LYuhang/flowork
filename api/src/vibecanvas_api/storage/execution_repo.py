# -*- coding: utf-8 -*-
"""Workflow-page run state repository.

This is deliberately not an execution-history store. It keeps one current
interactive run per user and workflow for the workflow page: node execution, whole
workflow execution, refresh recovery, and cancellation. Agent tools do not use
this repo; they write only their VFS outputs.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service
from vibecanvas_api.storage import stop_registry
from vibecanvas_api.storage.db import temporary_tenant_scope
from vibecanvas_api.storage.models import Workflow, WorkflowRunEvent, WorkflowRunState

_SIGNAL_LOCK = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt is not None else None


def _close_running_nodes(per_node: dict | None, *, status: str = "cancelled") -> dict:
    pn = dict(per_node or {})
    for node_id, raw in list(pn.items()):
        if not isinstance(raw, dict):
            continue
        node = dict(raw)
        if node.get("status") == "running":
            node["status"] = status
            pn[node_id] = node
    return pn


def _api_terminal_status(status: str) -> str:
    if status == "success":
        return "completed"
    if status == "stopped":
        return "stopped"
    if status == "error":
        return "error"
    return "running"


def _record_dict(e: WorkflowRunState) -> dict:
    return {
        "exec_id": e.turn_id or e.wf_id,
        "wf_id": e.wf_id,
        "status": e.status,
        "error": e.error,
        "per_node": dict(e.node_states or {}),
        "target_node_id": e.target_node_id,
        "started_at": _ts(e.started_at),
        "ended_at": _ts(e.ended_at),
        "seq": e.seq,
        "run_kind": e.run_kind,
        "cancel_requested": e.cancel_requested,
    }


class ExecutionRepo:
    """DB-backed current run state for workflow-page execution."""

    def __init__(self, session: AsyncSession, user_id: str):
        if session is None:
            raise ValueError("ExecutionRepo requires a tenant-bound DB session")
        self._session = session
        self._user_id = user_id

    async def _store_state(self, state: WorkflowRunState) -> None:
        encrypted = await content_encryption_service().encrypt_json(
            self._session,
            tenant_id=state.tenant_id,
            resource_type="workflow",
            resource_id=state.wf_id,
            purpose="workflow_run_state_private",
            record_id=state.wf_id,
            value={
                "node_states": state.node_states,
                "error": state.error,
            },
        )
        state.private_ciphertext = encrypted.ciphertext
        state.private_nonce = encrypted.nonce
        state.private_key_id = encrypted.key_id

    async def _materialize_state(
        self,
        state: WorkflowRunState | None,
    ) -> WorkflowRunState | None:
        if state is None:
            return None
        value = await content_encryption_service().decrypt_json(
            self._session,
            key_id=state.private_key_id,
            tenant_id=state.tenant_id,
            resource_type="workflow",
            resource_id=state.wf_id,
            purpose="workflow_run_state_private",
            record_id=state.wf_id,
            ciphertext=state.private_ciphertext,
            nonce=state.private_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("Workflow run state ciphertext must contain an object")
        state.node_states = value.get("node_states") or {}
        state.error = value.get("error")
        return state

    async def _state_for_turn(self, exec_id: str, *, for_update: bool = False) -> WorkflowRunState | None:
        stmt = select(WorkflowRunState).where(
            or_(WorkflowRunState.turn_id == exec_id, WorkflowRunState.wf_id == exec_id),
            WorkflowRunState.creator_user_id == self._user_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self._materialize_state(
            (await self._session.execute(stmt)).scalar_one_or_none()
        )

    async def _state_for_workflow(self, wf_id: str, *, for_update: bool = False) -> WorkflowRunState | None:
        stmt = select(WorkflowRunState).where(WorkflowRunState.wf_id == wf_id,
                                                     WorkflowRunState.creator_user_id == self._user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self._materialize_state(
            (await self._session.execute(stmt)).scalar_one_or_none()
        )

    async def _append_event(
        self,
        state: WorkflowRunState,
        event_type: str,
        payload: dict,
        *,
        store_state: bool = True,
    ) -> int:
        state.seq = int(state.seq or 0) + 1
        state.updated_at = _now()
        if store_state:
            await self._store_state(state)
        encrypted = await content_encryption_service().encrypt_json(
            self._session,
            tenant_id=state.tenant_id,
            resource_type="workflow",
            resource_id=state.wf_id,
            purpose="workflow_run_event",
            record_id=f"{state.wf_id}:{state.seq}",
            value=dict(payload or {}),
        )
        event = WorkflowRunEvent(
            wf_id=state.wf_id,
            seq=state.seq,
            tenant_id=state.tenant_id,
            creator_user_id=state.creator_user_id,
            event_type=event_type,
            payload_ciphertext=encrypted.ciphertext,
            payload_nonce=encrypted.nonce,
            payload_key_id=encrypted.key_id,
        )
        event.payload = payload
        self._session.add(event)
        await self._session.flush()
        return state.seq

    async def start_execution(
        self,
        workflow_id: str,
        version: Any,
        exec_id: str,
        *,
        target_node_id: Optional[str] = None,
        is_single_node: Optional[bool] = None,
        use_mp_event: bool = False,
        workflow_tenant_id: str | None = None,
    ) -> None:
        if target_node_id is None and is_single_node:
            target_node_id = "__single__"
        run_kind = "node" if target_node_id is not None else "workflow"
        state = await self._state_for_workflow(workflow_id, for_update=True)
        if state is not None and state.status in {"pending", "running"}:
            raise RuntimeError(f"workflow {workflow_id} already has a running execution")

        await self._session.execute(
            delete(WorkflowRunEvent).where(WorkflowRunEvent.wf_id == workflow_id,
                                           WorkflowRunEvent.creator_user_id == self._user_id)
        )
        now = _now()
        if state is None:
            tenant_id = await self._session.scalar(text("SELECT current_setting('app.tenant_id')::uuid"))
            source_tenant_id = workflow_tenant_id or str(tenant_id)
            async with temporary_tenant_scope(self._session, source_tenant_id):
                source_exists = await self._session.scalar(select(Workflow.wf_id).where(
                    Workflow.wf_id == workflow_id, Workflow.deleted_at.is_(None),
                ))
            if source_exists is None:
                raise LookupError(f"workflow {workflow_id} not found")
            state = WorkflowRunState(
                wf_id=workflow_id,
                tenant_id=tenant_id,
                workflow_tenant_id=source_tenant_id,
                creator_user_id=self._user_id,
            )
        state.creator_user_id = self._user_id
        state.turn_id = exec_id
        state.run_kind = run_kind
        state.status = "running"
        state.target_node_id = target_node_id
        state.seq = 0
        state.node_states = {}
        state.cancel_requested = False
        state.error = None
        state.started_at = now
        state.updated_at = now
        state.ended_at = None
        await self._store_state(state)
        self._session.add(state)
        await self._session.flush()
        await self._append_event(
            state,
            "EXEC_UPDATE",
            {"wf_id": workflow_id, "status": "started"},
            store_state=False,
        )
        with _SIGNAL_LOCK:
            stop_registry.register(exec_id, use_mp=use_mp_event)

    async def update_node_execution(
        self,
        exec_id: str,
        node_id: str,
        status: Optional[str] = None,
        inputs: Optional[dict] = None,
        result_append: Optional[str] = None,
        result_overwrite: Optional[str] = None,
        error: Optional[str] = None,
        duration: Optional[float] = None,
    ) -> None:
        state = await self._state_for_turn(exec_id, for_update=True)
        if state is None:
            return
        pn = dict(state.node_states or {})
        node = dict(pn.get(node_id) or {
            "status": "pending",
            "inputs": {},
            "execution_result": "",
        })
        if status is not None:
            node["status"] = status
        if inputs is not None:
            merged = dict(node.get("inputs") or {})
            merged.update(inputs)
            node["inputs"] = merged
        if result_overwrite is not None:
            node["execution_result"] = result_overwrite
        elif result_append is not None:
            node["execution_result"] = (
                node.get("execution_result") or "") + result_append
        if error is not None:
            node["error"] = error
        if duration is not None:
            node["duration"] = duration
        pn[node_id] = node
        state.node_states = pn
        payload = {"wf_id": state.wf_id, "node_id": node_id}
        if status is not None:
            payload["status"] = status
        if result_overwrite is not None:
            payload["result"] = result_overwrite
        if error is not None:
            payload["error"] = error
        if duration is not None:
            payload["duration"] = duration
        if inputs is not None:
            payload["inputs"] = inputs
        await self._append_event(state, "EXEC_UPDATE", payload)

    async def stop_execution(
        self, exec_id: str, *, per_node: Optional[dict] = None,
    ) -> None:
        stop_registry.signal(exec_id)
        state = await self._state_for_turn(exec_id, for_update=True)
        if state is None:
            return
        state.status = "stopped"
        state.cancel_requested = True
        state.node_states = _close_running_nodes(
            per_node if per_node is not None else state.node_states,
        )
        state.ended_at = _now()
        await self._append_event(
            state,
            "EXEC_UPDATE",
            {"wf_id": state.wf_id, "status": "stopped"},
        )

    async def finish_execution(
        self,
        exec_id: str,
        *,
        status: str = "success",
        error: Optional[str] = None,
        per_node: Optional[dict] = None,
        terminal_payload: Optional[dict] = None,
    ) -> None:
        state = await self._state_for_turn(exec_id, for_update=True)
        if state is None:
            return
        state.status = status
        if error is not None:
            state.error = error
        state.node_states = _close_running_nodes(
            per_node if per_node is not None else state.node_states,
            status="completed" if status == "success" else "error",
        )
        state.ended_at = _now()
        payload = dict(terminal_payload or {
            "wf_id": state.wf_id,
            "status": _api_terminal_status(status),
        })
        payload.setdefault("wf_id", state.wf_id)
        payload.setdefault("status", _api_terminal_status(status))
        if error:
            payload["error"] = error
        await self._append_event(state, "EXEC_UPDATE", payload)

    def get_stop_event(self, exec_id: str):
        return stop_registry.get(exec_id)

    def discard_stop_event(self, exec_id: str) -> None:
        stop_registry.discard(exec_id)

    async def get_execution(self, exec_id: str) -> Optional[dict]:
        state = await self._state_for_turn(exec_id)
        return _record_dict(state) if state is not None else None

    async def latest_execution_id(self, wf_id: str) -> Optional[str]:
        state = await self._state_for_workflow(wf_id)
        return state.turn_id if state is not None else None

    async def latest_execution(self, wf_id: str) -> Optional[dict]:
        state = await self._state_for_workflow(wf_id)
        return _record_dict(state) if state is not None else None

    async def latest_running_execution(self, wf_id: str) -> Optional[dict]:
        state = await self._state_for_workflow(wf_id)
        if state is None or state.status not in {"pending", "running"}:
            return None
        return _record_dict(state)

    async def list_executions(self, wf_id: str) -> list[dict]:
        state = await self._state_for_workflow(wf_id)
        return [_record_dict(state)] if state is not None else []
