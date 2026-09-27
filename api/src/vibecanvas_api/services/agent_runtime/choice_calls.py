"""Short host RPCs for one long-lived, durable MCP user-choice invocation."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.capability import verify_agent_capability
from vibecanvas_api.services.platform_mcp import invocation
from vibecanvas_api.services.platform_mcp.interactive_tools.render_choices import ChoicesInput, ChoicesRequest
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo


def result_for(row):
    if row.status == "submitted":
        return {"status": "selected", "selected_ids": row.interaction_result_json["selected_ids"],
                "message": "The user confirmed their selection."}
    expired = row.status == "expired" or bool((row.decision_payload_json or {}).get("reason"))
    return {"status": "expired" if expired else "cancelled", "selected_ids": [],
            "message": "No user selection was received before the tool call ended." if expired
            else "No user selection was received. The user cancelled."}


async def dispatch(token: str, arguments: dict) -> dict:
    cap = verify_agent_capability(token, secret=config.signing_secret, server="interactive")
    if cap is None:
        raise PermissionError("Invalid or expired interactive capability.")
    invocation._require_tool_capability(cap, server="interactive", tool_name="render_choices")
    await agent_context.resolve_context(cap)
    action = arguments.get("action")
    call_id = str(arguments.get("call_id") or "")
    if not call_id or len(call_id) > 128:
        raise ValueError("A bounded choice call ID is required.")
    # Deterministic, run-scoped IDs make retries idempotent, not duplicate cards.
    key = uuid.uuid5(uuid.NAMESPACE_URL, f"choices:{cap.tenant_id}:{cap.turn_id}:{call_id}").hex
    hitl_id, artifact_id = f"hitl_{key}", f"ia_{key}"
    params = {"id": hitl_id, "tenant": cap.tenant_id, "run": cap.turn_id}
    async with session_scope(tenant_id=cap.tenant_id) as session:
        repo = HitlRepo(session)
        if action == "start":
            request = ChoicesRequest.model_validate(arguments.get("input"))
            item_id = str(arguments.get("item_id") or "")
            if not item_id:
                raise ValueError("Choice call has no Runtime tool item.")
            # Also serializes concurrent retry of the same start request.
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"), params)
            if request.choice_set_id:
                from .browser_download_choices import resolve_choices
                choices = await resolve_choices(session, cap, request, hitl_id)
            else:
                choices = ChoicesInput.model_validate(request.model_dump(exclude={"choice_set_id"}))
            existing = await repo.get_request(hitl_id)
            if existing is None:
                await session.execute(text("""INSERT INTO interactive_call_leases(call_id,tenant_id,run_id,expires_at)
                    VALUES (:id,CAST(:tenant AS uuid),:run,now()+interval '60 seconds')"""), params)
                definition = {"kind": "interactive_artifact", "artifact_id": artifact_id, "hitl_request_id": hitl_id,
                    "component_type": "user_input", "completion_mode": "wait_for_submit", "title": choices.title,
                    "props": {"mode": "choices", "message": choices.description, "questions": [{
                        "id": "selected_ids", "label": choices.title, "multiple": choices.multiple,
                        "options": [{"value": o.id, "label": o.label, "description": o.description} for o in choices.options]}]},
                    "widget_state": {}, "interaction_schema": {"interaction_type": "choices", "hide_result": True}}
                envelope = {"schema_version": 1, "status": "success", "error": None,
                    "content": "Waiting for user selection.", "artifact": {"kind": "interactive_artifact", "target": {}},
                    "payload": {"kind": "interactive_artifact", "artifact": definition, "hitl_request_id": hitl_id,
                                "pending_interaction": True}, "meta": {"tool": "render_choices", "hitl_type": "elicitation"}}
                projection = {"type": "tool_update", "tool_call_id": item_id, "artifact": envelope, "status": "running"}
                ui = {"type": "INTERACTION_REQUIRED", "hitl_request_id": hitl_id, "hitl_type": "elicitation",
                    "title": choices.title, "prompt_text": choices.description, "artifact_id": artifact_id,
                    "projection_event": projection}
                await repo.create_interactive_artifact(artifact_id=artifact_id, tenant_id=cap.tenant_id,
                    chat_id=cap.chat_id, run_id=cap.turn_id, component_type="user_input", completion_mode="wait_for_submit",
                    title=choices.title, definition_json=definition, artifact_ref=None, content_hash=None)
                await repo.create_request(hitl_request_id=hitl_id, tenant_id=cap.tenant_id, chat_id=cap.chat_id,
                    run_id=cap.turn_id, artifact_id=artifact_id, hitl_type="elicitation", title=choices.title,
                    prompt_text=choices.description, ui_payload_json=ui, agent_payload_json=choices.model_dump(),
                    runtime_correlation_json={"source": "render_choices", "runtime_request_id": hitl_id,
                        "runtime_method": "render_choices", "runtime_item_id": item_id,
                        "choice_set_id": request.choice_set_id},
                    resume_payload_json={"runtime_session_id": cap.runtime_session_id}, mark_run_waiting=False)
                await repo.link_artifact_hitl(artifact_id, hitl_id)
                if request.choice_set_id:
                    from .browser_download_choices import bind_choice
                    await bind_choice(session, request.choice_set_id, hitl_id)
            else:
                if existing.agent_payload_json != choices.model_dump():
                    raise ValueError("A choice call cannot change its options after starting.")
            row = await repo.get_request(hitl_id)
            events = [{"event_type": "INTERACTION_REQUIRED", "payload": row.ui_payload_json},
                      {"event_type": "CHAT_EVENT", "payload": row.ui_payload_json["projection_event"]}]
        elif action in {"poll", "cancel", "ack"}:
            row = await repo.get_request(hitl_id)
            if row is None or row.chat_id != cap.chat_id or row.run_id != cap.turn_id:
                raise PermissionError("Choice call does not belong to this active turn.")
            events = []
            choice_set_id = (row.runtime_correlation_json or {}).get("choice_set_id")
            reconnecting = False
            if row.status == "pending" and choice_set_id:
                from .browser_download_choices import BrowserTransportUnavailable, load_candidates
                try:
                    await load_candidates(session, cap, choice_set_id)
                except BrowserTransportUnavailable:
                    # A panel refresh reauthenticates its offscreen WebSocket.
                    # Keep the same call alive only within its remaining lease:
                    # no renewal/transfer until the original fence is restored.
                    reconnecting = True
                except PermissionError:
                    row, _ = await repo.resolve(hitl_request_id=hitl_id, decision="cancel",
                        decision_payload={"reason": "The originating browser download capture ended."})
            if action == "cancel":
                row, _ = await repo.resolve(hitl_request_id=hitl_id, decision="cancel",
                    decision_payload={"reason": "The tool call ended."})
            elif action == "ack" and row.status != "pending":
                await repo.mark_runtime_control_delivered(hitl_id)
            elif row.status == "pending" and not reconnecting:
                await session.execute(text("""UPDATE interactive_call_leases SET expires_at=now()+interval '60 seconds'
                    WHERE call_id=:id AND run_id=:run AND expires_at>now()"""), params)
        else:
            raise ValueError("Unknown choice lifecycle action.")
        if row.status != "pending":
            await session.execute(text("DELETE FROM interactive_call_leases WHERE call_id=:id"), params)
            return {"result": result_for(row), "_events": [{"event_type": "HITL_RESOLVED",
                "payload": {"hitl_request_id": hitl_id, "status": row.status}}]}
        return {"pending": True, "_events": events}
