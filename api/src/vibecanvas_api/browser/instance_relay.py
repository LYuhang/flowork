"""One non-durable browser relay subscription per API instance.

No retries or replay: a delivery acknowledgement only confirms the owning
instance wrote the frame, not that the browser action succeeded. Lost or late
acknowledgements leave the outcome unknown. Redis is trusted internal transport.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
import json
import logging
import uuid

from redis.exceptions import RedisError
from redis.asyncio import ConnectionPool, Redis

from .connection_directory import ConnectionDirectory, ConnectionOwner


log = logging.getLogger(__name__)


class RelayUnavailable(Exception):
    """No command was dispatched by this relay."""


class RelayDeliveryUnknown(Exception):
    """The write may have happened; the caller must not automatically resend."""


class InstanceRelay:
    def __init__(self, directory: ConnectionDirectory, instance_id: str,
                 deliver: Callable[[ConnectionOwner, object], Awaitable[bool]],
                 *, timeout: float = 5, max_pending: int = 512):
        self.directory = directory
        self.redis = directory.redis
        self.instance_id = instance_id
        self.deliver = deliver
        self.timeout = timeout
        self.max_pending = max_pending
        self.lost = asyncio.Event()
        self.pending: dict[str, asyncio.Future] = {}
        self.handlers: set[asyncio.Task] = set()
        self.lanes: dict[str, tuple[asyncio.Lock, int]] = {}
        # Pub/Sub can legitimately be silent indefinitely. Ordinary directory
        # queries and publishes retain their bounded command socket timeout.
        pool = self.redis.connection_pool
        self.subscriber = Redis.from_pool(ConnectionPool(
            connection_class=pool.connection_class,
            **{**pool.connection_kwargs, "socket_timeout": None}))
        self.subscription = None
        self.listener: asyncio.Task | None = None

    def _channel(self, instance_id: str) -> str:
        return f"{self.directory.prefix}:instance:{instance_id}"

    async def start(self) -> None:
        if self.subscription is not None:
            raise RuntimeError("browser relay already started")
        self.subscription = self.subscriber.pubsub()
        try:
            await self.subscription.subscribe(self._channel(self.instance_id))
            # Await the subscription acknowledgement before publishing routes.
            async with asyncio.timeout(self.timeout):
                while True:
                    message = await self.subscription.get_message(timeout=self.timeout)
                    if message and message["type"] == "subscribe":
                        break
            self.listener = asyncio.create_task(self._listen())
        except BaseException:
            await self.close()
            raise

    async def send(self, owner: ConnectionOwner, payload: object) -> bool:
        if self.lost.is_set() or self.listener is None:
            raise RelayUnavailable("browser relay is disconnected")
        if len(self.pending) >= self.max_pending:
            raise RelayUnavailable("browser relay is at capacity")
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            raw = json.dumps({"type": "deliver", "id": identifier,
                              "reply_to": self.instance_id, "owner": asdict(owner),
                              "payload": payload})
            async with asyncio.timeout(self.timeout):
                recipients = await self.redis.publish(self._channel(owner.instance_id), raw)
                if recipients == 0:
                    return False
                status = await future
            if status == "unknown":
                raise RelayDeliveryUnknown("browser transport write could not be confirmed")
            if status == "busy":
                raise RelayUnavailable("owning browser relay is at capacity")
            return status == "sent"
        except (TimeoutError, OSError, RedisError) as exc:
            raise RelayDeliveryUnknown("browser relay acknowledgement unavailable") from exc
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _listen(self) -> None:
        try:
            async for message in self.subscription.listen():
                if message["type"] != "message":
                    continue
                data = json.loads(message["data"])
                if data.get("type") == "ack":
                    future = self.pending.get(data["id"])
                    if future is not None and not future.done():
                        future.set_result(data["status"])
                elif data.get("type") == "deliver":
                    if len(self.handlers) >= self.max_pending:
                        await self._ack(data, "busy")
                        continue
                    key = data["owner"]["connection_id"]
                    lock, count = self.lanes.get(key, (asyncio.Lock(), 0))
                    self.lanes[key] = (lock, count + 1)
                    task = asyncio.create_task(self._deliver(data, lock))
                    self.handlers.add(task)
                    task.add_done_callback(lambda done, key=key: self._handler_done(done, key))
        except Exception as exc:
            # Keep diagnostics free of page data, Redis addresses and credentials.
            log.warning("browser_relay_listener_lost instance_id=%s error_type=%s",
                        self.instance_id, type(exc).__name__)
            # The registry lifecycle closes local sockets and removes ownership.
        finally:
            self.lost.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RelayDeliveryUnknown("browser relay connection lost"))

    async def _ack(self, message: dict, status: str) -> None:
        await self.redis.publish(self._channel(message["reply_to"]),
                                 json.dumps({"type": "ack", "id": message["id"], "status": status}))

    def _handler_done(self, task: asyncio.Task, key: str) -> None:
        self.handlers.discard(task)
        lock, count = self.lanes[key]
        if count == 1:
            del self.lanes[key]
        else:
            self.lanes[key] = (lock, count - 1)

    async def _deliver(self, message: dict, lock: asyncio.Lock) -> None:
        status = "unknown"
        try:
            async with lock:
                owner = ConnectionOwner(**message["owner"])
                current = await self.directory.get(owner.transport_id, owner.channel)
                if owner.instance_id != self.instance_id or current != owner:
                    status = "missing"
                else:
                    status = "sent" if await self.deliver(owner, message["payload"]) else "missing"
        except Exception:
            # Socket exceptions can contain private frame content. Return only
            # the uncertain outcome; never log or resend the payload.
            status = "unknown"
        try:
            await self._ack(message, status)
        except Exception:
            self.lost.set()

    async def close(self) -> None:
        self.lost.set()
        tasks = [*self.handlers]
        if self.listener is not None:
            tasks.append(self.listener)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.subscription is not None:
            await self.subscription.aclose()
        await self.subscriber.aclose()
