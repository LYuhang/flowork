from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from vibecanvas_api.services import openrouter_connection as subject
from vibecanvas_api.services.agent_runtime.capabilities import (
    LANGCHAIN_OPENROUTER_PREFIX,
    codex_capabilities,
    langchain_capabilities,
    langchain_credential_id,
    langchain_openrouter_model,
)


def _model(model_id: str = "openai/gpt-5") -> dict:
    return {
        "id": model_id,
        "name": "GPT-5",
        "description": "Tool-capable text model",
        "context_length": 400_000,
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools", "temperature"],
        "reasoning": {
            "supported_efforts": ["high", "medium", "low"],
            "default_effort": "medium",
        },
        "pricing": {"prompt": "0.000001", "completion": "0.000004"},
    }


def test_pkce_authorization_is_s256_and_contains_no_verifier() -> None:
    state, verifier, challenge = subject.new_pkce_material()
    url = subject.authorization_url(
        callback=f"https://flowork.example/settings/openrouter/callback/{state}",
        challenge=challenge,
    )
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).scheme == "https"
    assert urlsplit(url).netloc == "openrouter.ai"
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [challenge]
    assert verifier not in url
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert state not in subject.state_digest(state)


def test_catalog_keeps_only_text_tool_models_and_preserves_unavailable_current() -> None:
    image_only = _model("vendor/image")
    image_only["architecture"]["output_modalities"] = ["image"]
    no_tools = _model("vendor/plain")
    no_tools["supported_parameters"] = ["temperature"]
    models = subject.normalize_catalog({"data": [_model(), image_only, no_tools]})
    assert [model["id"] for model in models] == ["openai/gpt-5"]
    assert models[0]["supports_tools"] is True
    assert models[0]["supports_web_search"] is False
    assert models[0]["context_length"] == 400_000
    assert models[0]["supported_reasoning_efforts"] == ["low", "medium", "high"]
    assert models[0]["default_reasoning_effort"] == "medium"

    merged = subject.merge_catalog_with_current(
        models, current_model_id="anthropic/retired-model",
    )
    assert merged[-1]["id"] == "anthropic/retired-model"
    assert merged[-1]["available"] is False


def test_catalog_keeps_max_effort_selectable_but_uses_a_balanced_default() -> None:
    model = _model("stealth/ox-alpha")
    model["reasoning"] = {
        "supported_efforts": ["low", "high", "max"],
        "default_effort": "max",
    }

    normalized = subject.normalize_model(model)

    assert normalized is not None
    assert normalized["supported_reasoning_efforts"] == ["low", "high", "max"]
    assert normalized["default_reasoning_effort"] == "low"


def test_catalog_preserves_hosted_web_search_capability_separately() -> None:
    searchable = _model()
    searchable["supported_parameters"].append("web_search_options")

    model = subject.normalize_model(searchable)

    assert model is not None
    assert model["supports_tools"] is True
    assert model["supports_web_search"] is True


