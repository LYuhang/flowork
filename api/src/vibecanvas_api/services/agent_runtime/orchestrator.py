"""Backend control-plane orchestration for sandbox Agent Runtimes.

This module is the only Chat execution path allowed to select an SDK adapter.
It translates the stable runtime event protocol into the existing product event
protocol; the caller persists those product events before exposing them to SSE.
No SDK-native wire object crosses this boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import structlog

from vibecanvas_api.services.agent_runtime.approval import PreToolApprovalPolicy
from vibecanvas_api.services.agent_runtime.mcp_host_gateway import (
    handle_mcp_gateway_request,
)
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeControlResponse,
    RuntimeEvent,
    RuntimeOpenRequest,
    RuntimeRequestCorrelation,
    RuntimeTurnRequest,
    RuntimeType,
)
from vibecanvas_api.services.agent_runtime.registry import create_runtime_adapter
from vibecanvas_api.services.chat_workspace import chat_workspace_scope_id
from vibecanvas_api.services.sandbox.coordinator import get_sandbox_coordinator
from vibecanvas_api.services.vfs_volume import get_chat_runtime_volume_provider
from vibecanvas_api.storage.agent_runtime_repo import AgentRuntimeRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo

logger = structlog.get_logger(__name__)

def _runtime_status_payload(
    turn_request: RuntimeTurnRequest,
    *,
    phase: str,
    first_turn: bool,
    label: str | None = None,
) -> dict[str, Any]:
    """Build one truthful, SDK-neutral user-facing Runtime phase event."""
    payload: dict[str, Any] = {
        "phase": phase,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "first_turn": first_turn,
        "runtime_type": turn_request.runtime_type.value,
        "operation_id": turn_request.turn_id,
    }
    if label:
        payload["label"] = label
    return payload


def private_runtime_root(runtime_type: RuntimeType, chat_id: str) -> str:
    """Return the platform-owned internal state namespace for one Chat.

    Codex owns a Chat-scoped CODEX_HOME and stores only that Chat's thread state
    below it. Provider/account credentials are host-brokered and never mounted.
    """
    del chat_id
    if runtime_type != RuntimeType.CODEX:
        raise RuntimeError(f"runtime adapter unavailable: {runtime_type.value}")
    return "/runtime/.codex"


def _product_events(event: RuntimeEvent) -> list[tuple[str, dict]]:
    """Project one SDK-neutral RuntimeEvent into durable product events."""
    if event.type in {"runtime.started", "runtime.completed"}:
        return []
    if event.type == "runtime.failed":
        raise RuntimeError(str(event.payload.get("message") or "agent runtime failed"))
    if event.type == "projection":
        event_type = event.payload.get("event_type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("runtime projection is missing event_type")
        payload = event.payload.get("payload")
        return [(event_type, payload if isinstance(payload, dict) else {})]

    # Native events used by future adapters. The frontend's product protocol is
    # intentionally stable even when an SDK changes its notification names.
    chat_event_types = {
        "message.start": "message_start",
        "message.delta": "message_delta",
        "message.end": "message_end",
        "tool.start": "tool_start",
        "tool.update": "tool_update",
        "tool.end": "tool_end",
    }
    if event.type in chat_event_types:
        return [("CHAT_EVENT", {"type": chat_event_types[event.type], **event.payload})]
    if event.type == "approval.required":
        projection = event.payload.get("projection_event")
        events = [("HITL_REQUIRED", {
            key: event.payload[key]
            for key in (
                "hitl_request_id",
                "hitl_type",
                "title",
                "prompt_text",
                "actions",
            )
            if key in event.payload
        })]
        if isinstance(projection, dict):
            events.append(("CHAT_EVENT", projection))
        return events
    if event.type == "approval.resolved":
        return [("HITL_RESOLVED", event.payload)]
    if event.type == "interaction.required":
        projection = event.payload.get("projection_event")
        events = [("INTERACTION_REQUIRED", event.payload)]
        if isinstance(projection, dict):
            events.append(("CHAT_EVENT", projection))
        return events
    if event.type == "interaction.resolved":
        return [("INTERACTION_RESOLVED", event.payload)]
    if event.type == "artifact":
        return [("ARTIFACT", event.payload)]
    if event.type == "usage":
        return [("USAGE", event.payload)]
    if event.type == "checkpoint":
        # Checkpoints are backend control-plane state, not frontend events.
        return []
    raise ValueError(f"unsupported runtime event type: {event.type}")


class AgentRuntimeOrchestrator:
    """Open one Chat-bound sandbox Runtime and stream one idempotent Turn."""

    def __init__(
        self,
        sandbox_manager=None,
        approval_policy=None,
    ) -> None:
        self._sandbox_manager = sandbox_manager or get_sandbox_coordinator()
        self._approval_policy = approval_policy or PreToolApprovalPolicy()

    @staticmethod
    def _adapter(runtime_type: RuntimeType, sandbox):
        return create_runtime_adapter(runtime_type.value, sandbox)

    async def respond(
        self,
        *,
        open_request: RuntimeOpenRequest,
        response: RuntimeControlResponse,
        workspace_scope_id: str,
        current_workflow_id: str | None,
    ) -> None:
        """Deliver a decision after the DB transition is already durable."""
        sandbox = await self._sandbox_manager.get_loaded_session(
            open_request.tenant_id, workspace_scope_id,
        )
        if sandbox is None:
            raise LookupError("agent runtime is not loaded on this API worker")
        runtime = self._adapter(open_request.runtime_type, sandbox)
        await runtime.open(open_request)
        try:
            await runtime.respond(response)
        finally:
            await runtime.close()

    async def delete_state(self, open_request: RuntimeOpenRequest) -> bool:
        """Delete adapter-owned Chat state without exposing its storage model."""
        if open_request.runtime_type == RuntimeType.CODEX:
            provider = get_chat_runtime_volume_provider()
            return await asyncio.to_thread(
                provider.delete,
                tenant_id=open_request.tenant_id,
                user_id=open_request.user_id,
                chat_scope_id=chat_workspace_scope_id(open_request.chat_id),
            )
        raise RuntimeError(f"unsupported runtime: {open_request.runtime_type.value}")

    @staticmethod
    def _prepare_approval(event: RuntimeEvent) -> RuntimeEvent:
        """Persist an SDK approval gate before the frontend can observe it."""
        payload = dict(event.payload)
        hitl_request_id = str(payload.get("hitl_request_id") or "")
        if not hitl_request_id:
            raise ValueError("runtime approval is missing hitl_request_id")
        correlation = RuntimeRequestCorrelation.model_validate(
            payload.get("runtime_correlation") or {}
        )
        title = str(payload.get("title") or "Approval required")
        prompt_text = str(
            payload.get("prompt_text") or "Review this operation before continuing."
        )
        actions = payload.get("actions")
        actions = actions if isinstance(actions, list) else []
        tool_call_id = str(correlation.runtime_item_id or hitl_request_id)
        agent_payload = payload.get("agent_payload")
        agent_payload = agent_payload if isinstance(agent_payload, dict) else {}
        tool_name = str(agent_payload.get("tool") or agent_payload.get("method") or "codex")
        artifact_id = f"ia_{hitl_request_id.removeprefix('hitl_')}"
        definition = {
            "kind": "interactive_artifact",
            "schema_version": 1,
            "artifact_id": artifact_id,
            "hitl_request_id": hitl_request_id,
            "title": title,
            "component_type": "approval",
            "props": {
                "fields": [
                    {"name": "tool", "label": "Tool", "value": tool_name},
                    {"name": "reason", "label": "Reason", "value": prompt_text},
                ]
            },
            "interaction_schema": {
                "prompt_text": prompt_text,
                "submit_label": "Approve",
                "cancel_label": "Deny",
            },
            "completion_mode": "wait_for_submit",
            "height": 260,
            "placement": "inline",
            "preview": {"mode": "none"},
            "widget_state": {},
            "interaction_state": {
                "is_interacted": False,
                "status": "pending",
                "result": {},
            },
        }
        artifact = {
            "schema_version": 1,
            "status": "success",
            "error": None,
            "content": "Waiting for user approval before executing this operation.",
            "content_abstract": f"{tool_name} → waiting for user approval",
            "ref": f"tool://hitl/{hitl_request_id}",
            "artifact": {"kind": "interactive_artifact", "target": {}},
            "payload": {
                "kind": "interactive_artifact",
                "content_type": "application/vnd.vibecanvas.interactive+json",
                "artifact": definition,
                "artifact_preview": None,
                "artifact_ref": None,
                "hitl_request_id": hitl_request_id,
                "pending_approval": True,
            },
            "meta": {
                "tool": tool_name,
                "hitl_type": "pre_tool_approval",
                "pending_approval": True,
            },
        }
        projection_event = {
            "type": "tool_update",
            "tool_call_id": tool_call_id,
            "artifact": artifact,
            "status": "running",
        }
        # The tenant id is not part of RuntimeEvent by design; the caller uses
        # its authoritative RuntimeTurnRequest when it writes product state.
        return event.model_copy(update={
            "payload": {
                "hitl_request_id": hitl_request_id,
                "hitl_type": "pre_tool_approval",
                "title": title,
                "prompt_text": prompt_text,
                "actions": actions,
                "projection_event": projection_event,
                "_persist": {
                    "artifact_id": artifact_id,
                    "definition": definition,
                    "agent_payload": agent_payload,
                    "runtime_correlation": correlation.model_dump(mode="json"),
                },
            }
        })

    @staticmethod
    async def _persist_approval_for_turn(
        event: RuntimeEvent,
        turn_request: RuntimeTurnRequest,
    ) -> RuntimeEvent:
        prepared = AgentRuntimeOrchestrator._prepare_approval(event)
        payload = dict(prepared.payload)
        private = payload.pop("_persist")
        async with session_scope(tenant_id=turn_request.tenant_id) as session:
            repo = HitlRepo(session)
            await repo.create_interactive_artifact(
                artifact_id=private["artifact_id"],
                tenant_id=turn_request.tenant_id,
                chat_id=turn_request.chat_id,
                run_id=turn_request.turn_id,
                component_type="approval",
                completion_mode="wait_for_submit",
                title=payload["title"],
                definition_json=private["definition"],
                artifact_ref=None,
                content_hash=None,
                hitl_request_id=None,
            )
            await repo.create_request(
                hitl_request_id=payload["hitl_request_id"],
                tenant_id=turn_request.tenant_id,
                chat_id=turn_request.chat_id,
                run_id=turn_request.turn_id,
                artifact_id=private["artifact_id"],
                hitl_type="pre_tool_approval",
                title=payload["title"],
                prompt_text=payload["prompt_text"],
                ui_payload_json={
                    "type": "HITL_REQUIRED",
                    **payload,
                },
                agent_payload_json=private["agent_payload"],
                runtime_correlation_json=private["runtime_correlation"],
                resume_payload_json={
                    "runtime_type": event.runtime_type.value,
                    "runtime_session_id": event.runtime_session_id,
                },
                mark_run_waiting=True,
            )
            await repo.link_artifact_hitl(
                private["artifact_id"], payload["hitl_request_id"]
            )
        return prepared.model_copy(update={"payload": payload})

    @staticmethod
    async def _persist_interaction_for_turn(
        event: RuntimeEvent,
        turn_request: RuntimeTurnRequest,
    ) -> RuntimeEvent:
        """Persist same-Turn native input or bind a completed artifact gate."""
        payload = dict(event.payload)
        hitl_request_id = str(payload.get("hitl_request_id") or "")
        artifact_id = str(payload.get("artifact_id") or "")
        if not hitl_request_id or not artifact_id:
            raise ValueError("runtime interaction is missing stable ids")
        correlation = RuntimeRequestCorrelation.model_validate(
            payload.get("runtime_correlation") or {}
        )
        if payload.get("resume_mode") == "same_turn":
            definition = payload.get("interaction_definition")
            if not isinstance(definition, dict):
                raise ValueError("runtime interaction is missing its form definition")
            if (
                definition.get("kind") != "interactive_artifact"
                or definition.get("component_type") != "user_input"
            ):
                raise ValueError("runtime interaction form definition is invalid")
            title = str(payload.get("title") or definition.get("title") or "Input required")
            prompt_text = str(
                payload.get("prompt_text")
                or "Provide the requested information to continue."
            )
            tool_call_id = str(
                payload.get("tool_call_id")
                or correlation.runtime_item_id
                or hitl_request_id
            )
            agent_payload = payload.get("agent_payload")
            agent_payload = agent_payload if isinstance(agent_payload, dict) else {}
            artifact_envelope = {
                "schema_version": 1,
                "status": "success",
                "error": None,
                "content": "Waiting for user input before continuing.",
                "content_abstract": "Runtime → waiting for user input",
                "ref": f"tool://hitl/{hitl_request_id}",
                "artifact": {"kind": "interactive_artifact", "target": {}},
                "payload": {
                    "kind": "interactive_artifact",
                    "content_type": "application/vnd.vibecanvas.interactive+json",
                    "artifact": definition,
                    "artifact_preview": None,
                    "artifact_ref": None,
                    "hitl_request_id": hitl_request_id,
                    "pending_interaction": True,
                },
                "meta": {
                    "tool": str(agent_payload.get("method") or "runtime_input"),
                    "hitl_type": "elicitation",
                    "pending_interaction": True,
                },
            }
            projection_event = {
                "type": "tool_update",
                "tool_call_id": tool_call_id,
                "artifact": artifact_envelope,
                "status": "running",
            }
            async with session_scope(tenant_id=turn_request.tenant_id) as session:
                repo = HitlRepo(session)
                await repo.create_interactive_artifact(
                    artifact_id=artifact_id,
                    tenant_id=turn_request.tenant_id,
                    chat_id=turn_request.chat_id,
                    run_id=turn_request.turn_id,
                    component_type="user_input",
                    completion_mode="wait_for_submit",
                    title=title,
                    definition_json=definition,
                    artifact_ref=None,
                    content_hash=None,
                    hitl_request_id=None,
                )
                await repo.create_request(
                    hitl_request_id=hitl_request_id,
                    tenant_id=turn_request.tenant_id,
                    chat_id=turn_request.chat_id,
                    run_id=turn_request.turn_id,
                    artifact_id=artifact_id,
                    hitl_type="elicitation",
                    title=title,
                    prompt_text=prompt_text,
                    ui_payload_json={
                        "type": "INTERACTION_REQUIRED",
                        "hitl_request_id": hitl_request_id,
                        "hitl_type": "elicitation",
                        "title": title,
                        "prompt_text": prompt_text,
                        "artifact_id": artifact_id,
                        "projection_event": projection_event,
                    },
                    agent_payload_json=agent_payload,
                    runtime_correlation_json=correlation.model_dump(mode="json"),
                    resume_payload_json={
                        "runtime_type": event.runtime_type.value,
                        "runtime_session_id": event.runtime_session_id,
                        "resume_mode": "same_turn",
                    },
                    mark_run_waiting=True,
                )
                await repo.link_artifact_hitl(artifact_id, hitl_request_id)
            return event.model_copy(update={
                "payload": {
                    "hitl_request_id": hitl_request_id,
                    "hitl_type": "elicitation",
                    "title": title,
                    "prompt_text": prompt_text,
                    "artifact_id": artifact_id,
                    "resume_mode": "same_turn",
                    "projection_event": projection_event,
                }
            })

        artifact_envelope = payload.get("artifact")
        artifact_envelope = (
            artifact_envelope if isinstance(artifact_envelope, dict) else {}
        )
        async with session_scope(tenant_id=turn_request.tenant_id) as session:
            repo = HitlRepo(session)
            artifact = await repo.get_artifact(artifact_id)
            if artifact is None or artifact.chat_id != turn_request.chat_id:
                raise RuntimeError(
                    f"interactive artifact {artifact_id} is not durable in this chat"
                )
            definition = (
                artifact.definition_json
                if isinstance(artifact.definition_json, dict)
                else {}
            )
            interaction_schema = definition.get("interaction_schema")
            interaction_schema = (
                interaction_schema
                if isinstance(interaction_schema, dict)
                else {}
            )
            continue_only = bool(
                definition.get("require_human_confirm")
                or interaction_schema.get("interaction_type") == "continue"
            )
            interaction_type = "continue" if continue_only else "input"
            hitl_type = "post_tool_review" if continue_only else "elicitation"
            title = str(
                definition.get("title") or artifact.title or "Interactive review"
            )
            prompt_text = (
                "Review the interactive content, then click Continue."
                if continue_only
                else "Review the interactive content and submit or cancel before continuing."
            )
            agent_payload = {
                "tool": "render_interactive",
                "artifact_id": artifact_id,
                "component_type": artifact.component_type,
                "title": title,
                "awaiting_user_input": True,
                "resume_mode": "new_turn",
                "interaction_type": interaction_type,
            }
            request = await repo.create_request(
                hitl_request_id=hitl_request_id,
                tenant_id=turn_request.tenant_id,
                chat_id=turn_request.chat_id,
                run_id=turn_request.turn_id,
                artifact_id=artifact_id,
                hitl_type=hitl_type,
                title=title,
                prompt_text=prompt_text,
                ui_payload_json={
                    "type": (
                        "INTERACTIVE_CONTINUE_REQUIRED"
                        if continue_only
                        else "INTERACTIVE_INPUT_REQUIRED"
                    ),
                    "artifact_id": artifact_id,
                    "artifact_ref": f"interactive_artifact:{artifact_id}",
                    "interaction_type": interaction_type,
                    "projection_event": {
                        "type": "tool_end",
                        "tool_call_id": str(
                            payload.get("tool_call_id") or artifact_id
                        ),
                        "name": "render_interactive",
                        "status": "done",
                        "content": str(artifact_envelope.get("content") or ""),
                        "artifact": artifact_envelope,
                    },
                },
                agent_payload_json=agent_payload,
                runtime_correlation_json=correlation.model_dump(mode="json"),
                resume_payload_json={
                    "tool": "render_interactive",
                    "artifact_id": artifact_id,
                    "completion_mode": "wait_for_submit",
                    "resume_mode": "new_turn",
                },
                # The completed Codex Turn is intentionally not kept in a
                # cross-worker resumable waiting state.
                mark_run_waiting=False,
            )
            if (
                request.chat_id != turn_request.chat_id
                or request.artifact_id != artifact_id
            ):
                raise RuntimeError("runtime interaction HITL binding mismatch")
            await repo.link_artifact_hitl(artifact_id, hitl_request_id)
            await repo.commit()
        # Product-facing gate metadata is derived from the encrypted durable
        # artifact, not trusted from an SDK adapter's event payload.
        payload.update({
            "hitl_type": hitl_type,
            "title": title,
            "prompt_text": prompt_text,
            "agent_payload": agent_payload,
        })
        return event.model_copy(update={"payload": payload})

    async def stream_turn(
        self,
        *,
        open_request: RuntimeOpenRequest,
        turn_request: RuntimeTurnRequest,
        workspace_scope_id: str,
        current_workflow_id: str | None,
        stop_event: asyncio.Event,
    ) -> AsyncIterator[tuple[str, dict]]:
        turn_started = perf_counter()
        if open_request.chat_id != turn_request.chat_id:
            raise ValueError("runtime open/turn chat_id mismatch")
        if open_request.runtime_session_id != turn_request.runtime_session_id:
            raise ValueError("runtime open/turn session mismatch")
        if open_request.runtime_type != turn_request.runtime_type:
            raise ValueError("runtime open/turn type mismatch")
        if open_request.runtime_root != turn_request.runtime_root:
            raise ValueError("runtime open/turn root mismatch")

        # runtime_state_ref may be allocated when the Chat binding is created,
        # before the first model Turn. The command snapshot is the authoritative
        # product-level first-Turn flag used by both Runtime adapters.
        first_turn = bool(turn_request.command_context.is_first)
        # Emit phases before the await they describe. This is intentionally
        # done for warm turns too: resident reuse still performs an acquire,
        # while the frontend coalesces phases that finish in under 400ms.
        yield (
            "RUNTIME_STATUS",
            _runtime_status_payload(
                turn_request,
                phase="acquiring_sandbox",
                first_turn=first_turn,
            ),
        )
        phase_started = perf_counter()
        sandbox = await self._sandbox_manager.get_session(
            turn_request.tenant_id,
            workspace_scope_id,
            turn_request.user_id,
            expose_run=True,
            expose_runtime=turn_request.runtime_type == RuntimeType.CODEX,
            lease="resident",
        )
        logger.info(
            "agent_runtime_timing",
            phase="sandbox_session_acquire",
            elapsed_ms=int((perf_counter() - phase_started) * 1000),
            runtime_type=turn_request.runtime_type.value,
            chat_id=turn_request.chat_id,
            turn_id=turn_request.turn_id,
            first_turn=first_turn,
        )
        runtime = self._adapter(turn_request.runtime_type, sandbox)

        yield (
            "RUNTIME_STATUS",
            _runtime_status_payload(
                turn_request,
                phase="initializing_runtime",
                first_turn=first_turn,
            ),
        )
        phase_started = perf_counter()
        await runtime.open(open_request)
        logger.info(
            "agent_runtime_timing",
            phase="runtime_adapter_open",
            elapsed_ms=int((perf_counter() - phase_started) * 1000),
            runtime_type=turn_request.runtime_type.value,
            chat_id=turn_request.chat_id,
            turn_id=turn_request.turn_id,
        )
        approval_tasks: set[asyncio.Task] = set()

        async def cancel_pending_hitl() -> None:
            """Freeze every unresolved gate before tearing down the Runtime.

            The cancel endpoint commits ``AgentRun.status=cancel_requested``
            before setting ``stop_event``.  Resolving the requests here
            therefore cannot accidentally move the Run back to ``running``;
            it only makes the user-visible cards terminal and durable.
            """
            async with session_scope(
                tenant_id=turn_request.tenant_id
            ) as session:
                repo = HitlRepo(session)
                pending = await repo.list_pending_for_run(turn_request.turn_id)
                for request in pending:
                    await repo.resolve(
                        hitl_request_id=request.hitl_request_id,
                        decision="cancel",
                        decision_payload={
                            "reason": "turn_cancelled",
                            "message": (
                                "The user stopped this Agent turn before "
                                "completing the pending interaction."
                            ),
                        },
                        interaction_result={
                            "reason": "turn_cancelled",
                            "message": (
                                "This interaction was cancelled because the "
                                "Agent turn was stopped."
                            ),
                        },
                    )

        async def deliver_persisted_approval(hitl_request_id: str) -> None:
            """Use PostgreSQL as the cross-worker approval rendezvous."""
            while True:
                await asyncio.sleep(0.4)
                async with session_scope(
                    tenant_id=turn_request.tenant_id
                ) as session:
                    row = await HitlRepo(session).get_request(hitl_request_id)
                    if row is None:
                        raise RuntimeError(
                            f"HITL request {hitl_request_id} disappeared"
                        )
                    if row.status == "pending":
                        continue
                    status = str(row.status)
                    correlation = dict(row.runtime_correlation_json or {})
                    decision_payload = dict(row.decision_payload_json or {})
                action = {
                    "approved": "approve",
                    "denied": "deny",
                    "cancelled": "cancel",
                }.get(status)
                if action is None:
                    raise RuntimeError(
                        f"invalid pre-tool approval status: {status}"
                    )
                await runtime.respond(
                    RuntimeControlResponse(
                        request_id=hitl_request_id,
                        chat_id=turn_request.chat_id,
                        turn_id=turn_request.turn_id,
                        gate_type="pre_tool_approval",
                        action=action,
                        persisted=True,
                        payload=decision_payload,
                        correlation=correlation,
                    )
                )
                return

        async def deliver_persisted_interaction(hitl_request_id: str) -> None:
            """Resume a suspended Runtime input request through the DB rendezvous."""
            while True:
                await asyncio.sleep(0.4)
                async with session_scope(
                    tenant_id=turn_request.tenant_id
                ) as session:
                    row = await HitlRepo(session).get_request(hitl_request_id)
                    if row is None:
                        raise RuntimeError(
                            f"HITL request {hitl_request_id} disappeared"
                        )
                    if row.status == "pending":
                        continue
                    status = str(row.status)
                    correlation = dict(row.runtime_correlation_json or {})
                    decision_payload = dict(row.decision_payload_json or {})
                    interaction_result = dict(row.interaction_result_json or {})
                action = {
                    "submitted": "submit",
                    "cancelled": "cancel",
                }.get(status)
                if action is None:
                    raise RuntimeError(
                        f"invalid Runtime interaction status: {status}"
                    )
                await runtime.respond(
                    RuntimeControlResponse(
                        request_id=hitl_request_id,
                        chat_id=turn_request.chat_id,
                        turn_id=turn_request.turn_id,
                        gate_type="post_tool_interaction",
                        action=action,
                        persisted=True,
                        payload={
                            "decision_payload": decision_payload,
                            "interaction_result": interaction_result,
                        },
                        correlation=correlation,
                    )
                )
                return

        yield (
            "RUNTIME_STATUS",
            _runtime_status_payload(
                turn_request,
                phase="connecting_model",
                first_turn=first_turn,
            ),
        )
        runtime_events = runtime.run_turn(turn_request)
        stop_task = asyncio.create_task(stop_event.wait())
        completed = False
        first_runtime_event = True
        first_product_event = True
        first_model_text_delta = True
        runtime_ready_at: float | None = None
        try:
            while True:
                next_event = asyncio.create_task(anext(runtime_events))
                done, _ = await asyncio.wait(
                    {next_event, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    # A Runtime may be suspended inside an SDK-native approval
                    # request and consequently produce no more events.  Do not
                    # wait for that iterator to advance: persist/freeze HITL,
                    # ask the Runtime to interrupt, then cancel the transport
                    # read.  Closing the host iterator tears down the sandbox
                    # process and lets the outer Turn loop emit its durable
                    # terminal ``error(code=cancelled)`` frame.
                    await cancel_pending_hitl()
                    try:
                        await asyncio.wait_for(
                            runtime.cancel(turn_request.turn_id),
                            timeout=1.0,
                        )
                    except (TimeoutError, LookupError, RuntimeError):
                        # The broker may not be registered yet or may already
                        # be closing. Cancelling ``next_event`` below is the
                        # authoritative Runtime-boundary interruption.
                        pass
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    break
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    break
                if first_runtime_event:
                    first_runtime_event = False
                    logger.info(
                        "agent_runtime_timing",
                        phase="first_runtime_event",
                        event_type=event.type,
                        elapsed_ms=int((perf_counter() - turn_started) * 1000),
                        runtime_type=turn_request.runtime_type.value,
                        chat_id=turn_request.chat_id,
                        turn_id=turn_request.turn_id,
                    )
                if event.type == "runtime.started":
                    runtime_ready_at = perf_counter()
                    yield (
                        "RUNTIME_STATUS",
                        _runtime_status_payload(
                            turn_request,
                            phase="awaiting_first_output",
                            first_turn=first_turn,
                        ),
                    )
                    timings = event.payload.get("timings_ms")
                    if isinstance(timings, dict):
                        logger.info(
                            "codex_runtime_setup_timing",
                            runtime_type=turn_request.runtime_type.value,
                            chat_id=turn_request.chat_id,
                            turn_id=turn_request.turn_id,
                            first_turn=first_turn,
                            mcp_server_count=event.payload.get("mcp_server_count"),
                            **{
                                f"timing_{key}": value
                                for key, value in timings.items()
                                if isinstance(value, (int, float))
                            },
                        )
                if event.type == "runtime.completed":
                    completed = True
                if event.type == "checkpoint":
                    state_ref = str(event.payload.get("state_ref") or "")
                    previous_state_ref = str(
                        event.payload.get("previous_state_ref") or ""
                    )
                    async with session_scope(
                        tenant_id=turn_request.tenant_id
                    ) as session:
                        binding = await AgentRuntimeRepo(
                            session, turn_request.user_id
                        ).set_runtime_state_ref(
                            turn_request.chat_id,
                            runtime_type=event.runtime_type.value,
                            runtime_session_id=event.runtime_session_id,
                            state_ref=state_ref,
                            previous_state_ref=previous_state_ref or None,
                        )
                        if binding is None:
                            raise RuntimeError(
                                "runtime checkpoint Chat no longer exists"
                            )
                if event.type == "mcp.gateway.requested":
                    await runtime.respond(
                        await handle_mcp_gateway_request(event, turn_request)
                    )
                    # Private MCP transport traffic is never persisted as a
                    # product event or exposed to the browser client.
                    continue
                if event.type == "approval.requested":
                    payload = dict(event.payload)
                    correlation = RuntimeRequestCorrelation.model_validate(
                        payload.get("runtime_correlation") or {}
                    )
                    agent_payload = payload.get("agent_payload")
                    agent_payload = (
                        agent_payload if isinstance(agent_payload, dict) else {}
                    )
                    arguments = agent_payload.get("arguments")
                    arguments = arguments if isinstance(arguments, dict) else {}
                    policy_hint = payload.get("policy")
                    policy_hint = (
                        policy_hint if isinstance(policy_hint, dict) else {}
                    )
                    decision = self._approval_policy.evaluate(
                        approval_mode=turn_request.approval_mode,
                        source=correlation.source,
                        tool_name=str(
                            agent_payload.get("tool")
                            or agent_payload.get("method")
                            or ""
                        ),
                        arguments=arguments,
                        native_required=bool(
                            policy_hint.get("native_required", False)
                        ),
                    )
                    if decision.action != "wait":
                        await runtime.respond(
                            RuntimeControlResponse(
                                request_id=str(
                                    payload.get("hitl_request_id") or ""
                                ),
                                chat_id=turn_request.chat_id,
                                turn_id=turn_request.turn_id,
                                gate_type="pre_tool_approval",
                                action=(
                                    "approve"
                                    if decision.action == "allow"
                                    else "deny"
                                ),
                                persisted=False,
                                payload={"policy_reason": decision.reason},
                                correlation=correlation,
                            )
                        )
                        continue
                    event = event.model_copy(update={"type": "approval.required"})

                if event.type == "approval.required":
                    event = await self._persist_approval_for_turn(
                        event, turn_request
                    )
                    hitl_request_id = str(
                        event.payload.get("hitl_request_id") or ""
                    )
                    approval_task = asyncio.create_task(
                        deliver_persisted_approval(hitl_request_id)
                    )
                    approval_tasks.add(approval_task)
                if event.type == "interaction.required":
                    event = await self._persist_interaction_for_turn(
                        event, turn_request
                    )
                    if event.payload.get("resume_mode") == "same_turn":
                        hitl_request_id = str(
                            event.payload.get("hitl_request_id") or ""
                        )
                        interaction_task = asyncio.create_task(
                            deliver_persisted_interaction(hitl_request_id)
                        )
                        approval_tasks.add(interaction_task)
                for product_event in _product_events(event):
                    product_payload = product_event[1]
                    product_kind = (
                        str(product_payload.get("type") or "")
                        if isinstance(product_payload, dict)
                        else ""
                    )
                    if product_kind in {"tool_start", "tool_update"}:
                        yield (
                            "RUNTIME_STATUS",
                            _runtime_status_payload(
                                turn_request,
                                phase="running_tool",
                                first_turn=first_turn,
                                label=str(
                                    product_payload.get("name")
                                    or product_payload.get("tool_name")
                                    or ""
                                ) or None,
                            ),
                        )
                    if first_product_event:
                        first_product_event = False
                        now = perf_counter()
                        logger.info(
                            "agent_runtime_timing",
                            phase="first_product_event",
                            event_type=product_event[0],
                            elapsed_ms=int((now - turn_started) * 1000),
                            application_setup_ms=(
                                int((runtime_ready_at - turn_started) * 1000)
                                if runtime_ready_at is not None
                                else None
                            ),
                            model_first_event_ms=(
                                int((now - runtime_ready_at) * 1000)
                                if runtime_ready_at is not None
                                else None
                            ),
                            runtime_type=turn_request.runtime_type.value,
                            chat_id=turn_request.chat_id,
                            turn_id=turn_request.turn_id,
                            first_turn=first_turn,
                        )
                    if event.type == "message.delta" and first_model_text_delta:
                        first_model_text_delta = False
                        now = perf_counter()
                        logger.info(
                            "agent_runtime_timing",
                            phase="first_model_text_delta",
                            elapsed_ms=int((now - turn_started) * 1000),
                            application_setup_ms=(
                                int((runtime_ready_at - turn_started) * 1000)
                                if runtime_ready_at is not None
                                else None
                            ),
                            model_ttft_ms=(
                                int((now - runtime_ready_at) * 1000)
                                if runtime_ready_at is not None
                                else None
                            ),
                            runtime_type=turn_request.runtime_type.value,
                            chat_id=turn_request.chat_id,
                            turn_id=turn_request.turn_id,
                            first_turn=first_turn,
                        )
                    yield product_event
                    if product_kind == "tool_end":
                        yield (
                            "RUNTIME_STATUS",
                            _runtime_status_payload(
                                turn_request,
                                phase="finalizing",
                                first_turn=first_turn,
                            ),
                        )
            if not completed and not stop_event.is_set():
                raise RuntimeError("agent runtime stream ended without runtime.completed")
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await runtime_events.aclose()
            for task in approval_tasks:
                task.cancel()
            await asyncio.gather(*approval_tasks, return_exceptions=True)
            await runtime.close()
            set_session_lease = getattr(
                self._sandbox_manager,
                "set_session_lease",
                None,
            )
            if set_session_lease is not None:
                await set_session_lease(
                    turn_request.tenant_id,
                    workspace_scope_id,
                    "interactive",
                )
            logger.info(
                "agent_runtime_timing",
                phase="turn_total",
                elapsed_ms=int((perf_counter() - turn_started) * 1000),
                runtime_type=turn_request.runtime_type.value,
                chat_id=turn_request.chat_id,
                turn_id=turn_request.turn_id,
                completed=completed,
                cancelled=stop_event.is_set(),
            )
