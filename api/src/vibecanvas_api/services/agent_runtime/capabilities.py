"""Runtime-owned model and reasoning-effort catalogs."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.codex_account import CodexAccountService
from vibecanvas_api.services.agent_runtime.compatibility import (
    compatible_api,
    runtime_supports_api,
)
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeCapabilities,
    RuntimeModelOption,
    RuntimeReasoningEffortOption,
    RuntimeType,
)
from vibecanvas_api.services.codex_cli import resolve_codex_executable

CODEX_OPENROUTER_PREFIX = "codex:openrouter:"
CODEX_CREDENTIAL_PREFIX = "codex:credential:"
CODEX_ACCOUNT_MODEL_PREFIX = "codex:account:"
CODEX_MANAGED_MODEL_PREFIX = "codex:managed:"


def runtime_model_connection_id(
    runtime_type: RuntimeType | str,
    model_id: str,
) -> str:
    """Return a stable, non-secret identifier for the selected connection.

    The identifier deliberately omits the concrete provider model so users can
    switch models within one connection without changing its identity. It does
    retain the credential/profile id so Resume and audit records never collapse
    several user-owned sources into a generic ``codex:api`` bucket.
    """
    runtime = RuntimeType(runtime_type)
    if runtime != RuntimeType.CODEX:
        raise ValueError("model_not_available_for_runtime")
    if model_id.startswith(CODEX_ACCOUNT_MODEL_PREFIX):
        return "codex:account"
    if model_id.startswith(CODEX_OPENROUTER_PREFIX):
        credential_id = model_id.removeprefix(
            CODEX_OPENROUTER_PREFIX
        ).partition(":")[0]
        return f"codex:openrouter:{credential_id}"
    if model_id.startswith(CODEX_CREDENTIAL_PREFIX):
        return model_id
    if model_id.startswith(CODEX_MANAGED_MODEL_PREFIX):
        profile_id = model_id.removeprefix(
            CODEX_MANAGED_MODEL_PREFIX
        ).partition(":")[0]
        return f"codex:managed:{profile_id}"
    raise ValueError("model_not_available_for_runtime")

_OPENAI_REASONING_EFFORTS = (
    ("minimal", "Minimal", "Fastest supported reasoning mode."),
    ("low", "Low", "Lighter reasoning for straightforward tasks."),
    ("medium", "Medium", "Balanced reasoning depth and latency."),
    ("high", "High", "Deeper reasoning for complex tasks."),
    ("xhigh", "Extra high", "Maximum broadly supported reasoning depth."),
)

_REASONING_EFFORT_DESCRIPTIONS = {
    "none": ("None", "Disable optional model reasoning."),
    "minimal": ("Minimal", "Fastest supported reasoning mode."),
    "low": ("Low", "Lighter reasoning for straightforward tasks."),
    "medium": ("Medium", "Balanced reasoning depth and latency."),
    "high": ("High", "Deeper reasoning for complex tasks."),
    "xhigh": ("Extra high", "Very deep reasoning for demanding tasks."),
    "max": ("Maximum", "Maximum reasoning supported by the model."),
}


def _provider_from_model(model: str) -> str:
    provider, separator, _ = model.partition(":")
    return provider if separator else ""


def _model_name(model: str) -> str:
    _provider, separator, name = model.partition(":")
    return name if separator else model


def _normalized_provider(provider: str) -> str:
    return provider.strip().lower().replace("-", "_")


def _openai_reasoning_efforts(provider: str) -> list[RuntimeReasoningEffortOption]:
    normalized = provider.strip().lower().replace("-", "_")
    if normalized not in {"openai", "azure", "azure_openai"}:
        return []
    return [
        RuntimeReasoningEffortOption(id=value, label=label, description=description)
        for value, label, description in _OPENAI_REASONING_EFFORTS
    ]


def _catalog_reasoning_efforts(
    model: Mapping[str, Any],
) -> list[RuntimeReasoningEffortOption]:
    values = model.get("supported_reasoning_efforts")
    if not isinstance(values, list):
        return []
    result: list[RuntimeReasoningEffortOption] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value not in _REASONING_EFFORT_DESCRIPTIONS:
            continue
        label, description = _REASONING_EFFORT_DESCRIPTIONS[value]
        result.append(RuntimeReasoningEffortOption(
            id=value,
            label=label,
            description=description,
        ))
    return result


def codex_openrouter_model(model_id: str | None) -> str | None:
    return _openrouter_model_from_id(model_id, prefix=CODEX_OPENROUTER_PREFIX)


def _openrouter_model_from_id(
    model_id: str | None,
    *,
    prefix: str,
) -> str | None:
    if model_id is None or not model_id.startswith(prefix):
        return None
    raw = model_id.removeprefix(prefix)
    _credential, separator, encoded = raw.partition(":")
    if not separator or not encoded:
        raise ValueError("model_not_available_for_runtime")
    try:
        padding = "=" * (-len(encoded) % 4)
        value = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("model_not_available_for_runtime") from exc
    if not value or "/" not in value or len(value) > 300:
        raise ValueError("model_not_available_for_runtime")
    return value


async def codex_capabilities(
    credential_rows: Iterable[Mapping[str, Any]],
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    selected_managed_profile_id: str | None = None,
    auth_methods: Iterable[str] | None = None,
) -> RuntimeCapabilities:
    """Build the Codex catalog without exposing account credentials.

    API-backed requests use the host Runtime Model Broker and never expose a
    provider key. The optional ChatGPT mode contributes only model metadata;
    its official account cache is mounted later for an explicitly selected
    account request.
    """
    if resolve_codex_executable() is None:
        return RuntimeCapabilities(
            runtime_type=RuntimeType.CODEX,
            runtime_available=False,
            authenticated=None,
            source="codex.app-server+runtime-model-broker",
            error_code="codex_cli_unavailable",
        )

    allowed_auth = frozenset(
        auth_methods
        if auth_methods is not None
        else config.codex_runtime_auth_methods
    )
    models: list[RuntimeModelOption] = []
    for profile in (
        config.codex_managed_apis if "managed_api" in allowed_auth else ()
    ):
        compatibility = compatible_api(
            RuntimeType.CODEX,
            api_source="managed_api",
            provider="openai",
        )
        assert compatibility is not None
        profile_id = str(profile["id"])
        for index, model_name in enumerate(profile["models"]):
            models.append(RuntimeModelOption(
                id=(
                    f"{CODEX_MANAGED_MODEL_PREFIX}{profile_id}:{model_name}"
                ),
                label=f"{profile['name']} · {model_name}",
                description=(
                    "Operator-managed OpenAI API; connection details stay "
                    "on the host."
                ),
                api_source="managed_api",
                api_protocol=compatibility.api_protocol,
                provider="openai",
                provider_model_id=model_name,
                is_default=(
                    profile_id == selected_managed_profile_id and index == 0
                ),
                supported_reasoning_efforts=_openai_reasoning_efforts("openai"),
                default_reasoning_effort=None,
            ))
    for row in credential_rows if "personal_api" in allowed_auth else ():
        credential_id = str(row.get("id") or "").strip()
        model_name = str(row.get("model_name") or "").strip()
        provider = _normalized_provider(str(row.get("provider") or ""))
        api_source = str(row.get("connection_kind") or "manual")
        if (
            not credential_id
            or not model_name
            or not runtime_supports_api(
                RuntimeType.CODEX,
                api_source=api_source,
                provider=provider,
            )
        ):
            continue
        if api_source == "openrouter_oauth":
            if row.get("catalog_error_code") == "openrouter_credentials_rejected":
                continue
            compatibility = compatible_api(
                RuntimeType.CODEX,
                api_source=api_source,
                provider=provider,
            )
            assert compatibility is not None
            for model in row.get("model_catalog") or []:
                if not isinstance(model, Mapping):
                    continue
                openrouter_model_id = str(model.get("id") or "").strip()
                if not openrouter_model_id:
                    continue
                encoded = base64.urlsafe_b64encode(
                    openrouter_model_id.encode("utf-8")
                ).rstrip(b"=").decode("ascii")
                pricing = model.get("pricing")
                pricing = pricing if isinstance(pricing, Mapping) else {}
                models.append(RuntimeModelOption(
                    id=f"{CODEX_OPENROUTER_PREFIX}{credential_id}:{encoded}",
                    label=str(model.get("name") or openrouter_model_id),
                    description=str(model.get("description") or ""),
                    api_source=api_source,
                    api_protocol=compatibility.api_protocol,
                    provider="openrouter",
                    provider_model_id=openrouter_model_id,
                    context_length=model.get("context_length"),
                    input_modalities=list(model.get("input_modalities") or []),
                    output_modalities=list(model.get("output_modalities") or []),
                    supports_tools=bool(model.get("supports_tools")),
                    supports_web_search=bool(model.get("supports_web_search")),
                    input_price=(
                        str(pricing["prompt"])
                        if pricing.get("prompt") is not None else None
                    ),
                    output_price=(
                        str(pricing["completion"])
                        if pricing.get("completion") is not None else None
                    ),
                    available=bool(model.get("available", True)),
                    supported_reasoning_efforts=_catalog_reasoning_efforts(model),
                    default_reasoning_effort=(
                        str(model.get("default_reasoning_effort"))
                        if model.get("default_reasoning_effort") is not None
                        else None
                    ),
                ))
            continue
        name = str(row.get("name") or model_name).strip()
        compatibility = compatible_api(
            RuntimeType.CODEX,
            api_source=api_source,
            provider=provider,
        )
        assert compatibility is not None
        models.append(RuntimeModelOption(
            id=f"{CODEX_CREDENTIAL_PREFIX}{credential_id}",
            label=name,
            description=f"{provider} · {model_name}",
            api_source=api_source,
            api_protocol=compatibility.api_protocol,
            provider=provider,
            provider_model_id=model_name,
            supported_reasoning_efforts=_openai_reasoning_efforts(provider),
            default_reasoning_effort=None,
        ))
    account_authenticated = False
    if "chatgpt" in allowed_auth and tenant_id and user_id:
        account_service = CodexAccountService(tenant_id, user_id)
        account_status = await account_service.status()
        account_authenticated = account_status.authenticated
        if account_authenticated:
            try:
                account_models = await account_service.list_models()
            except RuntimeError:
                account_models = []
            if account_models:
                # A connected account is the preferred Codex connection for a
                # new Chat. Existing Chats are projected through
                # `_with_chat_model_default` and retain their durable binding,
                # so changing the catalog default cannot silently move an
                # active conversation from its selected API connection.
                models = [
                    model.model_copy(update={"is_default": False})
                    for model in models
                ]
            account_default_index = next(
                (
                    index
                    for index, account_model in enumerate(account_models)
                    if account_model.is_default
                ),
                0,
            )
            for index, account_model in enumerate(account_models):
                models.append(account_model.model_copy(update={
                    "id": f"{CODEX_ACCOUNT_MODEL_PREFIX}{account_model.id}",
                    "is_default": index == account_default_index,
                    "api_source": "chatgpt_account",
                    "api_protocol": "codex_app_server",
                    "provider": "chatgpt",
                    "provider_model_id": account_model.id,
                    "description": (
                        account_model.description
                        or "Available through the connected ChatGPT account."
                    ),
                }))
    if not models:
        return RuntimeCapabilities(
            runtime_type=RuntimeType.CODEX,
            runtime_available=True,
            authenticated=False,
            source="codex.app-server+runtime-model-broker",
            error_code="codex_responses_model_unavailable",
        )
    default = next((model.id for model in models if model.is_default), None)
    return RuntimeCapabilities(
        runtime_type=RuntimeType.CODEX,
        runtime_available=True,
        authenticated=True,
        source="codex.app-server+runtime-model-broker",
        models=models,
        default_model_id=default or (models[0].id if models else None),
    )


def codex_credential_id(model_id: str | None) -> uuid.UUID | None:
    """Resolve a public Codex broker model id to its saved credential."""
    if model_id is None:
        return None
    if model_id.startswith(CODEX_OPENROUTER_PREFIX):
        raw = model_id.removeprefix(CODEX_OPENROUTER_PREFIX).partition(":")[0]
    elif model_id.startswith(CODEX_CREDENTIAL_PREFIX):
        raw = model_id.removeprefix(CODEX_CREDENTIAL_PREFIX)
    else:
        raise ValueError("model_not_available_for_runtime")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError("model_not_available_for_runtime") from exc


def codex_account_model_id(model_id: str | None) -> str | None:
    if model_id is None or not model_id.startswith(CODEX_ACCOUNT_MODEL_PREFIX):
        return None
    value = model_id.removeprefix(CODEX_ACCOUNT_MODEL_PREFIX).strip()
    if not value or len(value) > 512:
        raise ValueError("model_not_available_for_runtime")
    return value


def codex_managed_model(model_id: str | None) -> tuple[str, str] | None:
    if model_id is None or not model_id.startswith(CODEX_MANAGED_MODEL_PREFIX):
        return None
    value = model_id.removeprefix(CODEX_MANAGED_MODEL_PREFIX)
    profile_id, separator, model = value.partition(":")
    if not separator or not profile_id or not model or len(model) > 512:
        raise ValueError("model_not_available_for_runtime")
    return profile_id, model


def validate_model_effort(
    capabilities: RuntimeCapabilities,
    *,
    model_id: str | None,
    reasoning_effort: str | None,
) -> RuntimeModelOption | None:
    """Validate a per-turn selection against the same catalog the UI renders."""
    effective_model_id = model_id or capabilities.default_model_id
    model = next(
        (option for option in capabilities.models if option.id == effective_model_id),
        None,
    )
    if model is None:
        raise ValueError("model_not_available_for_runtime")
    if reasoning_effort is not None:
        allowed = {option.id for option in model.supported_reasoning_efforts}
        if reasoning_effort not in allowed:
            raise ValueError("reasoning_effort_not_supported_by_model")
    return model
