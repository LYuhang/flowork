"""Sandbox-local authenticated WebSocket relay for the Browser CLI worker."""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from typing import Any

import websockets

from vibecanvas_api.browser.connection_errors import upstream_close


@dataclass(slots=True)
class BrowserCdpRelay:
    server: Any
    endpoint: str
    local_bearer: str
    state: dict[str, str]
    connections: set[Any]

    async def activate(self, *, upstream_url: str, upstream_bearer: str) -> None:
        if not upstream_url.startswith(("ws://", "wss://")):
            raise ValueError(
                "Playwright CDP upstream must be an absolute ws(s) URL"
            )
        if not upstream_bearer or any(
            char in upstream_bearer for char in "\r\n"
        ):
            raise ValueError("Playwright CDP bearer is missing or invalid")
        await self.deactivate()
        self.state["upstream_url"] = upstream_url
        self.state["upstream_bearer"] = upstream_bearer
        self.state["activation_id"] = secrets.token_hex(16)

    async def deactivate(self) -> None:
        self.state.clear()
        connections = list(self.connections)
        self.connections.clear()
        for connection in connections:
            await connection.close(code=1012, reason="Agent Turn ended")

    async def close(self) -> None:
        await self.deactivate()
        self.server.close()
        await self.server.wait_closed()


async def start_browser_cdp_relay(
    *,
    local_bearer: str,
) -> BrowserCdpRelay:
    """Expose a loopback CDP socket while upstream traffic uses Host egress."""
    if not local_bearer or any(char in local_bearer for char in "\r\n"):
        raise ValueError("Playwright local relay bearer is missing or invalid")
    proxy = os.environ.get("VC_RUNTIME_EGRESS_PROXY") or None
    state: dict[str, str] = {}
    connections: set[Any] = set()

    async def relay_connection(local: Any) -> None:
        authorization = str(local.request.headers.get("Authorization") or "")
        if authorization != f"Bearer {local_bearer}":
            await local.close(code=4401, reason="Unauthorized")
            return
        upstream_url = state.get("upstream_url", "")
        upstream_bearer = state.get("upstream_bearer", "")
        activation_id = state.get("activation_id")
        if not upstream_url or not upstream_bearer:
            await local.close(code=4403, reason="Agent Turn inactive")
            return
        connections.add(local)
        try:
            async with websockets.connect(
                upstream_url,
                additional_headers={
                    "Authorization": f"Bearer {upstream_bearer}",
                },
                proxy=proxy,
                open_timeout=15,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_size=None,
                compression=None,
            ) as upstream:
                # A completed handshake must not revive a closed turn, even
                # if a later activation uses the same URL and credentials.
                if state.get("activation_id") != activation_id or local.state.name != "OPEN":
                    await local.close(code=1012, reason="Agent Turn ended")
                    return

                async def pump(source: Any, destination: Any) -> None:
                    async for message in source:
                        if state.get("activation_id") != activation_id:
                            return
                        await destination.send(message)

                local_to_upstream = asyncio.create_task(pump(local, upstream))
                upstream_to_local = asyncio.create_task(pump(upstream, local))
                pumps = {local_to_upstream, upstream_to_local}
                try:
                    done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                    if upstream_to_local in done and local.state.name not in {"CLOSING", "CLOSED"}:
                        # Exiting this handler implicitly closes with 1000,
                        # hiding initialization refusals and permission loss.
                        code, reason = upstream_close(upstream.close_code, upstream.close_reason)
                        await local.close(code=code, reason=reason)
                finally:
                    for task in pumps:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if local.state.name not in {"CLOSING", "CLOSED"}:
                await local.close(code=1011, reason="Upstream unavailable")
        finally:
            connections.discard(local)

    server = await websockets.serve(
        relay_connection,
        "127.0.0.1",
        0,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=None,
        compression=None,
    )
    sockets = list(server.sockets or [])
    if not sockets:
        server.close()
        await server.wait_closed()
        raise RuntimeError("Playwright CDP relay exposed no socket")
    port = int(sockets[0].getsockname()[1])
    return BrowserCdpRelay(
        server=server,
        endpoint=f"ws://127.0.0.1:{port}/",
        local_bearer=local_bearer,
        state=state,
        connections=connections,
    )


__all__ = ["BrowserCdpRelay", "start_browser_cdp_relay"]
