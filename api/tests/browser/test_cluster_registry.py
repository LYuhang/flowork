import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from vibecanvas_api.browser.cluster_registry import BrowserConnections


@asynccontextmanager
async def instances(socket, count=2, ttl=60):
    runtimes = []
    try:
        for _ in range(count):
            runtime = BrowserConnections(Redis(unix_socket_path=socket, decode_responses=True,
                                               retry=Retry(NoBackoff(), 0)))
            runtime.directory.ttl = ttl
            await runtime.start()
            runtimes.append(runtime)
        yield runtimes
    finally:
        for runtime in runtimes:
            await runtime.close()


async def test_extension_cdp_and_authorization_can_use_different_instances(directory):
    routes, socket = directory
    async with instances(socket, 3) as (extension, cdp, authorization):
        extension_send, cdp_send = AsyncMock(), AsyncMock()
        await extension.bind("tenant:user:browser", extension_send, AsyncMock(), "session")
        await cdp.bind("tenant:user:browser", cdp_send, AsyncMock(), "", "chat:one")
        owner = await authorization.directory.find_for_session("tenant", "user", "session")
        assert owner.instance_id == extension.instance_id
        assert await cdp.send(owner.transport_id, "snapshot request")
        extension_send.assert_awaited_once_with("snapshot request")
        assert await extension.send(owner.transport_id, {"id": 1, "result": {}}, "chat:one")
        cdp_send.assert_awaited_once_with({"id": 1, "result": {}})
        assert not await extension.send(owner.transport_id, {}, "chat:another")
        assert await authorization.directory.find_for_session("tenant", "other-user", "session") is None
    assert await routes.get("tenant:user:browser") is None


async def test_replacement_closes_only_old_socket_and_old_cleanup_cannot_remove_it(directory):
    routes, socket = directory
    async with instances(socket, ttl=1) as (old_instance, new_instance):
        old_closed = asyncio.Event()

        async def close_old():
            old_closed.set()

        old_send, new_send = AsyncMock(), AsyncMock()
        old = await old_instance.bind("tenant:user:browser", old_send, close_old, "session")
        new = await new_instance.bind("tenant:user:browser", new_send, AsyncMock(), "session")
        await asyncio.wait_for(old_closed.wait(), 2)
        assert not await old_instance.unbind(old.transport_id, sender=old_send)
        assert await routes.get(old.transport_id) == new
        assert await old_instance.send(old.transport_id, "observation")
        old_send.assert_not_awaited()
        new_send.assert_awaited_once_with("observation")


async def test_relay_loss_closes_local_sockets_and_removes_advertised_routes(directory):
    routes, socket = directory
    async with instances(socket) as (extension, cdp):
        closed = asyncio.Event()

        async def close():
            closed.set()

        await extension.bind("tenant:user:browser", AsyncMock(), close, "session")
        await extension.relay.close()
        await asyncio.wait_for(closed.wait(), 1)
        await extension.maintenance
        assert await routes.get("tenant:user:browser") is None
        assert not await cdp.send("tenant:user:browser", "click")


async def test_old_failed_write_cannot_remove_replacement_in_same_instance(directory):
    routes, socket = directory
    async with instances(socket, 1) as (runtime,):
        started, release = asyncio.Event(), asyncio.Event()

        async def old_send(payload):
            started.set()
            await release.wait()
            raise OSError("old socket write failed")

        await runtime.bind("tenant:user:browser", old_send, AsyncMock(), "session")
        call = asyncio.create_task(runtime.send("tenant:user:browser", "click"))
        await started.wait()
        new_send = AsyncMock()
        new = await runtime.bind("tenant:user:browser", new_send, AsyncMock(), "session")
        release.set()
        result = await asyncio.gather(call, return_exceptions=True)
        assert isinstance(result[0], Exception)
        assert await routes.get(new.transport_id) == new
        assert await runtime.send(new.transport_id, "snapshot")
        new_send.assert_awaited_once_with("snapshot")


async def test_directory_failure_does_not_dispatch_or_retry(directory, monkeypatch):
    import pytest
    from redis.exceptions import ConnectionError
    from vibecanvas_api.browser.instance_relay import RelayUnavailable

    _, socket = directory
    async with instances(socket, 1) as (runtime,):
        send = AsyncMock()
        await runtime.bind("tenant:user:browser", send, AsyncMock(), "session")
        lookup = AsyncMock(side_effect=ConnectionError("unavailable"))
        monkeypatch.setattr(runtime.directory, "get", lookup)
        with pytest.raises(RelayUnavailable, match="not dispatched"):
            await runtime.send("tenant:user:browser", "click")
        lookup.assert_awaited_once()
        send.assert_not_awaited()


async def test_replaced_connection_is_rejected_before_maintenance_tick(directory):
    _, socket = directory
    async with instances(socket) as (old_instance, new_instance):
        old = await old_instance.bind("tenant:user:browser", AsyncMock(), AsyncMock(), "session")
        assert await old_instance.is_current(old)
        new = await new_instance.bind("tenant:user:browser", AsyncMock(), AsyncMock(), "session")
        assert not await old_instance.is_current(old)
        assert await new_instance.is_current(new)
