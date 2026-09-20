"""OpenRouter OAuth PKCE and user-scoped model-catalog primitives.

All provider URLs are fixed constants. Callers receive classified errors only;
upstream bodies, authorization codes, verifiers, and API keys are never logged
or returned through Flowork APIs.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from vibecanvas_api.config import config
from vibecanvas_api.services.pinned_http import request_pinned_public_url
from vibecanvas_api.services.public_url import PublicUrlError


OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_USER_MODELS_URL = "https://openrouter.ai/api/v1/models/user"
OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
PKCE_TTL_SECONDS = 10 * 60
CATALOG_TTL_SECONDS = 5 * 60
MAX_CATALOG_MODELS = 1000
OPENROUTER_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
logger = structlog.get_logger(__name__)


def product_default_reasoning_effort(
    supported: list[str],
    provider_default: Any,
) -> str | None:
    """Choose a capable default without silently opting users into max effort.

    OpenRouter model metadata can advertise ``max`` as the provider default.
    That is useful as an explicit user choice, but it is a poor product default
    for an Agent that may make many sequential tool calls. Preserve ordinary
    provider defaults and otherwise prefer a balanced/deep effort that the
    model actually supports.
    """
    candidate = provider_default if isinstance(provider_default, str) else None
    if candidate in supported and candidate not in {"xhigh", "max"}:
        return candidate
    # Prefer a latency- and reliability-conscious default when the provider only
    # exposes coarse low/high/max choices.  Long Agent turns can contain many
    # sequential tool calls; users can still opt into high/max explicitly.
    for effort in ("medium", "low", "high", "minimal", "none", "xhigh", "max"):
        if effort in supported:
            return effort
    return None


class OpenRouterConnectionError(RuntimeError):
    def __init__(self, code: str, *, upstream_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.upstream_status = upstream_status


def new_pkce_material() -> tuple[str, str, str]:
    """Return opaque state, RFC 7636 verifier, and S256 challenge."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return state, verifier, challenge


def state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def callback_url(state: str) -> str:
    """Build the one allowed browser callback from deployment-owned config.

    OpenRouter guarantees that it appends an authorization ``code`` to the
    callback, but does not guarantee that a query string already present on
    ``callback_url`` survives. Keep the one-time state in the path so the
    browser can always return both values to Flowork.
    """
    return config.public_urls.absolute(
        f"settings/openrouter/callback/{state}"
    )


