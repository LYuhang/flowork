"""Transport tests use real loopback sockets, without a model or live project."""

import asyncio
import base64
import json

import pytest
from vibecanvas_api.routes import sandbox_forwarding as gateway
from vibecanvas_api.services.sandbox.service_forwarding import forward_service
from websockets.asyncio.server import serve


class Channel:
    def __init__(self):
        self.sent = asyncio.Queue()
        self.incoming = asyncio.Queue()

    async def send(self, value):
        await self.sent.put(value)

    async def recv(self):
        return await self.incoming.get()


@pytest.mark.asyncio
async def test_http_stream_preserves_method_query_body_and_backpressure():
    observed = []

    async def handler(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        body = await reader.readexactly(5)
        observed.append((headers, body))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n9\r\ndata: 1\n\n\r\n"
        )
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(b"9\r\ndata: 2\n\n\r\n0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        channel = Channel()
        task = asyncio.create_task(
            forward_service(
                channel, dict(port=port, path="/api?x=2", method="POST", body=base64.b64encode(b"hello").decode())
            )
        )
        async with asyncio.timeout(3):
            headers = await channel.sent.get()
            assert headers["status"] == 200
            await asyncio.sleep(0.1)
            assert channel.sent.empty()  # waiting for downstream ACK, not buffering
            await channel.incoming.put({"action": "ack"})
            body = b""
            while not task.done():
                next_item = asyncio.create_task(channel.sent.get())
                done, _ = await asyncio.wait({task, next_item}, return_when=asyncio.FIRST_COMPLETED)
                if next_item not in done:
                    next_item.cancel()
                    break
                body += base64.b64decode(next_item.result()["data"])
                await channel.incoming.put({"action": "ack"})
            await task
        assert body == b"data: 1\n\ndata: 2\n\n"
        assert observed[0][0].startswith(b"POST /api?x=2 HTTP/1.1")
        assert observed[0][1] == b"hello"


@pytest.mark.asyncio
async def test_websocket_roundtrip_text_binary_and_disconnect():
    async def echo(socket):
        async for message in socket:
            await socket.send(message)

    async with serve(echo, "127.0.0.1", 0, subprotocols=["qa"]) as server:
        channel = Channel()
        task = asyncio.create_task(
            forward_service(
                channel,
                dict(port=server.sockets[0].getsockname()[1], path="/live", websocket=True, subprotocols=["qa"]),
            )
        )
        async with asyncio.timeout(3):
            assert await channel.sent.get() == {"type": "ready", "subprotocol": "qa"}
            for raw, binary in [(b"hello", False), (b"\x00\xff", True)]:
                await channel.incoming.put({"action": "data", "binary": binary, "data": base64.b64encode(raw).decode()})
                result = await channel.sent.get()
                assert result["binary"] == binary
                assert base64.b64decode(result["data"]) == raw
                await channel.incoming.put({"action": "ack"})
            await channel.incoming.put({"action": "cancel"})
            await task


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://128.0.0.1",
        "file:///tmp/x",
        "http://user:pass@localhost:80",
        "http://localhost:99999",
        "http://localhost:0",
    ],
)
def test_reject_non_loopback_or_invalid_urls(url):
    with pytest.raises(ValueError):
        gateway.local_service_url(url)


@pytest.mark.parametrize(
    "url,port", [("http://localhost:3000/a?q=1", 3000), ("https://127.0.0.1", 443), ("http://[::1]:8080", 8080)]
)
def test_parse_loopback(url, port):
    assert gateway.local_service_url(url)[1] == port


def test_capability_is_bound_to_port_runtime_and_origin(monkeypatch):
    monkeypatch.setenv("FLOWORK_SERVICE_PREVIEW_DOMAIN", "preview.example.test")
    import time

    claims = dict(tenant="tenant", scope="scope", runtime_id="first", port=3000, exp=int(time.time()) + 100)
    token = gateway.codec().encrypt(json.dumps(claims).encode()).decode()
    host = gateway.hostname(token)
    assert gateway.claims_for(token, host)["runtime_id"] == "first"
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        gateway.claims_for(token, host.replace("p3000-", "p4000-"))
    with pytest.raises(HTTPException):
        gateway.claims_for(token + "bad", host)
    newer = gateway.codec().encrypt(json.dumps({**claims, "runtime_id": "second"}).encode()).decode()
    assert gateway.hostname(newer) != host


