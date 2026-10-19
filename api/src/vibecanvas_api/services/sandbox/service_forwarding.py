"""Stream one HTTP/WebSocket request to the resident sandbox's loopback only."""

from __future__ import annotations

import asyncio
import base64

import httpx


async def forward_service(channel, request: dict) -> None:
    port = request["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid service port")
    scheme = request.get("scheme", "http")
    if scheme not in {"http", "https"}:
        raise ValueError("invalid service scheme")
    path = request.get("path", "/")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("invalid service path")
    host = request.get("loopback_host", "127.0.0.1")
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("invalid loopback host")
    authority = "[::1]" if host == "::1" else host
    url = f"{scheme}://{authority}:{port}{path}"

    async def emit(message):
        await channel.send(message)
        # One outstanding chunk per request bounds memory even for slow clients.
        async with asyncio.timeout(60):
            if (await channel.recv()).get("action") != "ack":
                raise ConnectionError("forwarding cancelled")

    if request.get("websocket"):
        from websockets.asyncio.client import connect

        async with connect(
            url.replace("http", "ws", 1),
            additional_headers=request.get("headers", {}),
            subprotocols=request.get("subprotocols") or None,
            proxy=None,
            open_timeout=15,
            max_size=4 * 1024 * 1024,
        ) as socket:
            await channel.send({"type": "ready", "subprotocol": socket.subprotocol})

            async def receive():
                async for value in socket:
                    await channel.send(
                        {
                            "type": "data",
                            "binary": isinstance(value, bytes),
                            "data": base64.b64encode(value if isinstance(value, bytes) else value.encode()).decode(),
                        }
                    )
                    # Browser ACKs via the same inbound pump as client messages.
                    await acknowledgement.wait()
                    acknowledgement.clear()

            acknowledgement = asyncio.Event()
            reader = asyncio.create_task(receive())
            incoming = asyncio.create_task(channel.recv())
            try:
                while True:
                    done, _ = await asyncio.wait({reader, incoming}, return_when=asyncio.FIRST_COMPLETED)
                    if reader in done:
                        await reader
                        return
                    message = incoming.result()
                    if message.get("action") == "ack":
                        acknowledgement.set()
                    elif message.get("action") == "data":
                        raw = base64.b64decode(message["data"], validate=True)
                        await socket.send(raw if message.get("binary") else raw.decode())
                    else:
                        return
                    incoming = asyncio.create_task(channel.recv())
            finally:
                reader.cancel()
                incoming.cancel()
                await asyncio.gather(reader, incoming, return_exceptions=True)
    else:
        timeout = httpx.Timeout(30, connect=10, read=None)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                request["method"],
                url,
                headers=request.get("headers", {}),
                content=base64.b64decode(request.get("body", "")),
            ) as response:
                await emit(
                    {"type": "headers", "status": response.status_code, "headers": list(response.headers.multi_items())}
                )
                async for chunk in response.aiter_raw():
                    if chunk:
                        await emit({"type": "data", "data": base64.b64encode(chunk).decode()})
