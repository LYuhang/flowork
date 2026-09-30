"""Host-side, capability-authenticated model egress broker.

Sandbox Runtimes call this OpenAI/provider-compatible reverse proxy with a
short-lived capability in the provider's normal API-key position. The host
revalidates either the browser Session and Chat Run, or the durable Workflow
execution fence, plus current membership and resource authorization, then
resolves the real provider secret just in time. Provider credentials never
cross the host↔sandbox boundary.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import re
from typing import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
import httpx
import structlog

from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.auth.live_identity import (
    LiveIdentityError,
    resolve_live_authorization_identity,
)
from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
    scope_authz_service,
)
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.security.secret_service import SecretServiceError, secret_service
from vibecanvas_api.services.agent_runtime.model_capability import (
    RuntimeModelCapability,
    authorization_model_generation,
    model_config_revision,
    verify_runtime_model_capability,
)
from vibecanvas_api.services.agent_runtime.workflow_model_capability import (
    RuntimeWorkflowModelCapability,
    verify_runtime_workflow_model_capability,
)
from vibecanvas_api.services.llm_connection_secrets import (
    hydrate_llm_connection_credentials,
)
from vibecanvas_api.services.pinned_http import PinnedAsyncHTTPTransport
from vibecanvas_api.services.public_url import PublicUrlError, validate_public_http_url
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo


router = APIRouter(tags=["runtime-model-broker"])
logger = structlog.get_logger(__name__)

_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_MAX_REWRITTEN_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_COMPATIBILITY_ERROR_BYTES = 1024 * 1024
_MAX_NAMESPACE_COMPATIBILITY_TARGETS = 256
_MAX_PATH_BYTES = 2048
_MAX_SSE_OBSERVER_LINE_BYTES = 64 * 1024
_MODEL_QUERY_SECRET_NAMES = frozenset({"key", "api_key", "access_token"})
_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})
_INBOUND_CREDENTIAL_HEADERS = frozenset({
    "authorization",
    "api-key",
    "x-api-key",
    "x-goog-api-key",
})
_SAFE_REQUEST_HEADERS = frozenset({
    "accept",
    "accept-encoding",
    "content-type",
    "user-agent",
    "openai-beta",
    "anthropic-version",
    "anthropic-beta",
    "x-goog-api-client",
})
_GOOGLE_MODEL_PATH = re.compile(
    r"^(?:v\d+(?:beta\d*)?/)?models/[^/]+:"
    r"(?:generateContent|streamGenerateContent)$"
)
_NAMESPACE_UNSUPPORTED_TARGETS: set[tuple[str, str]] = set()
_WEB_SEARCH_EXTERNAL_UNSUPPORTED_TARGETS: set[tuple[str, str]] = set()
_REASONING_SUMMARY_UNSUPPORTED_TARGETS: set[tuple[str, str]] = set()
_INPUT_STATUS_REQUIRED_TARGETS: set[tuple[str, str]] = set()
_INPUT_PHASE_UNSUPPORTED_TARGETS: set[tuple[str, str]] = set()
_CUSTOM_TOOL_HISTORY_UNSUPPORTED_TARGETS: set[tuple[str, str]] = set()
_OPTIONAL_REQUEST_FIELDS_UNSUPPORTED_TARGETS: dict[
    tuple[str, str],
    set[str],
] = {}
_OPTIONAL_COMPATIBILITY_REQUEST_FIELDS = frozenset({
    "client_metadata",
    "prompt_cache_key",
})


@dataclass(frozen=True, slots=True)
class RuntimeModelTarget:
    provider: str
    model: str
    base_url: str
    proxy: str | None
    api_key: str
    pinned_addresses: dict[str, tuple[str, ...]]


def _normalized_provider(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _split_model(value: str) -> tuple[str, str]:
    provider, separator, model = str(value or "").partition(":")
    return (
        (_normalized_provider(provider), model.strip())
        if separator
        else ("", str(value or "").strip())
    )


def _default_base_url(provider: str) -> str:
    defaults = {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "google": "https://generativelanguage.googleapis.com",
        "google_genai": "https://generativelanguage.googleapis.com",
        "gemini": "https://generativelanguage.googleapis.com",
        "groq": "https://api.groq.com/openai/v1",
        "mistralai": "https://api.mistral.ai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "cohere": "https://api.cohere.com/v2",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    return defaults.get(provider, "")


def _extract_capability(request: Request) -> str:
    values: set[str] = set()
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        values.add(authorization[7:].strip())
    for name in ("api-key", "x-api-key", "x-goog-api-key"):
        value = request.headers.get(name, "").strip()
        if value:
            values.add(value)
    if len(values) != 1:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_model_capability_invalid"},
        )
    return values.pop()


async def _bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"code": "runtime_model_request_too_large"},
            )
        body.extend(chunk)
    return bytes(body)


def _model_path_allowed(provider: str, path: str) -> bool:
    clean = path.strip("/")
    if not clean or "\\" in clean or "\x00" in clean:
        return False
    if any(part in {"", ".", ".."} for part in clean.split("/")):
        return False
    provider = _normalized_provider(provider)
    if provider in {"google", "google_genai", "gemini"}:
        return bool(_GOOGLE_MODEL_PATH.fullmatch(clean))
    suffixes = {
        "chat/completions",
        "responses",
        "messages",
    }
    return any(clean == suffix or clean.endswith(f"/{suffix}") for suffix in suffixes)


def _target_url(base_url: str, path: str, request: Request) -> str:
    if len(path.encode("utf-8")) > _MAX_PATH_BYTES:
        raise HTTPException(status_code=404, detail={"code": "model_path_denied"})
    parts = urlsplit(base_url)
    base_path = parts.path.rstrip("/")
    target_path = f"{base_path}/{path.strip('/')}"
    query = list(parse_qsl(parts.query, keep_blank_values=True))
    query.extend(
        (name, value)
        for name, value in request.query_params.multi_items()
        if name.casefold() not in _MODEL_QUERY_SECRET_NAMES
    )
    return urlunsplit(
        (parts.scheme, parts.netloc, target_path, urlencode(query, doseq=True), "")
    )


def _validate_requested_model(
    *,
    body: bytes,
    path: str,
    provider: str,
    allowed_model: str,
) -> None:
    """Prevent a model-bound capability from selecting another model.

    Most providers carry the model in the JSON body. Google encodes it in the
    request path. The broker forwards the original bytes only after this exact
    comparison, so validation does not alter streaming or provider payloads.
    """
    normalized = _normalized_provider(provider)
    if normalized in {"google", "google_genai", "gemini"}:
        match = re.search(r"(?:^|/)models/([^/:]+):", path.strip("/"))
        selected = match.group(1) if match else ""
    else:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "runtime_model_request_json_invalid"},
            ) from exc
        selected = (
            str(payload.get("model") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
    if not selected or selected != allowed_model:
        raise HTTPException(
            status_code=403,
            detail={"code": "runtime_model_selection_denied"},
        )


def _responses_request_shape(body: bytes) -> dict[str, object]:
    """Return a content-free request summary for provider compatibility logs.

    Prompts, tool descriptions, schemas, arguments, metadata values and
    credentials are deliberately excluded. The summary is sufficient to tell
    whether a custom Responses endpoint rejected a hosted, custom, namespace,
    or ordinary function tool without copying user content into logs.
    """
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"json": False}
    if not isinstance(payload, dict):
        return {"json": True, "root_type": type(payload).__name__}
    tools = payload.get("tools")
    input_items = payload.get("input")
    tool_types = Counter(
        str(tool.get("type") or "missing")
        for tool in tools or []
        if isinstance(tool, dict)
    ) if isinstance(tools, list) else Counter()
    input_types = Counter(
        str(item.get("type") or "missing")
        for item in input_items or []
        if isinstance(item, dict)
    ) if isinstance(input_items, list) else Counter()
    return {
        "json": True,
        "keys": sorted(str(key) for key in payload),
        "tool_types": dict(sorted(tool_types.items())),
        "input_types": dict(sorted(input_types.items())),
        "stream": payload.get("stream") is True,
    }


def _bounded_error_discriminator(value: object) -> str | None:
    """Return a provider error discriminator without copying error prose."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    return value


