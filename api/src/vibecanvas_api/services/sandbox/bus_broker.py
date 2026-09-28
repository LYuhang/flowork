# -*- coding: utf-8 -*-
"""API-side host broker for the host↔sandbox UDS message bus.

The HOST (listener/acceptor) role of the bus. The in-sandbox engine connector
lives in ``vibecanvas_engine.sandbox_bus`` (PURE ENGINE); this broker IMPORTS the
framing from there (api already depends on engine) so there is exactly ONE wire
implementation, preventing protocol drift.

Lifecycle:

  * :func:`socket_path_for` allocates a SHORT per-run host socket path
    ``/tmp/vcbus/{digest16}/bus.sock`` and ASSERTS it is ≤107 bytes (the AF_UNIX hard
    limit — the naïve run-tier path is already ~108 and FAILS). PER-RUN dir, not a
    shared ``/tmp/vcbus``: a shared dir under ``--host-uds=open`` would let one
    sandbox ``connect()`` to a peer run's bus (cross-run leak — FIX-4).
  * :class:`BusBroker` listens with ``asyncio.start_unix_server`` (async accept —
    it must NOT block the event loop) and exposes ``messages()``, an async
    iterator over the framed messages from the FIRST accepted connection (one
    sandbox per run). ``close()`` stops the server, unlinks the socket, and
    rmtrees the per-run dir.

The in-sandbox destination of the bind is a FIXED short path
(``IN_SANDBOX_BUS_DIR`` / ``IN_SANDBOX_BUS_SOCK``); the provider binds the host
per-run dir there rw and sets ``VC_BUS_SOCK`` to the in-sandbox socket path.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from contextlib import suppress
from typing import AsyncIterator

import structlog

from vibecanvas_engine.sandbox_bus import encode_frame, read_frame

logger = structlog.get_logger(__name__)


class RuntimeTurnBus:
    """One Turn's view of a resident Project bus, including late-frame fencing."""

    def __init__(self, router: "RuntimeBusRouter", turn_id: str):
        self.router = router
        self.turn_id = turn_id
        self.queue: asyncio.Queue = asyncio.Queue()

    async def send(self, message: dict) -> None:
        await self.router.broker.send({**message, "turn_id": self.turn_id})

    async def messages(self) -> AsyncIterator[dict]:
        while True:
            message = await self.queue.get()
            if isinstance(message, Exception):
                raise message
            yield message

    def detach(self) -> None:
        self.router.turns.pop(self.turn_id, None)


class RuntimeBusRouter:
    """Exactly one socket reader; never let sibling Chats consume each other's frames."""

    def __init__(self, broker: "BusBroker"):
        self.broker = broker
        self.turns: dict[str, RuntimeTurnBus] = {}
        self.failure: Exception | None = None
        self.task = asyncio.create_task(self._read())

    def attach(self, turn_id: str) -> RuntimeTurnBus:
        if self.failure is not None:
            raise self.failure
        if turn_id in self.turns:
            raise RuntimeError(f"agent runtime turn {turn_id} is already active")
        turn = RuntimeTurnBus(self, turn_id)
        self.turns[turn_id] = turn
        return turn

    async def _read(self) -> None:
        failure = ConnectionError("agent runtime bus disconnected")
        try:
            async for message in self.broker.messages():
                turn = self.turns.get(str(message.get("turn_id") or ""))
                if turn is not None:
                    turn.queue.put_nowait(message)
                # Detached Turns may still emit cleanup frames. Drop them;
                # a result without the exact Turn identity must never finish B.
        except Exception as exc:
            failure = exc
        finally:
            self.failure = failure
            for turn in self.turns.values():
                turn.queue.put_nowait(failure)

    async def close(self) -> None:
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

# The host-side per-run socket ROOT. Short by design so the full socket path stays
# ≤107 bytes regardless of the configured fs_root / storage root (FIX-4).
BUS_ROOT = "/tmp/vcbus"

# AF_UNIX pathname socket hard limit (sun_path, including the NUL terminator on
# Linux this is effectively 108 bytes of usable path → assert ≤107 to be safe).
MAX_SOCKET_PATH = 107

# The FIXED in-sandbox bind destination. The provider binds the host per-run dir
# here rw; the engine connects to IN_SANDBOX_BUS_SOCK (via VC_BUS_SOCK). Short so
# the in-sandbox path is also well under the limit. A DEDICATED top-level mount
# point — NOT under ``/run`` (binding over ``/run/__exec__`` would SHADOW the
# file channel's workflow.json / result.json / events.ndjson).
IN_SANDBOX_BUS_DIR = "/vcbus"
IN_SANDBOX_BUS_SOCK = IN_SANDBOX_BUS_DIR + "/bus.sock"