@pytest.mark.asyncio
async def test_resolve_preserves_path_query_fragment_and_uses_existing_sandbox(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from vibecanvas_api.auth.deps import AuthContext
    from vibecanvas_api.routes import service_preview as preview

    monkeypatch.setenv("FLOWORK_SERVICE_PREVIEW_DOMAIN", "preview.example.test")

    class DB:
        project = "project-a"

        async def execute(self, query):
            return SimpleNamespace(scalar_one_or_none=lambda: self.project)

    class Sandbox:
        async def service_forwarding_identity(self):
            return {"runtime_id": "generation-a"}

    calls = []

    class Manager:
        async def get_loaded_session(self, tenant, scope):
            calls.append((tenant, scope))
            return Sandbox()

    monkeypatch.setattr(preview, "get_sandbox_manager", Manager)
    db = DB()
    auth = AuthContext(user_id="user", tenant_id="tenant", email="qa@example.test")
    result = await preview.resolve(
        preview.OpenService(chat_id="chat-a", url="http://localhost:3100/demo?a=1#part"), auth, db
    )
    assert result["port"] == 3100
    assert result["path"] == "/demo?a=1#part"
    assert len(calls) == 1
    token = result["url"].split("token=")[1]
    claims = gateway.claims_for(token, gateway.urlsplit(result["url"]).hostname)
    assert claims["project"] == "project-a"
    assert claims["runtime_id"] == "generation-a"
    db.project = None
    with pytest.raises(HTTPException) as error:
        await preview.resolve(preview.OpenService(chat_id="other", url="http://localhost:3100/"), auth, db)
    assert error.value.status_code == 404
    assert len(calls) == 1  # unauthorized callers never reach sandboxd


@pytest.mark.asyncio
async def test_preview_host_never_falls_through_to_platform_routes(monkeypatch):
    import httpx
    from fastapi import FastAPI

    monkeypatch.setenv("FLOWORK_SERVICE_PREVIEW_DOMAIN", "preview.example.test")
    platform = FastAPI()

    @platform.get("/api/v1/private")
    async def private():
        return {"secret": "platform"}

    app = gateway.SandboxForwardingHostMiddleware(platform)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://anything.preview.example.test"
    ) as client:
        denied = await client.get("/api/v1/private")
        assert denied.status_code == 401
        assert "platform" not in denied.text
        main = await client.get("https://app.example.test/api/v1/private")
        assert main.json() == {"secret": "platform"}


@pytest.mark.asyncio
async def test_stale_runtime_is_rejected_before_forwarding(monkeypatch):
    from types import SimpleNamespace

    from vibecanvas_api.services.sandbox.manager import SandboxSession

    async def identity():
        return {"runtime_id": "new-generation"}

    session = SimpleNamespace(service_forwarding_identity=identity)
    stream = SandboxSession.service_forwarding_stream(session, {"runtime_id": "old-generation"})
    with pytest.raises(RuntimeError, match="sandbox_changed"):
        await anext(stream)


def test_render_contract_accepts_loopback_without_new_tool():
    from vibecanvas_api.services.platform_mcp.interactive_tools.schema import validate_view

    result = validate_view({"type": "url_preview", "url": "http://localhost:3000/demo"})
    assert result.url == "http://localhost:3000/demo"


@pytest.mark.asyncio
async def test_cross_origin_post_is_rejected_before_sandbox_access(monkeypatch):
    import time

    import httpx

    monkeypatch.setenv("FLOWORK_SERVICE_PREVIEW_DOMAIN", "preview.example.test")
    claims = dict(tenant="qa", scope="qa", runtime_id="qa", port=3000, exp=int(time.time()) + 100)
    token = gateway.codec().encrypt(json.dumps(claims).encode()).decode()
    host = gateway.hostname(token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.gateway), base_url=f"https://{host}"
    ) as client:
        client.cookies.set(gateway.COOKIE, token)
        response = await client.post("/change", headers={"Origin": "https://other.example.test"}, content=b"change")
    assert response.status_code == 403
    assert "invalid_forwarding_origin" in response.text


@pytest.mark.asyncio
async def test_app_cookies_are_host_scoped_and_gateway_credential_never_reaches_service(monkeypatch):
    import time

    import httpx

    monkeypatch.setenv("FLOWORK_SERVICE_PREVIEW_DOMAIN", "preview.example.test")
    claims = dict(
        tenant="qa",
        user="qa",
        project="qa",
        scope="qa",
        runtime_id="qa",
        port=3000,
        scheme="http",
        exp=int(time.time()) + 100,
    )
    token = gateway.codec().encrypt(json.dumps(claims).encode()).decode()
    host = gateway.hostname(token)
    requests = []

    class Session:
        async def service_forwarding_stream(self, payload):
            requests.append(payload)
            yield {
                "type": "headers",
                "status": 200,
                "headers": [
                    ("content-type", "text/plain"),
                    ("set-cookie", gateway.COOKIE + "=bad; Secure; Path=/"),
                    ("set-cookie", "app_session=hello; Domain=.example.test; HttpOnly; Path=/"),
                ],
            }
            yield {"type": "data", "data": base64.b64encode(b"ok").decode()}
            yield {"type": "end"}

    async def authorized(claims):
        return Session()

    monkeypatch.setattr(gateway, "authorized_session", authorized)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.gateway), base_url=f"https://{host}"
    ) as client:
        client.cookies.set(gateway.COOKIE, token)
        response = await client.get("/app", headers={"Authorization": "Bearer application-token"})
        assert response.status_code == 200
        assert response.headers.get_list("set-cookie") == ["app_session=hello; HttpOnly; Path=/; Secure"]
        await client.get("/next")
    assert "cookie" not in requests[0]["headers"]
    assert requests[0]["headers"]["authorization"] == "Bearer application-token"
    assert requests[1]["headers"]["cookie"] == "app_session=hello"


@pytest.mark.asyncio
async def test_disconnect_during_attach_releases_activity_lease():
    from types import SimpleNamespace

    from vibecanvas_api.services.sandbox.manager import SandboxSession

    activity = []

    async def identity():
        return {"runtime_id": "same"}

    def attach(_):
        raise ConnectionError("sandbox disconnected")

    session = SimpleNamespace(
        service_forwarding_identity=identity,
        _begin_activity=lambda: activity.append("begin"),
        _end_activity=lambda: activity.append("end"),
        _runtime_router=SimpleNamespace(attach=attach),
    )
    stream = SandboxSession.service_forwarding_stream(session, {"runtime_id": "same", "identifier": "request"})
    with pytest.raises(ConnectionError):
        await anext(stream)
    assert activity == ["begin", "end"]