def _provider_error_shape(payload: object) -> dict[str, str]:
    """Extract only documented error codes/types from a provider payload."""
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    if not isinstance(error, dict):
        response = payload.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return {}
    shape: dict[str, str] = {}
    for key in ("code", "type", "param"):
        value = _bounded_error_discriminator(error.get(key))
        if value is not None:
            shape[key] = value
    return shape


def _upstream_error_shape(body: bytes) -> dict[str, str]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _provider_error_shape(payload)


@dataclass(frozen=True, slots=True)
class NamespaceCompatibilityRewrite:
    body: bytes
    flat_to_namespaced: dict[str, tuple[str, str]]


class _UpstreamResponseTooLarge(RuntimeError):
    pass


class _SseShapeObserver:
    """Observe only public event/type discriminators from an SSE stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.event_types: Counter[str] = Counter()
        self.item_types: Counter[str] = Counter()
        self.failed_codes: Counter[str] = Counter()
        self.chunks = 0
        self.bytes = 0
        self.oversized_lines = 0

    @staticmethod
    def _bounded_discriminator(value: object) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 128:
            return None
        return value

    def _observe_line(self, line: bytes) -> None:
        stripped = line.strip()
        if not stripped.startswith(b"data:"):
            return
        raw = stripped.partition(b":")[2].strip()
        if not raw or raw == b"[DONE]":
            if raw == b"[DONE]":
                self.event_types["[DONE]"] += 1
            return
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.event_types["invalid_json"] += 1
            return
        if not isinstance(payload, dict):
            return
        event_type = self._bounded_discriminator(payload.get("type"))
        if event_type is not None:
            self.event_types[event_type] += 1
        if event_type == "response.failed":
            for key, value in _provider_error_shape(payload).items():
                self.failed_codes[f"{key}:{value}"] += 1
        item = payload.get("item")
        if isinstance(item, dict):
            item_type = self._bounded_discriminator(item.get("type"))
            if item_type is not None:
                self.item_types[item_type] += 1

    def feed(self, chunk: bytes) -> None:
        self.chunks += 1
        self.bytes += len(chunk)
        self._buffer.extend(chunk)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > _MAX_SSE_OBSERVER_LINE_BYTES:
                    self._buffer.clear()
                    self.oversized_lines += 1
                return
            line = bytes(self._buffer[: newline + 1])
            del self._buffer[: newline + 1]
            self._observe_line(line)

    def summary(self) -> dict[str, object]:
        if self._buffer:
            self._observe_line(bytes(self._buffer))
            self._buffer.clear()
        return {
            "event_types": dict(sorted(self.event_types.items())),
            "item_types": dict(sorted(self.item_types.items())),
            "failed_codes": dict(sorted(self.failed_codes.items())),
            "chunks": self.chunks,
            "bytes": self.bytes,
            "oversized_lines": self.oversized_lines,
        }


async def _read_upstream_bounded(
    response: httpx.Response,
    *,
    limit: int,
) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise _UpstreamResponseTooLarge
        body.extend(chunk)
    return bytes(body)


def _join_namespaced_tool_name(namespace: str, name: str) -> str:
    """Match Codex's canonical `<namespace>__<tool>` compatibility name."""
    return f"{namespace.rstrip('_')}__{name.lstrip('_')}"


def _requires_eager_namespace_compatibility(provider: str) -> bool:
    """Return whether a Responses provider accepts only standard tools.

    OpenRouter advertises ordinary function-tool support per model, but its
    Responses router does not advertise Codex's newer namespace container.
    Waiting for an explicit schema error is insufficient: some routed models
    accept the request and later emit ``response.failed`` when the namespaced
    tool is selected. Flattening is lossless and the response is restored for
    Codex, so apply it deterministically at this boundary.
    """
    return _normalized_provider(provider) in {"openrouter", "open_router"}