def socket_path_for(run_id: str) -> str:
    """Return the SHORT per-run host bus socket path for ``run_id``.

    ``{BUS_ROOT}/{digest16}/bus.sock`` where ``digest16`` is a stable digest of
    the complete run id. A plain prefix is unsafe for namespaced ids such as
    ``background-job_...`` because concurrent jobs share the same first
    characters and would unlink/rebind their peer's socket. ASSERTS the result
    is ≤ :data:`MAX_SOCKET_PATH` bytes (the AF_UNIX limit) so a
    misconfiguration that would silently fail the ``bind`` is caught loudly.
    """
    digest16 = hashlib.sha256((run_id or "0").encode("utf-8")).hexdigest()[:16]
    path = os.path.join(BUS_ROOT, digest16, "bus.sock")
    encoded = path.encode("utf-8")
    assert len(encoded) <= MAX_SOCKET_PATH, (
        f"bus socket path {path!r} is {len(encoded)} bytes > "
        f"{MAX_SOCKET_PATH} (AF_UNIX limit)"
    )
    return path


class BusBroker:
    """Host listener for ONE run's bus. Accepts a single sandbox connection and
    yields its framed messages.

    Created with the host socket path (from :func:`socket_path_for`). ``start()``
    binds + listens (async); ``messages()`` drains the first connection's frames;
    ``close()`` tears everything down (server close + unlink + rmtree the per-run
    dir).
    """

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._server: "asyncio.AbstractServer | None" = None
        # The first accepted connection's reader, delivered via this future so
        # ``messages()`` can await it without racing ``start()``. Created in
        # ``start()`` (bound to the running loop) — NOT in __init__, which may run
        # off-loop.
        self._conn: "asyncio.Future[asyncio.StreamReader] | None" = None
        self._writer: "asyncio.StreamWriter | None" = None

    async def start(self) -> None:
        """Bind + listen on the per-run UDS (async accept). Unlinks any stale
        socket and creates the per-run dir first (pathname socket, not abstract).
        """
        self._conn = asyncio.get_running_loop().create_future()
        run_dir = os.path.dirname(self.socket_path)
        os.makedirs(run_dir, exist_ok=True)
        # Pathname socket: a stale file at the path blocks bind. Unlink it.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._on_connect, path=self.socket_path
        )

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # One sandbox per run — keep only the FIRST connection. A second
        # (shouldn't happen for a one-shot run) is closed immediately.
        if self._conn is None or self._conn.done():
            writer.close()
            return
        self._writer = writer
        self._conn.set_result(reader)

    async def messages(self) -> AsyncIterator[dict]:
        """Yield framed messages from the (first) accepted connection.

        Awaits the sandbox connection, then reads frames until clean EOF
        (``read_frame`` returns ``None``). The caller owns the recv deadline
        (FIX-5b: the route wraps this in ``asyncio.wait_for(timeout)``)."""
        if self._conn is None:
            raise RuntimeError("BusBroker.messages() called before start()")
        reader = await self._conn
        while True:
            msg = await read_frame(reader)
            if msg is None:
                return
            yield msg

    async def wait_connected(self) -> None:
        """Wait until the first sandbox connector is accepted."""
        if self._conn is None:
            raise RuntimeError("BusBroker.wait_connected() called before start()")
        await self._conn

    def is_connected(self) -> bool:
        """Return whether the accepted full-duplex connection is still usable.

        A resident runsc process can briefly remain observable after its guest
        channel has reached EOF.  Process liveness alone therefore is not a
        sufficient reuse signal for an interactive Runtime.  StreamReader gets
        EOF notifications from the transport even when no caller is currently
        draining ``messages()``, so this check is both non-blocking and able to
        reject that half-dead state before the next Turn writes to it.
        """
        if self._conn is None or not self._conn.done() or self._conn.cancelled():
            return False
        if self._writer is None or self._writer.is_closing():
            return False
        try:
            reader = self._conn.result()
        except (asyncio.CancelledError, Exception):
            return False
        return not reader.at_eof()

    async def send(self, message: dict) -> None:
        """Send one framed host→sandbox message on the accepted connection."""
        await self.wait_connected()
        if not self.is_connected() or self._writer is None:
            raise ConnectionError("sandbox bus connection is not writable")
        self._writer.write(encode_frame(message))
        await self._writer.drain()

    async def close(self) -> None:
        """Stop the server, unlink the socket, rmtree the per-run dir. Best-effort
        + idempotent (safe on an already-closed broker)."""
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("bus_socket_unlink_failed", path=self.socket_path,
                           exc_info=True)
        shutil.rmtree(os.path.dirname(self.socket_path), ignore_errors=True)
