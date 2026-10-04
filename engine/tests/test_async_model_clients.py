"""Prompt providers must yield the loop and close only the cancelled client."""
import asyncio
from types import SimpleNamespace

import pytest

from vibecanvas_engine import custom_llms


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "azure", "anthropic", "gemini"])
async def test_concurrent_calls_cancel_one_and_close_transports(monkeypatch, provider):
    started, finish, clients = {}, {}, []

    class Client:
        def __init__(self, **kwargs):
            self.key = kwargs["api_key"]
            self.closed = False
            clients.append(self)
            started[self.key] = asyncio.Event()
            finish[self.key] = asyncio.Event()
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            self.messages = SimpleNamespace(create=self.create)
            self.models = SimpleNamespace(generate_content=self.create)
            self.aio = self

        async def create(self, **kwargs):
            started[self.key].set()
            await finish[self.key].wait()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=self.key))],
                content=[SimpleNamespace(type="text", text=self.key)], text=self.key)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        def close(self):
            self.closed = True

    name, target = {
        "openai": ("OpenAIModel", "openai.AsyncOpenAI"),
        "azure": ("AzureOpenAIModel", "openai.AsyncAzureOpenAI"),
        "anthropic": ("AnthropicModel", "anthropic.AsyncAnthropic"),
        "gemini": ("GeminiModel", "google.genai.Client"),
    }[provider]
    monkeypatch.setattr(target, Client)
    cls = getattr(custom_llms, name)
    tasks = {
        key: asyncio.create_task(cls(model_name="test", api_key=key, api_url="https://broker.test").acall(
            {"conversations": [{"from": "human", "value": "hello"}], "image": []}))
        for key in ("cancelled", "sibling")
    }
    try:
        async with asyncio.timeout(3):
            while len(started) < 2 or not all(event.is_set() for event in started.values()):
                await asyncio.sleep(.001)
        tasks["cancelled"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks["cancelled"]
        assert next(c for c in clients if c.key == "cancelled").closed
        assert not tasks["sibling"].done()
        assert not next(c for c in clients if c.key == "sibling").closed
        finish["sibling"].set()
        assert await tasks["sibling"] == "sibling"
        assert all(c.closed for c in clients)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
