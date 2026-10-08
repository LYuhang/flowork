import asyncio
from contextlib import asynccontextmanager

import pytest

from vibecanvas_api.browser.instance_relay import InstanceRelay, RelayDeliveryUnknown


@asynccontextmanager
async def relays(directory, deliver, timeout=1):
    async def reject(_owner, _payload):
        return False

    first = InstanceRelay(directory, "api-a", reject, timeout=timeout)
    second = InstanceRelay(directory, "api-b", deliver, timeout=timeout)
    await first.start()
    await second.start()
    try:
        yield first, second
    finally:
        await first.close()
        await second.close()


async def test_cross_instance_delivery_and_missing_instance(directory):
    routes, _ = directory
    frames = []

    async def deliver(owner, payload):
        frames.append((owner.transport_id, payload))
        return True

    owner = await routes.register(instance_id="api-b", transport_id="tenant:user:browser", session_id="s")
    async with relays(routes, deliver) as (first, second):
        assert await first.send(owner, {"method": "snapshot"})
        assert frames == [(owner.transport_id, {"method": "snapshot"})]
        assert not first.pending
        await second.close()
        assert not await first.send(owner, {"method": "click"})
        assert len(frames) == 1


async def test_late_cleanup_and_stale_route_never_deliver_to_replacement(directory):
    routes, _ = directory
    frames = []

    async def deliver(owner, payload):
        frames.append(owner.connection_id)
        return True

    old = await routes.register(instance_id="api-b", transport_id="tenant:user:browser", session_id="old")
    new = await routes.register(instance_id="api-b", transport_id="tenant:user:browser", session_id="new")
    async with relays(routes, deliver) as (first, _):
        assert not await first.send(old, "old command")
        assert frames == []
        assert await first.send(new, "new observation")
        assert frames == [new.connection_id]


async def test_timeout_is_unknown_and_late_ack_does_not_replay_action(directory):
    routes, _ = directory
    release = asyncio.Event()
    entered = asyncio.Event()
    frames = []

    async def deliver(owner, payload):
        frames.append(payload)
        entered.set()
        if payload == "click":
            await release.wait()
        return True

    owner = await routes.register(instance_id="api-b", transport_id="tenant:user:browser", session_id="s")
    async with relays(routes, deliver, timeout=0.2) as (first, second):
        pending = asyncio.create_task(first.send(owner, "click"))
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RelayDeliveryUnknown):
            await pending
        assert first.pending == {}
        release.set()
        await asyncio.gather(*second.handlers)
        assert await first.send(owner, "snapshot")
        assert frames == ["click", "snapshot"]


async def test_socket_write_failure_is_unknown_without_exposing_payload(directory):
    routes, _ = directory
    calls = 0

    async def deliver(owner, payload):
        nonlocal calls
        calls += 1
        raise RuntimeError("sensitive browser frame")

    owner = await routes.register(instance_id="api-b", transport_id="tenant:user:browser", session_id="s")
    async with relays(routes, deliver) as (first, _):
        with pytest.raises(RelayDeliveryUnknown) as error:
            await first.send(owner, "click")
        assert "sensitive" not in str(error.value)
        assert calls == 1


async def test_frames_are_ordered_per_connection_without_blocking_other_browsers(directory):
    routes, _ = directory
    entered = asyncio.Event()
    release = asyncio.Event()
    frames = []

    async def deliver(owner, payload):
        if payload == "first":
            entered.set()
            await release.wait()
        frames.append(payload)
        return True

    owner = await routes.register(instance_id="api-b", transport_id="tenant:user:one", session_id="s1")
    other = await routes.register(instance_id="api-b", transport_id="tenant:user:two", session_id="s2")
    async with relays(routes, deliver) as (first, second):
        call1 = asyncio.create_task(first.send(owner, "first"))
        await asyncio.wait_for(entered.wait(), 1)
        call2 = asyncio.create_task(first.send(owner, "second"))
        assert await first.send(other, "other-browser")
        assert frames == ["other-browser"]
        release.set()
        assert await call1
        assert await call2
        assert frames == ["other-browser", "first", "second"]
        await asyncio.sleep(0)
        assert second.lanes == {}
