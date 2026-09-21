"""Central Runtime/API-source/provider compatibility registry.

The capability projection follows one explicit chain:

    API source -> provider -> Runtime compatibility -> model -> reasoning levels

Source connectors own authentication and catalog discovery. Runtime adapters
own protocol compatibility. Model catalogs own model-level features such as
reasoning effort. Keeping these dimensions separate lets a new source or
Runtime extend the registry without teaching the web client provider-specific
rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from vibecanvas_api.services.agent_runtime.protocol import RuntimeType


@dataclass(frozen=True, slots=True)
class ApiSourceDescriptor:
    id: str
    authentication: str
    catalog: str


API_SOURCE_REGISTRY: dict[str, ApiSourceDescriptor] = {
    "manual": ApiSourceDescriptor(
        id="manual",
        authentication="api_key",
        catalog="configured_model",
    ),
    "openrouter_oauth": ApiSourceDescriptor(
        id="openrouter_oauth",
        authentication="oauth_pkce",
        catalog="remote_user_catalog",
    ),
    "chatgpt_account": ApiSourceDescriptor(
        id="chatgpt_account",
        authentication="runtime_account",
        catalog="runtime_account_catalog",
    ),
    "managed_api": ApiSourceDescriptor(
        id="managed_api",
        authentication="operator_secret",
        catalog="configured_catalog",
    ),
}


@dataclass(frozen=True, slots=True)
class RuntimeApiCompatibility:
    api_source: str
    api_protocol: str
    providers: frozenset[str] | None = None


_CODEX_RESPONSES_PROVIDERS = frozenset({"openai", "azure", "azure_openai"})


RUNTIME_API_COMPATIBILITY: dict[RuntimeType, tuple[RuntimeApiCompatibility, ...]] = {
    RuntimeType.CODEX: (
        RuntimeApiCompatibility(
            api_source="openrouter_oauth",
            api_protocol="openai_responses",
            providers=frozenset({"openrouter"}),
        ),
        RuntimeApiCompatibility(
            api_source="manual",
            api_protocol="openai_responses",
            providers=_CODEX_RESPONSES_PROVIDERS,
        ),
        RuntimeApiCompatibility(
            api_source="chatgpt_account",
            api_protocol="codex_app_server",
            providers=frozenset({"chatgpt"}),
        ),
        RuntimeApiCompatibility(
            api_source="managed_api",
            api_protocol="openai_responses",
            providers=frozenset({"openai"}),
        ),
    ),
}

if any(
    entry.api_source not in API_SOURCE_REGISTRY
    for entries in RUNTIME_API_COMPATIBILITY.values()
    for entry in entries
):
    raise RuntimeError("Runtime compatibility references an unknown API source")


def normalize_provider(provider: str) -> str:
    return str(provider or "").strip().lower().replace("-", "_")


def compatible_api(
    runtime_type: RuntimeType,
    *,
    api_source: str,
    provider: str,
) -> RuntimeApiCompatibility | None:
    """Return the declared transport contract, or ``None`` if unsupported."""
    normalized_provider = normalize_provider(provider)
    return next(
        (
            entry
            for entry in RUNTIME_API_COMPATIBILITY[runtime_type]
            if entry.api_source == api_source
            and (
                entry.providers is None
                or normalized_provider in entry.providers
            )
        ),
        None,
    )


def runtime_supports_api(
    runtime_type: RuntimeType,
    *,
    api_source: str,
    provider: str,
) -> bool:
    return compatible_api(
        runtime_type,
        api_source=api_source,
        provider=provider,
    ) is not None
