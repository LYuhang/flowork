"""Build a bounded Runtime-neutral recovery transcript from Chat history."""

from __future__ import annotations

import json
from typing import Any

from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeDurableAttachment,
    RuntimeDurableHistoryMessage,
    RuntimeDurableHistorySnapshot,
    RuntimeDurableToolCall,
)

_MAX_SOURCE_MESSAGES = 512
_MAX_TOTAL_TEXT_CHARS = 560_000
_MAX_MESSAGE_TEXT_CHARS = 32_768
_MAX_TOOL_ARGUMENT_CHARS = 32_768
_HEAD_MESSAGES_WHEN_TRUNCATED = 12


def _bounded_text(value: Any, limit: int) -> str:
    if isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "\n…[truncated]"


def _tool_calls(value: Any) -> list[RuntimeDurableToolCall]:
    if not isinstance(value, list):
        return []
    result: list[RuntimeDurableToolCall] = []
    for raw in value[:32]:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        function = function if isinstance(function, dict) else {}
        result.append(RuntimeDurableToolCall(
            tool_call_id=_bounded_text(raw.get("id"), 512),
            name=_bounded_text(
                function.get("name") or raw.get("name"),
                256,
            ),
            arguments=_bounded_text(
                function.get("arguments") or raw.get("arguments"),
                _MAX_TOOL_ARGUMENT_CHARS,
            ),
        ))
    return result


def _attachments(value: Any) -> list[RuntimeDurableAttachment]:
    if not isinstance(value, list):
        return []
    result: list[RuntimeDurableAttachment] = []
    for raw in value[:32]:
        if not isinstance(raw, dict):
            continue
        result.append(RuntimeDurableAttachment(
            name=_bounded_text(raw.get("label") or raw.get("name") or raw.get("filename"), 512),
            context=raw if raw.get("schema_version") == 1 else None,
            path=_bounded_text(raw.get("path"), 2_048),
            media_type=_bounded_text(
                raw.get("media_type") or raw.get("type") or raw.get("mime_type"),
                256,
            ),
        ))
    return result


def _project_message(raw: dict[str, Any]) -> RuntimeDurableHistoryMessage | None:
    role = str(raw.get("role") or "")
    if role not in {"user", "assistant", "tool", "system"}:
        return None
    content = raw.get("content")
    content = content if isinstance(content, dict) else {}
    if content.get("visibility") == "hidden":
        return None
    message_id = str(raw.get("message_id") or "")
    if not message_id:
        return None
    meta = raw.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    tool_calls = _tool_calls(content.get("tool_calls"))
    text = _bounded_text(content.get("text"), _MAX_MESSAGE_TEXT_CHARS)
    attachments = _attachments(content.get("attachments"))
    if not text and not tool_calls and not attachments:
        return None
    return RuntimeDurableHistoryMessage(
        message_id=_bounded_text(message_id, 1_024),
        turn_id=(
            _bounded_text(raw.get("turn_id"), 1_024)
            if raw.get("turn_id") is not None
            else None
        ),
        role=role,
        text=text,
        tool_calls=tool_calls,
        tool_call_id=(
            _bounded_text(content.get("tool_call_id"), 512)
            if content.get("tool_call_id")
            else None
        ),
        attachments=attachments,
        status=(
            _bounded_text(meta.get("status"), 128)
            if meta.get("status")
            else None
        ),
    )


def build_durable_history_snapshot(
    rows: list[dict[str, Any]],
    *,
    source_total: int | None = None,
) -> RuntimeDurableHistorySnapshot:
    """Project completed rows while preserving the beginning and recent tail."""

    source = rows[-_MAX_SOURCE_MESSAGES:]
    total = max(len(rows), source_total or 0)
    projected = [item for raw in source if (item := _project_message(raw))]
    last_turn_id = next(
        (
            str(raw["turn_id"])
            for raw in reversed(rows)
            if raw.get("turn_id")
        ),
        None,
    )
    costs = [
        len(item.model_dump_json())
        for item in projected
    ]
    if sum(costs) <= _MAX_TOTAL_TEXT_CHARS:
        return RuntimeDurableHistorySnapshot(
            messages=projected,
            last_turn_id=last_turn_id,
            truncated=total > len(source),
            omitted_message_count=max(0, total - len(source)),
        )

    head_count = min(_HEAD_MESSAGES_WHEN_TRUNCATED, len(projected))
    kept_indices = set(range(head_count))
    used = sum(costs[:head_count])
    for index in range(len(projected) - 1, head_count - 1, -1):
        if used + costs[index] > _MAX_TOTAL_TEXT_CHARS:
            continue
        kept_indices.add(index)
        used += costs[index]
    messages = [
        item for index, item in enumerate(projected) if index in kept_indices
    ]
    omitted = total - len(messages)
    return RuntimeDurableHistorySnapshot(
        messages=messages,
        last_turn_id=last_turn_id,
        truncated=True,
        omitted_message_count=max(0, omitted),
    )
