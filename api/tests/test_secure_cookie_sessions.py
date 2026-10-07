"""Production-shaped HttpOnly Session and extension exchange regressions."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from vibecanvas_api.app import build_app
from vibecanvas_api.auth.session_security import (
    _allowed_origins,
    _secure_cookie_transport,
)
from vibecanvas_api.config import config

ORIGIN = "http://testserver"
EXTENSION_ORIGIN = "https://testserver"


def _csrf(client: AsyncClient, audience: str = "web") -> str:
    value = client.cookies.get(f"vibecanvas-{audience}-csrf")
    assert value
    return value


def _unsafe_headers(client: AsyncClient, audience: str = "web") -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": _csrf(client, audience),
    }


async def _register(client: AsyncClient, label: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={
            "email": f"{label}_{uuid.uuid4().hex[:10]}@example.com",
            "username": label,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def cookie_mode(monkeypatch):
    monkeypatch.setattr(config, "web_session_cookie_enabled", True)
    monkeypatch.setattr(config, "extension_scoped_token_enabled", True)
    # Keep the test transport on local HTTP; production is separately required
    # to use public HTTPS and therefore emits __Host- Secure cookies.
    monkeypatch.setattr(config.public_urls, "public_url", "")


@pytest.mark.asyncio
async def test_cookie_session_hides_bearer_and_enforces_origin_csrf(
    client,
    cookie_mode,
):
    registered = await _register(client, "cookie-primary")
    assert "session_token" not in registered
    assert "vibecanvas-web-session" in client.cookies
    assert "vibecanvas-web-csrf" in client.cookies

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["session"]["authentication_strength"] == "password"

    missing_csrf = await client.post(
        "/api/v1/organizations",
        headers={"Origin": ORIGIN},
        json={"name": "Denied", "slug": f"denied-{uuid.uuid4().hex[:8]}"},
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "csrf_validation_failed"

    wrong_origin = await client.post(
        "/api/v1/organizations",
        headers={
            "Origin": "https://attacker.example",
            "X-CSRF-Token": _csrf(client),
        },
        json={"name": "Denied", "slug": f"denied-{uuid.uuid4().hex[:8]}"},
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["detail"]["code"] == "invalid_request_origin"

    created = await client.post(
        "/api/v1/organizations",
        headers=_unsafe_headers(client),
        json={"name": "Allowed", "slug": f"allowed-{uuid.uuid4().hex[:8]}"},
    )
    assert created.status_code == 201, created.text


@pytest.mark.asyncio
async def test_local_http_cookie_override_keeps_login_session_usable(
    client,
    cookie_mode,
    monkeypatch,
):
    monkeypatch.setattr(
        config.public_urls,
        "public_url",
        "https://workspace-proxy.example/studio",
    )
    monkeypatch.setattr(config, "web_session_cookie_secure", False)

    registered = await _register(client, "local-http")
    assert "session_token" not in registered
    assert "vibecanvas-web-session" in client.cookies
    assert "__Host-vibecanvas-web-session" not in client.cookies
    assert (await client.get("/api/v1/auth/me")).status_code == 200


def test_production_cookie_transport_cannot_be_downgraded(monkeypatch):
    monkeypatch.setattr(config, "environment", "production")
    monkeypatch.setattr(config, "web_session_cookie_secure", False)
    assert _secure_cookie_transport() is True


def test_production_origin_allowlist_ignores_request_host(monkeypatch):
    monkeypatch.setattr(config, "environment", "production")
    monkeypatch.setattr(
        config.public_urls,
        "public_url",
        "https://workspace.example.com",
    )
    monkeypatch.setenv(
        "VIBECANVAS_API_CORS_ORIGINS",
        "https://workspace.example.com",
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/organizations",
            "headers": [(b"host", b"attacker.example")],
            "scheme": "https",
            "server": ("attacker.example", 443),
        }
    )

    origins = _allowed_origins(request)

    assert "https://workspace.example.com" in origins
    assert "https://attacker.example" not in origins


@pytest.mark.asyncio
async def test_session_list_rotate_and_targeted_revoke(cookie_mode):
    app = build_app()
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url=ORIGIN) as primary,
        AsyncClient(transport=transport, base_url=ORIGIN) as secondary,
    ):
        first = await _register(primary, "session-owner")
        email = first["user"]["email"]
        second = await secondary.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"email": email, "password": "pw12345678"},
        )
        assert second.status_code == 200

        listed = await primary.get("/api/v1/auth/sessions")
        assert listed.status_code == 200
        rows = listed.json()["items"]
        assert len(rows) == 2
        current = next(item for item in rows if item["current"])
        other = next(item for item in rows if not item["current"])
        old_cookie = primary.cookies.get("vibecanvas-web-session")

        rotated = await primary.post(
            "/api/v1/auth/sessions/current/rotate",
            headers=_unsafe_headers(primary),
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["session_id"] == current["session_id"]
        assert primary.cookies.get("vibecanvas-web-session") != old_cookie

        revoked = await primary.delete(
            f"/api/v1/auth/sessions/{other['session_id']}",
            headers=_unsafe_headers(primary),
        )
        assert revoked.status_code == 204
        assert (await secondary.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_extension_one_time_exchange_and_parent_logout_cascade(cookie_mode):
    app = build_app()
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url=ORIGIN) as primary,
        AsyncClient(transport=transport, base_url=EXTENSION_ORIGIN) as extension,
        AsyncClient(transport=transport, base_url=EXTENSION_ORIGIN) as replay,
    ):
        await _register(primary, "extension-parent")
        code_response = await primary.post(
            "/api/v1/auth/extension/exchange-code",
            headers=_unsafe_headers(primary),
        )
        assert code_response.status_code == 200, code_response.text
        code = code_response.json()["code"]

        exchanged = await extension.post(
            "/api/v1/auth/extension/exchange",
            headers={"Origin": EXTENSION_ORIGIN},
            json={"code": code},
        )
        assert exchanged.status_code == 200, exchanged.text
        assert "session_token" not in exchanged.json()
        assert "__Host-vibecanvas-extension-session" in extension.cookies
        assert "__Host-vibecanvas-extension-csrf" in extension.cookies
        extension_me = await extension.get("/api/v1/auth/me")
        assert extension_me.status_code == 200
        browser_capability = await extension.post(
            "/api/v1/browser/token",
            headers={
                "Origin": EXTENSION_ORIGIN,
                "X-CSRF-Token": extension.cookies.get(
                    "__Host-vibecanvas-extension-csrf"
                ),
            },
            json={"wf_id": "wf-extension", "browser_id": "browser-1"},
        )
        assert browser_capability.status_code == 200, browser_capability.text
        from vibecanvas_api.browser.scoped_token import verify_scoped_token
        from vibecanvas_api.routes.browser import _browser_session_is_live

        scoped = verify_scoped_token(
            browser_capability.json()["token"],
            config.browser_token_secret,
        )
        assert scoped is not None
        assert scoped.session_audience == "extension"
        assert scoped.browser_id == "browser-1"
        assert await _browser_session_is_live(scoped) is True

        # Opening another panel from the same parent login preserves the
        # cookie and Session used by a live browser capability.
        original_cookie = extension.cookies.get("__Host-vibecanvas-extension-session")
        fresh_code = await primary.post(
            "/api/v1/auth/extension/exchange-code", headers=_unsafe_headers(primary),
        )
        renewed = await extension.post(
            "/api/v1/auth/extension/exchange",
            headers={"Origin": EXTENSION_ORIGIN}, json={"code": fresh_code.json()["code"]},
        )
        assert renewed.status_code == 200, renewed.text
        assert not renewed.headers.get_list("set-cookie")
        assert extension.cookies.get("__Host-vibecanvas-extension-session") == original_cookie
        assert (await extension.get("/api/v1/auth/me")).json()["session"]["session_id"] == scoped.session_id
        assert await _browser_session_is_live(scoped) is True

        duplicate = await replay.post(
            "/api/v1/auth/extension/exchange",
            headers={"Origin": EXTENSION_ORIGIN},
            json={"code": code},
        )
        assert duplicate.status_code == 400

        logout = await primary.post(
            "/api/v1/auth/logout",
            headers=_unsafe_headers(primary),
        )
        assert logout.status_code == 204
        assert (await primary.get("/api/v1/auth/me")).status_code == 401
        assert (await extension.get("/api/v1/auth/me")).status_code == 401
        assert await _browser_session_is_live(scoped) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("same_user", [True, False])
async def test_extension_exchange_does_not_reuse_another_parent_session(cookie_mode, same_user):
    transport = ASGITransport(app=build_app())
    async with (
        AsyncClient(transport=transport, base_url=ORIGIN) as first,
        AsyncClient(transport=transport, base_url=ORIGIN) as second,
        AsyncClient(transport=transport, base_url=EXTENSION_ORIGIN) as extension,
    ):
        registered = await _register(first, "extension-original")
        if same_user:
            logged_in = await second.post(
                "/api/v1/auth/login", headers={"Origin": ORIGIN},
                json={"email": registered["user"]["email"], "password": "pw12345678"},
            )
            assert logged_in.status_code == 200
        else:
            await _register(second, "extension-other")

        sessions = []
        for parent in (first, second):
            code = await parent.post("/api/v1/auth/extension/exchange-code", headers=_unsafe_headers(parent))
            result = await extension.post(
                "/api/v1/auth/extension/exchange", headers={"Origin": EXTENSION_ORIGIN},
                json={"code": code.json()["code"]},
            )
            assert result.status_code == 200, result.text
            assert result.headers.get_list("set-cookie")
            me = (await extension.get("/api/v1/auth/me")).json()
            assert me["user_id"] == (await parent.get("/api/v1/auth/me")).json()["user_id"]
            sessions.append(me["session"]["session_id"])
        assert sessions[0] != sessions[1]
