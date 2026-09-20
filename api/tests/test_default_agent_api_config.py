from __future__ import annotations

import pytest

from vibecanvas_api.config import AgentConfig, AppConfig
from vibecanvas_api.services.llm_credentials_inject import merge_agent_settings_override


def test_default_agent_api_env_builds_platform_agent_cfg(monkeypatch):
    monkeypatch.setenv(
        "VIBECANVAS_DEFAULT_AGENT_API",
        "{'provider': 'OpenAI', 'model': 'gpt-4o', "
        "'model_context_length': 256000, "
        "'base_url': 'https://api.example.test/v1', "
        "'proxy': 'http://proxy.example:8080', 'api_key': 'test-only-api-key'}",
    )

    cfg = AgentConfig({})

    assert cfg.model == "openai:gpt-4o"
    assert cfg.base_url == "https://api.example.test/v1"
    assert cfg.proxy == "http://proxy.example:8080"
    assert cfg.api_key == "test-only-api-key"
    assert cfg.model_context_tokens == 256000
    assert cfg.compaction_v2.window_tokens == 256000
    expected_runtime = {
        "model": "openai:gpt-4o",
        "base_url": "https://api.example.test/v1",
        "api_key": "test-only-api-key",
        "proxy": "http://proxy.example:8080",
        "model_context_tokens": 256000,
    }
    runtime_cfg = cfg.to_agent_cfg()
    assert {key: runtime_cfg[key] for key in expected_runtime} == expected_runtime
    assert "model_context_tokens" not in cfg.to_init_kwargs()
    assert "proxy" not in cfg.to_init_kwargs()


def test_agent_config_has_no_implicit_model_or_api(monkeypatch):
    monkeypatch.delenv("VIBECANVAS_DEFAULT_AGENT_API", raising=False)
    monkeypatch.delenv("DEFAULT_API", raising=False)

    cfg = AgentConfig({})

    assert cfg.model == ""
    assert cfg.api_key == ""


def test_runtime_model_egress_defaults_to_host_and_rejects_unknown_policy(
    monkeypatch,
):
    monkeypatch.delenv("RUNTIME_MODEL_EGRESS_POLICY", raising=False)
    assert AppConfig({}).runtime_model_egress_policy == "host"

    monkeypatch.setenv("RUNTIME_MODEL_EGRESS_POLICY", "unknown")
    try:
        AppConfig({})
    except ValueError as exc:
        assert "RUNTIME_MODEL_EGRESS_POLICY" in str(exc)
    else:  # pragma: no cover - explicit startup contract
        raise AssertionError("unknown egress policy should fail startup")


def test_sandbox_egress_defaults_to_host_network_and_rejects_unknown_policy(
    monkeypatch,
):
    monkeypatch.delenv("SANDBOX_EGRESS_MODE", raising=False)
    assert AppConfig({}).sandbox_egress_mode == "host-network"

    monkeypatch.setenv("SANDBOX_EGRESS_MODE", "unknown")
    try:
        AppConfig({})
    except ValueError as exc:
        assert "SANDBOX_EGRESS_MODE" in str(exc)
    else:  # pragma: no cover - explicit startup contract
        raise AssertionError("unknown sandbox egress mode should fail startup")


