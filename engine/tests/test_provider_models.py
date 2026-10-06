# -*- coding: utf-8 -*-
"""Provider model routing + stubbed inference for the 4 canonical providers.

The 4 CANONICAL provider id strings (frontend uses these verbatim):
    openai → OpenAIModel
    azure_openai → AzureOpenAIModel
    anthropic → AnthropicModel
    google_genai → GoogleGenaiModel
Matching is case-insensitive; the legacy 'Gemini'/'google' family fallback and
the OpenAI-compatible default must still work (no regression).

All SDK network calls are monkeypatched — NO real API calls.
"""
import types as _t

import httpx
import pytest

import vibecanvas_engine.custom_llms as custom_llms
from vibecanvas_engine.nodes.prompt import PromptNode


@pytest.mark.parametrize("reason,expected", [
    ("length", "token budget was exhausted"),
    ("stop", "cause is unknown"),
    ("content_filter", "filtered the response"),
    ("tool_calls", "tool calls instead of text"),
    (None, "finish_reason=unknown"),
    ("private-provider-payload", "finish_reason=unknown"),
])
def test_empty_completion_preserves_safe_finish_reason(reason, expected):
    response = _t.SimpleNamespace(choices=[_t.SimpleNamespace(
        message=_t.SimpleNamespace(content="  "), finish_reason=reason,
    )])
    with pytest.raises(RuntimeError) as exc:
        custom_llms._openai_completion_text(response)
    assert expected in str(exc.value)
    assert "private-provider-payload" not in str(exc.value)


# --------------------------------------------------------------------------- #
# Routing: provider id -> class                                               #
# --------------------------------------------------------------------------- #

def _build(provider):
    """Run _build_injected_model on a minimal entry and return the instance."""
    return PromptNode._build_injected_model({
        "provider": provider,
        "model_name": "",          # force the class default
        "api_url": "",
        "api_key": "k",
        "timeout": 42,
    })


@pytest.mark.parametrize("provider,expected_cls,expected_model", [
    ("openai", custom_llms.OpenAIModel, "gpt-4o"),
    ("azure_openai", custom_llms.AzureOpenAIModel, "gpt-4o"),
    ("anthropic", custom_llms.AnthropicModel, "claude-opus-4-8"),
    ("google_genai", custom_llms.GoogleGenaiModel, "gemini-2.0-flash"),
])
def test_canonical_provider_routes_to_class(provider, expected_cls, expected_model):
    m = _build(provider)
    assert type(m) is expected_cls
    assert m.model_name == expected_model      # class default filled in
    assert m.api_key == "k"
    assert m.timeout == 42


@pytest.mark.parametrize("provider", ["OPENAI", "Azure_OpenAI", "Anthropic", "GOOGLE_GENAI"])
def test_canonical_matching_is_case_insensitive(provider):
    m = _build(provider)
    assert type(m) in (
        custom_llms.OpenAIModel, custom_llms.AzureOpenAIModel,
        custom_llms.AnthropicModel, custom_llms.GoogleGenaiModel,
    )


def test_legacy_gemini_family_still_routes_to_gemini():
    # 'google'/'gemini' are NOT canonical ids — must keep hitting GeminiModel.
    assert type(_build("gemini")) is custom_llms.GeminiModel
    assert type(_build("google")) is custom_llms.GeminiModel
    # GoogleGenaiModel subclasses GeminiModel, so guard against accidental match.
    assert type(_build("gemini")) is not custom_llms.GoogleGenaiModel


def test_unknown_provider_defaults_to_openai_compatible():
    m = _build("moonshot")
    assert type(m) is custom_llms.OpenAIModel
    assert m.api_url == "https://api.openai.com/v1"


def test_entry_values_override_defaults():
    m = PromptNode._build_injected_model({
        "provider": "anthropic",
        "model_name": "claude-sonnet-4-6",
        "api_url": "https://gateway.example/v1",
        "api_key": "test-only-secret",
        "timeout": 90,
    })
    assert type(m) is custom_llms.AnthropicModel
    assert m.model_name == "claude-sonnet-4-6"
    assert m.api_url == "https://gateway.example/v1"
    assert m.api_key == "test-only-secret"
    assert m.timeout == 90


def test_injected_model_threads_proxy_to_openai_class():
    # An entry carrying ``proxy`` → the OpenAI-compatible client stores it.
    m = PromptNode._build_injected_model({
        "provider": "openai",
        "model_name": "gpt-4o",
        "api_url": "https://api.openai.com/v1",
        "api_key": "k",
        "proxy": "http://p:8080",
    })
    assert type(m) is custom_llms.OpenAIModel
    assert m.proxy == "http://p:8080"


