# -*- coding: utf-8 -*-
"""Provider clients used with host-brokered model descriptors.

These classes are not registered in ``llm_registry``. PromptNode constructs
them from an execution-scoped descriptor whose key is a short-lived broker
capability and whose URL points to the host Runtime Model Broker. Provider
credentials and user endpoints do not enter the workflow sandbox.
"""

from __future__ import annotations

import json
import asyncio
import mimetypes
from typing import Any, Dict, Optional

import httpx

from .model_utils import convert_input, encode_image
from .register import BaseLLM


def _parse_extra_body(config: Dict[str, Any]) -> Optional[dict]:
    """Resolve an optional ``inference_config['extra_body']`` into a dict to pass
    as the OpenAI client's ``extra_body=`` (provider-specific request-body extras,
    e.g. ``{"reasoning_effort": "high"}``). Accepts a dict or a JSON-object string;
    returns None when absent/blank/unparseable (never raises — a bad value is
    simply ignored so it can't tear down the call)."""
    raw = config.get("extra_body")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _openai_completion_text(response: Any) -> str:
    """Return completion text or raise a useful provider-shape error.

    Some OpenAI-compatible gateways can answer with HTTP 200 while carrying an
    error-shaped body (or ``choices: [null]``). Indexing that response directly
    hides the provider failure behind ``'NoneType' object is not subscriptable``.
    Keep the workflow error actionable without echoing request data or secrets.
    """
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices or choices[0] is None:
        extra = getattr(response, "model_extra", None)
        error = getattr(response, "error", None)
        if error is None and isinstance(extra, dict):
            error = extra.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("code")
        else:
            detail = getattr(error, "message", None) or str(error or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Provider returned no completion choices{suffix}")

    message = getattr(choices[0], "message", None)
    if message is None:
        raise RuntimeError("Provider returned a completion without a message")
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise RuntimeError("LLM call cancelled")


class OpenAIModel(BaseLLM):
    """OpenAI-compatible API model.

    Covers: OpenAI (GPT), DeepSeek, Moonshot, Qwen, and any provider
    that implements the OpenAI chat completions API.
    """

    def __init__(self, model_name: str, api_key: str,
                 api_url: str = "https://api.openai.com/v1",
                 timeout: int = 60,
                 proxy: Optional[str] = None):
        self.model_name = model_name
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        # Optional HTTP/HTTPS proxy for outbound calls (None → no proxy; the
        # client is built exactly as before).
        self.proxy = proxy

    def _prepare(self, conversation_dict, inference_config=None):
        config = inference_config or {}

        # ShareGPT conversation_dict → OpenAI messages. convert_input splits the
        # text on the inline <<image>> placeholder and interleaves each image at
        # its authored position; encode_image reads each source (a real /run file
        # path — already mapped by PromptNode — an HTTP URL, or raw bytes). This
        # is the canonical multimodal path for every OpenAI-compatible provider.
        min_pixels = config.get("min_pixels", 1 * 1)
        max_pixels = config.get("max_pixels", 512 * 512)
        messages = convert_input(conversation_dict, min_pixels=min_pixels, max_pixels=max_pixels)

        client_kwargs: Dict[str, Any] = dict(
            api_key=self.api_key,
            base_url=self.api_url,
            timeout=self.timeout,
        )
        create_kwargs: Dict[str, Any] = dict(
            model=self.model_name,
            messages=messages,
            temperature=config.get("temperature", 0.7),
            max_tokens=config.get("max_tokens", 2048),
            top_p=config.get("top_p", 0.95),
        )
        extra_body = _parse_extra_body(config)
        if extra_body is not None:
            create_kwargs["extra_body"] = extra_body
        return client_kwargs, create_kwargs

    @staticmethod
    def _text(response):
        return _openai_completion_text(response)

    def __call__(self, conversation_dict, inference_config=None, stop_event=None):
        from openai import OpenAI
        _check_stop(stop_event)
        client_kwargs, create_kwargs = self._prepare(conversation_dict, inference_config)
        if self.proxy:
            client_kwargs["http_client"] = httpx.Client(proxy=self.proxy, timeout=self.timeout)
        client = OpenAI(**client_kwargs)
        try:
            response = client.chat.completions.create(**create_kwargs)
            _check_stop(stop_event)
            return self._text(response)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    async def acall(self, conversation_dict, inference_config=None, stop_event=None):
        from openai import AsyncOpenAI
        _check_stop(stop_event)
        # File/image conversion is blocking; model I/O remains native async.
        client_kwargs, create_kwargs = await asyncio.to_thread(
            self._prepare, conversation_dict, inference_config)
        if self.proxy:
            client_kwargs["http_client"] = httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout)
        async with AsyncOpenAI(**client_kwargs) as client:
            response = await client.chat.completions.create(**create_kwargs)
            _check_stop(stop_event)
            return self._text(response)


