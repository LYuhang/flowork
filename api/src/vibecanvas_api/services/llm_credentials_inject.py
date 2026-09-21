# -*- coding: utf-8 -*-
"""Assemble brokered model entries that PromptNode resolves at runtime.

The user configures a PromptNode's ``model_name`` to the NAME of a saved tenant
LLM credential (or a built-in provider like 'OpenAI'/'Gemini'/a registered
platform model). The NAME — never the api_key — is what lives in node_config /
the frontend. BEFORE a run, the api:

  1. scans the workflow dict for PromptNode ``model_name`` values that are NOT
     built-ins,
  2. fetches the current user's authorized credential metadata,
  3. builds ``{name: {provider, model_name, api_url=<host broker>,
     api_key=<short-lived capability>}}`` for referenced names only,

and injects it into the engine run as ``extra['llm_credentials']``. No provider
key, provider URL, or proxy credential is serialized into the sandbox. The
host broker revalidates active membership, Workflow execute access, credential
use access, configuration revision, and authorization generation on every
provider request.

The engine side (``vibecanvas_engine.nodes.prompt.PromptNode._build_injected_model``)
maps each entry's ``provider`` to the right BaseLLM subclass.
"""
from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_engine.register import llm_registry

from vibecanvas_api.config import config as app_config
from vibecanvas_api.services.agent_runtime.model_capability import (
    authorization_model_generation,
    model_config_revision,
)
from vibecanvas_api.services.agent_runtime.workflow_model_capability import (
    mint_runtime_workflow_model_capability,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo
from vibecanvas_api.storage.sync_session import run_in_short_session

logger = structlog.get_logger(__name__)

PLATFORM_DEFAULT_MODEL_NAME = "Default API"


def platform_default_credential_entry() -> dict | None:
    """Return non-secret platform-default model metadata.

    PromptNode/SubAgentNode use `node_config.model_name` as a stable handle.
    The chat agent's provider-prefixed model string (for example
    `openai:gpt-4o`) is not that handle; expose it as `Default API` and resolve
    it here at execution time, the same way tenant credentials are resolved.
    """
    cfg = app_config.agent
    model = str(getattr(cfg, "model", "") or "")
    if not model:
        return None
    if ":" in model:
        provider, model_name = model.split(":", 1)
    else:
        provider, model_name = "", model
    entry = {
        "provider": provider,
        "model_name": model_name,
        "model_context_tokens": getattr(cfg, "model_context_tokens", None),
        "timeout": getattr(cfg, "timeout", None),
    }
    # The key is only checked for availability. It is deliberately never
    # included in the returned catalog metadata.
    if (
        not entry["provider"]
        or not entry["model_name"]
        or not getattr(cfg, "api_key", "")
    ):
        return None
    return entry


def platform_default_model_aliases() -> set[str]:
    """Handles that should resolve to the platform default credential.

    `Default API` is the public handle agents should write. Older generated
    workflows may still contain the provider-prefixed model id or bare provider
    model name; keep those resolving at runtime so existing canvases do not fail.
    """
    entry = platform_default_credential_entry()
    if not entry:
        return set()
    aliases = {PLATFORM_DEFAULT_MODEL_NAME}
    provider = str(entry.get("provider") or "").strip()
    model_name = str(entry.get("model_name") or "").strip()
    configured = str(getattr(app_config.agent, "model", "") or "").strip()
    if model_name:
        aliases.add(model_name)
    if provider and model_name:
        aliases.add(f"{provider}:{model_name}")
    if configured:
        aliases.add(configured)
    return {a for a in aliases if a}

# Built-in model_name values that are resolved WITHOUT a tenant credential:
#   * the two inline custom providers (engine ``custom_llms.CUSTOM_PROVIDERS``)
#   * any platform model registered in the engine ``llm_registry``
# Anything else in a PromptNode's model_name is treated as a saved-credential
# NAME to look up. Computed once at import (registry is populated at app boot via
# ``enums.load_config_and_sync``); a saved name that happens to collide with a
# registered model is intentionally shadowed by the built-in (the registry wins).
_BUILTIN_CUSTOM_PROVIDERS = ("OpenAI", "Gemini")


def _builtin_model_names() -> set[str]:
    """The set of model_name values that need NO credential lookup.

    Reads the live engine registry + the static custom providers. Computed
    per-call (not cached) so it reflects the registry snapshot at run time."""
    names = set(_BUILTIN_CUSTOM_PROVIDERS)
    names.update(llm_registry.list_all())
    return names


# Node types that reference a saved-credential NAME at
# ``node_config["model_name"]`` and rely on the injected ``llm_credentials``
# mapping for runtime model resolution. PromptNode is the original consumer;
# SubAgentNode stores its model at the same key and resolves
# it solely through the injected mapping (its engine node calls
# ``build_model_from_credentials``), so it MUST be scanned identically — see
# [[feedback_byo_llm_one_scheme]] (one BYO-LLM scheme, several call sites).
_MODEL_NAME_NODE_TYPES = ("PromptNode", "SubAgentNode")


def reject_inline_model_credentials(workflow_dict: dict) -> None:
    """Fail before sandbox launch when a legacy node embeds a provider secret."""
    for node_id, node in (workflow_dict or {}).items():
        if node_id == "__meta__" or not isinstance(node, dict):
            continue
        if node.get("node_type") not in _MODEL_NAME_NODE_TYPES:
            continue
        custom = (node.get("node_config") or {}).get("custom_model_config") or {}
        if not isinstance(custom, dict):
            continue
        if any(
            str(custom.get(key) or "").strip()
            for key in ("api_key", "proxy")
        ):
            raise ValueError(
                "Inline model credentials are not supported; save the API "
                "credential in API Keys and select it by name."
            )


def _normalized_provider(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _brokered_entry(
    *,
    organization_id: str,
    user_id: str,
    workflow_id: str,
    execution_id: str,
    execution_resource_type: str,
    credential_id: str | None,
    provider: str,
    model: str,
    config_revision: str,
    model_context_tokens: object = None,
    timeout: object = None,
    principal_type: str = "user",
    principal_id: str | None = None,
    principal_generation: int = 0,
) -> dict:
    token = mint_runtime_workflow_model_capability(
        organization_id=organization_id,
        user_id=user_id,
        workflow_id=workflow_id,
        execution_id=execution_id,
        execution_resource_type=execution_resource_type,
        credential_id=credential_id,
        provider=provider,
        model=model,
        config_revision=config_revision,
        authorization_generation=authorization_model_generation(
            model_id=app_config.openfga_authorization_model_id,
        ),
        secret=app_config.signing_secret,
        ttl_s=app_config.mcp.runtime_model_capability_ttl_s,
        principal_type=principal_type,
        principal_id=principal_id,
        principal_generation=principal_generation,
    )
    return {
        "provider": provider,
        "model_name": model,
        "model_context_tokens": model_context_tokens,
        "api_url": (
            f"{app_config.mcp.platform_internal_base_url}"
            "/api/internal/runtime-model/v1"
        ),
        # Provider SDKs already accept their key in this position. The value is
        # a broker capability, never the provider secret.
        "api_key": token,
        "timeout": timeout,
    }


def collect_referenced_credential_names(workflow_dict: dict) -> set[str]:
    """Scan a workflow dict for ``node_config["model_name"]`` values that are NOT
    built-ins — i.e. candidate saved-credential names to resolve.

    Both ``PromptNode`` and ``SubAgentNode`` store their model at the same
    ``node_config["model_name"]`` key and are handled identically (saved names
    only; built-ins are skipped). Names are deduped across node types.

    A workflow is a flat ``{node_id: node}`` dict (+ a reserved ``__meta__``
    key). Pure / side-effect-free so it is unit-testable without a DB."""
    builtins = _builtin_model_names()
    default_aliases = platform_default_model_aliases()
    referenced: set[str] = set()
    for node_id, node in (workflow_dict or {}).items():
        if node_id == "__meta__" or not isinstance(node, dict):
            continue
        if node.get("node_type") not in _MODEL_NAME_NODE_TYPES:
            continue
        name = (node.get("node_config") or {}).get("model_name")
        if isinstance(name, str) and name and (name not in builtins or name in default_aliases):
            referenced.add(name)
    return referenced


async def build_llm_credentials_extra(
    workflow_dict: dict,
    session: AsyncSession,
    *,
    organization_id: str,
    user_id: str,
    workflow_id: str,
    execution_id: str,
    execution_resource_type: str,
    principal_type: str = "user",
    principal_id: str | None = None,
    principal_generation: int = 0,
) -> dict[str, dict]:
    """Build the ``extra['llm_credentials']`` mapping for ``workflow_dict``.

    The ``api_key`` values in the result are signed broker capabilities, not
    provider secrets. Empty dict when no saved/default model is referenced.

    ``session`` MUST already be tenant-bound (``session_scope(tenant_id=...)`` /
    ``run_in_short_session`` with the tenant CV set) — RLS scopes the rows. Reads
    only metadata required to mint a capability. Fail-soft: a repo error logs +
    returns whatever platform-default aliases were resolved."""
    reject_inline_model_credentials(workflow_dict)
    referenced = collect_referenced_credential_names(workflow_dict)
    if not referenced:
        return {}

    mapping: dict[str, dict] = {}
    default_entry = platform_default_credential_entry()
    if default_entry:
        default_provider = _normalized_provider(default_entry.get("provider"))
        default_model = str(default_entry.get("model_name") or "").strip()
        brokered_default = _brokered_entry(
            organization_id=organization_id,
            user_id=user_id,
            workflow_id=workflow_id,
            execution_id=execution_id,
            execution_resource_type=execution_resource_type,
            credential_id=None,
            provider=default_provider,
            model=default_model,
            config_revision=model_config_revision(
                provider=default_provider,
                model=default_model,
                updated_at="platform-process-config",
            ),
            model_context_tokens=default_entry.get("model_context_tokens"),
            timeout=default_entry.get("timeout"),
            principal_type=principal_type,
            principal_id=principal_id,
            principal_generation=principal_generation,
        )
        for alias in sorted(platform_default_model_aliases() & referenced):
            mapping[alias] = brokered_default
    try:
        rows = await LlmCredentialsRepo(session).list_for_user(user_id)
    except Exception:  # pragma: no cover - fail-soft, never abort the run here
        logger.warning("llm_credentials_fetch_failed", exc_info=True)
        return mapping

    for row in rows:
        name = row.get("name")
        if name in referenced:
            if not row.get("secret_ref"):
                # A strict SecretService reference is mandatory.
                logger.warning(
                    "llm_credential_not_encrypted",
                    credential_id=str(row.get("id") or ""),
                )
                continue
            provider = _normalized_provider(row.get("provider"))
            model = str(row.get("model_name") or "").strip()
            mapping[name] = _brokered_entry(
                organization_id=organization_id,
                user_id=user_id,
                workflow_id=workflow_id,
                execution_id=execution_id,
                execution_resource_type=execution_resource_type,
                credential_id=str(row["id"]),
                provider=provider,
                model=model,
                config_revision=model_config_revision(
                    provider=provider,
                    model=model,
                    updated_at=row.get("updated_at"),
                ),
                model_context_tokens=row.get("model_context_tokens"),
                timeout=row.get("timeout"),
                principal_type=principal_type,
                principal_id=principal_id,
                principal_generation=principal_generation,
            )
    return mapping


async def inject_into_run_context_async(
    run_context: dict,
    workflow_dict: dict,
    tenant_id: str,
    *,
    user_id: str,
    workflow_id: str,
    execution_id: str,
    execution_resource_type: str,
    principal_type: str = "user",
    principal_id: str | None = None,
    principal_generation: int = 0,
) -> dict:
    """ASYNC (on-loop) one-shot: open a short tenant-bound session, build the
    credential mapping for ``workflow_dict``, and (if non-empty) merge it into
    ``run_context`` under ``llm_credentials``.

    Returns the (mutated) ``run_context`` so the call reads as a pipe at the
    trigger seam. No-op (key absent) when no saved credential is referenced —
    keeps legacy runs byte-identical. Used by the in-API SSE / sync-invoke paths
    and the async background worker deployment task (all run ON an event loop)."""
    async with session_scope(tenant_id=tenant_id) as s:
        mapping = await build_llm_credentials_extra(
            workflow_dict,
            s,
            organization_id=tenant_id,
            user_id=user_id,
            workflow_id=workflow_id,
            execution_id=execution_id,
            execution_resource_type=execution_resource_type,
            principal_type=principal_type,
            principal_id=principal_id,
            principal_generation=principal_generation,
        )
    if mapping:
        run_context["llm_credentials"] = mapping
    return run_context


def inject_into_run_context_sync(
    run_context: dict,
    workflow_dict: dict,
    *,
    tenant_id: str,
    user_id: str,
    workflow_id: str,
    execution_id: str,
    execution_resource_type: str,
    principal_type: str = "user",
    principal_id: str | None = None,
    principal_generation: int = 0,
) -> dict:
    """SYNC (background worker, no running loop) one-shot. Reads the tenant from the
    ``current_sync_tenant_id`` CV (already set by the worker body) via
    ``run_in_short_session`` and merges the mapping into ``run_context``.

    Mirrors ``inject_into_run_context_async`` for the sync ``run_workflow_sync``
    path. MUST NOT be called on a running loop (``run_in_short_session`` drives
    its own ``asyncio.run``)."""
    mapping = run_in_short_session(
        lambda s: build_llm_credentials_extra(
            workflow_dict,
            s,
            organization_id=tenant_id,
            user_id=user_id,
            workflow_id=workflow_id,
            execution_id=execution_id,
            execution_resource_type=execution_resource_type,
            principal_type=principal_type,
            principal_id=principal_id,
            principal_generation=principal_generation,
        ))
    if mapping:
        run_context["llm_credentials"] = mapping
    return run_context


def merge_agent_settings_override(
    base_cfg: dict, *, credential_row: dict | None,
    temperature=None, max_tokens=None, timeout=None,
) -> dict:
    """Pure builder: shape a per-turn ``agent_cfg`` dict from a resolved
    credential row + the optional hyperparameter overrides.

    ``base_cfg`` is the JSON-serialisable default agent config dict used when
    ``credential_row`` is absent. Connection material is intentionally removed
    in both branches: the sandbox receives only a short-lived Runtime Model
    Broker capability at the later dispatch boundary. When a credential row is
    given, build a secretless descriptor whose ``model`` is
    ``"{provider}:{model_name}"``; only explicitly provided hyperparameters are
    included (an omitted one falls through to the provider/model default, NOT
    the platform default cfg).

    Side-effect free + DB-free so it is directly unit-testable."""
    if credential_row is None:
        cfg = {
            key: value
            for key, value in dict(base_cfg or {}).items()
            if key not in {"api_key", "base_url", "proxy"}
        }
    else:
        provider = credential_row.get("provider") or ""
        model_name = credential_row.get("model_name") or ""
        cfg = {"model": f"{provider}:{model_name}"}
        if credential_row.get("model_context_tokens"):
            cfg["model_context_tokens"] = int(credential_row["model_context_tokens"])
    if temperature is not None:
        cfg["temperature"] = float(temperature)
    if max_tokens is not None:
        cfg["max_tokens"] = int(max_tokens)
    if timeout is not None:
        cfg["timeout"] = int(timeout)
    return cfg
