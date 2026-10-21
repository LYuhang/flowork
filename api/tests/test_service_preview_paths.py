import base64

import httpx
import pytest
from fastapi import FastAPI
from vibecanvas_api.routes import sandbox_forwarding as gateway
from vibecanvas_api.routes.service_preview_paths import rewrite_document


def target(monkeypatch, browser_origin=None):
    monkeypatch.delenv("FLOWORK_SERVICE_PREVIEW_DOMAIN", raising=False)
    return gateway.issue_forwarding_target(
        tenant="tenant",
        user="user",
        project="project",
        scope="scope",
        runtime_id="runtime",
        port=8080,
        scheme="http",
        loopback_host="localhost",
        path="/clock/index.html?x=1",
        browser_origin=browser_origin,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://jump.example.test", "http://127.0.0.1:9001"])
async def test_path_proxy_isolates_credentials_and_rewrites_resources(monkeypatch, origin):
    entry = target(monkeypatch, origin)
    assert entry["url"].startswith(origin + "/preview/")
    observed = []

    class Session:
        async def service_forwarding_stream(self, payload):
            observed.append(payload)
            yield {
                "type": "headers",
                "status": 200,
                "headers": [
                    ("content-type", "text/html; charset=utf-8"),
                    ("set-cookie", "platform=override; Path=/"),
                ],
            }
            yield {
                "type": "data",
                "data": base64.b64encode(
                    b'<html><head></head><body><img src="/clock.png"><script>fetch("/api/time")</script></body></html>'
                ).decode(),
            }
            yield {"type": "end"}

    async def authorize(claims):
        assert claims["runtime_id"] == "runtime"
        return Session()

    monkeypatch.setattr(gateway, "authorized_session", authorize)
    app = gateway.SandboxForwardingHostMiddleware(FastAPI())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.example.test") as client:
        response = await client.get(
            entry["url"], headers={"Cookie": "login=private", "Authorization": "Bearer private"}
        )
        assert response.status_code == 200
        assert not response.headers.get("set-cookie")
        policy = response.headers["content-security-policy"]
        assert f"frame-ancestors {origin}" in policy
        assert f"connect-src {entry['forwarding_url']}" in policy
        assert "sandbox allow-scripts" in policy and "allow-same-origin" not in policy
        assert response.headers["access-control-allow-origin"] == "*"
        assert entry["forwarding_url"].split("/preview/")[1] in response.text
        assert observed[0]["path"] == "/clock/index.html?x=1"
        assert "cookie" not in observed[0]["headers"]
        assert "authorization" not in observed[0]["headers"]
        assert observed[0]["headers"]["accept-encoding"] == "identity"
        denied = await client.get(entry["url"].replace("/8080/", "/8081/"))
        assert denied.status_code == 401
        assert len(observed) == 1


def test_prefix_handles_html_relative_and_absolute_resources():
    result = rewrite_document(
        b'<!doctype html><html><head><base href="/"><link href="/style.css"></head><body><img src="photo.png"></body></html>',
        "text/html",
        "/preview/id/8080/",
        "/nested/index.html",
    ).decode()
    assert result.count("<base ") == 1
    assert '<base href="/preview/id/8080/nested/">' in result
    assert 'href="/preview/id/8080/style.css"' in result
    assert 'src="photo.png"' in result


def test_css_paths_are_prefixed_without_rewriting_remote_urls():
    result = rewrite_document(
        b"a{background:url(/img.png)}b{background:url(https://example.com/img.png)}",
        "text/css",
        "/preview/id/8080/",
        "/style.css",
    ).decode()
    assert "url(/preview/id/8080/img.png)" in result
    assert "url(https://example.com/img.png)" in result


def test_path_websocket_roundtrip_uses_same_sandbox(monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    entry = target(monkeypatch, "http://jump.example.test:9001")
    observed = []

    class Session:
        async def service_forwarding_stream(self, payload):
            self.messages = asyncio.Queue()
            observed.append(payload)
            yield {"type": "ready"}
            while True:
                message = await asyncio.wait_for(self.messages.get(), 3)
                yield {"type": "data", "data": message["data"], "binary": message["binary"]}

        async def service_forwarding_control(self, identifier, message):
            if message["action"] == "data":
                await self.messages.put(message)

    session = Session()

    async def authorize(claims):
        return session

    monkeypatch.setattr(gateway, "authorized_session", authorize)
    app = gateway.SandboxForwardingHostMiddleware(FastAPI())
    with TestClient(app) as client:
        with client.websocket_connect(
            entry["forwarding_url"].replace("http://", "ws://", 1) + "echo", headers={"Origin": "null", "Cookie": "login=private"}
        ) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "hello"
            ws.send_bytes(b"binary")
            assert ws.receive_bytes() == b"binary"
    assert observed[0]["path"] == "/echo"
    assert observed[0]["port"] == 8080
    assert "cookie" not in observed[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin, allowed", [
    ("https://jump.example.test:9443", True),
    ("https://unapproved.example.test", False),
])
async def test_resolve_binds_only_approved_browser_origin(monkeypatch, origin, allowed):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from urllib.parse import urlsplit
    from fastapi import HTTPException, Request
    from vibecanvas_api.auth import session_security
    from vibecanvas_api.routes import service_preview

    monkeypatch.delenv("FLOWORK_SERVICE_PREVIEW_DOMAIN", raising=False)
    monkeypatch.setattr(session_security, "config", SimpleNamespace(
        environment="production",
        public_urls=SimpleNamespace(public_url="https://primary.example.test"),
    ))
    monkeypatch.setattr(session_security, "configured_cors_origins", lambda: ["https://jump.example.test:9443"])
    sandbox = SimpleNamespace(
        service_forwarding_identity=AsyncMock(return_value={"runtime_id": "runtime"}),
        service_inventory=AsyncMock(return_value={"services": []}),
    )
    owned = AsyncMock(return_value=(sandbox, urlsplit("http://localhost:8080/clock/"), 8080, "project", "scope"))
    monkeypatch.setattr(service_preview, "owned_service", owned)
    request = Request({"type": "http", "method": "POST", "path": "/resolve", "headers": [
        (b"origin", origin.encode()), (b"host", b"internal-api:8000"),
        (b"x-forwarded-host", b"untrusted.example.test"),
    ]})
    body = service_preview.OpenService(chat_id="chat", url="http://localhost:8080/clock/")
    auth = SimpleNamespace(tenant_id="tenant", user_id="user")
    if not allowed:
        with pytest.raises(HTTPException) as error:
            await service_preview.resolve(body, request, auth, None)
        assert error.value.status_code == 403
        owned.assert_not_awaited()
        return
    result = await service_preview.resolve(body, request, auth, None)
    assert result["url"].startswith(origin + "/preview/")
    token = result["forwarding_url"].split("/preview/")[1].split("/")[0]
    import json
    assert json.loads(gateway.codec().decrypt(token.encode()))["browser_origin"] == origin