class AzureOpenAIModel(BaseLLM):
    """Azure OpenAI Service model.

    Wraps the ``openai`` SDK's ``AzureOpenAI`` client. The credential mapping
    mirrors OpenAIModel but reinterpreted for Azure's deployment model:

      * ``model_name`` → the Azure *deployment name* (NOT the base model id;
        on Azure the deployment is what you address in ``model=``).
      * ``api_key``    → the Azure resource key.
      * ``api_url``    → the Azure *endpoint* (e.g.
        ``https://<resource>.openai.azure.com``), passed as ``azure_endpoint``.

    Azure additionally requires an ``api_version``. It is read from
    ``inference_config["api_version"]`` when present, else defaults to a recent
    stable GA version. The request/response shape is identical to the
    OpenAI-compatible chat completions API, so the same ``convert_input``
    multimodal path is reused.
    """

    DEFAULT_API_VERSION = "2024-10-21"

    def __init__(self, model_name: str, api_key: str,
                 api_url: str = "",
                 timeout: int = 60,
                 proxy: Optional[str] = None):
        self.model_name = model_name
        self.api_key = api_key
        self.api_url = (api_url or "").rstrip("/")
        self.timeout = timeout
        # Optional HTTP/HTTPS proxy for outbound calls (None → no proxy).
        self.proxy = proxy

    def _prepare(self, conversation_dict, inference_config=None):
        config = inference_config or {}

        min_pixels = config.get("min_pixels", 1 * 1)
        max_pixels = config.get("max_pixels", 512 * 512)
        messages = convert_input(conversation_dict, min_pixels=min_pixels, max_pixels=max_pixels)

        client_kwargs: Dict[str, Any] = dict(
            api_key=self.api_key,
            azure_endpoint=self.api_url,
            api_version=config.get("api_version", self.DEFAULT_API_VERSION),
            timeout=self.timeout,
        )
        create_kwargs: Dict[str, Any] = dict(
            model=self.model_name,  # the Azure deployment name
            messages=messages,
            temperature=config.get("temperature", 0.7),
            max_tokens=config.get("max_tokens", 2048),
            top_p=config.get("top_p", 0.95),
        )
        extra_body = _parse_extra_body(config)
        if extra_body is not None:
            create_kwargs["extra_body"] = extra_body
        return client_kwargs, create_kwargs

    @staticmethod
    def _text(response):
        return _openai_completion_text(response)

    def __call__(self, conversation_dict, inference_config=None, stop_event=None):
        from openai import AzureOpenAI
        _check_stop(stop_event)
        client_kwargs, create_kwargs = self._prepare(conversation_dict, inference_config)
        if self.proxy:
            client_kwargs["http_client"] = httpx.Client(proxy=self.proxy, timeout=self.timeout)
        client = AzureOpenAI(**client_kwargs)
        try:
            response = client.chat.completions.create(**create_kwargs)
            _check_stop(stop_event)
            return self._text(response)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    async def acall(self, conversation_dict, inference_config=None, stop_event=None):
        from openai import AsyncAzureOpenAI
        _check_stop(stop_event)
        # File/image conversion is blocking; model I/O remains native async.
        client_kwargs, create_kwargs = await asyncio.to_thread(
            self._prepare, conversation_dict, inference_config)
        if self.proxy:
            client_kwargs["http_client"] = httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout)
        async with AsyncAzureOpenAI(**client_kwargs) as client:
            response = await client.chat.completions.create(**create_kwargs)
            _check_stop(stop_event)
            return self._text(response)