def _with_openrouter_server_tools(body: bytes) -> bytes:
    """Project Codex hosted tools onto OpenRouter's Responses vocabulary.

    OpenRouter accepts ordinary function tools plus server tools prefixed with
    ``openrouter:``. Codex's built-in ``web_search`` descriptor is an OpenAI
    server-tool shape, so forwarding it unchanged can make otherwise valid
    function-tool requests fail before the selected local tool is invoked.
    The projection retains web-search capability without forwarding any
    provider-specific optional fields.
    """
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        return body
    changed = False
    projected: list[object] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "web_search":
            projected.append({"type": "openrouter:web_search"})
            changed = True
        else:
            projected.append(tool)
    if not changed:
        return body
    payload["tools"] = projected
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _rewrite_namespaced_function_calls(
    value,
    *,
    namespaced_to_flat: dict[tuple[str, str], str],
) -> None:
    if isinstance(value, list):
        for item in value:
            _rewrite_namespaced_function_calls(
                item,
                namespaced_to_flat=namespaced_to_flat,
            )
        return
    if not isinstance(value, dict):
        return
    if value.get("type") == "function_call":
        namespace = value.get("namespace")
        name = value.get("name")
        if isinstance(namespace, str) and isinstance(name, str):
            flat = namespaced_to_flat.get((namespace, name))
            if flat is not None:
                value["name"] = flat
                value.pop("namespace", None)
    for child in value.values():
        _rewrite_namespaced_function_calls(
            child,
            namespaced_to_flat=namespaced_to_flat,
        )


def _flatten_namespace_tools(body: bytes) -> NamespaceCompatibilityRewrite | None:
    """Convert Responses namespace tools without removing any callable tool.

    Codex versions that predate provider capability negotiation send the newer
    Responses ``namespace`` tool shape to every custom provider. Some otherwise
    compatible providers only understand ordinary ``function`` tools. This
    projection uses Codex's own canonical names and also rewrites prior
    namespaced calls in the input, so multi-step tool turns retain their full
    semantics.
    """
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        return None
    existing_names = {
        tool.get("name")
        for tool in tools
        if isinstance(tool, dict)
        and tool.get("type") == "function"
        and isinstance(tool.get("name"), str)
    }
    flattened: list = []
    flat_to_namespaced: dict[str, tuple[str, str]] = {}
    namespaced_to_flat: dict[tuple[str, str], str] = {}
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "namespace":
            flattened.append(tool)
            continue
        namespace = tool.get("name")
        nested = tool.get("tools")
        if not isinstance(namespace, str) or not namespace or not isinstance(nested, list):
            raise HTTPException(
                status_code=400,
                detail={"code": "runtime_model_namespace_invalid"},
            )
        for child in nested:
            if (
                not isinstance(child, dict)
                or child.get("type") != "function"
                or not isinstance(child.get("name"), str)
                or not child["name"]
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "runtime_model_namespace_invalid"},
                )
            original_name = child["name"]
            flat_name = _join_namespaced_tool_name(namespace, original_name)
            if flat_name in existing_names or flat_name in flat_to_namespaced:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "runtime_model_namespace_collision"},
                )
            projected = dict(child)
            projected["name"] = flat_name
            flattened.append(projected)
            flat_to_namespaced[flat_name] = (namespace, original_name)
            namespaced_to_flat[(namespace, original_name)] = flat_name
    if not flat_to_namespaced:
        return None
    payload["tools"] = flattened
    _rewrite_namespaced_function_calls(
        payload.get("input"),
        namespaced_to_flat=namespaced_to_flat,
    )
    return NamespaceCompatibilityRewrite(
        body=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        flat_to_namespaced=flat_to_namespaced,
    )


def _restore_namespaced_function_calls(
    value,
    *,
    flat_to_namespaced: dict[str, tuple[str, str]],
) -> None:
    if isinstance(value, list):
        for item in value:
            _restore_namespaced_function_calls(
                item,
                flat_to_namespaced=flat_to_namespaced,
            )
        return
    if not isinstance(value, dict):
        return
    if value.get("type") == "function_call" and not value.get("namespace"):
        name = value.get("name")
        target = flat_to_namespaced.get(name) if isinstance(name, str) else None
        if target is not None:
            value["namespace"], value["name"] = target
    for child in value.values():
        _restore_namespaced_function_calls(
            child,
            flat_to_namespaced=flat_to_namespaced,
        )


