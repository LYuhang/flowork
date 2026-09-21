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
from datetime import datetime, timezone
from typing import Any


DEBUG_DIR = "/logs/.debug"
_MAX_MESSAGE_CHARS = 256 * 1024
_MAX_SNAPSHOT_CONTENT_CHARS = 3_500_000
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


def _truncate(text: str, limit: int = _MAX_MESSAGE_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return f"{text[:limit]}\n\n[Codex debug snapshot omitted {omitted} characters]", True


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
    bounded, truncated = _truncate(content)
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
        "content": bounded,
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
    content_chars = 0
    truncated = False
    prior_turns = thread.get("turns")
    prior_turns = prior_turns if isinstance(prior_turns, list) else []
    history_complete = True

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
            message, item_truncated = _thread_item_message(
                item,
                debug_id=f"dbg_msg_{len(messages) + 1:04d}",
                turn_id=native_turn_id,
            )
            next_chars = len(str(message.get("content") or ""))
            if content_chars + next_chars > _MAX_SNAPSHOT_CONTENT_CHARS:
                truncated = True
                break
            messages.append(message)
            content_chars += next_chars
            truncated = truncated or item_truncated
        if content_chars >= _MAX_SNAPSHOT_CONTENT_CHARS:
            break

    current_message, current_truncated = _base_message(
        debug_id=f"dbg_msg_{len(messages) + 1:04d}",
        source_id=f"{request.chat_id}:user:{request.turn_id}",
        role="user",
        content=_user_input_text(current_input),
        item_type="turnInput",
        turn_id=None,
        runtime_metadata={"current_turn": True},
    )
    messages.append(current_message)
    truncated = truncated or current_truncated

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
            "prior_turn_count": len(prior_turns),
            "mcp_server_count": len(
                request.mcp_desired_state.servers
                if request.mcp_desired_state is not None
                else []
            ),
            "skill_count": len(request.skills),
            "reasoning_effort": request.reasoning_effort,
            "snapshot_truncated": truncated,
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
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
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


__all__ = [
    "build_codex_debug_snapshot",
    "capture_codex_debug_snapshot",
    "write_codex_debug_snapshot",
]