def test_injected_model_omits_proxy_for_anthropic():
    # AnthropicModel ctor has no ``proxy`` param — the builder must NOT pass it
    # (would raise TypeError). The entry's proxy is simply ignored there.
    m = PromptNode._build_injected_model({
        "provider": "anthropic",
        "model_name": "claude-opus-4-8",
        "api_url": "",
        "api_key": "k",
        "proxy": "http://p:8080",
    })
    assert type(m) is custom_llms.AnthropicModel
    assert not hasattr(m, "proxy")


# --------------------------------------------------------------------------- #
# Stubbed inference — no real network                                         #
# --------------------------------------------------------------------------- #

def _convo(text="hello"):
    return {"conversations": [{"from": "human", "value": text}],
            "image": [], "video": [], "audio": []}


def test_azure_openai_call_uses_azure_client(monkeypatch):
    captured = {}

    class _Resp:
        choices = [_t.SimpleNamespace(message=_t.SimpleNamespace(content="azure-ok"))]

    class _Client:
        def __init__(self, **kw):
            captured["ctor"] = kw

        chat = _t.SimpleNamespace(completions=_t.SimpleNamespace(
            create=lambda **kw: (captured.update(create=kw) or _Resp())))

    monkeypatch.setattr("openai.AzureOpenAI", _Client, raising=False)
    m = custom_llms.AzureOpenAIModel(
        model_name="my-deployment", api_key="ak",
        api_url="https://res.openai.azure.com")
    out = m(_convo(), {"api_version": "2024-10-21"})

    assert out == "azure-ok"
    # api_url → azure_endpoint, model_name → deployment, api_version threaded.
    assert captured["ctor"]["azure_endpoint"] == "https://res.openai.azure.com"
    assert captured["ctor"]["api_key"] == "ak"
    assert captured["ctor"]["api_version"] == "2024-10-21"
    assert captured["create"]["model"] == "my-deployment"


def test_anthropic_call_maps_system_and_returns_text(monkeypatch):
    captured = {}

    class _Block:
        type = "text"
        text = "claude-ok"

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **kw):
            captured.update(kw)
            return _Resp()

    class _Client:
        def __init__(self, **kw):
            captured["ctor"] = kw
            self.messages = _Messages()

    fake_anthropic = _t.SimpleNamespace(Anthropic=_Client)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_anthropic)

    m = custom_llms.AnthropicModel(model_name="claude-opus-4-8", api_key="sk")
    convo = {
        "conversations": [
            {"from": "system", "value": "be terse"},
            {"from": "human", "value": "hi"},
        ],
        "image": [], "video": [], "audio": [],
    }
    out = m(convo, {"max_tokens": 64, "temperature": 0.3})

    assert out == "claude-ok"
    assert captured["ctor"]["api_key"] == "sk"
    assert captured["model"] == "claude-opus-4-8"
    assert captured["system"] == "be terse"
    assert captured["max_tokens"] == 64
    assert captured["temperature"] == 0.3
    # the human turn became a user message with a text block
    assert captured["messages"][0]["role"] == "user"
    assert captured["messages"][0]["content"][0]["text"] == "hi"


def test_anthropic_omits_sampling_params_when_absent(monkeypatch):
    captured = {}

    class _Resp:
        content = [_t.SimpleNamespace(type="text", text="ok")]

    class _Client:
        def __init__(self, **kw):
            self.messages = _t.SimpleNamespace(
                create=lambda **kw: (captured.update(kw) or _Resp()))

    monkeypatch.setitem(
        __import__("sys").modules, "anthropic", _t.SimpleNamespace(Anthropic=_Client))
    m = custom_llms.AnthropicModel(model_name="claude-opus-4-8", api_key="sk")
    m(_convo(), {"max_tokens": 32})  # no temperature/top_p
    assert "temperature" not in captured
    assert "top_p" not in captured


# --------------------------------------------------------------------------- #
# Optional outbound proxy (migration 023): when set, the OpenAI/Azure clients  #
# are built with an explicit httpx http_client carrying the proxy; absent →    #
# byte-identical to before (NO http_client passed).                            #
# --------------------------------------------------------------------------- #