def _rewrite_namespace_response_json(
    body: bytes,
    *,
    flat_to_namespaced: dict[str, tuple[str, str]],
) -> bytes:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    _restore_namespaced_function_calls(
        payload,
        flat_to_namespaced=flat_to_namespaced,
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _rewrite_namespace_sse_line(
    line: bytes,
    *,
    flat_to_namespaced: dict[str, tuple[str, str]],
) -> bytes:
    stripped = line.rstrip(b"\r\n")
    ending = line[len(stripped):]
    if not stripped.startswith(b"data:"):
        return line
    prefix, separator, data = stripped.partition(b":")
    payload = data.lstrip()
    whitespace = data[: len(data) - len(payload)]
    if not payload or payload == b"[DONE]":
        return line
    rewritten = _rewrite_namespace_response_json(
        payload,
        flat_to_namespaced=flat_to_namespaced,
    )
    return prefix + separator + whitespace + rewritten + ending


def _namespace_tools_rejected(status_code: int, body: bytes) -> bool:
    if status_code not in {400, 422}:
        return False
    detail = body[:1024 * 1024].lower()
    return b"namespace" in detail and any(
        marker in detail
        for marker in (
            b"unknown tool type",
            b"unsupported tool type",
            b"invalid tool type",
            b"tool.type",
        )
    )


def _custom_tool_history_rejected(status_code: int, body: bytes) -> bool:
    """Recognize a Responses endpoint missing custom-call input replay."""
    if status_code not in {400, 422}:
        return False
    detail = body[:_MAX_COMPATIBILITY_ERROR_BYTES].lower()
    return (
        b"custom_tool_call" in detail
        and b"input.type" in detail
        and any(
            marker in detail
            for marker in (b"unknown type", b"unsupported type", b"invalid type")
        )
    )


def _with_function_compatible_custom_tool_history(body: bytes) -> bytes:
    """Losslessly wrap rejected custom-tool history as function-call history.

    Some OpenAI-compatible Responses endpoints can emit Codex custom-tool
    calls but reject those same item types when they are replayed on the next
    Turn. Function call history is the older common denominator. The original
    free-form custom input remains byte-for-byte inside one JSON ``input``
    argument, and call ids/output pairing is preserved.
    """
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    items = payload.get("input") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return body
    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "custom_tool_call":
            custom_input = item.pop("input", "")
            item["type"] = "function_call"
            item["arguments"] = json.dumps(
                {"input": custom_input},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            changed = True
        elif item.get("type") == "custom_tool_call_output":
            item["type"] = "function_call_output"
            changed = True
    if not changed:
        return body
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _without_web_search_external_access(body: bytes) -> bytes:
    """Remove only a rejected optional hint; keep the hosted search tool."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        return body
    changed = False
    for tool in tools:
        if (
            isinstance(tool, dict)
            and tool.get("type") == "web_search"
            and "external_web_access" in tool
        ):
            tool.pop("external_web_access")
            changed = True
    if not changed:
        return body
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _web_search_external_access_rejected(status_code: int, body: bytes) -> bool:
    if status_code not in {400, 422}:
        return False
    detail = body[:1024 * 1024].lower()
    return b"external_web_access" in detail and any(
        marker in detail
        for marker in (
            b"unknown field",
            b"unrecognized field",
            b"extra field",
        )
    )


def _without_reasoning_summary(body: bytes) -> bytes:
    """Keep reasoning effort while omitting an unsupported summary hint."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    reasoning = payload.get("reasoning") if isinstance(payload, dict) else None
    if not isinstance(reasoning, dict) or "summary" not in reasoning:
        return body
    reasoning.pop("summary")
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _reasoning_summary_rejected(status_code: int, body: bytes) -> bool:
    if status_code not in {400, 422}:
        return False
    detail = body[:1024 * 1024].lower()
    return b"summary" in detail and any(
        marker in detail
        for marker in (
            b"unknown field",
            b"unrecognized field",
            b"extra field",
        )
    )


def _unsupported_optional_request_field(
    status_code: int,
    body: bytes,
) -> str | None:
    if status_code not in {400, 422}:
        return None
    detail = body[:1024 * 1024].lower()
    if not any(
        marker in detail
        for marker in (
            b"unknown field",
            b"unrecognized field",
            b"extra field",
        )
    ):
        return None
    return next(
        (
            field
            for field in _OPTIONAL_COMPATIBILITY_REQUEST_FIELDS
            if field.encode("ascii") in detail
        ),
        None,
    )


def _missing_input_status_rejected(status_code: int, body: bytes) -> bool:
    if status_code not in {400, 422}:
        return False
    detail = body[:_MAX_COMPATIBILITY_ERROR_BYTES].lower()
    return b"missing" in detail and b"input.status" in detail


def _with_completed_assistant_status(body: bytes) -> bytes:
    """Fill the assistant-history status required by strict Responses APIs.

    Codex currently omits this required field for assistant messages when it
    serializes multi-turn history to non-Azure compatible endpoints. User,
    developer and reasoning items have different schemas and must remain
    byte-semantically unchanged.
    """
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    items = payload.get("input") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return body
    changed = False
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "assistant"
            and "status" not in item
        ):
            item["status"] = "completed"
            changed = True
    if not changed:
        return body
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _input_message_phase_rejected(status_code: int, body: bytes) -> bool:
    """Detect an older Responses-compatible endpoint rejecting ``phase``.

    ``phase`` is a current Responses API field on assistant output messages.
    Some enterprise gateways implement an older schema and reject replayed
    Codex history with an explicit unknown-field error.  Only that exact,
    bounded rejection enables the compatibility projection.
    """
    if status_code not in {400, 422}:
        return False
    detail = body[:_MAX_COMPATIBILITY_ERROR_BYTES].lower()
    return b"phase" in detail and any(
        marker in detail
        for marker in (
            b"unknown field",
            b"unrecognized field",
            b"extra field",
        )
    )


def _without_input_message_phase(body: bytes) -> bytes:
    """Remove only assistant-history ``phase`` from a Responses request."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    items = payload.get("input") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return body
    changed = False
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "assistant"
            and "phase" in item
        ):
            item.pop("phase")
            changed = True
    if not changed:
        return body
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _without_optional_request_fields(body: bytes, fields: set[str]) -> bytes:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    changed = False
    for field in fields & _OPTIONAL_COMPATIBILITY_REQUEST_FIELDS:
        if field in payload:
            payload.pop(field)
            changed = True
    if not changed:
        return body
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _forward_headers(request: Request, *, provider: str, api_key: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.casefold()
        if lower in _SAFE_REQUEST_HEADERS or lower.startswith("x-stainless-"):
            if lower not in _INBOUND_CREDENTIAL_HEADERS:
                headers[name] = value
    provider = _normalized_provider(provider)
    if provider in {"anthropic"}:
        headers["x-api-key"] = api_key
    elif provider in {"azure", "azure_openai"}:
        headers["api-key"] = api_key
    elif provider in {"google", "google_genai", "gemini"}:
        headers["x-goog-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in response.headers.items():
        lower = name.casefold()
        if lower in _HOP_BY_HOP or lower in {"set-cookie", "location"}:
            continue
        result[name] = value
    return result


def _authz_context(
    capability: RuntimeModelCapability,
    *,
    auth: AuthContext,
) -> AuthzRequestContext:
    return AuthzRequestContext(
        active_organization_id=capability.organization_id,
        request_id=f"runtime-model:{capability.turn_id}",
        session_id=capability.session_id,
        session_generation=capability.session_generation,
        membership_id=auth.membership_id,
        membership_role=auth.membership_role,
        membership_status=auth.membership_status,
        authentication_strength=auth.authentication_strength,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )


async def _validated_user_destination(
    value: str,
    *,
    label: str,
) -> tuple[str, str, tuple[str, ...]]:
    """Accept an explicitly configured HTTP(S) model endpoint as-is.

    ``host`` (the default) gives the broker the host's network reachability so
    company-internal APIs, localhost gateways, private IPv4/IPv6 and WSL
    endpoints work exactly as configured. ``public_https`` is an explicit
    operator hardening mode that restores public-address/TLS validation and
    pins the validated DNS answers. Neither mode selects a different model.
    """
    if config.runtime_model_egress_policy == "public_https":
        try:
            target = await validate_public_http_url(
                value,
                label=label,
                require_https=True,
            )
        except PublicUrlError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "runtime_model_destination_unsafe"},
            ) from exc
        return target.url, target.hostname, target.addresses
    try:
        target = urlsplit(str(value or "").strip())
        # Accessing ``port`` also rejects malformed/out-of-range values.
        _ = target.port
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_model_destination_invalid", "field": label},
        ) from exc
    if target.scheme not in {"http", "https"} or not target.hostname:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_model_destination_invalid", "field": label},
        )
    return str(value).strip(), target.hostname, ()


async def _resolve_model_material(
    *,
    session,
    service,
    principal: PrincipalRef,
    authz_context: AuthzRequestContext,
    capability: RuntimeModelCapability | RuntimeWorkflowModelCapability,
) -> RuntimeModelTarget:
    """Resolve provider material only after the caller's root authorization."""
    pinned: dict[str, tuple[str, ...]] = {}
    managed_profile_id = getattr(capability, "managed_profile_id", None)
    is_workflow = isinstance(capability, RuntimeWorkflowModelCapability)
    if is_workflow and (managed_profile_id is not None or capability.credential_id is None):
        raise HTTPException(403, detail={"code": "workflow_manual_api_required"})
    if managed_profile_id is not None:
        profile = next(
            (
                item
                for item in config.codex_managed_apis
                if str(item["id"]) == managed_profile_id
            ),
            None,
        )
        if profile is None or capability.model not in profile["models"]:
            raise HTTPException(
                status_code=503,
                detail={"code": "runtime_model_managed_profile_unavailable"},
            )
        provider = "openai"
        model = capability.model
        api_key = str(profile["api_key"])
        base_url = str(profile["base_url"])
        proxy = None
        revision = model_config_revision(
            provider=provider,
            model=model,
            updated_at=f"managed:{profile['id']}",
        )
    elif capability.credential_id is None:
        provider, model = _split_model(config.agent.model)
        api_key = str(config.agent.api_key or "")
        base_url = str(config.agent.base_url or _default_base_url(provider))
        proxy = str(config.agent.proxy or "") or None
        revision = model_config_revision(
            provider=provider,
            model=model,
            updated_at="platform-process-config",
        )
    else:
        try:
            credential_uuid = uuid.UUID(capability.credential_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "runtime_model_capability_invalid"},
            ) from exc
        credential_decision = await service.check(
            principal,
            Action.USE,
            ResourceRef(
                ResourceType.LLM_CREDENTIAL,
                capability.credential_id,
                capability.organization_id,
            ),
            authz_context,
        )
        if not credential_decision.allowed:
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_model_credential_access_revoked"},
            )
        credential_repo = LlmCredentialsRepo(session)
        row = (
            await credential_repo.get(credential_uuid)
            if principal.type is PrincipalType.SERVICE_ACCOUNT
            else await credential_repo.get_for_user(
                credential_uuid,
                capability.user_id,
            )
        )
        if row is None or not row.get("enabled"):
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_model_credential_unavailable"},
            )
        if is_workflow:
            from vibecanvas_api.services.workflow_model_policy import is_workflow_credential
            if not is_workflow_credential(row) or str(row.get("user_id")) != capability.user_id:
                raise HTTPException(403, detail={"code": "workflow_manual_api_required"})
        provider = _normalized_provider(row.get("provider"))
        model = str(row.get("model_name") or "").strip()
        if row.get("connection_kind") == "openrouter_oauth":
            requested = str(capability.model or "").strip()
            catalog_ids = {
                str(item.get("id") or "").strip()
                for item in row.get("model_catalog") or []
                if isinstance(item, dict)
            }
            if requested not in catalog_ids:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "runtime_model_configuration_stale"},
                )
            model = requested
        revision = model_config_revision(
            provider=provider,
            model=model,
            updated_at=row.get("updated_at"),
        )
        hydrated = await hydrate_llm_connection_credentials(session, row)
        api_key = await secret_service().resolve_text(
            session,
            secret_ref=row["secret_ref"],
            tenant_id=row["tenant_id"],
            purpose="llm_api_key",
            resource_type="llm_credential",
            resource_id=row["id"],
        )
        base_url = str(hydrated.get("api_url") or _default_base_url(provider))
        proxy = str(hydrated.get("proxy") or "") or None

    if not api_key or not base_url:
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_model_credential_unavailable"},
        )
    base_url, host, addresses = await _validated_user_destination(
        base_url,
        label="model API URL",
    )
    if addresses:
        pinned[host] = addresses
    if proxy:
        proxy, proxy_host, proxy_addresses = await _validated_user_destination(
            proxy,
            label="model proxy URL",
        )
        if proxy_addresses:
            pinned[proxy_host] = proxy_addresses
    if (
        provider != _normalized_provider(capability.provider)
        or model != capability.model
        or revision != capability.config_revision
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_model_configuration_stale"},
        )
    return RuntimeModelTarget(
        provider=provider,
        model=model,
        base_url=base_url,
        proxy=proxy,
        api_key=api_key,
        pinned_addresses=pinned,
    )