@pytest.mark.asyncio
async def test_user_catalog_uses_authenticated_user_filtered_endpoint(monkeypatch) -> None:
    captured: dict = {}

    async def fake_request(method: str, url: str, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return httpx.Response(
            200,
            json={"data": [_model()]},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(subject, "request_pinned_public_url", fake_request)
    models = await subject.fetch_user_model_catalog("write-only-key")
    assert models[0]["id"] == "openai/gpt-5"
    assert captured["url"] == "https://openrouter.ai/api/v1/models/user"
    assert captured["headers"]["Authorization"] == "Bearer write-only-key"
    assert captured["headers"]["Accept-Encoding"] == "identity"


@pytest.mark.asyncio
async def test_openrouter_requests_use_explicit_control_plane_proxy(monkeypatch) -> None:
    requests: list[dict] = []

    async def fake_request(method: str, url: str, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        payload = {"key": "sk-or-test"} if method == "POST" else {"data": [_model()]}
        return httpx.Response(
            200,
            json=payload,
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(subject, "request_pinned_public_url", fake_request)
    monkeypatch.setattr(
        subject.config,
        "control_plane_http_proxy",
        "http://proxy.internal:7897",
    )
    monkeypatch.setattr(
        subject.config,
        "sandbox_egress_trusted_proxy_cidrs",
        ("198.18.0.0/15",),
    )

    assert await subject.exchange_authorization_code(
        code="authorization-code",
        verifier="verifier",
    ) == "sk-or-test"
    assert (await subject.fetch_user_model_catalog("sk-or-test"))[0]["id"] == "openai/gpt-5"
    assert [request["proxy"] for request in requests] == [
        "http://proxy.internal:7897",
        "http://proxy.internal:7897",
    ]
    assert [request["headers"]["Accept-Encoding"] for request in requests] == [
        "identity",
        "identity",
    ]
    assert [request["trusted_proxy_cidrs"] for request in requests] == [
        ("198.18.0.0/15",),
        ("198.18.0.0/15",),
    ]


@pytest.mark.asyncio
async def test_openrouter_requests_connect_directly_without_proxy(monkeypatch) -> None:
    captured: dict = {}

    async def fake_request(method: str, url: str, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return httpx.Response(
            200,
            json={"key": "sk-or-test"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(subject, "request_pinned_public_url", fake_request)
    monkeypatch.setattr(subject.config, "control_plane_http_proxy", "")
    monkeypatch.setattr(subject.config, "sandbox_egress_trusted_proxy_cidrs", ())

    await subject.exchange_authorization_code(
        code="authorization-code",
        verifier="verifier",
    )

    assert captured["proxy"] is None
    assert captured["trusted_proxy_cidrs"] == ()


@pytest.mark.asyncio
async def test_revoked_key_is_classified_without_returning_provider_body(
    monkeypatch,
) -> None:
    async def fake_request(method: str, url: str, **kwargs):
        del kwargs
        return httpx.Response(
            401,
            content=b"sensitive provider diagnostics",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(subject, "request_pinned_public_url", fake_request)
    with pytest.raises(subject.OpenRouterConnectionError) as caught:
        await subject.fetch_user_model_catalog("revoked-key")
    assert caught.value.code == "openrouter_credentials_rejected"
    assert "sensitive" not in str(caught.value)


def test_langchain_expands_openrouter_catalog_with_source_metadata() -> None:
    credential_id = "7c58b22c-1df3-4ecf-b904-ec6d5aa19f98"
    capabilities = langchain_capabilities([{
        "id": credential_id,
        "name": "OpenRouter",
        "provider": "openrouter",
        "runtime_scope": "langchain",
        "connection_kind": "openrouter_oauth",
        "model_name": "openai/gpt-5",
        "model_catalog": [{
            "id": "openai/gpt-5",
            "name": "GPT-5",
            "description": "",
            "context_length": 400_000,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supports_tools": True,
            "pricing": {"prompt": "0.1", "completion": "0.2"},
            "available": True,
        }],
    }])
    option = next(
        model for model in capabilities.models
        if model.id.startswith(LANGCHAIN_OPENROUTER_PREFIX)
    )
    assert str(langchain_credential_id(option.id)) == credential_id
    assert langchain_openrouter_model(option.id) == "openai/gpt-5"
    assert option.context_length == 400_000
    assert option.supports_tools is True
    assert option.api_source == "openrouter_oauth"
    assert option.api_protocol == "openai_compatible"


@pytest.mark.asyncio
async def test_openrouter_catalog_is_projected_to_codex_responses(
    monkeypatch,
) -> None:
    from vibecanvas_api.services.agent_runtime import capabilities as runtime_caps

    monkeypatch.setattr(runtime_caps, "resolve_codex_executable", lambda: "/bin/codex")
    result = await codex_capabilities(
        [{
            "id": "7c58b22c-1df3-4ecf-b904-ec6d5aa19f98",
            "provider": "openrouter",
            "runtime_scope": "langchain",
            "connection_kind": "openrouter_oauth",
            "model_name": "openai/gpt-5",
            "model_catalog": [_model()],
        }],
        auth_methods={"personal_api"},
    )
    assert len(result.models) == 1
    option = result.models[0]
    assert option.provider == "openrouter"
    assert option.provider_model_id == "openai/gpt-5"
    assert option.api_source == "openrouter_oauth"
    assert option.api_protocol == "openai_responses"


def test_revoked_openrouter_connection_is_not_advertised_to_langchain() -> None:
    result = langchain_capabilities([{
        "id": "7c58b22c-1df3-4ecf-b904-ec6d5aa19f98",
        "provider": "openrouter",
        "connection_kind": "openrouter_oauth",
        "model_name": "openai/gpt-5",
        "model_catalog": [_model()],
        "catalog_error_code": "openrouter_credentials_rejected",
    }])
    assert all(model.provider != "openrouter" for model in result.models)