class AnthropicModel(BaseLLM):
    """Anthropic (Claude) API model.

    Wraps the ``anthropic`` SDK's Messages API. Credential mapping:

      * ``model_name`` → the Claude model id (e.g. ``claude-opus-4-8``).
      * ``api_key``    → the Anthropic API key.
      * ``api_url``    → optional custom ``base_url`` (gateway/proxy); empty =
        the SDK default (``https://api.anthropic.com``).

    The ShareGPT ``conversation_dict`` is mapped onto Anthropic's shape: any
    ``system`` turns become the top-level ``system`` string, human/assistant
    turns become ``messages``, and images on the last user turn are attached as
    base64 ``image`` content blocks (same image sources ``encode_image``
    understands: a real /run file path already mapped by PromptNode, an http
    URL, or raw bytes). Anthropic requires ``max_tokens``; the response text is
    the concatenation of the returned ``text`` blocks.
    """

    def __init__(self, model_name: str = "claude-opus-4-8",
                 api_key: str = "",
                 api_url: str = "",
                 timeout: int = 60):
        self.model_name = model_name
        self.api_key = api_key
        self.api_url = (api_url or "").rstrip("/")
        self.timeout = timeout

    def _prepare(self, conversation_dict, inference_config=None):
        config = inference_config or {}

        client_kwargs: Dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout}
        if self.api_url:
            client_kwargs["base_url"] = self.api_url

        system_prompt = ""
        messages = []
        last_user_msg = None
        for turn in (conversation_dict or {}).get("conversations", []):
            role_src = (turn.get("from") or "").lower()
            content = turn.get("value", "")
            if role_src == "system":
                system_prompt = (system_prompt + "\n" + content).strip() if system_prompt else content
                continue
            role = "assistant" if role_src in ("gpt", "assistant") else "user"
            msg = {"role": role, "content": [{"type": "text", "text": content}]}
            messages.append(msg)
            if role == "user":
                last_user_msg = msg

        # Attach images as base64 blocks on the last user turn (Anthropic shape).
        images = (conversation_dict or {}).get("image") or []
        if images:
            image_blocks = []
            for s in images:
                if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://")):
                    image_blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": s},
                    })
                else:
                    b64 = encode_image(s, return_base64=True)
                    mime = (mimetypes.guess_type(s)[0] if isinstance(s, str) else None) or "image/jpeg"
                    image_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    })
            if image_blocks:
                if last_user_msg is not None:
                    last_user_msg["content"] = list(last_user_msg["content"]) + image_blocks
                else:
                    messages.append({"role": "user", "content": image_blocks})

        create_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": config.get("max_tokens", 2048),
        }
        if system_prompt:
            create_kwargs["system"] = system_prompt
        # Newer Claude models (Opus 4.7+/Fable 5) reject temperature/top_p; pass
        # them only when explicitly provided so default calls stay compatible.
        if "temperature" in config:
            create_kwargs["temperature"] = config["temperature"]
        if "top_p" in config:
            create_kwargs["top_p"] = config["top_p"]

        return client_kwargs, create_kwargs

    @staticmethod
    def _text(response):
        return "".join(block.text for block in (response.content or []) if getattr(block, "type", None) == "text")

    def __call__(self, conversation_dict, inference_config=None, stop_event=None):
        import anthropic
        _check_stop(stop_event)
        client_kwargs, create_kwargs = self._prepare(conversation_dict, inference_config)
        client = anthropic.Anthropic(**client_kwargs)
        try:
            response = client.messages.create(**create_kwargs)
            _check_stop(stop_event)
            return self._text(response)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    async def acall(self, conversation_dict, inference_config=None, stop_event=None):
        import anthropic
        _check_stop(stop_event)
        # File/image conversion is blocking; model I/O remains native async.
        client_kwargs, create_kwargs = await asyncio.to_thread(
            self._prepare, conversation_dict, inference_config)
        async with anthropic.AsyncAnthropic(**client_kwargs) as client:
            response = await client.messages.create(**create_kwargs)
            _check_stop(stop_event)
            return self._text(response)