def _stub_openai(monkeypatch, captured):
    class _Resp:
        choices = [_t.SimpleNamespace(message=_t.SimpleNamespace(content="ok"))]

    class _Client:
        def __init__(self, **kw):
            captured["ctor"] = kw
        chat = _t.SimpleNamespace(completions=_t.SimpleNamespace(
            create=lambda **kw: _Resp()))

    monkeypatch.setattr("openai.OpenAI", _Client, raising=False)


def test_openai_proxy_passes_http_client(monkeypatch):
    captured = {}
    _stub_openai(monkeypatch, captured)
    m = custom_llms.OpenAIModel(
        model_name="gpt-4o", api_key="k",
        api_url="https://api.openai.com/v1", timeout=42,
        proxy="http://p:8080")
    out = m(_convo(), {})
    assert out == "ok"
    # An explicit httpx.Client was handed to openai.OpenAI carrying the proxy.
    http_client = captured["ctor"].get("http_client")
    assert isinstance(http_client, httpx.Client)
    # httpx records the proxy on the mounted transports' connection pool; verify
    # one carries our proxy host:port (host is bytes in httpcore's URL).
    proxy_urls = [
        getattr(getattr(t, "_pool", None), "_proxy_url", None)
        for t in http_client._mounts.values()
        if t is not None
    ]
    assert any(
        u is not None and u.host in (b"p", "p") and u.port == 8080
        for u in proxy_urls
    )


def test_openai_no_proxy_omits_http_client(monkeypatch):
    captured = {}
    _stub_openai(monkeypatch, captured)
    m = custom_llms.OpenAIModel(
        model_name="gpt-4o", api_key="k",
        api_url="https://api.openai.com/v1", timeout=42)  # proxy defaults None
    m(_convo(), {})
    # Unchanged construction — NO http_client kwarg.
    assert "http_client" not in captured["ctor"]


def test_openai_reports_error_shaped_success_response(monkeypatch):
    class _Resp:
        choices = [None]
        model_extra = {
            "error": {
                "code": "provider_unavailable",
                "message": "No provider was available",
            }
        }

    class _Client:
        def __init__(self, **kw):
            pass

        chat = _t.SimpleNamespace(
            completions=_t.SimpleNamespace(create=lambda **kw: _Resp())
        )

    monkeypatch.setattr("openai.OpenAI", _Client, raising=False)
    model = custom_llms.OpenAIModel("m", "k", "https://example.test/v1")

    with pytest.raises(RuntimeError, match="No provider was available"):
        model(_convo(), {})


def test_openai_missing_message_has_actionable_error(monkeypatch):
    class _Resp:
        choices = [_t.SimpleNamespace(message=None)]

    class _Client:
        def __init__(self, **kw):
            pass

        chat = _t.SimpleNamespace(
            completions=_t.SimpleNamespace(create=lambda **kw: _Resp())
        )

    monkeypatch.setattr("openai.OpenAI", _Client, raising=False)
    model = custom_llms.OpenAIModel("m", "k", "https://example.test/v1")

    with pytest.raises(RuntimeError, match="without a message"):
        model(_convo(), {})


def test_azure_proxy_passes_http_client(monkeypatch):
    captured = {}

    class _Resp:
        choices = [_t.SimpleNamespace(message=_t.SimpleNamespace(content="ok"))]

    class _Client:
        def __init__(self, **kw):
            captured["ctor"] = kw
        chat = _t.SimpleNamespace(completions=_t.SimpleNamespace(
            create=lambda **kw: _Resp()))

    monkeypatch.setattr("openai.AzureOpenAI", _Client, raising=False)
    m = custom_llms.AzureOpenAIModel(
        model_name="dep", api_key="ak",
        api_url="https://res.openai.azure.com", proxy="http://p:8080")
    m(_convo(), {"api_version": "2024-10-21"})
    assert isinstance(captured["ctor"].get("http_client"), httpx.Client)


def test_azure_no_proxy_omits_http_client(monkeypatch):
    captured = {}

    class _Resp:
        choices = [_t.SimpleNamespace(message=_t.SimpleNamespace(content="ok"))]

    class _Client:
        def __init__(self, **kw):
            captured["ctor"] = kw
        chat = _t.SimpleNamespace(completions=_t.SimpleNamespace(
            create=lambda **kw: _Resp()))

    monkeypatch.setattr("openai.AzureOpenAI", _Client, raising=False)
    m = custom_llms.AzureOpenAIModel(
        model_name="dep", api_key="ak", api_url="https://res.openai.azure.com")
    m(_convo(), {"api_version": "2024-10-21"})
    assert "http_client" not in captured["ctor"]
