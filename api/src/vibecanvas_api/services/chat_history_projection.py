"""Project persisted interactive artifacts into chat history for display."""

from copy import deepcopy

from ..schemas.chat import HistoryMessage


def interactive_artifact_out(row, *, hitl_status: str | None = None) -> dict:
    definition = dict(row.definition_json or {})
    widget_state = dict(row.widget_state_json or {})
    interaction_result = dict(row.interaction_result_json or {})
    definition["artifact_id"] = row.artifact_id
    definition.setdefault("kind", "interactive_artifact")
    definition.setdefault("title", row.title)
    definition.setdefault("component_type", row.component_type)
    definition.setdefault("completion_mode", row.completion_mode)
    definition["widget_state"] = widget_state
    definition["hitl_request_id"] = row.hitl_request_id
    definition["interaction_state"] = {
        "is_interacted": bool(row.is_interacted),
        "status": hitl_status or (
            "submitted"
            if row.is_interacted
            else ("pending" if row.hitl_request_id else "none")
        ),
        "result": interaction_result,
    }
    return {
        "artifact_id": row.artifact_id,
        "chat_id": row.chat_id,
        "run_id": row.run_id,
        "hitl_request_id": row.hitl_request_id,
        "artifact": definition,
        "artifact_ref": row.artifact_ref,
        "content_hash": row.content_hash,
        "is_interacted": bool(row.is_interacted),
        "interaction_result_json": interaction_result,
    }


def hitl_history_projection(artifact_row, hitl_row) -> tuple[str, HistoryMessage] | None:
    """Build the durable tool-result projection for one HITL request.

    Runtime streams carry the initial projection so the card appears
    immediately.  History reads must reconstruct the same projection from the
    authoritative HITL/artifact rows; otherwise a refresh loses the card or
    revives a completed interaction as pending.
    """
    if hitl_row is None:
        return None
    ui_payload = (
        hitl_row.ui_payload_json
        if isinstance(hitl_row.ui_payload_json, dict)
        else {}
    )
    projection = ui_payload.get("projection_event")
    if not isinstance(projection, dict):
        return None
    tool_call_id = projection.get("tool_call_id")
    projected_artifact = projection.get("artifact")
    if not tool_call_id or not isinstance(projected_artifact, dict):
        return None

    envelope = deepcopy(projected_artifact)
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        envelope["payload"] = payload
    meta = envelope.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        envelope["meta"] = meta

    status = str(hitl_row.status or "pending")
    pending = status == "pending"
    is_pre_tool_approval = hitl_row.hitl_type == "pre_tool_approval"
    artifact_projection = interactive_artifact_out(
        artifact_row,
        hitl_status=status,
    )
    payload["artifact"] = artifact_projection["artifact"]
    payload["hitl_request_id"] = hitl_row.hitl_request_id
    payload["hitl_type"] = hitl_row.hitl_type
    meta["hitl_type"] = hitl_row.hitl_type
    correlation = getattr(hitl_row, "runtime_correlation_json", None) or {}
    if correlation.get("source") in {"flowork_cli", "browser_transfer"}:
        method = str(correlation.get("runtime_method") or "")
        meta["cli_tool_name"] = "flowork-cli " + method.replace(".", " ") if method else "flowork-cli"
        agent_payload = getattr(hitl_row, "agent_payload_json", None) or {}
        meta["cli_arguments"] = agent_payload.get("arguments") or {}
    if is_pre_tool_approval:
        payload["pending_approval"] = pending
        meta["pending_approval"] = pending
    else:
        # ``pending_approval`` means permission to execute a tool, not the
        # independent post-tool Continue gate. Reusing it for both makes an
        # HTML review restore as an "Approve Unknown tool" authorization card.
        payload.pop("pending_approval", None)
        meta.pop("pending_approval", None)

    created_at = getattr(artifact_row, "created_at", None)
    ts = created_at.timestamp() if created_at is not None else None
    return str(tool_call_id), HistoryMessage(
        id=f"hitl:{hitl_row.hitl_request_id}:projection",
        role="tool",
        content=str(envelope.get("content") or ""),
        ts=ts,
        tool_call_id=str(tool_call_id),
        artifact=envelope,
    )


def merge_hitl_history_projections(
    messages: list[HistoryMessage],
    projections: list[tuple[str, HistoryMessage]],
) -> list[HistoryMessage]:
    """Merge durable HITL cards into the ordinary persisted transcript."""
    stored_tool_call_ids = {
        message.tool_call_id
        for message in messages
        if message.role == "tool" and message.tool_call_id
    }
    by_tool_call: dict[str, list[HistoryMessage]] = {}
    for tool_call_id, projection in projections:
        by_tool_call.setdefault(tool_call_id, []).append(projection)

    projected_history: list[HistoryMessage] = []
    for original in messages:
        message = original
        if message.role == "tool" and message.tool_call_id:
            matching = by_tool_call.pop(message.tool_call_id, [])
            if matching:
                # A completed call already has its ordinary tool-result row.
                # Overlay the durable HITL projection onto that row instead of
                # emitting a second result with the same tool_call_id (the
                # frontend reducer correctly treats the latter as a replacement).
                message = message.model_copy(update={"artifact": matching[-1].artifact})
        projected_history.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if not isinstance(call, dict):
                continue
            tool_call_id = call.get("id") or call.get("tool_call_id")
            if tool_call_id and str(tool_call_id) not in stored_tool_call_ids:
                projected_history.extend(
                    by_tool_call.pop(str(tool_call_id), [])
                )
    # A CLI approval originates inside a shell command, not a native MCP call.
    # Restore its own persisted card even when the native transcript has no
    # matching synthetic call. Never manufacture calls for other providers.
    for tool_call_id, pending in by_tool_call.items():
        if tool_call_id.startswith(("cli_", "transfer_")):
            for projection in pending:
                cli_meta = (projection.artifact or {}).get("meta") or {}
                projected_history.append(HistoryMessage(
                    id=f"{projection.id}:call", role="assistant", content="", ts=projection.ts,
                    tool_calls=[{"id": tool_call_id, "name": cli_meta.get("cli_tool_name", "flowork-cli"),
                                 "args": cli_meta.get("cli_arguments", {})}],
                ))
                projected_history.append(projection)
    return projected_history