def authorization_url(*, callback: str, challenge: str) -> str:
    query = urlencode({
        "callback_url": callback,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{OPENROUTER_AUTH_URL}?{query}"


async def exchange_authorization_code(*, code: str, verifier: str) -> str:
    try:
        response = await request_pinned_public_url(
            "POST",
            OPENROUTER_KEY_URL,
            label="OpenRouter token endpoint",
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            },
            max_response_bytes=64 * 1024,
            trusted_proxy_cidrs=config.sandbox_egress_trusted_proxy_cidrs,
            proxy=config.control_plane_http_proxy or None,
            json={
                "code": code,
                "code_verifier": verifier,
                "code_challenge_method": "S256",
            },
        )
    except (httpx.HTTPError, OSError, PublicUrlError) as exc:
        logger.warning(
            "openrouter_transport_failed",
            operation="exchange_authorization_code",
            error_type=type(exc).__name__,
            cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
            proxy_configured=bool(config.control_plane_http_proxy),
        )
        raise OpenRouterConnectionError("openrouter_unreachable") from exc
    if response.status_code in {400, 401, 403}:
        raise OpenRouterConnectionError(
            "openrouter_authorization_rejected",
            upstream_status=response.status_code,
        )
    if not 200 <= response.status_code < 300:
        raise OpenRouterConnectionError(
            "openrouter_exchange_failed", upstream_status=response.status_code,
        )
    try:
        payload = response.json()
        key = str(payload.get("key") or "") if isinstance(payload, dict) else ""
    except ValueError as exc:
        raise OpenRouterConnectionError("openrouter_invalid_response") from exc
    if not key or len(key) > 4000:
        raise OpenRouterConnectionError("openrouter_invalid_response")
    return key


def normalize_model(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    model_id = str(raw.get("id") or "").strip()
    if not model_id or "/" not in model_id or len(model_id) > 300:
        return None
    architecture = raw.get("architecture")
    architecture = architecture if isinstance(architecture, dict) else {}
    input_modalities = [
        str(value) for value in architecture.get("input_modalities", [])
        if isinstance(value, str)
    ]
    output_modalities = [
        str(value) for value in architecture.get("output_modalities", [])
        if isinstance(value, str)
    ]
    parameters = {
        str(value) for value in raw.get("supported_parameters", [])
        if isinstance(value, str)
    }
    # Agent model choices must accept text and support a tool loop. Image-only,
    # embedding, and plain completion models stay outside the Runtime catalog.
    if "text" not in input_modalities or "text" not in output_modalities:
        return None
    if "tools" not in parameters:
        return None
    pricing = raw.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    reasoning = raw.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, dict) else None
    supported_efforts: list[str] = []
    default_effort: str | None = None
    if reasoning is not None:
        raw_efforts = reasoning.get("supported_efforts")
        if raw_efforts is None:
            # OpenRouter documents an explicit null as accepting every gateway
            # effort value.  A missing reasoning object means the model does
            # not expose an effort selector at all.
            supported_efforts = list(OPENROUTER_REASONING_EFFORTS)
        elif isinstance(raw_efforts, list):
            supported_efforts = [
                effort for effort in OPENROUTER_REASONING_EFFORTS
                if effort in raw_efforts
            ]
        default_effort = product_default_reasoning_effort(
            supported_efforts,
            reasoning.get("default_effort"),
        )
    context_length = raw.get("context_length")
    return {
        "id": model_id,
        "name": str(raw.get("name") or model_id)[:300],
        "description": str(raw.get("description") or "")[:2000],
        "context_length": (
            int(context_length)
            if isinstance(context_length, (int, float)) and context_length > 0
            else None
        ),
        "input_modalities": input_modalities[:12],
        "output_modalities": output_modalities[:12],
        "supports_tools": True,
        # OpenRouter exposes hosted search as a distinct optional request
        # parameter. Ordinary function-tool support does not imply that the
        # selected model can accept ``openrouter:web_search``.
        "supports_web_search": "web_search_options" in parameters,
        "supported_reasoning_efforts": supported_efforts,
        "default_reasoning_effort": default_effort,
        "pricing": {
            "prompt": str(pricing.get("prompt"))[:80]
            if pricing.get("prompt") is not None else None,
            "completion": str(pricing.get("completion"))[:80]
            if pricing.get("completion") is not None else None,
        },
        "available": True,
    }


def normalize_catalog(payload: Any) -> list[dict[str, Any]]:
    values = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise OpenRouterConnectionError("openrouter_invalid_response")
    models = [model for value in values if (model := normalize_model(value))]
    models.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return models[:MAX_CATALOG_MODELS]


async def fetch_user_model_catalog(api_key: str) -> list[dict[str, Any]]:
    try:
        response = await request_pinned_public_url(
            "GET",
            OPENROUTER_USER_MODELS_URL,
            label="OpenRouter user model catalog",
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {api_key}",
            },
            max_response_bytes=8 * 1024 * 1024,
            trusted_proxy_cidrs=config.sandbox_egress_trusted_proxy_cidrs,
            proxy=config.control_plane_http_proxy or None,
        )
    except (httpx.HTTPError, OSError, PublicUrlError) as exc:
        logger.warning(
            "openrouter_transport_failed",
            operation="fetch_user_model_catalog",
            error_type=type(exc).__name__,
            cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
            proxy_configured=bool(config.control_plane_http_proxy),
        )
        raise OpenRouterConnectionError("openrouter_unreachable") from exc
    if response.status_code in {401, 403}:
        raise OpenRouterConnectionError(
            "openrouter_credentials_rejected", upstream_status=response.status_code,
        )
    if not 200 <= response.status_code < 300:
        raise OpenRouterConnectionError(
            "openrouter_catalog_failed", upstream_status=response.status_code,
        )
    try:
        return normalize_catalog(response.json())
    except ValueError as exc:
        raise OpenRouterConnectionError("openrouter_invalid_response") from exc


def merge_catalog_with_current(
    models: list[dict[str, Any]], *, current_model_id: str,
) -> list[dict[str, Any]]:
    if not current_model_id or any(item["id"] == current_model_id for item in models):
        return models
    return [
        *models,
        {
            "id": current_model_id,
            "name": current_model_id,
            "description": "",
            "context_length": None,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supports_tools": True,
            "supported_reasoning_efforts": [],
            "default_reasoning_effort": None,
            "pricing": {"prompt": None, "completion": None},
            "available": False,
        },
    ]
