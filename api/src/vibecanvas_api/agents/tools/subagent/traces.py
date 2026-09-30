"""Serializable execution messages; image bytes never enter persisted traces."""
from __future__ import annotations

import json
import re
from typing import Any

_DATA_URL = re.compile(r"data:[^\s;,]+(?:;[^,\s]*)?;base64,[A-Za-z0-9+/=_-]+")


def omit_image_bytes(value: Any) -> Any:
    """Copy content, preserving metadata without mutating model input."""
    if isinstance(value, dict):
        return {
            key: "[base64 omitted]" if key in {"base64", "b64_json"}
            or (key == "data" and (value.get("encoding") == "base64" or value.get("type") == "base64"))
            else omit_image_bytes(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [omit_image_bytes(item) for item in value]
    if isinstance(value, str):
        # Tool results can themselves be JSON strings.
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(omit_image_bytes(json.loads(value)), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return _DATA_URL.sub("[image data URL omitted]", value)
    return value


def chatml_messages(messages: list) -> list[dict]:
    result = []
    for message in messages:
        def get(key, default=None):
            return message.get(key, default) if isinstance(message, dict) else getattr(message, key, default)
        role = get("role") or get("type", "")
        entry = {"role": {"ai": "assistant", "human": "user"}.get(role, role),
                 "content": omit_image_bytes(get("content", ""))}
        for key in ("name", "tool_call_id"):
            if get(key):
                entry[key] = get(key)
        calls = get("tool_calls") or []
        if calls:
            entry["tool_calls"] = [{
                "id": call.get("id", ""), "type": "function",
                "function": {"name": call.get("name", ""),
                             "arguments": json.dumps(omit_image_bytes(call.get("args", {})), ensure_ascii=False)},
            } for call in calls]
        result.append(entry)
    return result
