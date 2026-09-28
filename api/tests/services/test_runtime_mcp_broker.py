from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlsplit
import uuid

import pytest
from sqlalchemy import text
from starlette.requests import Request

from vibecanvas_api.routes.runtime_mcp_broker import (
    _forward_headers,
    _safe_stored_headers,
    _target_url,
)
from vibecanvas_api.services.agent_runtime.custom_mcp_capability import (
    mcp_config_revision,
    mint_runtime_custom_mcp_capability,
    verify_runtime_custom_mcp_capability,
)
from vibecanvas_api.services.agent_runtime.mcp_host_resolution import (
    McpSelectionError,
    resolve_custom_mcp_authority,
)
from vibecanvas_api.services.public_url import PublicUrlTarget


def _token(*, secret: str = "s" * 64, now: int = 1000) -> str:
    return mint_runtime_custom_mcp_capability(
        organization_id="org-1",
        user_id="user-1",
        chat_id="chat-1",
        turn_id="turn-1",
        runtime_session_id="runtime-1",
        session_id="session-1",
        session_generation=7,
        membership_id="membership-1",
        server_id="server-1",
        transport="streamable_http",
        config_revision="r" * 64,
        authorization_generation="a" * 64,
        secret=secret,
        ttl_s=120,
        now=now,
    )


def _request(
    *,
    headers: dict[str, str],
    query: bytes = b"",
    method: str = "POST",
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/api/internal/runtime-mcp/v1/server-1",
            "raw_path": b"/api/internal/runtime-mcp/v1/server-1",
            "query_string": query,
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("platform.test", 443),
        }
    )


def test_runtime_mcp_capability_is_scoped_signed_and_expiring():
    token = _token()
    capability = verify_runtime_custom_mcp_capability(
        token,
        secret="s" * 64,
        server_id="server-1",
        now=1050,
    )
    assert capability is not None
    assert capability.audience == "runtime-custom-mcp"
    assert capability.session_generation == 7
    assert capability.server_id == "server-1"
    assert capability.transport == "streamable_http"
    assert verify_runtime_custom_mcp_capability(
        token,
        secret="s" * 64,
        server_id="different",
        now=1050,
    ) is None
    assert verify_runtime_custom_mcp_capability(
        token + "x",
        secret="s" * 64,
        server_id="server-1",
        now=1050,
    ) is None
    assert verify_runtime_custom_mcp_capability(
        token,
        secret="s" * 64,
        server_id="server-1",
        now=1120,
    ) is None


def test_runtime_mcp_capability_payload_contains_no_remote_secret():
    token = _token()
    body = token.split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["res"] == ["chat:chat-1", "mcp_installation:server-1"]
    assert set(payload["act"]) == {
        "chat:execute",
        "mcp:call",
        "mcp_installation:use",
    }
    serialized = json.dumps(payload)
    for forbidden in ("api_key", "access_token", "headers", "env", "endpoint"):
        assert forbidden not in serialized


def test_runtime_mcp_config_revision_changes_with_installation_revision():
    first = mcp_config_revision(server_id="server-1", updated_at="v1")
    assert first == mcp_config_revision(server_id="server-1", updated_at="v1")
    assert first != mcp_config_revision(server_id="server-1", updated_at="v2")
    assert first != mcp_config_revision(server_id="server-2", updated_at="v1")


def test_runtime_mcp_headers_strip_browser_and_capability_credentials():
    token = _token()
    request = _request(
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": "mcp-session-1",
            "Cookie": "must-not-forward=1",
            "X-Forwarded-For": "10.0.0.1",
        }
    )
    target_headers = _safe_stored_headers(
        {
            "headers": {
                "Authorization": "Bearer real-remote-secret",
                "X-Private-MCP-Key": "remote-header-secret",
            }
        }
    )
    forwarded = _forward_headers(request, target_headers=target_headers)
    lower = {name.casefold(): value for name, value in forwarded.items()}
    assert lower["authorization"] == "Bearer real-remote-secret"
    assert lower["x-private-mcp-key"] == "remote-header-secret"
    assert lower["mcp-session-id"] == "mcp-session-1"
    assert "cookie" not in lower
    assert "x-forwarded-for" not in lower
    assert token not in repr(forwarded)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "metadata.example.test"},
        {"Content-Length": "1"},
        {"Cookie": "secret=1"},
        {"X-Forwarded-Host": "metadata.example.test"},
        {"X-Test": "ok\r\nX-Evil: yes"},
    ],
)
def test_runtime_mcp_rejects_unsafe_stored_headers(headers: dict[str, str]):
    with pytest.raises(ValueError, match="unsafe MCP header"):
        _safe_stored_headers({"headers": headers})


