"""Diagnostic boundary tests; not a substitute for real extension acceptance."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from vibecanvas_api.browser import connection_errors as errors
from vibecanvas_api.browser.envelope import decode
from vibecanvas_api.browser.cluster_registry import ControllerRegistry
from vibecanvas_api.routes import browser as routes


def test_public_reasons_are_fixed_short_ascii_and_do_not_echo_unknown_input():
    for reason in errors.SAFE_REASONS:
        assert len(reason.encode("utf-8")) <= 123
        assert reason.isascii()
    secret = "Bearer credential; Cookie: secret; https://private/?token=secret"
    assert errors.initialization_reason(secret) == errors.INITIALIZATION_FAILED
    for code in (None, 1005, 1006, 1015, 1011, 4401, 4403, 4409):
        public_code, reason = errors.upstream_close(code, secret)
        assert public_code not in (None, 1005, 1006, 1015)
        assert "secret" not in reason and "credential" not in reason
        assert len(reason.encode()) <= 123


@pytest.fixture
def endpoint(monkeypatch, browser_connections):
    capability = SimpleNamespace(organization_id="tenant", user_id="user",
                                 session_id="auth-session", chat_id="chat", expires_at=10**12)
    monkeypatch.setattr(routes, "verify_agent_capability", lambda *a, **kw: capability)
    monkeypatch.setattr(routes, "_platform_session_is_live", AsyncMock(return_value=True))
    monkeypatch.setattr(routes.registry, "find_for_session", AsyncMock(return_value="tenant:user:browser"))
    repo = AsyncMock()
    repo.get_browser_binding.return_value = {
        "status": "attaching", "browser_session_id": "brs_test", "browser_session_generation": 8,
    }
    monkeypatch.setattr(routes, "ChatRepo", lambda *a: repo)

    @asynccontextmanager
    async def scope(**kwargs):
        yield object()

    monkeypatch.setattr(routes, "session_scope", scope)
    controllers = ControllerRegistry()
    monkeypatch.setattr(routes, "playwright_controllers", controllers)
    confirm = AsyncMock()
    monkeypatch.setattr(routes, "confirm_sidepanel_browser_session", confirm)
    ws = SimpleNamespace(headers={"authorization": "Bearer fixture"},
                         client_state=WebSocketState.CONNECTED, application_state=WebSocketState.CONNECTED,
                         accept=AsyncMock(), close=AsyncMock(), send_json=AsyncMock(),
                         receive_json=AsyncMock(side_effect=WebSocketDisconnect()))
    actions = []
    outcome = {"message": {"result": {"initialized": True}}, "available": True}
    monkeypatch.setattr(routes.registry, "is_connected", AsyncMock(side_effect=lambda transport: outcome["available"]))

    async def send(transport, raw):
        frame = decode(raw)
        actions.append(frame["data"]["action"])
        assert frame["data"]["browser_session_id"] == "brs_test"
        assert frame["data"]["session_generation"] == 8
        if frame["data"]["action"] == "initialize" and outcome["message"] is not None:
            await controllers.forward_extension_message(
                transport_id=transport, channel="chat:chat", message=outcome["message"],
            )
        if frame["data"]["action"] == "request":
            await controllers.forward_extension_message(
                transport_id=transport, channel="chat:chat",
                message={"id": frame["data"]["request"]["id"], "result": {}},
            )
        return outcome["available"]

    monkeypatch.setattr(routes.registry, "send_to", send)
    return ws, controllers, confirm, actions, outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [*errors.INITIALIZATION_REASONS, "unknown credential-bearing exception"])
async def test_extension_refusal_is_reported_without_accepting_any_cdp_request(endpoint, message):
    ws, controllers, confirm, actions, outcome = endpoint
    outcome["message"] = {"error": {"code": -32603, "message": message}}
    await routes.playwright_cdp(ws)
    ws.close.assert_awaited_once_with(code=1011, reason=errors.initialization_reason(message))
    confirm.assert_not_awaited()
    ws.receive_json.assert_not_awaited()
    assert actions == ["initialize", "close"]
    assert not await controllers.forward_extension_message(transport_id="tenant:user:browser", channel="chat:chat", message={"id": 1})


@pytest.mark.asyncio
async def test_initialization_timeout_is_explicit_and_does_not_dispatch(endpoint, monkeypatch):
    ws, _, confirm, actions, outcome = endpoint
    outcome["message"] = None
    monkeypatch.setattr(routes, "PLAYWRIGHT_INITIALIZATION_TIMEOUT_SECONDS", 0.001)
    await asyncio.wait_for(routes.playwright_cdp(ws), timeout=1)
    ws.close.assert_awaited_once_with(code=1011, reason=errors.INITIALIZATION_TIMEOUT)
    confirm.assert_not_awaited()
    ws.receive_json.assert_not_awaited()
    assert actions == ["initialize", "close"]


@pytest.mark.asyncio
async def test_lease_confirmation_failure_is_not_a_successful_connection(endpoint):
    ws, _, confirm, actions, _ = endpoint
    confirm.side_effect = routes.BrowserSessionControlError("generation_mismatch", "private diagnostic")
    await routes.playwright_cdp(ws)
    ws.close.assert_awaited_once_with(code=4409, reason=errors.SESSION_CHANGED)
    ws.receive_json.assert_not_awaited()
    assert actions == ["initialize", "close"]


@pytest.mark.asyncio
async def test_missing_extension_closes_socket_and_cleans_pending_initialization(endpoint):
    ws, _, confirm, actions, outcome = endpoint
    outcome.update(message=None, available=False)
    await routes.playwright_cdp(ws)
    ws.close.assert_awaited_once_with(code=1011, reason=errors.EXTENSION_DISCONNECTED)
    confirm.assert_not_awaited()
    ws.receive_json.assert_not_awaited()
    assert actions == ["initialize", "close"]


@pytest.mark.asyncio
async def test_successful_initialization_still_forwards_cdp_and_cleans_disconnect(endpoint):
    ws, _, confirm, actions, _ = endpoint
    requests = iter([{"id": 1, "method": "Browser.getVersion"}])

    async def receive():
        request = next(requests, None)
        if request is None:
            ws.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect()
        assert confirm.await_count == 1
        return request

    ws.receive_json.side_effect = receive
    await routes.playwright_cdp(ws)
    ws.send_json.assert_awaited_once_with({"id": 1, "result": {}})
    ws.close.assert_not_awaited()
    assert actions == ["initialize", "request", "close"]
    routes.ChatRepo().mark_browser_lost.assert_not_awaited()


@pytest.mark.asyncio
async def test_extension_loss_updates_only_controllers_own_lease(endpoint):
    ws, _, _, _, outcome = endpoint

    async def disconnect():
        outcome["available"] = False
        raise WebSocketDisconnect()

    ws.receive_json.side_effect = disconnect
    await routes.playwright_cdp(ws)
    repo = routes.ChatRepo()
    repo.get_active_browser_binding_for_user.assert_not_awaited()
    repo.mark_browser_lost.assert_awaited_once_with(
        chat_id="chat", browser_session_id="brs_test", browser_session_generation=8,
        reason="websocket_disconnected",
    )


@pytest.mark.asyncio
async def test_replaced_controller_cannot_mark_lease_lost(endpoint):
    ws, controllers, _, actions, outcome = endpoint

    async def disconnect():
        await controllers.register(transport_id="tenant:user:browser", channel="chat:chat", send=AsyncMock(), close=AsyncMock())
        outcome["available"] = False
        raise WebSocketDisconnect()

    ws.receive_json.side_effect = disconnect
    await routes.playwright_cdp(ws)
    routes.ChatRepo().mark_browser_lost.assert_not_awaited()
    assert actions == ["initialize"]


@pytest.mark.asyncio
async def test_expired_turn_during_handshake_never_dispatches_buffered_commands(endpoint, monkeypatch):
    ws, _, confirm, actions, _ = endpoint
    live = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(routes, "_platform_session_is_live", live)
    await routes.playwright_cdp(ws)
    assert confirm.await_count == 1
    ws.close.assert_awaited_once_with(code=4401)
    ws.receive_json.assert_not_awaited()
    assert actions == ["initialize", "close"]


@pytest.mark.asyncio
async def test_idle_socket_revalidates_turn_and_cancels_pending_receive(endpoint, monkeypatch):
    ws, _, _, actions, _ = endpoint
    live = AsyncMock(side_effect=[True, True, False])
    monkeypatch.setattr(routes, "_platform_session_is_live", live)
    monkeypatch.setattr(routes, "PLAYWRIGHT_AUTHORIZATION_INTERVAL_SECONDS", 0.01, raising=False)
    cancelled = asyncio.Event()

    async def receive():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    ws.receive_json.side_effect = receive
    await asyncio.wait_for(routes.playwright_cdp(ws), timeout=1)
    assert cancelled.is_set()
    ws.close.assert_awaited_once_with(code=4401)
    assert actions == ["initialize", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant:user:browser", "generation", "session", "inactive"])
async def test_browser_fence_change_during_handshake_is_rejected(endpoint, monkeypatch, change):
    ws, _, confirm, actions, _ = endpoint

    async def changed(_lease):
        if change == "tenant:user:browser":
            monkeypatch.setattr(routes.registry, "find_for_session", AsyncMock(return_value="tenant:user:new-browser"))
        else:
            binding = routes.ChatRepo().get_browser_binding.return_value
            binding[{"generation": "browser_session_generation", "session": "browser_session_id", "inactive": "status"}[change]] = {
                "generation": 9, "session": "other-session", "inactive": "inactive",
            }[change]

    confirm.side_effect = changed
    await routes.playwright_cdp(ws)
    ws.close.assert_awaited_once_with(code=4409, reason=errors.SESSION_CHANGED)
    ws.receive_json.assert_not_awaited()
    assert actions == ["initialize", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, PermissionError("old turn"), RuntimeError("private-db-detail")])
async def test_cdp_identity_check_uses_full_live_platform_context(monkeypatch, failure):
    from vibecanvas_api.services.agent_resources import context as agent_context

    capability = object()
    context = AsyncMock(side_effect=failure)
    monkeypatch.setattr(agent_context, "resolve_context", context)
    assert await routes._platform_session_is_live(capability) is (failure is None)
    context.assert_awaited_once_with(capability)


@pytest.mark.asyncio
async def test_capability_expiring_as_receive_completes_does_not_forward(endpoint):
    ws, _, _, actions, _ = endpoint
    capability = routes.verify_agent_capability()

    async def receive():
        capability.expires_at = 0
        return {"id": 1, "method": "Runtime.evaluate", "params": {"expression": "mustNotExecute()"}}

    ws.receive_json.side_effect = receive
    await routes.playwright_cdp(ws)
    ws.close.assert_awaited_once_with(code=4401)
    assert actions == ["initialize", "close"]
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_relay_failure_reports_delivery_boundary_without_replaying(endpoint, monkeypatch, uncertain):
    from vibecanvas_api.browser.instance_relay import RelayUnavailable
    from vibecanvas_api.browser.connection_errors import TransportSendFailed

    ws, _, confirm, _, _ = endpoint
    failure = TransportSendFailed("private failure") if uncertain else RelayUnavailable("private failure")
    send = AsyncMock(side_effect=failure)
    monkeypatch.setattr(routes.registry, "send_to", send)
    await routes.playwright_cdp(ws)
    ws.close.assert_awaited_once_with(
        code=1011 if uncertain else 1013,
        reason=errors.RELAY_DELIVERY_UNKNOWN if uncertain else errors.RELAY_UNAVAILABLE)
    confirm.assert_not_awaited()
    actions = [decode(call.args[1])["data"]["action"] for call in send.await_args_list]
    assert actions == ["initialize", "close"]  # cleanup, never a second initialize


@pytest.mark.asyncio
async def test_directory_failure_before_handshake_is_reported(endpoint, monkeypatch):
    from redis.exceptions import ConnectionError

    ws, _, confirm, actions, _ = endpoint
    monkeypatch.setattr(routes.registry, "find_for_session", AsyncMock(side_effect=ConnectionError("private address")))
    await routes.playwright_cdp(ws)
    ws.close.assert_awaited_once_with(code=1013, reason=errors.RELAY_UNAVAILABLE)
    ws.accept.assert_not_awaited()
    confirm.assert_not_awaited()
    assert actions == []
