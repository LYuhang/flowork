"""PromptNode resolves an injected host-broker model mapping.

When the api injects ``extra["llm_credentials"]`` = { name: {provider, model_name,
api_url, api_key, timeout} }, PromptNode.__call__ must resolve ``self.model_name``
against that mapping and build the matching provider client. ``api_url`` is the
host broker and ``api_key`` is a short-lived execution capability, never the
provider secret. Inline provider secrets are rejected.
"""
import pytest

from vibecanvas_engine.nodes.prompt import PromptNode
import vibecanvas_engine.custom_llms as custom_llms


class _SpyOpenAI:
    """Stand-in for custom_llms.OpenAIModel: records ctor args, returns JSON."""
    last_kwargs = None

    def __init__(self, **kw):
        _SpyOpenAI.last_kwargs = kw

    def __call__(self, conversation_dict, inference_config=None, stop_event=None):
        return '{"ok": true}'


class _SpyGemini:
    last_kwargs = None

    def __init__(self, **kw):
        _SpyGemini.last_kwargs = kw

    def __call__(self, conversation_dict, inference_config=None, stop_event=None):
        return '{"ok": true}'


def _node(model_name, custom_model_config=None):
    n = PromptNode.__new__(PromptNode)
    n.node_config = {
        "prompt_template": "Say hi.\n{}",
        "model_name": model_name,
        "inference_config": {"temperature": 0.1, "max_tokens": 32, "top_k": -1, "top_p": 0.9},
        "custom_model_config": custom_model_config or {},
    }
    return n


@pytest.fixture(autouse=True)
def _patch_classes(monkeypatch):
    """Route both the provider-family dispatch and CUSTOM_PROVIDERS at our spies."""
    _SpyOpenAI.last_kwargs = None
    _SpyGemini.last_kwargs = None
    monkeypatch.setattr(custom_llms, "OpenAIModel", _SpyOpenAI)
    monkeypatch.setattr(custom_llms, "GeminiModel", _SpyGemini)
    monkeypatch.setattr(
        custom_llms,
        "CUSTOM_PROVIDERS",
        {
            "OpenAI": {"class": _SpyOpenAI, "default_url": "https://api.openai.com/v1", "default_model": "gpt-4o"},
            "Gemini": {"class": _SpyGemini, "default_url": "", "default_model": "gemini-2.0-flash"},
        },
    )


def test_injected_saved_name_builds_provider_from_entry():
    # The user picked a SAVED credential name; node_config holds only the name.
    n = _node("My DeepSeek")
    extra = {
        "llm_credentials": {
            "My DeepSeek": {
                "provider": "OpenAI",
                "model_name": "deepseek-chat",
                "api_url": "http://platform.test/api/internal/runtime-model/v1",
                "api_key": "workflow-capability",
                "timeout": 90,
            }
        }
    }
    n(inputs={}, previous_outputs={}, extra=extra)
    # Built OpenAIModel from the injected broker descriptor.
    assert _SpyOpenAI.last_kwargs == {
        "model_name": "deepseek-chat",
        "api_key": "workflow-capability",
        "api_url": "http://platform.test/api/internal/runtime-model/v1",
        "timeout": 90,
    }


def test_injected_gemini_family_routes_to_gemini_class():
    n = _node("My Gemini Key")
    extra = {
        "llm_credentials": {
            "My Gemini Key": {
                "provider": "Gemini",
                "model_name": "gemini-2.5-pro",
                "api_url": "",
                "api_key": "workflow-capability",
            }
        }
    }
    n(inputs={}, previous_outputs={}, extra=extra)
    assert _SpyGemini.last_kwargs["model_name"] == "gemini-2.5-pro"
    assert _SpyGemini.last_kwargs["api_key"] == "workflow-capability"
    assert _SpyOpenAI.last_kwargs is None  # did NOT touch the OpenAI path


def test_unknown_provider_defaults_to_openai_compatible():
    n = _node("Some Custom")
    extra = {
        "llm_credentials": {
            "Some Custom": {
                "provider": "moonshot",
                "model_name": "moonshot-v1",
                "api_url": "http://platform.test/api/internal/runtime-model/v1",
                "api_key": "workflow-capability",
            }
        }
    }
    n(inputs={}, previous_outputs={}, extra=extra)
    assert _SpyOpenAI.last_kwargs["model_name"] == "moonshot-v1"
    assert _SpyOpenAI.last_kwargs["api_url"] == (
        "http://platform.test/api/internal/runtime-model/v1"
    )


def test_no_injection_rejects_inline_custom_provider():
    n = _node("OpenAI", custom_model_config={
        "model_name": "gpt-4o-mini",
        "api_key": "inline-key",
        "api_url": "https://api.openai.com/v1",
        "timeout": 30,
    })
    result = n(inputs={}, previous_outputs={}, extra={})
    assert result["status"] == "error"
    assert "Workflow model is unavailable" in result["error_message"]
    assert _SpyOpenAI.last_kwargs is None


def test_injected_mapping_present_but_name_absent_rejects_inline_provider():
    n = _node("OpenAI", custom_model_config={
        "model_name": "gpt-4o",
        "api_key": "inline-key",
        "api_url": "https://api.openai.com/v1",
    })
    extra = {"llm_credentials": {"Other Saved Name": {"provider": "OpenAI", "model_name": "x", "api_key": "y"}}}
    result = n(inputs={}, previous_outputs={}, extra=extra)
    assert result["status"] == "error"
    assert "Workflow model is unavailable" in result["error_message"]
    assert _SpyOpenAI.last_kwargs is None
