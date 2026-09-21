from __future__ import annotations


import pytest

from vibecanvas_api.services.platform_mcp.config_tools import _credential_model_catalog, _get_config
from vibecanvas_api.agents.tools.decorator import ToolError


class Ctx:
    def __init__(self):
        self.surface = "chat"
        self.chat_id = "chat_1"
        self.active_commands = ["workflow"]
        self.available_commands = ["workflow", "browser"]
        self.current_workflow_id = "wf_1"
        self.agent_cfg = {
            "model": "openai:gpt-4o",
            "temperature": 0.2,
            "max_tokens": 4096,
            "timeout": 60,
            "model_context_tokens": 128000,
        }
        self.workflow = {
            "__meta__": {
                "workflow_id": "wf_1",
                "workflow_name": "Demo",
                "workflow_version": 2,
                "workflow_subversion": 3,
                "settings": {
                    "timeouts": {"code": 60},
                    "agent_tools": {"mcp_server_ids": ["hidden"]},
                },
            }
        }


def test_get_global_config_lists_models_without_secrets():
    data = _get_config("global", Ctx())
    assert data["scope"] == "global"
    assert "models" in data["__meta__"]["fields"]
    assert data["readonly"] is True
    assert isinstance(data["models"], dict)
    assert "api_key" not in repr(data).lower()


def test_get_global_config_hides_legacy_provider_placeholders():
    data = _get_config("global", Ctx())
    assert "OpenAI" not in data["models"]
    assert "Gemini" not in data["models"]


def test_credential_model_catalog_uses_public_api_key_fields():
    models = _credential_model_catalog([
        {
            "id": "cred_1",
            "name": "my-model",
            "description": "primary classifier model",
            "provider": "openai",
            "model_name": "private-provider-model",
            "model_context_tokens": 200000,
            "api_url": "https://example.invalid/v1",
            "api_key": "secret",
            "enabled": True,
        }
    ])

    assert models == {
        "my-model": {
            "model_id": "my-model",
            "name": "my-model",
            "description": "primary classifier model",
            "provider": "openai",
            "context_window_tokens": 200000,
            "enabled": True,
            "source": "credential",
        }
    }
    dumped = repr(models).lower()
    assert "secret" not in dumped
    assert "api_url" not in dumped
    assert "private-provider-model" not in dumped


def test_get_chat_config_contains_runtime_context_and_agent_config():
    data = _get_config("chat", Ctx())
    assert data["scope"] == "chat"
    assert "active_commands" in data["__meta__"]["fields"]
    assert data["surface"] == "chat"
    assert data["current_workflow_id"] == "wf_1"
    assert data["agent"]["model_id"] == "openai:gpt-4o"
    assert data["agent"]["context_window_tokens"] == 128000


def test_get_workflow_config_hides_agent_tools_and_includes_candidates():
    data = _get_config("workflow", Ctx())
    assert data["scope"] == "workflow"
    assert "programming_languages" in data["__meta__"]["fields"]
    assert data["programming_languages"]
    assert data["field_types"]
    assert "workflow_id" not in data
    assert "settings" not in data


def test_get_config_rejects_bad_scope():
    with pytest.raises(ToolError):
        _get_config("agent", Ctx())


def test_get_config_tool_is_platform_mcp_only():
    from vibecanvas_api.agents.tools import builtin_tool_names
    from vibecanvas_api.services.platform_mcp.config_tools import CONFIG_TOOLS

    names = builtin_tool_names()
    assert "get_config" not in names
    assert [tool.name for tool in CONFIG_TOOLS] == ["get_config"]
