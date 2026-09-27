"""Project Codex native thread state into the Runtime-neutral debug schema.

Some runtimes expose the exact message list passed to their model call. Codex
owns context assembly inside app-server, so its closest honest boundary is the
native Thread projection returned by ``thread/resume`` plus the input about to
be sent to ``turn/start``. Both use the same outer snapshot contract so the
frontend Inspector stays Runtime-neutral.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any
from types import SimpleNamespace


DEBUG_DIR = "/logs/.debug"
_MESSAGE_PREVIEW_CHARS = 1200
_MESSAGE_CHUNK_CHARS = 64 * 1024
_UNSAFE_FILE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _safe_file_component(value: Any) -> str:
    cleaned = _UNSAFE_FILE_COMPONENT.sub("_", str(value)).strip("._")
    return cleaned[:160] or "unknown"


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(value)


def _user_input_text(content: Any) -> str:
    if not isinstance(content, list):
        return _json_text(content)
    parts: list[str] = []
    for raw in content:
        if not isinstance(raw, dict):
            parts.append(str(raw))
            continue
        kind = str(raw.get("type") or "input")
        if kind == "text":
            parts.append(str(raw.get("text") or ""))
        elif kind in {"image", "audio"}:
            parts.append(f"[{kind}] {raw.get('url') or ''}".rstrip())
        elif kind in {"localImage", "localAudio"}:
            parts.append(f"[{kind}] {raw.get('path') or ''}".rstrip())
        elif kind in {"mention", "skill"}:
            label = str(raw.get("name") or kind)
            parts.append(f"[{kind}: {label}] {raw.get('path') or ''}".rstrip())
        else:
            parts.append(_json_text(raw))
    return "\n".join(part for part in parts if part)


def _base_message(
    *,
    debug_id: str,
    source_id: str | None,
    role: str,
    content: str,
    item_type: str,
    turn_id: str | None,
    synthetic: bool = False,
    synthetic_kind: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    path: str | None = None,
    error: bool = False,
    runtime_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    truncated = False
    message: dict[str, Any] = {
        "debug_id": debug_id,
        "source_message_id": source_id,
        "role": role,
        "synthetic": synthetic,
        "form": "raw",
        "token_field": "raw",
        "tokens": None,
        "token_slots": {
            "raw": None,
            "preview": None,
            "abstract": None,
            "ref": None,
            "compressed": None,
        },
        "content": content,
        "runtime_item_type": item_type,
        "runtime_metadata": {
            "codex_turn_id": turn_id,
            **(runtime_metadata or {}),
        },
    }
    if synthetic_kind:
        message["synthetic_kind"] = synthetic_kind
    if tool_name:
        message["tool_name"] = tool_name
    if tool_call_id:
        message["tool_call_id"] = tool_call_id
    if path:
        message["path"] = path
    if error:
        message["error"] = True
    if truncated:
        message["content_truncated"] = True
    return message, truncated


def _thread_item_message(
    item: dict[str, Any],
    *,
    debug_id: str,
    turn_id: str | None,
) -> tuple[dict[str, Any], bool]:
    kind = str(item.get("type") or "unknown")
    item_id = str(item.get("id") or "") or None
    metadata: dict[str, Any] = {}

    if kind == "userMessage":
        return _base_message(
            debug_id=debug_id,
            source_id=item_id,
            role="user",
            content=_user_input_text(item.get("content")),
            item_type=kind,
            turn_id=turn_id,
            runtime_metadata={"client_id": item.get("clientId")},
        )
    if kind == "agentMessage":
        metadata = {
            "phase": item.get("phase"),
            "memory_citation": item.get("memoryCitation"),
        }
        return _base_message(
            debug_id=debug_id,
            source_id=item_id,
            role="assistant",
            content=str(item.get("text") or ""),
            item_type=kind,
            turn_id=turn_id,
            runtime_metadata=metadata,
        )
    if kind == "reasoning":
        # Never expose raw hidden reasoning content. Codex's explicit summary is
        # the supported diagnostic surface.
        summary = item.get("summary")
        summary_text = "\n".join(str(value) for value in summary or [])
        return _base_message(
            debug_id=debug_id,
            source_id=item_id,
            role="assistant",
            content=summary_text or "[Codex reasoning summary unavailable]",
            item_type=kind,
            turn_id=turn_id,
            synthetic=True,
            synthetic_kind="reasoning_summary",
            runtime_metadata={"has_hidden_content": bool(item.get("content"))},
        )
    if kind in {"plan", "hookPrompt", "contextCompaction"}:
        if kind == "plan":
            content = str(item.get("text") or "")
        elif kind == "hookPrompt":
            content = _json_text(item.get("fragments") or [])
        else:
            content = "Codex compacted its native thread context at this point."
        return _base_message(
            debug_id=debug_id,
            source_id=item_id,
            role="system",
            content=content,
            item_type=kind,
            turn_id=turn_id,
            synthetic=True,
            synthetic_kind={
                "plan": "codex_plan",
                "hookPrompt": "codex_hook_prompt",
                "contextCompaction": "context_compaction",
            }[kind],
        )

    if kind == "commandExecution":
        status = str(item.get("status") or "")
        content = str(item.get("aggregatedOutput") or "")
        return _base_message(
            debug_id=debug_id,
            source_id=item_id,
            role="tool",
            content=content,
            item_type=kind,
            turn_id=turn_id,
            tool_name="shell",
            tool_call_id=item_id,
            path=str(item.get("cwd") or "") or None,
            error=status in {"failed", "declined", "errored"},
            runtime_metadata={
                "command": item.get("command"),
                "status": status,
                "exit_code": item.get("exitCode"),
                "duration_ms": item.get("durationMs"),
                "source": item.get("source"),
            },
        )
    if kind == "fileChange":
        status = str(item.get("status") or "")
        return _base_message(
            debug_id=debug_id,
            source_id=item_id,
            role="tool",
            content=_json_text(item.get("changes") or []),
            item_type=kind,
            turn_id=turn_id,
            tool_name="file_change",
            tool_call_id=item_id,
            error=status in {"failed", "declined", "errored"},
            runtime_metadata={"status": status},
        )
    if kind in {"mcpToolCall", "dynamicToolCall"}:
        status = str(item.get("status") or "")
        name = str(item.get("tool") or kind)
        result = (
            item.get("result")
            if item.get("result") is not None
            else item.get("contentItems")
        )
        content = _json_text({
            "arguments": item.get("arguments") or {},
            "result": result,
            "error": item.get("error"),
        })
        return _base_message(
            debug_id=debug_id,
            source_id=item_id,
            role="tool",
            content=content,
            item_type=kind,
            turn_id=turn_id,
            tool_name=name,
            tool_call_id=item_id,
            error=bool(item.get("error")) or status in {"failed", "errored"},
            runtime_metadata={
                "status": status,
                "server": item.get("server"),
                "namespace": item.get("namespace"),
                "plugin_id": item.get("pluginId"),
                "duration_ms": item.get("durationMs"),
            },
        )

    toolish = kind in {
        "webSearch",
        "imageView",
        "imageGeneration",
        "collabAgentToolCall",
        "subAgentActivity",
        "sleep",
    }
    content = _json_text({
        key: value for key, value in item.items() if key not in {"id", "type"}
    })
    path = str(item.get("path") or item.get("savedPath") or "") or None
    return _base_message(
        debug_id=debug_id,
        source_id=item_id,
        role="tool" if toolish else "system",
        content=content,
        item_type=kind,
        turn_id=turn_id,
        synthetic=not toolish,
        synthetic_kind=None if toolish else f"codex_{kind}",
        tool_name=kind if toolish else None,
        tool_call_id=item_id if toolish else None,
        path=path,
    )


def _durable_history_message(
    entry: Any,
    *,
    debug_id: str,
) -> tuple[dict[str, Any], bool]:
    """Project one backend-owned durable-transcript row into the debug schema.

    Used only when the native thread projection has nothing to show (for
    example, right after ``thread/fork`` re-materializes the thread and its
    own turn list starts empty even though the product conversation is not).
    """
    role = str(getattr(entry, "role", "") or "user")
    text = str(getattr(entry, "text", "") or "")
    tool_calls = getattr(entry, "tool_calls", None) or []
    content = text
    if not content and tool_calls:
        content = _json_text([
            {
                "tool_call_id": getattr(call, "tool_call_id", ""),
                "name": getattr(call, "name", ""),
                "arguments": getattr(call, "arguments", ""),
            }
            for call in tool_calls
        ])
    status = getattr(entry, "status", None)
    return _base_message(
        debug_id=debug_id,
        source_id=str(getattr(entry, "message_id", "") or "") or None,
        role=role,
        content=content,
        item_type="durableHistoryMessage",
        turn_id=str(getattr(entry, "turn_id", "") or "") or None,
        tool_call_id=str(getattr(entry, "tool_call_id", "") or "") or None,
        runtime_metadata={"status": status} if status else None,
    )


def _expand_recovery_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode only the platform's known recovery envelope, never arbitrary JSON.

    This is an observational projection: it does not rewrite the native thread
    or claim that recovered records were individual native model messages.
    """
    content = message["content"]
    marker = "<durable-conversation-history>"
    if (message["role"] != "user" or marker not in content or not content.startswith("<system-reminder>")
            or "The native Codex thread did not prove that it covered the durable product transcript." not in content.split(marker, 1)[0]):
        return [message]
    start = content.index(marker) + len(marker)
    encoded = content[start:].lstrip()
    try:
        records, end = json.JSONDecoder().raw_decode(encoded)
    except (ValueError, RecursionError):
        return [message]
    suffix = encoded[end:]
    closing = "</durable-conversation-history>"
    if not suffix.lstrip().startswith(closing) or not isinstance(records, list):
        return [message]
    if not all(isinstance(row, dict) and row.get("role") in {"user", "assistant", "tool", "system"}
               and isinstance(row.get("text", ""), str)
               and (row.get("tool_calls") is None or isinstance(row["tool_calls"], list))
               for row in records):
        return [message]
    projected = []
    for row in records:
        entry = SimpleNamespace(**{**row, "tool_calls": [SimpleNamespace(**call)
            for call in (row.get("tool_calls") or []) if isinstance(call, dict)]})
        item, _ = _durable_history_message(entry, debug_id="pending")
        item["runtime_item_type"] = "recoveredHistoryMessage"
        item["runtime_metadata"].update(projection="durable_recovery", native_source_id=message.get("source_message_id"))
        projected.append(item)
    # Retain the non-history instructions/current question without the nested list.
    remainder = content[:start - len(marker)] + f"[Recovered {len(records)} conversation messages; displayed separately.]" + suffix[suffix.index(closing) + len(closing):]
    projected.append({**message, "content": remainder})
    return projected


