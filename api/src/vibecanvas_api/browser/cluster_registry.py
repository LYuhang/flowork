"""Live socket ownership and browser relay lifecycle for each API event loop."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
import uuid

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from vibecanvas_api.config import config
from .connection_directory import ConnectionDirectory, ConnectionOwner
from .instance_relay import InstanceRelay, RelayUnavailable
from .connection_errors import TransportSendFailed


@dataclass
class Binding:
    owner: ConnectionOwner
    send: Callable[[object], Awaitable[None]]
    close: Callable[[], Awaitable[None]]


class BrowserConnections:
    def __init__(self, redis):
        self.redis = redis
        self.instance_id = uuid.uuid4().hex
        self.directory = ConnectionDirectory(redis)
        self.bindings: dict[tuple[str, str], Binding] = {}
        self.relay = InstanceRelay(self.directory, self.instance_id, self._deliver)
        self.registration_lock = asyncio.Lock()
        self.maintenance: asyncio.Task | None = None

    async def start(self):
        await self.relay.start()
        self.maintenance = asyncio.create_task(self._maintain())
        return self

    async def bind(self, transport_id, send, close, session_id, channel=""):
        if self.relay.lost.is_set():
            raise RelayUnavailable("browser relay is disconnected")
        key = (transport_id, channel)
        async with self.registration_lock:
            old = self.bindings.get(key)
            owner = ConnectionOwner(self.instance_id, uuid.uuid4().hex, transport_id, session_id, channel)
            binding = Binding(owner, send, close)
            self.bindings[key] = binding
            try:
                await self.directory.put(owner)
            except BaseException:
                if old is None:
                    self.bindings.pop(key, None)
                else:
                    self.bindings[key] = old
                raise
        if old is not None:
            with suppress(Exception):
                await old.close()
        return owner

    async def unbind(self, transport_id, channel="", sender=None):
        key = (transport_id, channel)
        async with self.registration_lock:
            binding = self.bindings.get(key)
            if binding is None or (sender is not None and sender is not binding.send):
                return False
            del self.bindings[key]
            return await self.directory.remove(binding.owner)

    async def _deliver(self, owner, payload):
        binding = self.bindings.get((owner.transport_id, owner.channel))
        if binding is None or binding.owner != owner:
            return False
        try:
            await binding.send(payload)
            return True
        except Exception:
            with suppress(Exception):
                await self.unbind(owner.transport_id, owner.channel, binding.send)
            with suppress(Exception):
                await binding.close()
            raise

    async def is_current(self, owner):
        if self.relay.lost.is_set():
            return False
        binding = self.bindings.get((owner.transport_id, owner.channel))
        if binding is None or binding.owner != owner:
            return False
        return await self.directory.get(owner.transport_id, owner.channel) == owner

    async def send(self, transport_id, payload, channel=""):
        if self.relay.lost.is_set():
            raise RelayUnavailable("browser relay is disconnected")
        try:
            owner = await self.directory.get(transport_id, channel)
        except (RedisError, OSError, TimeoutError) as exc:
            raise RelayUnavailable("browser connection directory is unavailable; command was not dispatched") from exc
        if owner is None:
            return False
        try:
            if owner.instance_id == self.instance_id:
                return await self._deliver(owner, payload)
            return await self.relay.send(owner, payload)
        except RelayUnavailable:
            raise
        except Exception as exc:
            raise TransportSendFailed("browser frame delivery is uncertain") from exc

    async def _maintain(self):
        try:
            while not self.relay.lost.is_set():
                try:
                    await asyncio.wait_for(self.relay.lost.wait(), self.directory.ttl / 3)
                    break
                except TimeoutError:
                    pass
                for key, binding in list(self.bindings.items()):
                    if not await self.directory.renew(binding.owner):
                        if self.bindings.get(key) is binding:
                            del self.bindings[key]
                            await binding.close()
        except Exception:
            self.relay.lost.set()
        finally:
            # Loss of the relay invalidates its sockets. The peers may establish
            # new connections, but no queued browser action is replayed.
            bindings = list(self.bindings.values())
            self.bindings.clear()
            for binding in bindings:
                with suppress(Exception):
                    await binding.close()
                with suppress(Exception):
                    await self.directory.remove(binding.owner)

    async def close(self):
        self.relay.lost.set()
        if self.maintenance is not None:
            await self.maintenance
        await self.relay.close()
        await self.redis.aclose()


_runtimes: dict[asyncio.AbstractEventLoop, asyncio.Task] = {}


async def connections() -> BrowserConnections:
    loop = asyncio.get_running_loop()
    task = _runtimes.get(loop)
    previous = None
    if task is not None and task.done():
        if task.cancelled() or task.exception() is not None or task.result().relay.lost.is_set():
            if not task.cancelled() and task.exception() is None:
                previous = task.result()
            task = None
    if task is None:
        async def start():
            if previous is not None:
                await previous.close()
            redis = Redis.from_url(config.redis.url, decode_responses=True,
                                   socket_connect_timeout=2, socket_timeout=5,
                                   retry=Retry(NoBackoff(), 0))
            runtime = BrowserConnections(redis)
            try:
                return await runtime.start()
            except BaseException:
                await redis.aclose()
                raise
        task = asyncio.create_task(start())
        _runtimes[loop] = task
    return await asyncio.shield(task)


async def close_connections():
    task = _runtimes.pop(asyncio.get_running_loop(), None)
    if task is not None:
        with suppress(Exception):
            await (await task).close()


class TransportRegistry:
    async def register(self, transport_id, send, *, session_id, close):
        return await (await connections()).bind(transport_id, send, close, session_id)

    async def unregister(self, transport_id, sender=None):
        task = _runtimes.get(asyncio.get_running_loop())
        if task is None or not task.done() or task.cancelled() or task.exception() is not None:
            return False
        return await task.result().unbind(transport_id, sender=sender)

    async def is_current(self, owner):
        return await (await connections()).is_current(owner)

    async def find_for_session(self, tenant_id, user_id, session_id):
        owner = await (await connections()).directory.find_for_session(tenant_id, user_id, session_id)
        return owner.transport_id if owner else None

    async def is_connected(self, transport_id):
        return await (await connections()).directory.get(transport_id) is not None

    async def send_to(self, transport_id, raw):
        return await (await connections()).send(transport_id, raw)


class ControllerRegistry:
    async def is_current(self, owner):
        return await (await connections()).is_current(owner)

    async def register(self, *, transport_id, channel, send, close):
        return await (await connections()).bind(transport_id, send, close, "", channel)

    async def unregister(self, *, transport_id, channel, sender=None):
        task = _runtimes.get(asyncio.get_running_loop())
        if task is None or not task.done() or task.cancelled() or task.exception() is not None:
            return False
        return await task.result().unbind(transport_id, channel, sender)

    async def forward_extension_message(self, *, transport_id, channel, message):
        return await (await connections()).send(transport_id, message, channel)


registry = TransportRegistry()
playwright_controllers = ControllerRegistry()
