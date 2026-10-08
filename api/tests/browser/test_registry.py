import asyncio

from unittest.mock import AsyncMock

import pytest
from vibecanvas_api.browser.cluster_registry import TransportRegistry
from vibecanvas_api.browser.connection_errors import TransportSendFailed

@pytest.mark.asyncio
async def test_register_send_unregister(browser_connections):
    reg = TransportRegistry()
    sent = []
    async def fake_send(raw: str): sent.append(raw)
    await reg.register("tenant:user:b1", fake_send, close=AsyncMock(), session_id="session")
    assert await reg.is_connected("tenant:user:b1")
    ok = await reg.send_to("tenant:user:b1", "hello")
    assert ok and sent == ["hello"]
    await reg.unregister("tenant:user:b1")
    assert not await reg.is_connected("tenant:user:b1")
    assert await reg.send_to("tenant:user:b1", "x") is False

@pytest.mark.asyncio
async def test_register_replaces_existing(browser_connections):
    reg = TransportRegistry()
    a, b = [], []
    async def send_a(raw: str): a.append(raw)
    async def send_b(raw: str): b.append(raw)
    await reg.register("tenant:user:browser", send_a, close=AsyncMock(), session_id="session")
    await reg.register("tenant:user:browser", send_b, close=AsyncMock(), session_id="session")  # replaces send_a
    await reg.send_to("tenant:user:browser", "msg")
    assert a == [] and b == ["msg"]


@pytest.mark.asyncio
async def test_sender_failure_is_uncertain_delivery_and_unregisters(browser_connections):
    reg = TransportRegistry()

    async def broken_send(_raw: str):
        raise RuntimeError("socket closed during write")

    await reg.register("tenant:user:b1", broken_send, close=AsyncMock(), session_id="session")
    with pytest.raises(TransportSendFailed):
        await reg.send_to("tenant:user:b1", "command")
    assert not await reg.is_connected("tenant:user:b1")


async def test_multiple_browsers_resolve_by_derived_extension_session(browser_connections):
    reg = TransportRegistry()

    async def send_a(_raw: str): ...
    async def send_b(_raw: str): ...

    await reg.register("tenant:user:browser-a", send_a, session_id="session-a", close=AsyncMock())
    await reg.register("tenant:user:browser-b", send_b, session_id="session-b", close=AsyncMock())

    assert (
        await reg.find_for_session("tenant", "user", "session-a")
        == "tenant:user:browser-a"
    )
    assert (
        await reg.find_for_session("tenant", "user", "session-b")
        == "tenant:user:browser-b"
    )
    assert await reg.find_for_session("tenant", "user", "unknown") is None


@pytest.mark.asyncio
async def test_failed_old_send_preserves_reconnected_transport(browser_connections):
    reg = TransportRegistry()
    sending = asyncio.Event()
    fail = asyncio.Event()
    received = []

    async def old_send(raw):
        sending.set()
        await fail.wait()
        raise OSError("old socket disconnected")

    async def new_send(raw):
        received.append(raw)

    transport = "tenant:user:browser"
    await reg.register(transport, old_send, session_id="old-session", close=AsyncMock())
    pending = asyncio.create_task(reg.send_to(transport, "old-action"))
    try:
        await asyncio.wait_for(sending.wait(), 1)
        await reg.register(transport, new_send, session_id="new-session", close=AsyncMock())
        fail.set()
        with pytest.raises(TransportSendFailed):
            await asyncio.wait_for(pending, 1)
        assert await reg.find_for_session("tenant", "user", "new-session") == transport
        assert not await reg.unregister(transport, old_send)
        assert await reg.send_to(transport, "next-observation")
        assert received == ["next-observation"]  # No replay of old-action.
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
