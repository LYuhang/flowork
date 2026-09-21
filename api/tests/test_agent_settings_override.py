# -*- coding: utf-8 -*-
"""Agent settings gear — per-turn LLM credential + hyperparam override.

Pure unit tests (no DB / no event loop) for the agent_cfg-builder seam and the
``_build_chat_model`` ``max_tokens`` forwarding the gear depends on. Runtime
credential selection is covered by the host Model Broker integration tests;
here we lock the provider-shaped descriptor builder.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx

from vibecanvas_api.services.llm_credentials_inject import (
    merge_agent_settings_override,
)
from vibecanvas_api.services import workflow_subagent


def test_merge_builds_provider_colon_model_from_credential_row():
    row = {
        "provider": "google_genai",
        "model_name": "gemini-2.5-flash",
        "model_context_tokens": 1000000,
        "api_url": "https://example.test/v1",
        "api_key": "test-only-secret",
    }
    cfg = merge_agent_settings_override(
        {"model": "platform-default:foo"}, credential_row=row,
        temperature=0.7, max_tokens=2048, timeout=45,
    )
    assert cfg["model"] == "google_genai:gemini-2.5-flash"
    assert "api_key" not in cfg
    assert "base_url" not in cfg
    assert "proxy" not in cfg
    assert "test-only-secret" not in repr(cfg)
    assert cfg["model_context_tokens"] == 1000000
    assert cfg["temperature"] == 0.7
    assert cfg["max_tokens"] == 2048
    assert cfg["timeout"] == 45


def test_merge_does_not_project_connection_fields_from_credential_row():
    row = {
        "provider": "openai", "model_name": "gpt-4o", "api_key": "k",
        "api_url": "https://provider.example/v1?credential=secret",
        "proxy": "http://proxy:8080",
    }
    cfg = merge_agent_settings_override({}, credential_row=row)
    assert {"api_key", "base_url", "proxy"}.isdisjoint(cfg)


def test_merge_omits_connection_fields_when_falsy():
    for row in (
        {"provider": "openai", "model_name": "gpt-4o", "api_key": "k"},
        {"provider": "openai", "model_name": "gpt-4o", "api_key": "k", "proxy": ""},
    ):
        cfg = merge_agent_settings_override({}, credential_row=row)
        assert "proxy" not in cfg


def test_merge_only_includes_provided_hyperparams():
    row = {"provider": "openai", "model_name": "gpt-4o", "api_key": "k"}
    cfg = merge_agent_settings_override(
        {"model": "x:y"}, credential_row=row, temperature=0.2,
    )
    assert cfg["model"] == "openai:gpt-4o"
    assert cfg["temperature"] == 0.2
    # Omitted hyperparams must NOT appear (fall through to provider default).
    assert "max_tokens" not in cfg
    assert "timeout" not in cfg
    # No api_url on the row → no base_url key.
    assert "base_url" not in cfg


def test_merge_no_credential_strips_base_connection_fields():
    base = {
        "model": "platform:default",
        "api_key": "platform-key",
        "base_url": "https://provider.example/v1",
        "proxy": "http://proxy.example:8080",
    }
    cfg = merge_agent_settings_override(
        base, credential_row=None, max_tokens=512,
    )
    assert cfg["model"] == "platform:default"
    assert {"api_key", "base_url", "proxy"}.isdisjoint(cfg)
    assert cfg["max_tokens"] == 512
    # Must be a copy, not the same object (caller's base untouched).
    assert "max_tokens" not in base


def test_canonical_provider_passthrough():
    for provider in ("openai", "azure_openai", "anthropic", "google_genai"):
        cfg = merge_agent_settings_override(
            {}, credential_row={"provider": provider, "model_name": "m", "api_key": "k"},
        )
        assert cfg["model"] == f"{provider}:m"


def test_build_chat_model_forwards_max_tokens():
    captured = {}

    def _fake_init(model_str, **kwargs):
        captured["model_str"] = model_str
        captured["kwargs"] = kwargs
        return "FAKE_MODEL"

    cfg = {
        "model": "openai:gpt-4o",
        "api_key": "k",
        "temperature": 0.5,
        "max_tokens": 1234,
        "timeout": 30,
    }
    with patch("langchain.chat_models.init_chat_model", _fake_init):
        out = workflow_subagent.build_workflow_chat_model(cfg)
    assert out == "FAKE_MODEL"
    assert captured["model_str"] == "openai:gpt-4o"
    assert captured["kwargs"]["max_tokens"] == 1234
    assert captured["kwargs"]["temperature"] == 0.5
    assert captured["kwargs"]["timeout"] == 30


def test_build_chat_model_threads_proxy_into_httpx_clients():
    # When agent_cfg carries a proxy, the OpenAI-family http clients are built
    # with it; absent → no proxy (trust_env default).
    seen = {"sync": [], "async": []}

    real_client = httpx.Client
    real_async = httpx.AsyncClient

    def _spy_client(**kw):
        seen["sync"].append(kw)
        return real_client(**kw)

    def _spy_async(**kw):
        seen["async"].append(kw)
        return real_async(**kw)

    cfg = {
        "model": "openai:gpt-4o",
        "api_key": "k",
        "timeout": 30,
        "proxy": "http://proxy:8080",
    }
    with patch("langchain.chat_models.init_chat_model", lambda m, **kw: kw), \
         patch.object(workflow_subagent.httpx, "Client", _spy_client), \
         patch.object(workflow_subagent.httpx, "AsyncClient", _spy_async):
        workflow_subagent.build_workflow_chat_model(cfg)
    assert seen["sync"] and seen["sync"][0].get("proxy") == "http://proxy:8080"
    assert seen["async"] and seen["async"][0].get("proxy") == "http://proxy:8080"


def test_build_chat_model_no_proxy_omits_proxy_kwarg():
    seen = []
    real_client = httpx.Client

    def _spy_client(**kw):
        seen.append(kw)
        return real_client(**kw)

    cfg = {"model": "openai:gpt-4o", "api_key": "k", "timeout": 30}
    with patch("langchain.chat_models.init_chat_model", lambda m, **kw: kw), \
         patch.object(workflow_subagent.httpx, "Client", _spy_client), \
         patch.object(workflow_subagent.httpx, "AsyncClient", lambda **kw: real_client(**kw)):
        workflow_subagent.build_workflow_chat_model(cfg)
    assert seen and "proxy" not in seen[0]
