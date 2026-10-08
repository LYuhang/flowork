import asyncio

import pytest
from vibecanvas_api.browser.registry import TransportRegistry, TransportSendFailed

@pytest.mark.asyncio
async def test_register_send_unregister():
    reg = TransportRegistry()
    sent = []
    async def fake_send(raw: str): sent.append(raw)
    reg.register("t:b1", fake_send)
    assert reg.is_connected("t:b1")
    ok = await reg.send_to("t:b1", "hello")
    assert ok and sent == ["hello"]
    reg.unregister("t:b1")
    assert not reg.is_connected("t:b1")
    assert await reg.send_to("t:b1", "x") is False

@pytest.mark.asyncio
async def test_register_replaces_existing():
    # DEVIATION: the plan's Step-5 used a placeholder lambda (`lambda raw: a.append(raw)
    # or _noop()`) that referenced an undefined `_noop` and wasn't a coroutine. Replaced
    # with two real coroutines; the intent is preserved verbatim — a second register for
    # the same transport id REPLACES the first (one live connection per transport, §4.3).
    reg = TransportRegistry()
    a, b = [], []
    async def send_a(raw: str): a.append(raw)
    async def send_b(raw: str): b.append(raw)
    reg.register("t", send_a)
    reg.register("t", send_b)  # replaces send_a
    await reg.send_to("t", "msg")
    assert a == [] and b == ["msg"]


@pytest.mark.asyncio
async def test_sender_failure_is_uncertain_delivery_and_unregisters():
    reg = TransportRegistry()

    async def broken_send(_raw: str):
        raise RuntimeError("socket closed during write")

    reg.register("t:b1", broken_send)
    with pytest.raises(TransportSendFailed):
        await reg.send_to("t:b1", "command")
    assert not reg.is_connected("t:b1")


def test_multiple_browsers_resolve_by_derived_extension_session():
    reg = TransportRegistry()

    async def send_a(_raw: str): ...
    async def send_b(_raw: str): ...

    reg.register("tenant:user:browser-a", send_a, session_id="session-a")
    reg.register("tenant:user:browser-b", send_b, session_id="session-b")

    assert reg.find_for_user("tenant", "user") is None
    assert (
        reg.find_for_session("tenant", "user", "session-a")
        == "tenant:user:browser-a"
    )
    assert (
        reg.find_for_session("tenant", "user", "session-b")
        == "tenant:user:browser-b"
    )
    assert reg.find_for_session("tenant", "user", "unknown") is None


@pytest.mark.asyncio
async def test_failed_old_send_preserves_reconnected_transport():
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
    reg.register(transport, old_send, session_id="old-session")
    pending = asyncio.create_task(reg.send_to(transport, "old-action"))
    try:
        await asyncio.wait_for(sending.wait(), 1)
        reg.register(transport, new_send, session_id="new-session")
        fail.set()
        with pytest.raises(TransportSendFailed):
            await asyncio.wait_for(pending, 1)
        assert reg.find_for_session("tenant", "user", "new-session") == transport
        assert not reg.unregister(transport, old_send)
        assert await reg.send_to(transport, "next-observation")
        assert received == ["next-observation"]  # No replay of old-action.
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
