"""Platform MCP config tools — read safe runtime configuration by scope."""
from __future__ import annotations

from typing import Any, Literal

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.config import AgentConfig, config as app_config
from vibecanvas_api.enums import FIELD_TYPES, get_programming_languages, get_prompt_models
from vibecanvas_api.services.llm_credentials_inject import (
    PLATFORM_DEFAULT_MODEL_NAME,
    platform_default_credential_entry,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_llm_credentials import LlmCredentialsRepo

_LEGACY_PROVIDER_PLACEHOLDERS = {"OpenAI", "Gemini"}

_SCOPE_DESCRIPTIONS = {
    "global": {
        "description": "Platform-level read-only candidates shared across chats and workflows.",
        "fields": {
            "models": (
                "Available LLM model candidates keyed by model_id. For user API "
                "credentials, model_id equals the credential name and is the value "
                "to use in PromptNode/SubAgentNode node_config.model_name. Each "
                "entry includes name, description, provider, context_window_tokens, "
                "enabled, and source. Secrets and private connection fields are "
                "not returned."
            ),
        },
    },
    "chat": {
        "description": "Current conversation runtime context and agent inference settings.",
        "fields": {
            "surface": "Where the agent is running, such as chat or browser.",
            "chat_id": "Current chat id.",
            "active_commands": "Commands currently activated in this chat, such as build.",
            "available_commands": "Commands this surface can activate.",
            "current_workflow_id": "Workflow currently selected for workflow operations, if any.",
            "agent": "Current agent model configuration: model_id, temperature, max_tokens, timeout, and context_window_tokens.",
        },
    },
    "workflow": {
        "description": "Workflow authoring candidates.",
        "fields": {
            "programming_languages": "System default programming-language candidates for code/config fields.",
            "field_types": "System default field-type candidates.",
        },
    },
}


def _scope_meta(scope: str) -> dict:
    return {
        "description": _SCOPE_DESCRIPTIONS[scope]["description"],
        "fields": dict(_SCOPE_DESCRIPTIONS[scope]["fields"]),
    }


@register_render("get_config")
def _render_get_config(raw: dict, ctx) -> Rendered:
    scope = raw.get("scope", "?")
    return Rendered(
        content=raw,
        content_type="application/json",
        abstract=f"Loaded {scope} config",
        extras={"scope": scope},
    )


def _agent_config_view(cfg: Any) -> dict:
    if isinstance(cfg, AgentConfig):
        return {
            "model_id": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": None,
            "timeout": cfg.timeout,
            "context_window_tokens": cfg.compaction_v2.window_tokens,
        }
    if isinstance(cfg, dict):
        return {
            "model_id": cfg.get("model") or cfg.get("model_id"),
            "temperature": cfg.get("temperature"),
            "max_tokens": cfg.get("max_tokens"),
            "timeout": cfg.get("timeout"),
            "context_window_tokens": cfg.get("model_context_tokens"),
        }
    return {
        "model_id": str(cfg) if cfg else "",
        "temperature": None,
        "max_tokens": None,
        "timeout": None,
        "context_window_tokens": None,
    }


def _model_catalog() -> dict[str, dict]:
    models: dict[str, dict] = {}
    default_entry = platform_default_credential_entry()
    if default_entry:
        models[PLATFORM_DEFAULT_MODEL_NAME] = {
            "model_id": PLATFORM_DEFAULT_MODEL_NAME,
            "name": PLATFORM_DEFAULT_MODEL_NAME,
            "label": "Default API",
            "description": "Platform-provided default model configured by the service.",
            "provider": default_entry.get("provider"),
            "context_window_tokens": app_config.agent.compaction_v2.window_tokens,
            "max_output_tokens": None,
            "enabled": True,
            "source": "platform_default",
        }
    for name in get_prompt_models():
        model_id = str(name)
        if model_id in _LEGACY_PROVIDER_PLACEHOLDERS:
            continue
        if model_id == app_config.agent.model:
            continue
        models[model_id] = {
            "model_id": model_id,
            "label": model_id,
            "provider": "builtin",
            "context_window_tokens": None,
            "max_output_tokens": None,
            "enabled": True,
        }
    return models


def _credential_model_catalog(rows: list[dict]) -> dict[str, dict]:
    """Public model candidates derived from user-configured API credentials.

    PromptNode/SubAgentNode resolve saved credentials by credential *name*.
    Therefore ``model_id`` is intentionally the public credential name, not the
    provider's private ``model_name`` / endpoint tuple.
    """
    models: dict[str, dict] = {}
    for row in rows:
        if row.get("enabled") is False:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        models[name] = {
            "model_id": name,
            "name": name,
            "description": row.get("description"),
            "provider": row.get("provider"),
            "context_window_tokens": row.get("model_context_tokens"),
            "enabled": True,
            "source": "credential",
        }
    return models


async def workflow_model_catalog_for_user(session, user_id: str) -> dict[str, dict]:
    """Return the exact public model handles valid in authored workflows.

    This is shared by the Agent config tool and the HTTP Workflow Check route
    so discovery and validation cannot drift. Provider model ids and secrets
    are deliberately absent; PromptNode/SubAgentNode persist only these keys.
    """
    rows = await LlmCredentialsRepo(session).list_for_user(user_id)
    return {**_model_catalog(), **_credential_model_catalog(rows)}


async def _credential_models_for_context(ctx) -> dict[str, dict]:
    tenant_id = getattr(ctx, "tenant_id", None)
    if not tenant_id:
        return {}
    async with session_scope(tenant_id=str(tenant_id)) as s:
        rows = await LlmCredentialsRepo(s).list_for_user(str(ctx.username))
    return _credential_model_catalog(rows)


def _get_config(scope: str, ctx) -> dict:
    if scope == "global":
        return {
            "scope": "global",
            "__meta__": _scope_meta("global"),
            "readonly": True,
            "models": _model_catalog(),
        }
    if scope == "chat":
        return {
            "scope": "chat",
            "__meta__": _scope_meta("chat"),
            "surface": getattr(ctx, "surface", "chat"),
            "chat_id": getattr(ctx, "chat_id", ""),
            "active_commands": list(getattr(ctx, "active_commands", []) or []),
            "available_commands": list(getattr(ctx, "available_commands", []) or []),
            "current_workflow_id": getattr(ctx, "current_workflow_id", None),
            "agent": _agent_config_view(getattr(ctx, "agent_cfg", None) or app_config.agent),
        }
    if scope == "workflow":
        return {
            "scope": "workflow",
            "__meta__": _scope_meta("workflow"),
            "programming_languages": list(get_programming_languages()),
            "field_types": list(FIELD_TYPES),
        }
    raise ToolError("invalid_scope", "scope must be one of: global, chat, workflow")


async def _get_config_async(scope: str, ctx) -> dict:
    data = _get_config(scope, ctx)
    if scope == "global":
        credential_models = await _credential_models_for_context(ctx)
        if credential_models:
            data["models"] = {**data.get("models", {}), **credential_models}
    return data


async def available_workflow_model_ids(ctx) -> set[str]:
    """Current enabled model handles visible to one Platform MCP context."""
    data = await _get_config_async("global", ctx)
    return set((data.get("models") or {}).keys())


@tool_output(content_type="application/json", tool="get_config")
async def _do_get_config(scope: Literal["global", "chat", "workflow"], runtime: ToolRuntime) -> dict:
    return await _get_config_async(str(scope), runtime.context)


@tool(response_format="content_and_artifact")
async def get_config(scope: Literal["global", "chat", "workflow"], *, runtime: ToolRuntime):
    """Read safe runtime configuration for one scope.

    Scope guide:
    - `global`: platform-level read-only candidates shared across chats and
      workflows. Use it to discover available LLM models. Returned fields:
      `models`.
    - `chat`: current conversation runtime context and agent inference settings.
      Use it to inspect active commands, available commands, selected workflow,
      and model/runtime parameters. Returned fields: `surface`, `chat_id`,
      `active_commands`, `available_commands`, `current_workflow_id`, `agent`.
    - `workflow`: workflow authoring candidates. Use it for programming language
      and field type candidates. Returned fields: `programming_languages`,
      `field_types`.

    Args:
        scope: one of `global`, `chat`, or `workflow`.

    Returns:
        content = JSON config for the requested scope, plus `__meta__` field
        descriptions. No secrets are returned.
    """
    return await _do_get_config(scope, runtime=runtime)


CONFIG_TOOLS = [get_config]
