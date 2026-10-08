import pytest
from starlette.testclient import TestClient
from vibecanvas_api.browser.gateway import build_app


async def _register(client) -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "bx@example.com", "username": "Test User", "password": "pw12345678"},
    )
    return r.json()["session_token"]


@pytest.mark.asyncio
async def test_mint_requires_auth(client):
    r = await client.post(
        "/api/v1/browser/token", json={"wf_id": "wf1", "browser_id": "b1"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mint_returns_scoped_token(client):
    from vibecanvas_api.browser.scoped_token import verify_scoped_token
    from vibecanvas_api.config import config

    tok = await _register(client)
    r = await client.post(
        "/api/v1/browser/token",
        json={"wf_id": "wf1", "browser_id": "b1"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert "token" in r.json() and r.json()["expires_in"] == 900
    scoped = verify_scoped_token(r.json()["token"], config.browser_token_secret)
    assert scoped is not None
    assert scoped.browser_id == "b1"
    assert scoped.extension_id == config.browser_extension_id
    assert scoped.session_id
    assert scoped.session_generation == 1


@pytest.mark.asyncio
async def test_production_mint_requires_derived_extension_session(client, monkeypatch):
    from vibecanvas_api.config import config

    tok = await _register(client)
    monkeypatch.setattr(config, "environment", "production")
    response = await client.post(
        "/api/v1/browser/token",
        json={"wf_id": "wf1", "browser_id": "b1"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "extension_session_required"


def test_ws_echo_with_valid_scoped_token(monkeypatch, directory):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from vibecanvas_api.browser.cluster_registry import close_connections

    from vibecanvas_api.browser.scoped_token import mint_scoped_token
    from vibecanvas_api.browser.ws_auth import (
        BROWSER_WS_PROTOCOL,
        build_browser_ws_protocols,
    )
    from vibecanvas_api.config import config
    from vibecanvas_api.routes import browser as browser_routes

    secret = config.browser_token_secret
    tok = mint_scoped_token(
        "u1",
        "t1",
        "wf1",
        secret,
        browser_id="b1",
        extension_id=config.browser_extension_id,
        session_id="00000000-0000-0000-0000-000000000001",
        session_generation=1,
        session_audience="extension",
    )

    async def session_is_live(_scoped):
        return True

    monkeypatch.setattr(browser_routes, "_browser_session_is_live", session_is_live)
    _, socket = directory
    monkeypatch.setattr(config.redis, "url", f"unix://{socket}")

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await close_connections()

    app = FastAPI(lifespan=lifespan)
    app.include_router(browser_routes.transport_router)
    with TestClient(app) as client, client.websocket_connect(
        "/api/v1/browser/ws",
        subprotocols=build_browser_ws_protocols(tok, "b1"),
        headers={"origin": f"chrome-extension://{config.browser_extension_id}"},
    ) as ws:
        assert ws.accepted_subprotocol == BROWSER_WS_PROTOCOL
        authenticated = ws.receive_json()
        assert authenticated["data"]["type"] == "auth_status"
        assert isinstance(authenticated["data"]["expires_at"], int)
        ws.send_text(
            '{"v":1,"kind":"ping","id":"c1","channel":"chat:1",'
            '"transport":"x","data":{"hi":1}}'
        )
        reply = ws.receive_json()
        assert reply["kind"] == "echo" and reply["id"] == "c1"
        assert reply["data"] == {"hi": 1}
        # Transport ownership is tenant + authenticated user + extension instance;
        # browser ids are only unique within one user's extension installation.
        assert reply["transport"] == "t1:u1:b1"


def test_ws_rejects_bad_token():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from vibecanvas_api.browser.ws_auth import build_browser_ws_protocols

    app = build_app()
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(
            "/api/v1/browser/ws",
            subprotocols=build_browser_ws_protocols("bad", "b1"),
        ) as ws:
            ws.receive_text()


@pytest.mark.parametrize(
    "offered_browser,origin",
    [
        ("b2", "chrome-extension://mkfldhmlgdbpmhplaphhcfcdcoaakcik"),
        ("b1", "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("b1", "https://app.example.com"),
        ("b1", None),
    ],
)
def test_ws_rejects_wrong_browser_or_extension_origin(
    monkeypatch, offered_browser, origin
):
    from starlette.websockets import WebSocketDisconnect

    from vibecanvas_api.browser.scoped_token import mint_scoped_token
    from vibecanvas_api.browser.ws_auth import build_browser_ws_protocols
    from vibecanvas_api.config import config
    from vibecanvas_api.routes import browser as browser_routes

    token = mint_scoped_token(
        "u1",
        "t1",
        "wf1",
        config.browser_token_secret,
        browser_id="b1",
        extension_id=config.browser_extension_id,
        session_id="00000000-0000-0000-0000-000000000001",
        session_generation=1,
        session_audience="extension",
    )

    async def session_is_live(_scoped):
        return True

    monkeypatch.setattr(browser_routes, "_browser_session_is_live", session_is_live)
    kwargs = {
        "subprotocols": build_browser_ws_protocols(token, offered_browser),
    }
    if origin is not None:
        kwargs["headers"] = {"origin": origin}
    with pytest.raises(WebSocketDisconnect):
        with TestClient(build_app()).websocket_connect(
            "/api/v1/browser/ws", **kwargs
        ) as ws:
            ws.receive_text()


@pytest.mark.parametrize(
    "path,protocols",
    [
        ("/api/v1/browser/ws?token=secret&browser=b1", None),
        ("/api/v1/browser/ws", None),
        ("/api/v1/browser/ws", ["vibecanvas.browser.v1"]),
        (
            "/api/v1/browser/ws",
            [
                "vibecanvas.browser.v1",
                "vibecanvas.browser.auth.one",
                "vibecanvas.browser.auth.two",
                "vibecanvas.browser.id.b1",
            ],
        ),
    ],
)
def test_ws_rejects_url_credentials_and_incomplete_or_ambiguous_protocols(
    path, protocols
):
    from starlette.websockets import WebSocketDisconnect

    app = build_app()
    kwargs = {"subprotocols": protocols} if protocols is not None else {}
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(path, **kwargs) as ws:
            ws.receive_text()
