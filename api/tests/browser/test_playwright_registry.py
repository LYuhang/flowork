from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.browser.cluster_registry import ControllerRegistry


@pytest.mark.asyncio
async def test_playwright_controller_registry_is_transport_and_chat_scoped(browser_connections):
    registry = ControllerRegistry()
    received: list[dict] = []

    async def send(message: dict) -> None:
        received.append(message)

    await registry.register(
        transport_id="tenant:user:browser",
        channel="chat:allowed",
        send=send,
        close=AsyncMock(),
    )
    assert not await registry.forward_extension_message(
        transport_id="tenant:user:browser",
        channel="chat:other",
        message={"id": 1},
    )
    assert not await registry.forward_extension_message(
        transport_id="tenant:other:browser",
        channel="chat:allowed",
        message={"id": 1},
    )
    assert await registry.forward_extension_message(
        transport_id="tenant:user:browser",
        channel="chat:allowed",
        message={"id": 1, "result": {}},
    )
    assert received == [{"id": 1, "result": {}}]


@pytest.mark.asyncio
async def test_stale_controller_cannot_unregister_replacement(browser_connections):
    registry = ControllerRegistry()

    async def old(_message: dict) -> None:
        pass

    async def new(_message: dict) -> None:
        pass

    await registry.register(transport_id="tenant:user:browser", channel="c", send=old, close=AsyncMock())
    await registry.register(transport_id="tenant:user:browser", channel="c", send=new, close=AsyncMock())
    assert not await registry.unregister(
        transport_id="tenant:user:browser",
        channel="c",
        sender=old,
    )
    assert await registry.unregister(
        transport_id="tenant:user:browser",
        channel="c",
        sender=new,
    )
