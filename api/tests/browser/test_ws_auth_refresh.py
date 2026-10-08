"""A panel reload renews authorization without replacing a live transport."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from vibecanvas_api.browser.envelope import decode, encode
from vibecanvas_api.browser.registry import TransportRegistry
from vibecanvas_api.browser.scoped_token import mint_scoped_token
from vibecanvas_api.browser.ws_auth import build_browser_ws_protocols
from vibecanvas_api.routes import browser as routes


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "user_id", "tenant_id", "wf_id", "browser_id", "extension_id",
                                   "session_id", "session_generation", "session_audience", "signature", "revoked"])
async def test_refresh_keeps_transport_only_for_verified_same_live_identity(monkeypatch, change):
    values = dict(user_id="user", tenant_id="tenant", wf_id="workflow", browser_id="browser",
                  extension_id="extension", session_id="session", session_generation=1,
                  session_audience="extension", now=1000)
    token = mint_scoped_token(**values, secret="test-secret")
    updated = {**values, "now": 1001}
    if change in values:
        updated[change] = 2 if change == "session_generation" else "different"
    fresh = mint_scoped_token(**updated, secret="different" if change == "signature" else "test-secret")
    monkeypatch.setattr(routes, "config", SimpleNamespace(browser_token_secret="test-secret", browser_extension_id="extension"))
    monkeypatch.setattr(routes.time, "time", lambda: 1002)
    live = AsyncMock(side_effect=[True, False] if change == "revoked" else None, return_value=True)
    monkeypatch.setattr(routes, "_browser_session_is_live", live)
    registry = TransportRegistry()
    monkeypatch.setattr(routes, "registry", registry)
    sender = None

    async def receive():
        nonlocal sender
        if sender is None:
            sender = registry._senders["tenant:user:browser"]
            return encode("auth_refresh", id="refresh", channel="system", transport="pending", data={"token": fresh})
        assert registry._senders["tenant:user:browser"] is sender
        raise WebSocketDisconnect()

    @asynccontextmanager
    async def scope(**kwargs):
        yield object()

    monkeypatch.setattr(routes, "session_scope", scope)
    repo = AsyncMock()
    # A different browser of this account may own the active control lease.
    repo.get_active_browser_binding_for_user.return_value = {
        "chat_id": "another-browser-chat", "status": "attached",
        "browser_session_id": "another-browser-lease", "browser_session_generation": 7,
    }
    monkeypatch.setattr(routes, "ChatRepo", lambda *args: repo)
    ws = SimpleNamespace(headers={"sec-websocket-protocol": ", ".join(build_browser_ws_protocols(token, "browser")),
                                  "origin": "chrome-extension://extension"},
                         accept=AsyncMock(), close=AsyncMock(), send_text=AsyncMock(), receive_text=receive)
    await routes.ws_hub(ws)
    ws.accept.assert_awaited_once()
    result = decode(ws.send_text.call_args.args[0])
    assert result["data"] == {"type": "auth_refresh", "ok": change is None,
                              "expires_at": 1901 if change is None else 1900}
    assert token not in ws.send_text.call_args.args[0] and fresh not in ws.send_text.call_args.args[0]
    assert not registry.is_connected("tenant:user:browser")
    repo.get_active_browser_binding_for_user.assert_not_awaited()
    repo.mark_browser_lost.assert_not_awaited()
