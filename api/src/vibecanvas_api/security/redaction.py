"""Central redaction for logs, traces, crash text, and audit metadata.

This module deliberately operates on a *copy* of observability data.  It is
not a business-data serializer and must never be inserted into the Agent/SSE
path: stream payloads retain their existing event/id/data contract.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


REDACTED = "[REDACTED]"

_SECRET_KEYS = frozenset({
    "api_key", "apikey", "authorization", "auth_token", "bearer",
    "client_secret", "code_verifier", "cookie", "credential",
    "credentials", "hmac_secret", "id_token", "password", "passwd",
    "private_key", "proxy_authorization", "refresh_token", "secret",
    "secret_key", "session_cookie", "session_token", "smtp_password",
    "token", "access_token", "webhook_secret",
})
_CONTENT_KEYS = frozenset({
    "body", "chat_content", "completion", "content", "debug_snapshot",
    "error", "error_dict", "errors", "exception", "final_outputs", "html",
    "input", "inputs", "messages", "model_input", "model_output", "output",
    "outputs", "payload", "prompt", "query", "request_body", "response_body",
    "result", "results", "stderr", "stdout", "text", "transcript",
    "workflow_json",
})

_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_BASIC_RE = re.compile(r"(?i)(\bbasic\s+)[A-Za-z0-9+/=_-]+")
_PEM_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
    re.DOTALL,
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"client[_-]?secret|session[_-]?token|smtp[_-]?password|webhook[_-]?secret)"
    r"\b\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|'
    r'client[_-]?secret|session[_-]?token|secret|token)"\s*:\s*)"[^"]*"'
)
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"client[_-]?secret|signature|sig|token)=)[^&#\s]*"
)
_SHARE_CAPABILITY_RE = re.compile(r"(/(?:api/v1/public/chat-shares|share)/)[A-Za-z0-9_-]{43}(?=[/?#\s\"']|$)")


def _normalized_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_secret_key(key: object) -> bool:
    value = _normalized_key(key)
    return (
        value in _SECRET_KEYS
        or value.endswith("_password")
        or value.endswith("_secret")
        or value.endswith("_token")
        or value.endswith("_api_key")
        or value.endswith("_private_key")
    )


def _is_content_key(key: object) -> bool:
    value = _normalized_key(key)
    return value in _CONTENT_KEYS or value.endswith("_content")


def redact_text(value: str) -> str:
    """Remove common credential material embedded in otherwise useful text."""
    redacted = _PEM_RE.sub(REDACTED, value)
    redacted = _BEARER_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _BASIC_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _JSON_SECRET_RE.sub(r'\1"' + REDACTED + '"', redacted)
    redacted = _ASSIGNMENT_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _URL_SECRET_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _SHARE_CAPABILITY_RE.sub(r"\1" + REDACTED, redacted)
    return redacted


def redact_value(value: Any, *, redact_content: bool = True) -> Any:
    """Recursively return a JSON/log-safe redacted copy of ``value``.

    Unknown objects are left intact so structlog can retain its normal
    exception and renderer handling.  Mapping/list/tuple/set containers are
    copied and never mutated in place.
    """
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, child in value.items():
            if _is_secret_key(key) or (redact_content and _is_content_key(key)):
                result[key] = REDACTED
            else:
                result[key] = redact_value(child, redact_content=redact_content)
        return result
    if isinstance(value, list):
        return [redact_value(item, redact_content=redact_content) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, redact_content=redact_content) for item in value)
    if isinstance(value, set):
        return {redact_value(item, redact_content=redact_content) for item in value}
    if isinstance(value, str):
        return redact_text(value)
    return value


def structlog_redaction_processor(_logger, _method, event_dict):
    """Structlog/stdlib processor installed immediately before rendering."""
    return redact_value(event_dict, redact_content=True)