def build_codex_debug_snapshot(
    *,
    request: Any,
    thread: dict[str, Any],
    thread_id: str,
    current_input: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one pre-turn Codex context snapshot without mutating Runtime data."""
    stamp = _utc_stamp()
    snapshot_id = (
        f"{stamp}__turn_{_safe_file_component(request.chat_id)}__codex_"
        f"{_safe_file_component(request.turn_id)}"
    )
    messages: list[dict[str, Any]] = []
    prior_turns = thread.get("turns")
    prior_turns = prior_turns if isinstance(prior_turns, list) else []
    history_complete = True
    history_source = "native_thread"

    for turn in prior_turns:
        if not isinstance(turn, dict):
            continue
        native_turn_id = str(turn.get("id") or "") or None
        if str(turn.get("itemsView") or "full") != "full":
            history_complete = False
        items = turn.get("items")
        items = items if isinstance(items, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            message, _ = _thread_item_message(
                item,
                debug_id=f"dbg_msg_{len(messages) + 1:04d}",
                turn_id=native_turn_id,
            )
            messages.append(message)

    # ``thread/start``/``thread/fork`` return thread identity/config only, so
    # ``prior_turns`` is routinely empty right after a fork even though the
    # product conversation is not (forking re-materializes the native thread
    # under a new configuration and does not carry its turn list along).
    # PostgreSQL is the authoritative product transcript in that case: fall
    # back to it so the Inspector still shows the real prior conversation.
    durable_history = getattr(request, "durable_history", None)
    durable_messages = getattr(durable_history, "messages", None)
    durable_messages = durable_messages if isinstance(durable_messages, list) else []
    durable_turn_ids: set[str] = set()
    if not prior_turns and durable_messages:
        history_source = "durable_history_fallback"
        history_complete = not bool(getattr(durable_history, "truncated", False))
        for entry in durable_messages:
            turn_id = str(getattr(entry, "turn_id", "") or "") or None
            if turn_id:
                durable_turn_ids.add(turn_id)
            message, _ = _durable_history_message(
                entry,
                debug_id=f"dbg_msg_{len(messages) + 1:04d}",
            )
            messages.append(message)

    current_message, _ = _base_message(
        debug_id=f"dbg_msg_{len(messages) + 1:04d}",
        source_id=f"{request.chat_id}:user:{request.turn_id}",
        role="user",
        content=_user_input_text(current_input),
        item_type="turnInput",
        turn_id=None,
        runtime_metadata={"current_turn": True},
    )
    messages.append(current_message)

    # Recovery JSON is a transport envelope, not one giant human utterance.
    expanded = []
    seen_recovered_ids = {
        m.get("source_message_id") for m in messages
        if m.get("runtime_item_type") == "durableHistoryMessage"
    }
    for message in messages:
        for projected in _expand_recovery_message(message):
            source = projected.get("source_message_id")
            if projected.get("runtime_item_type") == "recoveredHistoryMessage":
                if source and source in seen_recovered_ids:
                    continue
                if source:
                    seen_recovered_ids.add(source)
            expanded.append(projected)
    messages = expanded
    folded_count = 0
    for index, message in enumerate(messages, 1):
        message["debug_id"] = f"dbg_msg_{index:04d}"
        content = message["content"]
        message["content_chars"] = len(content)
        if len(content) > _MESSAGE_PREVIEW_CHARS:
            folded_count += 1
            message["content"] = content[:_MESSAGE_PREVIEW_CHARS]
            message["_full_content"] = content
            message["content_ref"] = f"{DEBUG_DIR}/{snapshot_id}.messages/{index:04d}"
            message["content_part_count"] = (len(content) + _MESSAGE_CHUNK_CHARS - 1) // _MESSAGE_CHUNK_CHARS

    selected_model = request.model.get("id") if isinstance(request.model, dict) else None
    provider = str(thread.get("modelProvider") or "")
    return {
        "schema_version": 1,
        "kind": "agent_model_input_snapshot",
        "runtime_type": "codex",
        "snapshot_semantics": "runtime_thread_input",
        "snapshot_id": snapshot_id,
        "chat_id": request.chat_id,
        "thread_id": thread_id,
        "turn_id": request.turn_id,
        "model_call_index": 1,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target": {
            "provider": provider,
            "model_id": str(selected_model or ""),
            "context_window_tokens": None,
        },
        "runtime_metadata": {
            "history_complete": history_complete,
            "history_mode": thread.get("historyMode"),
            "history_source": history_source,
            "prior_turn_count": (
                len(prior_turns)
                if history_source == "native_thread"
                else len(durable_turn_ids) or len(durable_messages)
            ),
            "mcp_server_count": len(
                request.mcp_desired_state.servers
                if request.mcp_desired_state is not None
                else []
            ),
            "skill_count": len(request.skills),
            "reasoning_effort": request.reasoning_effort,
            "snapshot_truncated": False,
            "folded_message_count": folded_count,
        },
        "memory_config_snapshot": {
            "compaction_v2_enabled": False,
            "context_manifest_mode": request.context_manifest.mode,
            "adapter_mode": request.context_manifest.adapter_mode,
        },
        "context_manifest": request.context_manifest.model_dump(mode="json"),
        "context_decisions": [{
            "section_id": section.id,
            "action": "included",
            "reason": "Codex owns native context assembly; product manifest is observational",
            "before_tokens": section.token_estimate,
            "after_tokens": None,
        } for section in request.context_manifest.sections],
        "tool_registry": [{
            "name": server.name,
            "origin": server.source,
            "config_revision": server.configuration_revision,
            "required": server.required,
        } for server in (
            request.mcp_desired_state.servers
            if request.mcp_desired_state is not None
            else []
        )],
        "runtime_policy": {
            "approval_mode": request.approval_mode,
            "session_memory_scope": "codex_thread",
            "long_term_memory_enabled": False,
        },
        "token_total": None,
        "messages": messages,
    }


def write_codex_debug_snapshot(payload: dict[str, Any]) -> str | None:
    """Atomically write a Codex snapshot to the mounted chat workspace."""
    if os.environ.get("AGENT_DEBUG_VIEW_ENABLED") != "1":
        return None
    root = os.path.abspath(DEBUG_DIR)
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.abspath(os.path.join(root, f"{payload['snapshot_id']}.json"))
    if not path.startswith(root.rstrip("/") + "/"):
        raise ValueError("Codex debug snapshot path escaped /logs/.debug")
    # Publish content before the manifest, so every expandable reference exists.
    public = {**payload, "messages": []}
    for index, message in enumerate(payload.get("messages", []), 1):
        record = dict(message)
        full = record.pop("_full_content", None)
        if full is not None:
            directory = os.path.join(root, f"{payload['snapshot_id']}.messages", f"{index:04d}")
            os.makedirs(directory, mode=0o700, exist_ok=True)
            for part, offset in enumerate(range(0, len(full), _MESSAGE_CHUNK_CHARS)):
                content_path = os.path.join(directory, f"{part}.txt")
                with open(content_path + ".tmp", "w", encoding="utf-8") as handle:
                    handle.write(full[offset:offset + _MESSAGE_CHUNK_CHARS])
                os.replace(content_path + ".tmp", content_path)
        public["messages"].append(record)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(public, file, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
    return path


def capture_codex_debug_snapshot(
    *,
    request: Any,
    thread: dict[str, Any],
    thread_id: str,
    current_input: list[dict[str, Any]],
) -> str | None:
    """Build and write in one worker-thread friendly call."""
    return write_codex_debug_snapshot(build_codex_debug_snapshot(
        request=request,
        thread=thread,
        thread_id=thread_id,
        current_input=current_input,
    ))


def capture_codex_command_output_observations(
    *, request: Any, observations: dict[str, Any],
) -> str | None:
    """Persist content-free native pipe evidence outside the snapshot index."""
    if os.environ.get("AGENT_DEBUG_VIEW_ENABLED") != "1" or not (
        observations.get("events") or observations.get("alternate_channel_counts")
    ):
        return None
    root = os.path.join(os.path.abspath(DEBUG_DIR), "native-events")
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.join(root, f"{_utc_stamp()}__{_safe_file_component(request.turn_id)}.json")
    payload = {
        "kind": "codex_command_output_observations",
        "chat_id": request.chat_id,
        "turn_id": request.turn_id,
        "boundary": "native_app_server_stdout_before_product_projection",
        **observations,
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".command-output-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


__all__ = [
    "build_codex_debug_snapshot",
    "capture_codex_debug_snapshot",
    "capture_codex_command_output_observations",
    "write_codex_debug_snapshot",
]
