"""Real loopback WebSocket relay checks; no application/browser credentials."""

import asyncio

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from vibecanvas_api.browser.connection_errors import (
    EXTENSION_DISCONNECTED, INITIALIZATION_REASONS, upstream_close,
)
from vibecanvas_api.services.agent_runtime.mcp_browser_transport import start_browser_cdp_relay


@pytest.mark.asyncio
@pytest.mark.parametrize("code,reason", [
    (1011, next(iter(INITIALIZATION_REASONS.values()))),
    (1011, "Bearer secret; Cookie: private; https://private/?token=credential"),
    (4401, "private authorization detail"),
    (4409, "private lease identity"),
    (1000, ""),
    (1006, ""),
])
async def test_upstream_closure_preserves_safe_reason_and_never_reconnects(monkeypatch, code, reason):
    monkeypatch.delenv("VC_RUNTIME_EGRESS_PROXY", raising=False)
    calls = []

    async def upstream(ws):
        calls.append(ws.request.headers.get("Authorization"))
        assert await ws.recv() == "request"
        await ws.send("response")
        if code == 1006:
            ws.transport.abort()
        else:
            await ws.close(code=code, reason=reason)

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        relay = await start_browser_cdp_relay(local_bearer="local-fixture")
        try:
            await relay.activate(upstream_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                 upstream_bearer="upstream-fixture")
            async with websockets.connect(relay.endpoint, proxy=None,
                                          additional_headers={"Authorization": "Bearer local-fixture"}) as client:
                await client.send("request")
                assert await asyncio.wait_for(client.recv(), 2) == "response"
                with pytest.raises(ConnectionClosed) as closed:
                    await asyncio.wait_for(client.recv(), 2)
                expected = upstream_close(code, reason)
                assert (closed.value.rcvd.code, closed.value.rcvd.reason) == expected
                if reason.startswith("Bearer"):
                    assert closed.value.rcvd.reason == EXTENSION_DISCONNECTED
            assert calls == ["Bearer upstream-fixture"]
        finally:
            await relay.close()
        assert not relay.connections


@pytest.mark.asyncio
@pytest.mark.parametrize("bearer,active,expected", [("wrong", True, 4401), ("local-fixture", False, 4403)])
async def test_relay_denies_wrong_local_token_or_inactive_turn(monkeypatch, bearer, active, expected):
    monkeypatch.delenv("VC_RUNTIME_EGRESS_PROXY", raising=False)
    relay = await start_browser_cdp_relay(local_bearer="local-fixture")
    try:
        if active:
            await relay.activate(upstream_url="ws://127.0.0.1:1", upstream_bearer="never-sent")
        async with websockets.connect(relay.endpoint, proxy=None,
                                      additional_headers={"Authorization": f"Bearer {bearer}"}) as client:
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(client.recv(), 2)
            assert closed.value.rcvd.code == expected
    finally:
        await relay.close()


@pytest.mark.asyncio
async def test_turn_deactivation_closes_both_pumps(monkeypatch):
    monkeypatch.delenv("VC_RUNTIME_EGRESS_PROXY", raising=False)
    connected = asyncio.Event()
    ended = asyncio.Event()

    async def upstream(ws):
        connected.set()
        await ws.wait_closed()
        ended.set()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        relay = await start_browser_cdp_relay(local_bearer="local-fixture")
        try:
            await relay.activate(upstream_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                 upstream_bearer="upstream-fixture")
            async with websockets.connect(relay.endpoint, proxy=None,
                                          additional_headers={"Authorization": "Bearer local-fixture"}) as client:
                await asyncio.wait_for(connected.wait(), 2)
                await relay.deactivate()
                with pytest.raises(ConnectionClosed) as closed:
                    await asyncio.wait_for(client.recv(), 2)
                assert closed.value.rcvd.code == 1012
                await asyncio.wait_for(ended.wait(), 2)
            assert not relay.state and not relay.connections
        finally:
            await relay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reactivate", [False, True])
async def test_turn_ending_during_handshake_never_forwards_buffered_commands(monkeypatch, reactivate):
    monkeypatch.delenv("VC_RUNTIME_EGRESS_PROXY", raising=False)
    handshake_started = asyncio.Event()
    release_handshake = asyncio.Event()
    upstream_ended = asyncio.Event()
    received = []

    async def delay_handshake(connection, request):
        handshake_started.set()
        await release_handshake.wait()

    async def upstream(ws):
        try:
            async for message in ws:
                received.append(message)
        except ConnectionClosed:
            pass
        finally:
            upstream_ended.set()

    async with websockets.serve(upstream, "127.0.0.1", 0,
                                process_request=delay_handshake) as server:
        relay = await start_browser_cdp_relay(local_bearer="local-fixture")
        material = dict(upstream_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                        upstream_bearer="upstream-fixture")
        try:
            await relay.activate(**material)
            async with websockets.connect(relay.endpoint, proxy=None,
                                          additional_headers={"Authorization": "Bearer local-fixture"}) as client:
                await asyncio.wait_for(handshake_started.wait(), 2)
                await client.send("must-not-execute-after-turn-ended")
                await relay.deactivate()
                if reactivate:
                    # Identical credentials must not revive an earlier turn's socket.
                    await relay.activate(**material)
                release_handshake.set()
                await asyncio.wait_for(upstream_ended.wait(), 2)
                assert received == []
                with pytest.raises(ConnectionClosed) as closed:
                    await client.recv()
                assert closed.value.rcvd.code == 1012
        finally:
            release_handshake.set()
            await relay.close()