class GeminiModel(BaseLLM):
    """Google Gemini API model."""

    def __init__(self, model_name: str = "gemini-2.0-flash",
                 api_key: str = "",
                 api_url: str = "",
                 timeout: int = 60):
        self.model_name = model_name
        self.api_key = api_key
        self.api_url = (api_url or "").rstrip("/")
        self.timeout = timeout

    def _prepare(self, conversation_dict, inference_config=None):
        from google import genai

        config = inference_config or {}

        http_options = None
        if self.api_url:
            # Saved/default Workflow credentials point this SDK at the host
            # Runtime Model Broker. The SDK appends its normal
            # v1beta/models/...:generateContent path; the capability remains in
            # the provider's regular API-key position.
            http_options = genai.types.HttpOptions(
                base_url=self.api_url,
                timeout=self.timeout * 1000,
            )
        client_kwargs = dict(api_key=self.api_key, http_options=http_options)

        contents = []
        last_user_content = None
        for turn in (conversation_dict or {}).get("conversations", []):
            role_map = {"human": "user", "user": "user", "gpt": "model", "assistant": "model"}
            role = role_map.get((turn.get("from") or "").lower(), "user")
            content = turn.get("value", "")
            c = genai.types.Content(
                role=role,
                parts=[genai.types.Part(text=content)],
            )
            contents.append(c)
            if role == "user":
                last_user_content = c

        # Attach images to the last user Content alongside its text part. Image
        # entries are real file paths (a /run file already mapped by PromptNode),
        # http(s) URLs, or raw bytes — the same sources encode_image understands.
        images = (conversation_dict or {}).get("image") or []
        if images:
            image_parts = []
            for s in images:
                if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://")):
                    # Let Gemini fetch the remote file directly.
                    image_parts.append(
                        genai.types.Part.from_uri(file_uri=s, mime_type=None))
                else:
                    raw = encode_image(s, return_base64=False)
                    mime = (mimetypes.guess_type(s)[0] if isinstance(s, str) else None) or "image/jpeg"
                    image_parts.append(
                        genai.types.Part.from_bytes(data=raw, mime_type=mime))
            if image_parts:
                if last_user_content is not None:
                    last_user_content.parts = list(last_user_content.parts) + image_parts
                else:
                    contents.append(genai.types.Content(role="user", parts=image_parts))

        create_kwargs = dict(
            model=self.model_name,
            contents=contents,
            config=genai.types.GenerateContentConfig(
                temperature=config.get("temperature", 0.7),
                max_output_tokens=config.get("max_tokens", 2048),
                top_p=config.get("top_p", 0.95),
                top_k=config.get("top_k", -1) if config.get("top_k", -1) > 0 else None,
            ),
        )
        return client_kwargs, create_kwargs

    @staticmethod
    def _text(response):
        return response.text or ""

    def __call__(self, conversation_dict, inference_config=None, stop_event=None):
        from google import genai
        _check_stop(stop_event)
        client_kwargs, create_kwargs = self._prepare(conversation_dict, inference_config)
        client = genai.Client(**client_kwargs)
        try:
            response = client.models.generate_content(**create_kwargs)
            _check_stop(stop_event)
            return self._text(response)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    async def acall(self, conversation_dict, inference_config=None, stop_event=None):
        from google import genai
        _check_stop(stop_event)
        # File/image conversion is blocking; model I/O remains native async.
        client_kwargs, create_kwargs = await asyncio.to_thread(
            self._prepare, conversation_dict, inference_config)
        client = genai.Client(**client_kwargs)
        try:
            async with client.aio as aio:
                response = await aio.models.generate_content(**create_kwargs)
                _check_stop(stop_event)
                return self._text(response)
        finally:
            client.close()


class GoogleGenaiModel(GeminiModel):
    """Google GenAI (Gemini) model — the canonical ``google_genai`` provider.

    Behaviorally identical to :class:`GeminiModel` (both target the
    ``google.genai`` SDK); this subclass exists so the canonical provider id
    ``google_genai`` resolves to a class named for the provider, while
    ``Gemini``/``gemini``/``google`` keep routing to ``GeminiModel`` for
    back-compat.
    """


CUSTOM_PROVIDERS = {
    "OpenAI": {
        "class": OpenAIModel,
        "default_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "Gemini": {
        "class": GeminiModel,
        "default_url": "",
        "default_model": "gemini-2.0-flash",
    },
}


# --- Canonical provider id → class routing -----------------------------------
# The 4 canonical provider id strings the frontend uses verbatim. Matched
# case-insensitively by PromptNode._build_injected_model (precedence ABOVE the
# legacy CUSTOM_PROVIDERS / family fallbacks). Values carry the class plus its
# default base url / model so an injected credential entry can omit them.
CANONICAL_PROVIDERS = {
    "openai": {
        "class": OpenAIModel,
        "class_name": "OpenAIModel",
        "default_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "azure_openai": {
        "class": AzureOpenAIModel,
        "class_name": "AzureOpenAIModel",
        "default_url": "",
        "default_model": "gpt-4o",
    },
    "anthropic": {
        "class": AnthropicModel,
        "class_name": "AnthropicModel",
        "default_url": "",
        "default_model": "claude-opus-4-8",
    },
    "google_genai": {
        "class": GoogleGenaiModel,
        "class_name": "GoogleGenaiModel",
        "default_url": "",
        "default_model": "gemini-2.0-flash",
    },
}
