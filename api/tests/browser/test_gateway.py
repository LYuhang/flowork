from unittest.mock import AsyncMock, Mock

import pytest
from starlette.routing import Match

from vibecanvas_api.browser import gateway


def test_gateway_owns_transport_routes_and_api_only_mints_tokens():
    from vibecanvas_api.app import build_app

    app = gateway.build_app()
    api = build_app()
    for path in ("/api/v1/browser/ws", "/api/v1/browser/playwright/cdp"):
        scope = {"type": "websocket", "path": path, "root_path": ""}
        assert any(r.matches(scope)[0] == Match.FULL for r in app.routes)
        assert not any(r.matches(scope)[0] == Match.FULL for r in api.routes)
    scope = {"type": "http", "method": "POST", "path": "/api/v1/browser/token", "root_path": ""}
    assert any(r.matches(scope)[0] == Match.FULL for r in api.routes)
    assert not any(r.matches(scope)[0] == Match.FULL for r in app.routes)


@pytest.mark.parametrize("probe_fails", [False, True])
async def test_gateway_closes_transports_before_shared_clients(monkeypatch, probe_fails):
    events = []
    monkeypatch.setattr(gateway.config, "environment", "test")
    monkeypatch.setattr(gateway, "init_engine", Mock())
    monkeypatch.setattr(gateway, "dispose_engine", AsyncMock(side_effect=lambda: events.append("db")))
    monkeypatch.setattr(gateway, "close_connections", AsyncMock(side_effect=lambda: events.append("transport")))
    client = Mock(probe=AsyncMock(side_effect=RuntimeError("unavailable") if probe_fails else None),
                  close=AsyncMock(side_effect=lambda: events.append("auth")))
    monkeypatch.setattr(gateway, "openfga_client_from_config", lambda: client)
    publish = Mock()
    monkeypatch.setattr(gateway, "set_authorization_client", publish)
    app = gateway.build_app()
    if probe_fails:
        with pytest.raises(RuntimeError, match="unavailable"):
            async with gateway.lifespan(app):
                pytest.fail("Gateway must not serve without its authorization service")
        assert events == ["auth", "db"]
        publish.assert_not_called()
    else:
        async with gateway.lifespan(app):
            publish.assert_called_once_with(client)
        assert events == ["transport", "auth", "db"]
        publish.assert_called_with(None)


def test_cdp_uses_gateway_instead_of_business_api(monkeypatch):
    from vibecanvas_api.services.agent_runtime.mcp_host_gateway import _playwright_cdp_url

    monkeypatch.setattr(gateway.config, "browser_gateway_internal_base_url", "https://gateway.internal:8001")
    assert _playwright_cdp_url() == "wss://gateway.internal:8001/api/v1/browser/playwright/cdp"