async def _authorize_and_resolve_target(
    request: Request,
    capability: RuntimeModelCapability,
) -> RuntimeModelTarget:
    try:
        uuid.UUID(capability.session_id)
        uuid.UUID(capability.user_id)
        uuid.UUID(capability.organization_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_model_capability_invalid"},
        ) from exc

    current_authorization_generation = authorization_model_generation(
        model_id=config.openfga_authorization_model_id,
    )
    if capability.authorization_generation != current_authorization_generation:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_model_authorization_generation_stale"},
        )

    async with session_scope() as identity_session:
        try:
            live_auth = await resolve_live_authorization_identity(
                identity_session,
                session_id=capability.session_id,
                user_id=capability.user_id,
                organization_id=capability.organization_id,
                session_generation=capability.session_generation,
                membership_id=capability.membership_id,
            )
        except LiveIdentityError as exc:
            code = (
                "runtime_model_membership_revoked"
                if "membership" in exc.reason
                else "runtime_model_session_revoked"
            )
            raise HTTPException(
                status_code=403,
                detail={"code": code},
            ) from exc

    async with session_scope(tenant_id=capability.organization_id) as session:
        run = await AgentRunsRepo(session).get_for_chat(
            capability.chat_id,
            capability.turn_id,
            creator_user_id=capability.user_id,
        )
        if run is None or run.status != "running":
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_model_turn_inactive"},
            )
        service = authz_service_for_session(
            session=session,
            organization_id=capability.organization_id,
            openfga_client=getattr(request.app.state, "openfga_client", None),
        )
        service = scope_authz_service(
            service,
            session=session,
            auth=live_auth,
            request=request,
        )
        principal = PrincipalRef(PrincipalType.USER, capability.user_id)
        authz_context = _authz_context(
            capability,
            auth=live_auth,
        )
        chat_decision = await service.check(
            principal,
            Action.EXECUTE,
            ResourceRef(
                ResourceType.CHAT,
                capability.chat_id,
                capability.organization_id,
            ),
            authz_context,
        )
        if not chat_decision.allowed:
            raise HTTPException(
                status_code=403,
                detail={"code": "runtime_model_chat_access_revoked"},
            )

        return await _resolve_model_material(
            session=session,
            service=service,
            principal=principal,
            authz_context=authz_context,
            capability=capability,
        )