def test_sandbox_egress_defaults_to_public_and_validates_restricted_policies(
    monkeypatch,
):
    for name in (
        "SANDBOX_EGRESS_POLICY",
        "SANDBOX_EGRESS_ALLOW_HOSTS",
        "SANDBOX_EGRESS_PRIVATE_TARGETS",
        "SANDBOX_AGENT_EGRESS_POLICY",
        "SANDBOX_AGENT_EGRESS_ALLOW_HOSTS",
        "SANDBOX_AGENT_EGRESS_PRIVATE_TARGETS",
        "SANDBOX_EGRESS_TRUSTED_PROXY_CIDRS",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = AppConfig({})
    assert cfg.sandbox_egress_policy == "public"
    assert cfg.sandbox_egress_allow_hosts == ()
    assert cfg.sandbox_egress_private_targets == ()
    assert cfg.sandbox_egress_trusted_proxy_cidrs == ()

    monkeypatch.setenv("SANDBOX_EGRESS_POLICY", "allowlist")
    monkeypatch.setenv(
        "SANDBOX_EGRESS_ALLOW_HOSTS",
        ".openai.com, mcp.example.com, .openai.com",
    )
    monkeypatch.setenv(
        "SANDBOX_EGRESS_PRIVATE_TARGETS",
        "intranet.example:8443,[::1]:9000",
    )
    monkeypatch.setenv(
        "SANDBOX_EGRESS_TRUSTED_PROXY_CIDRS",
        "198.18.0.0/15,198.18.0.0/15",
    )
    cfg = AppConfig({})
    assert cfg.sandbox_egress_allow_hosts == (
        ".openai.com",
        "mcp.example.com",
    )
    assert cfg.sandbox_egress_private_targets == (
        ("intranet.example", 8443),
        ("::1", 9000),
    )
    assert cfg.sandbox_egress_trusted_proxy_cidrs == ("198.18.0.0/15",)

    monkeypatch.setenv("SANDBOX_EGRESS_POLICY", "platform-only")
    monkeypatch.delenv("SANDBOX_EGRESS_ALLOW_HOSTS")
    assert AppConfig({}).sandbox_egress_policy == "platform-only"


def test_sandbox_egress_allowlist_requires_hosts(monkeypatch):
    monkeypatch.setenv("SANDBOX_EGRESS_POLICY", "allowlist")
    monkeypatch.delenv("SANDBOX_EGRESS_ALLOW_HOSTS", raising=False)
    try:
        AppConfig({})
    except ValueError as exc:
        assert "SANDBOX_EGRESS_ALLOW_HOSTS" in str(exc)
    else:  # pragma: no cover - explicit startup contract
        raise AssertionError("an empty Runtime egress allowlist must fail startup")


def test_control_plane_proxy_is_explicit_and_validated(monkeypatch):
    monkeypatch.delenv("FLOWORK_CONTROL_PLANE_HTTP_PROXY", raising=False)
    assert AppConfig({}).control_plane_http_proxy == ""

    monkeypatch.setenv(
        "FLOWORK_CONTROL_PLANE_HTTP_PROXY",
        "http://host.docker.internal:7897",
    )
    assert (
        AppConfig({}).control_plane_http_proxy
        == "http://host.docker.internal:7897"
    )

    monkeypatch.setenv("FLOWORK_CONTROL_PLANE_HTTP_PROXY", "socks5://proxy:1080")
    with pytest.raises(ValueError, match="FLOWORK_CONTROL_PLANE_HTTP_PROXY"):
        AppConfig({})

def test_platform_default_api_can_be_hard_disabled_without_falling_back(
    monkeypatch,
):
    monkeypatch.setenv(
        "VIBECANVAS_DEFAULT_AGENT_API",
        '{"provider":"openai","model":"gpt-env","api_key":"env-key"}',
    )
    monkeypatch.setenv("AGENT_API_KEY", "legacy-env-key")
    monkeypatch.setenv("VIBECANVAS_DISABLE_PLATFORM_DEFAULT_API", "1")

    cfg = AgentConfig({
        "model": "openai:gpt-file",
        "base_url": "https://file.example/v1",
        "proxy": "https://proxy.example",
        "api_key": "file-key",
    })

    assert cfg.model == ""
    assert cfg.base_url == ""
    assert cfg.proxy == ""
    assert cfg.api_key == ""


def test_default_api_short_alias_accepts_json_and_api_url(monkeypatch):
    monkeypatch.delenv("VIBECANVAS_DEFAULT_AGENT_API", raising=False)
    monkeypatch.setenv(
        "DEFAULT_API",
        '{"provider":"google_genai","model_name":"gemini-2.5-flash",'
        '"context_window_tokens":1000000,"api_url":"https://gemini.example/v1",'
        '"api_key":"secret"}',
    )

    cfg = AgentConfig({})

    assert cfg.model == "google_genai:gemini-2.5-flash"
    assert cfg.base_url == "https://gemini.example/v1"
    assert cfg.model_context_tokens == 1000000


def test_agent_settings_hyperparams_preserve_non_secret_default_api_cfg(monkeypatch):
    monkeypatch.setenv(
        "VIBECANVAS_DEFAULT_AGENT_API",
        "{'provider': 'OpenAI', 'model': 'gpt-4o-mini', "
        "'model_context_tokens': 128000, 'api_key': 'test-only-default-key'}",
    )
    app = AppConfig({})

    cfg = merge_agent_settings_override(
        app.agent.to_agent_cfg(),
        credential_row=None,
        temperature=0.2,
        timeout=45,
    )

    assert cfg["model"] == "openai:gpt-4o-mini"
    # Provider connection material remains host-side.  The sandbox receives a
    # short-lived Runtime Model Broker capability at dispatch time, never the
    # platform provider credential from VIBECANVAS_DEFAULT_AGENT_API.
    assert {"api_key", "base_url", "proxy"}.isdisjoint(cfg)
    assert "test-only-default-key" not in repr(cfg)
    assert cfg["model_context_tokens"] == 128000
    assert cfg["temperature"] == 0.2
    assert cfg["timeout"] == 45


def test_enterprise_sso_is_opt_in(monkeypatch):
    monkeypatch.delenv("ENTERPRISE_SSO_ENABLED", raising=False)
    assert AppConfig({}).enterprise_sso_enabled is False

    monkeypatch.setenv("ENTERPRISE_SSO_ENABLED", "true")
    assert AppConfig({}).enterprise_sso_enabled is True