def test_runtime_mcp_query_cannot_override_configured_secret():
    request = _request(
        headers={},
        query=b"api_key=attacker&session=runtime-session",
    )
    assert _target_url(
        "https://mcp.example.test/rpc?api_key=configured-secret",
        request,
    ) == (
        "https://mcp.example.test/rpc?"
        "api_key=configured-secret&session=runtime-session"
    )


@pytest.mark.asyncio
async def test_stdio_mcp_allows_only_secretless_chat_sandbox_descriptor(
    client,
    monkeypatch,
):
    from vibecanvas_api.routes import mcp_servers as mcp_servers_route

    async def successful_handshake(**_kwargs):
        return {
            "status": "ok",
            "tool_count": 0,
            "tool_names": [],
            "tools": [],
        }

    monkeypatch.setattr(
        mcp_servers_route,
        "handshake_one",
        successful_handshake,
    )
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"stdio_boundary_{uuid.uuid4().hex[:12]}@example.com",
            "username": "Stdio Boundary User",
            "password": "pw12345678",
        },
    )
    assert register.status_code in (200, 201), register.text
    headers = {"Authorization": f"Bearer {register.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    registrations: dict[str, uuid.UUID] = {}
    suffix = uuid.uuid4().hex[:8]
    for kind, auth_config in (
        ("safe", {"type": "none"}),
        ("secret", {"type": "bearer", "token": "must-stay-host-side"}),
    ):
        prefix = f"{kind}_{suffix}"
        created = await client.post(
            "/api/v1/mcp-servers",
            headers=headers,
            json={
                "name": prefix,
                "tool_prefix": prefix,
                "transport": "stdio",
                "endpoint": "/usr/bin/mcp-test",
                "auth_config": auth_config,
                "connection_config": {
                    "command": "/usr/bin/mcp-test",
                    "args": ["--stdio"],
                },
            },
        )
        assert created.status_code == 201, created.text
        registrations[kind] = uuid.UUID(created.json()["id"])

    safe_server_id = registrations["safe"]
    secret_server_id = registrations["secret"]

    claims = {
        "user_id": me["user_id"],
        "chat_id": "chat-stdio-boundary",
        "turn_id": "turn-stdio-boundary",
        "runtime_session_id": "runtime-stdio-boundary",
        "session_id": str(uuid.uuid4()),
        "session_generation": 1,
        "membership_id": str(uuid.uuid4()),
    }
    safe = await resolve_custom_mcp_authority(
        me["tenant_id"],
        **claims,
        server_ids=[str(safe_server_id)],
    )
    assert len(safe) == 1
    assert safe[0].connection == {
        "transport": "stdio",
        "command": "/usr/bin/mcp-test",
        "args": ["--stdio"],
    }
    with pytest.raises(McpSelectionError, match="cannot be exposed"):
        await resolve_custom_mcp_authority(
            me["tenant_id"],
            **claims,
            server_ids=[str(secret_server_id)],
        )


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_chat_custom_mcp_broker_keeps_remote_secrets_on_host(
    client,
    pg_engine,
    monkeypatch,
):
    from vibecanvas_api.routes import chats as chats_route
    from vibecanvas_api.routes import mcp_servers as mcp_servers_route
    from vibecanvas_api.routes import runtime_mcp_broker as broker_route
    from vibecanvas_api.services.agent_runtime import (
        mcp_host_resolution,
    )

    upstream_request: dict[str, object] = {}

    async def upstream_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        header_blob = await reader.readuntil(b"\r\n\r\n")
        head, initial = header_blob.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            headers[name.casefold()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        body = bytearray(initial)
        if len(body) < content_length:
            body.extend(await reader.readexactly(content_length - len(body)))
        upstream_request.update(
            request_line=lines[0],
            headers=headers,
            body=bytes(body),
        )
        payload = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Mcp-Session-Id: upstream-session\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]

    async def allow_structural_destination(_connection):
        return {"mcp.test"}

    async def resolve_test_destination(value, **_kwargs):
        parts = urlsplit(value)
        return PublicUrlTarget(
            url=value,
            hostname=str(parts.hostname),
            port=int(parts.port or 80),
            addresses=("127.0.0.1",),
        )

    monkeypatch.setattr(
        mcp_host_resolution,
        "validate_mcp_connection_destination",
        allow_structural_destination,
    )
    monkeypatch.setattr(
        broker_route,
        "validate_public_http_url",
        resolve_test_destination,
    )

    async def successful_handshake(**_kwargs):
        return {
            "status": "ok",
            "tool_count": 0,
            "tool_names": [],
            "tools": [],
        }

    monkeypatch.setattr(
        mcp_servers_route,
        "handshake_one",
        successful_handshake,
    )

    dispatched_turns = []

    class FakeRuntimeOrchestrator:
        async def stream_turn(self, **kwargs):
            dispatched_turns.append(kwargs["turn_request"])
            yield ("NO_OP", {})

    monkeypatch.setattr(
        chats_route,
        "AgentRuntimeOrchestrator",
        FakeRuntimeOrchestrator,
    )

    register = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"mcp_broker_{uuid.uuid4().hex[:12]}@example.com",
            "username": "MCP Broker User",
            "password": "pw12345678",
        },
    )
    assert register.status_code in (200, 201), register.text
    browser_headers = {"Authorization": f"Bearer {register.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=browser_headers)).json()
    endpoint = f"http://mcp.test:{port}/rpc?configured=remote-query-secret"
    registration_suffix = uuid.uuid4().hex[:10]
    created = await client.post(
        "/api/v1/mcp-servers",
        headers=browser_headers,
        json={
            "name": f"Remote test {registration_suffix}",
            "tool_prefix": f"remote_{registration_suffix}",
            "transport": "streamable_http",
            "endpoint": endpoint,
            "auth_config": {
                "type": "bearer",
                "token": "remote-bearer-secret",
            },
            "connection_config": {
                "headers": {"X-Private-Key": "remote-header-secret"},
            },
        },
    )
    assert created.status_code == 201, created.text
    server_id = uuid.UUID(created.json()["id"])

    # Bearer/header/query credentials are all SecretService-backed. The
    # structural row consumed by Runtime descriptor code contains no secret.
    async with pg_engine.connect() as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT endpoint, auth_config, connection_config, "
                    "auth_secret_ref, connection_secret_ref "
                    "FROM mcp_servers WHERE id=:id"
                ),
                {"id": server_id},
            )
        ).mappings().one()
    assert "remote-query-secret" not in stored["endpoint"]
    assert "remote-bearer-secret" not in str(stored["auth_config"])
    assert "remote-header-secret" not in str(stored["connection_config"])
    assert stored["auth_secret_ref"] is not None
    assert stored["connection_secret_ref"] is not None

    scope_id = (
        await client.get("/api/v1/chats/bootstrap", headers=browser_headers)
    ).json()["carrier_scope_id"]
    project = await client.post("/api/v1/projects", headers=browser_headers, json={"name": "MCP broker"})
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    selection = await client.put(f"/api/v1/projects/{project_id}/mcp", headers=browser_headers,
                                 json={"mcp_server_ids": [str(server_id)], "mcp_config_revision": 0})
    assert selection.status_code == 200, selection.text
    sent = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/chat_mcp_broker/messages",
        json={
            "role": "user",
            "content": "use the selected MCP",
            "project_id": project_id,
        },
        headers=browser_headers,
    )
    assert sent.status_code == 200, sent.text
    assert len(dispatched_turns) == 1
    turn = dispatched_turns[0]
    custom = next(
        server for server in turn.mcp_host_servers if server.source == "custom"
    )
    capability = custom.connection["headers"]["Authorization"].removeprefix(
        "Bearer "
    )
    assert custom.connection["url"].endswith(
        f"/api/internal/runtime-mcp/v1/{server_id}"
    )
    for forbidden in (
        "remote-bearer-secret",
        "remote-header-secret",
        "remote-query-secret",
    ):
        assert forbidden not in repr(turn)

    # The fake Runtime completed immediately; reopen the Run so the broker can
    # exercise its live-Turn fence.
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": me["tenant_id"]},
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='running' WHERE run_id=:run_id"),
            {"run_id": turn.turn_id},
        )

    try:
        response = await client.post(
            f"/api/internal/runtime-mcp/v1/{server_id}?"
            "configured=attacker&runtime=1",
            headers={
                "Authorization": f"Bearer {capability}",
                "Content-Type": "application/json",
                "Mcp-Protocol-Version": "2025-06-18",
                "Cookie": "must-not-forward=1",
                "X-Forwarded-For": "10.0.0.1",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["mcp-session-id"] == "upstream-session"
        upstream_headers = upstream_request["headers"]
        assert isinstance(upstream_headers, dict)
        assert upstream_headers["authorization"] == "Bearer remote-bearer-secret"
        assert upstream_headers["x-private-key"] == "remote-header-secret"
        assert "cookie" not in upstream_headers
        assert "x-forwarded-for" not in upstream_headers
        assert capability not in repr(upstream_request)
        assert upstream_request["request_line"] == (
            "POST /rpc?configured=remote-query-secret&runtime=1 HTTP/1.1"
        )
    finally:
        upstream.close()
        await upstream.wait_closed()

    async with pg_engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET generation=generation+1 WHERE user_id=:user_id"),
            {"user_id": me["user_id"]},
        )
    denied = await client.post(
        f"/api/internal/runtime-mcp/v1/{server_id}",
        headers={"Authorization": f"Bearer {capability}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "runtime_mcp_session_revoked"