async def _authorize_and_resolve_workflow_target(
    request: Request,
    capability: RuntimeWorkflowModelCapability,
) -> RuntimeModelTarget:
    from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution

    return await authorize_workflow_execution(request, capability, resolve=_resolve_model_material)


async def _proxy_runtime_model_request(request: Request, path: str):
    token = _extract_capability(request)
    chat_capability = verify_runtime_model_capability(
        token,
        secret=config.signing_secret,
    )
    workflow_capability = (
        None
        if chat_capability is not None
        else verify_runtime_workflow_model_capability(
            token,
            secret=config.signing_secret,
        )
    )
    capability = chat_capability or workflow_capability
    if capability is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_model_capability_invalid"},
        )
    if not _model_path_allowed(capability.provider, path):
        raise HTTPException(status_code=404, detail={"code": "model_path_denied"})
    try:
        target = (
            await _authorize_and_resolve_target(request, chat_capability)
            if chat_capability is not None
            else await _authorize_and_resolve_workflow_target(
                request,
                workflow_capability,
            )
        )
    except SecretServiceError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_model_credential_unavailable"},
        ) from exc
    body = await _bounded_body(request)
    _validate_requested_model(
        body=body,
        path=path,
        provider=capability.provider,
        allowed_model=capability.model,
    )
    namespace_rewrite = (
        _flatten_namespace_tools(body)
        if path.strip("/").endswith("responses")
        else None
    )
    compatibility_target_key = (
        _normalized_provider(target.provider),
        target.base_url.rstrip("/"),
    )
    namespace_compatibility = (
        namespace_rewrite is not None
        and (
            _requires_eager_namespace_compatibility(target.provider)
            or compatibility_target_key in _NAMESPACE_UNSUPPORTED_TARGETS
        )
    )
    openrouter_responses_compatibility = (
        path.strip("/").endswith("responses")
        and _normalized_provider(target.provider) in {"openrouter", "open_router"}
    )
    web_search_compatibility = (
        compatibility_target_key in _WEB_SEARCH_EXTERNAL_UNSUPPORTED_TARGETS
    )
    reasoning_summary_compatibility = (
        compatibility_target_key in _REASONING_SUMMARY_UNSUPPORTED_TARGETS
    )
    input_status_compatibility = (
        compatibility_target_key in _INPUT_STATUS_REQUIRED_TARGETS
    )
    input_phase_compatibility = (
        compatibility_target_key in _INPUT_PHASE_UNSUPPORTED_TARGETS
    )
    custom_tool_history_compatibility = (
        compatibility_target_key in _CUSTOM_TOOL_HISTORY_UNSUPPORTED_TARGETS
    )
    unsupported_optional_fields = set(
        _OPTIONAL_REQUEST_FIELDS_UNSUPPORTED_TARGETS.get(
            compatibility_target_key,
            (),
        )
    )
    target_url = _target_url(target.base_url, path, request)
    transport = PinnedAsyncHTTPTransport(
        addresses=target.pinned_addresses,
        proxy=target.proxy,
    )
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0),
        follow_redirects=False,
        trust_env=False,
    )
    outbound_request_shape: dict[str, object] = {}

    async def send_upstream() -> httpx.Response:
        nonlocal outbound_request_shape
        headers = _forward_headers(
            request,
            provider=target.provider,
            api_key=target.api_key,
        )
        request_body = body
        if namespace_compatibility and namespace_rewrite is not None:
            request_body = namespace_rewrite.body
        if openrouter_responses_compatibility:
            request_body = _with_openrouter_server_tools(request_body)
            # Keep the outbound payload within OpenRouter's documented
            # OpenResponses surface. Codex may send newer OpenAI-only replay
            # hints which OpenRouter currently reports as a generic provider
            # ``server_error`` rather than a bounded schema rejection.
            request_body = _without_reasoning_summary(request_body)
            request_body = _without_input_message_phase(request_body)
            request_body = _with_completed_assistant_status(request_body)
            request_body = _without_optional_request_fields(
                request_body,
                {"client_metadata"},
            )
        if web_search_compatibility:
            request_body = _without_web_search_external_access(request_body)
        if reasoning_summary_compatibility:
            request_body = _without_reasoning_summary(request_body)
        if input_status_compatibility:
            request_body = _with_completed_assistant_status(request_body)
        if input_phase_compatibility:
            request_body = _without_input_message_phase(request_body)
        if custom_tool_history_compatibility:
            request_body = _with_function_compatible_custom_tool_history(
                request_body
            )
        if unsupported_optional_fields:
            request_body = _without_optional_request_fields(
                request_body,
                unsupported_optional_fields,
            )
        outbound_request_shape = (
            _responses_request_shape(request_body)
            if path.strip("/").endswith("responses")
            else {"path": path.strip("/")}
        )
        if (
            namespace_compatibility
            or openrouter_responses_compatibility
            or web_search_compatibility
            or reasoning_summary_compatibility
            or input_status_compatibility
            or input_phase_compatibility
            or custom_tool_history_compatibility
            or unsupported_optional_fields
        ):
            # Response rewriting operates on the provider bytes, so request an
            # identity representation rather than forwarding Codex compression.
            headers["Accept-Encoding"] = "identity"
        return await client.send(
            client.build_request(
                "POST",
                target_url,
                headers=headers,
                content=request_body,
            ),
            stream=True,
        )

    try:
        for _attempt in range(12):
            upstream = await send_upstream()
            rejection_body = b""
            if 400 <= upstream.status_code < 600:
                rejection_body = await _read_upstream_bounded(
                    upstream,
                    limit=_MAX_COMPATIBILITY_ERROR_BYTES,
                )
            if (
                namespace_rewrite is not None
                and not namespace_compatibility
                and _namespace_tools_rejected(
                    upstream.status_code,
                    rejection_body,
                )
            ):
                await upstream.aclose()
                if len(_NAMESPACE_UNSUPPORTED_TARGETS) >= (
                    _MAX_NAMESPACE_COMPATIBILITY_TARGETS
                ):
                    _NAMESPACE_UNSUPPORTED_TARGETS.clear()
                _NAMESPACE_UNSUPPORTED_TARGETS.add(compatibility_target_key)
                logger.info(
                    "runtime_model_namespace_compatibility_enabled",
                    provider=target.provider,
                    destination=urlsplit(target.base_url).hostname,
                )
                namespace_compatibility = True
                continue
            if (
                not web_search_compatibility
                and _web_search_external_access_rejected(
                    upstream.status_code,
                    rejection_body,
                )
            ):
                await upstream.aclose()
                if len(_WEB_SEARCH_EXTERNAL_UNSUPPORTED_TARGETS) >= (
                    _MAX_NAMESPACE_COMPATIBILITY_TARGETS
                ):
                    _WEB_SEARCH_EXTERNAL_UNSUPPORTED_TARGETS.clear()
                _WEB_SEARCH_EXTERNAL_UNSUPPORTED_TARGETS.add(
                    compatibility_target_key
                )
                logger.info(
                    "runtime_model_web_search_compatibility_enabled",
                    provider=target.provider,
                    destination=urlsplit(target.base_url).hostname,
                )
                web_search_compatibility = True
                continue
            if (
                not reasoning_summary_compatibility
                and _reasoning_summary_rejected(
                    upstream.status_code,
                    rejection_body,
                )
            ):
                await upstream.aclose()
                if len(_REASONING_SUMMARY_UNSUPPORTED_TARGETS) >= (
                    _MAX_NAMESPACE_COMPATIBILITY_TARGETS
                ):
                    _REASONING_SUMMARY_UNSUPPORTED_TARGETS.clear()
                _REASONING_SUMMARY_UNSUPPORTED_TARGETS.add(
                    compatibility_target_key
                )
                logger.info(
                    "runtime_model_reasoning_summary_compatibility_enabled",
                    provider=target.provider,
                    destination=urlsplit(target.base_url).hostname,
                )
                reasoning_summary_compatibility = True
                continue
            unsupported_field = _unsupported_optional_request_field(
                upstream.status_code,
                rejection_body,
            )
            if (
                unsupported_field is not None
                and unsupported_field not in unsupported_optional_fields
            ):
                await upstream.aclose()
                if len(_OPTIONAL_REQUEST_FIELDS_UNSUPPORTED_TARGETS) >= (
                    _MAX_NAMESPACE_COMPATIBILITY_TARGETS
                ):
                    _OPTIONAL_REQUEST_FIELDS_UNSUPPORTED_TARGETS.clear()
                unsupported_optional_fields.add(unsupported_field)
                _OPTIONAL_REQUEST_FIELDS_UNSUPPORTED_TARGETS[
                    compatibility_target_key
                ] = set(unsupported_optional_fields)
                logger.info(
                    "runtime_model_optional_field_compatibility_enabled",
                    provider=target.provider,
                    destination=urlsplit(target.base_url).hostname,
                    field=unsupported_field,
                )
                continue
            if (
                not input_status_compatibility
                and _missing_input_status_rejected(
                    upstream.status_code,
                    rejection_body,
                )
            ):
                await upstream.aclose()
                if len(_INPUT_STATUS_REQUIRED_TARGETS) >= (
                    _MAX_NAMESPACE_COMPATIBILITY_TARGETS
                ):
                    _INPUT_STATUS_REQUIRED_TARGETS.clear()
                _INPUT_STATUS_REQUIRED_TARGETS.add(compatibility_target_key)
                logger.info(
                    "runtime_model_input_status_compatibility_enabled",
                    provider=target.provider,
                    destination=urlsplit(target.base_url).hostname,
                )
                input_status_compatibility = True
                continue
            if (
                not input_phase_compatibility
                and _input_message_phase_rejected(
                    upstream.status_code,
                    rejection_body,
                )
            ):
                await upstream.aclose()
                if len(_INPUT_PHASE_UNSUPPORTED_TARGETS) >= (
                    _MAX_NAMESPACE_COMPATIBILITY_TARGETS
                ):
                    _INPUT_PHASE_UNSUPPORTED_TARGETS.clear()
                _INPUT_PHASE_UNSUPPORTED_TARGETS.add(compatibility_target_key)
                logger.info(
                    "runtime_model_input_phase_compatibility_enabled",
                    provider=target.provider,
                    destination=urlsplit(target.base_url).hostname,
                )
                input_phase_compatibility = True
                continue
            if (
                not custom_tool_history_compatibility
                and _custom_tool_history_rejected(
                    upstream.status_code,
                    rejection_body,
                )
            ):
                await upstream.aclose()
                if len(_CUSTOM_TOOL_HISTORY_UNSUPPORTED_TARGETS) >= (
                    _MAX_NAMESPACE_COMPATIBILITY_TARGETS
                ):
                    _CUSTOM_TOOL_HISTORY_UNSUPPORTED_TARGETS.clear()
                _CUSTOM_TOOL_HISTORY_UNSUPPORTED_TARGETS.add(
                    compatibility_target_key
                )
                logger.info(
                    "runtime_model_custom_tool_history_compatibility_enabled",
                    provider=target.provider,
                    destination=urlsplit(target.base_url).hostname,
                )
                custom_tool_history_compatibility = True
                continue
            if rejection_body:
                logger.warning(
                    "runtime_model_upstream_rejected",
                    provider=target.provider,
                    model=target.model,
                    destination=urlsplit(target.base_url).hostname,
                    status_code=upstream.status_code,
                    upstream_error_shape=_upstream_error_shape(rejection_body),
                    request_shape=outbound_request_shape,
                )
                # ``aread`` consumed the small provider error response. Rebuild
                # it so the caller still receives the provider's exact failure.
                headers = _response_headers(upstream)
                status_code = upstream.status_code
                media_type = upstream.headers.get("content-type")
                await upstream.aclose()
                await client.aclose()
                headers.pop("content-length", None)
                headers.pop("content-encoding", None)
                return Response(
                    rejection_body,
                    status_code=status_code,
                    headers=headers,
                    media_type=media_type,
                )
            break
    except Exception as exc:
        await client.aclose()
        logger.warning(
            "runtime_model_upstream_unavailable",
            scope_type=("chat" if chat_capability is not None else "workflow"),
            scope_id=(
                chat_capability.chat_id
                if chat_capability is not None
                else workflow_capability.workflow_id
            ),
            execution_id=(
                chat_capability.turn_id
                if chat_capability is not None
                else workflow_capability.execution_id
            ),
            provider=target.provider,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail={"code": "runtime_model_upstream_unavailable"},
        ) from exc
    if 300 <= upstream.status_code < 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail={"code": "runtime_model_redirect_denied"},
        )

    response_headers = _response_headers(upstream)
    content_type = upstream.headers.get("content-type", "").casefold()

    async def stream_body() -> AsyncIterator[bytes]:
        observer = _SseShapeObserver()
        try:
            if not namespace_compatibility or namespace_rewrite is None:
                async for chunk in upstream.aiter_raw():
                    observer.feed(chunk)
                    yield chunk
                return
            buffer = bytearray()
            async for chunk in upstream.aiter_raw():
                observer.feed(chunk)
                buffer.extend(chunk)
                if len(buffer) > _MAX_REWRITTEN_RESPONSE_BYTES:
                    raise _UpstreamResponseTooLarge
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffer[: newline + 1])
                    del buffer[: newline + 1]
                    yield _rewrite_namespace_sse_line(
                        line,
                        flat_to_namespaced=(
                            namespace_rewrite.flat_to_namespaced
                        ),
                    )
            if buffer:
                yield _rewrite_namespace_sse_line(
                    bytes(buffer),
                    flat_to_namespaced=namespace_rewrite.flat_to_namespaced,
                )
        finally:
            logger.info(
                "runtime_model_upstream_stream_shape",
                provider=target.provider,
                model=target.model,
                destination=urlsplit(target.base_url).hostname,
                request_shape=outbound_request_shape,
                response_shape=observer.summary(),
            )
            await upstream.aclose()
            await client.aclose()

    if (
        namespace_compatibility
        and namespace_rewrite is not None
        and "text/event-stream" not in content_type
    ):
        try:
            response_body = await _read_upstream_bounded(
                upstream,
                limit=_MAX_REWRITTEN_RESPONSE_BYTES,
            )
            response_body = _rewrite_namespace_response_json(
                response_body,
                flat_to_namespaced=namespace_rewrite.flat_to_namespaced,
            )
        except _UpstreamResponseTooLarge as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "runtime_model_response_too_large"},
            ) from exc
        finally:
            await upstream.aclose()
            await client.aclose()
        response_headers.pop("content-length", None)
        response_headers.pop("content-encoding", None)
        return Response(
            response_body,
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=None,
        )

    if namespace_compatibility:
        response_headers.pop("content-length", None)
        response_headers.pop("content-encoding", None)

    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=None,
    )


@router.post(
    "/api/internal/runtime-model/v1/{path:path}",
    include_in_schema=False,
)
async def runtime_model_request(request: Request, path: str):
    return await _proxy_runtime_model_request(request, path)


__all__ = ["RuntimeModelTarget", "router"]
