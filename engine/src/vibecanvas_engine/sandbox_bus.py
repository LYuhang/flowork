# -*- coding: utf-8 -*-
"""Host↔sandbox UDS message-bus wire protocol.

This module is the SINGLE source of the bus wire format. It lives in the engine
(NO ``vibecanvas_api`` import — same purity contract as ``sandbox_entry.py``) so
the in-sandbox connector AND the api-side host broker share one framing
implementation: the api broker IMPORTS the framing helpers from here (api already
depends on engine), so there is no drift.

Layered design:

  * Message contract (transport-neutral, never changes): typed framed messages.
    ``node_event`` (sandbox→host: a raw astream event), ``result`` (sandbox→host:
    terminal), ``cancel`` (host→sandbox), and RESERVED-for-future ``inject``
    (host→sandbox) + ``output_stream`` (sandbox→host). Only node_event / result /
    cancel are WIRED in v1; ``inject`` / ``output_stream`` are protocol-defined
    but not produced.
  * ``Channel`` interface (the seam): ``async send(msg)``, ``async recv() -> msg
    | None`` (None = closed), ``async close()``. The in-sandbox role is the
    CONNECTOR (this module's :class:`UdsClientChannel`); the host role is the
    LISTENER/acceptor (api-side ``bus_broker.py``, built on the same framing).

Framing: one frame is a 4-byte big-endian length prefix plus a JSON
payload. JSON is zero-dep + language-agnostic; non-serializable values degrade
via ``json.dumps(..., default=str)`` (matching ``sandbox_entry``). Unknown
message ``type`` → the consumer ignores it (forward-compat).
"""

from __future__ import annotations

import asyncio
import errno
import json
import struct

# Message types (the protocol vocabulary). v1 WIRES node_event / result / cancel;
# inject / output_stream are RESERVED for the bidirectional-future (browser-frame
# injection) — defined here so a later producer/consumer is a new message type,
# not a transport change.
MSG_NODE_EVENT = "node_event"
MSG_RESULT = "result"
MSG_CANCEL = "cancel"
MSG_INJECT = "inject"  # RESERVED (host→sandbox external data) — not produced in v1
MSG_OUTPUT_STREAM = "output_stream"  # RESERVED (sandbox→host streamed output)
# Agent Runtime messages use the same framing/channel, but carry only the
# platform-owned stable protocol. Runtime SDK objects are translated by
# the in-sandbox adapter before they cross this boundary.
MSG_RUNTIME_REQUEST = "runtime_request"       # host→sandbox: one turn request
MSG_RUNTIME_EVENT = "runtime_event"           # sandbox→host: streamed event
MSG_RUNTIME_CONTROL = "runtime_control"       # host→sandbox: approve/cancel/etc.
MSG_RUNTIME_RESULT = "runtime_result"         # sandbox→host: clean terminal
MSG_RUNTIME_ERROR = "runtime_error"           # sandbox→host: adapter failure
MSG_BACKGROUND_JOB_REQUEST = "background_job_request"
MSG_BACKGROUND_JOB_EVENT = "background_job_event"
MSG_BACKGROUND_JOB_RESULT = "background_job_result"
# Runtime State Broker RPC.  The sandbox can request checkpoint operations but
# never receives a database address or database credential.  Payload values are
# serializer-owned opaque bytes (base64 on the JSON wire); only the Runtime that
# produced them deserializes them.
MSG_RUNTIME_STATE_REQUEST = "runtime_state_request"
MSG_RUNTIME_STATE_RESPONSE = "runtime_state_response"

_LEN = struct.Struct(">I")  # 4-byte big-endian unsigned length prefix


def encode_frame(msg: dict) -> bytes:
    """Serialize ONE message dict to a framed wire bytes object.

    ``json.dumps(default=str)`` so a non-JSON-native value (numpy / Decimal /
    bytes / PIL inside a node event) degrades to its ``str()`` rather than raising
    mid-stream (mirrors ``exec_events.to_exec_update`` + ``sandbox_entry``)."""
    body = json.dumps(msg, ensure_ascii=False, default=str).encode("utf-8")
    return _LEN.pack(len(body)) + body


async def read_frame(
    reader: asyncio.StreamReader, *, max_len: "int | None" = None
) -> "dict | None":
    """Read ONE framed message from an :class:`asyncio.StreamReader`.

    Returns the decoded dict, or ``None`` at clean EOF (the peer closed). Raises
    on a truncated frame (a length prefix with fewer than ``n`` body bytes is a
    protocol/transport error, surfaced rather than silently swallowed).

    ``max_len`` (optional) caps the declared body length: when set and the
    4-byte length prefix exceeds it, a :class:`ValueError` is raised BEFORE the
    ``readexactly(length)`` allocation — so a compromised peer can't make the
    reader allocate an arbitrarily huge buffer (memory DoS). ``None`` (the
    DEFAULT) leaves the frame uncapped so the workflow bus — which carries large
    legit node-event frames — is unaffected. Callers that frame tiny, known-small
    payloads (e.g. the egress broker's ``{host, port}`` header) pass a small cap.
    """
    try:
        header = await reader.readexactly(_LEN.size)
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            return None  # clean EOF at a frame boundary.
        raise
    (length,) = _LEN.unpack(header)
    if max_len is not None and length > max_len:
        raise ValueError(
            f"frame length {length} exceeds max_len {max_len}"
        )
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


class UdsClientChannel:
    """The in-sandbox CONNECTOR role of the bus (a :class:`Channel`).

    Full-duplex over one pathname UDS connection: an independent reader stream +
    writer stream off the same socket. ``send`` writes a framed message + drains;
    ``recv`` reads the next framed message (``None`` = peer closed); ``close``
    shuts the writer.

    Constructed via :func:`connect_bus` (which owns the retry-connect loop for
    host/sandbox startup ordering); the instance itself assumes an already-open
    connection.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def send(self, msg: dict) -> None:
        self._writer.write(encode_frame(msg))
        await self._writer.drain()

    async def recv(self) -> "dict | None":
        return await read_frame(self._reader)

    async def close(self) -> None:
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            # Best-effort close — a half-dead peer must not raise out of teardown.
            pass


async def connect_bus(
    socket_path: str, *, retries: int = 100, delay: float = 0.05
) -> UdsClientChannel:
    """Connect to the host bus listener at ``socket_path``, returning a
    :class:`UdsClientChannel`.

    The retry loop handles the sandbox starting
    BEFORE the host has ``bind``/``listen``ed, so a transient ECONNREFUSED /
    ENOENT / FileNotFoundError is retried up to ``retries`` times with ``delay``
    seconds between attempts. Any other OSError (or exhausting the retries)
    propagates — a permanently-missing listener is a real failure, not a hang.
    """
    last_exc: "OSError | None" = None
    for _ in range(max(1, retries)):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            return UdsClientChannel(reader, writer)
        except (ConnectionRefusedError, FileNotFoundError) as e:
            last_exc = e
            await asyncio.sleep(delay)
        except OSError as e:
            # ENOENT can surface as a bare OSError on some libc paths; treat it
            # like the startup-ordering races above and retry. Anything else is
            # a hard failure.
            if e.errno in (errno.ENOENT, errno.ECONNREFUSED):
                last_exc = e
                await asyncio.sleep(delay)
            else:
                raise
    # Retries exhausted — surface the last transient so the caller sees WHY.
    raise last_exc if last_exc is not None else ConnectionRefusedError(socket_path)
