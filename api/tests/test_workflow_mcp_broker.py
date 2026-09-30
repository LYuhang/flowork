from contextlib import asynccontextmanager
import hashlib
import json
import os
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from vibecanvas_api.routes import workflow_mcp_broker as broker
from vibecanvas_api.services.sandbox.mcp_probe_entry import _probe


@pytest.mark.asyncio
async def test_broker_checks_access_and_tool_contract_before_resolving_credentials(monkeypatch):
    identifier = str(uuid4())
    tools = [{"name": "add", "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}}]
    fingerprint = hashlib.sha256(json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    capability = SimpleNamespace(server_id=identifier, organization_id="org", tools_fingerprint=fingerprint)
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=False)))
    repo = SimpleNamespace(get=AsyncMock(return_value={"enabled": True, "connection_status": "not_required", "last_tool_names": tools}))
    hydrate = AsyncMock(return_value={"private": "secret"})
    monkeypatch.setattr(broker, "McpServersRepo", lambda session: repo)
    monkeypatch.setattr(broker, "hydrate_connection_credentials", hydrate)
    kwargs = dict(session=object(), service=service, principal=object(), authz_context=object(), capability=capability)
    with pytest.raises(HTTPException) as denied:
        await broker.resolve_call(**kwargs, body={"tool_name": "add", "arguments": {"a": 1}})
    assert denied.value.status_code == 403
    repo.get.assert_not_awaited()
    hydrate.assert_not_awaited()
    service.check.return_value = SimpleNamespace(allowed=True)
    for body, status in [({"tool_name": "delete", "arguments": {}}, 403),
                         ({"tool_name": "add", "arguments": {"a": "bad"}}, 422)]:
        with pytest.raises(HTTPException) as caught:
            await broker.resolve_call(**kwargs, body=body)
        assert caught.value.status_code == status
        hydrate.assert_not_awaited()
    capability.tools_fingerprint = "b" * 64
    with pytest.raises(HTTPException) as changed:
        await broker.resolve_call(**kwargs, body={"tool_name": "add", "arguments": {"a": 1}})
    assert changed.value.status_code == 409
    hydrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolated_mcp_worker_rechecks_live_schema_before_side_effects(monkeypatch):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="add", inputSchema=schema)])),
        call_tool=AsyncMock(return_value={"content": [{"type": "text", "text": "2"}]}))
    @asynccontextmanager
    async def connect(connection):
        yield session
    monkeypatch.setattr("vibecanvas_api.services.sandbox.mcp_client.mcp_client_session", connect)
    request = {"action": "call", "connection": {"transport": "stdio"}, "tool_name": "add",
               "arguments": {"a": 2}, "input_schema": schema}
    assert (await _probe(request))["result"]["content"][0]["text"] == "2"
    session.call_tool.reset_mock()
    with pytest.raises(ValueError, match="schema changed"):
        await _probe({**request, "input_schema": {"type": "object"}})
    session.call_tool.assert_not_awaited()


@pytest.mark.skipif(os.environ.get("FLOWORK_TEST_SKILL_MOUNT") != "1" or not shutil.which("bwrap"),
                    reason="explicit native Bubblewrap MCP invocation check")
def test_real_stdio_mcp_tool_executes_in_isolated_sandbox():
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    source = "from mcp.server.fastmcp import FastMCP\nmcp=FastMCP('test')\n@mcp.tool()\ndef add(a:int,b:int)->int:\n return a+b\nmcp.run()\n"
    provider = BubblewrapProvider(shutil.which("bwrap"))
    connection = {"transport": "stdio", "command": sys.executable, "args": ["-c", source]}
    manifest = provider.run_mcp_probe(request={"connection": connection, "timeout_s": 20}, timeout=20, allow_hosts=set())
    assert manifest["status"] == "ok", manifest
    tool = next(item for item in manifest["tool_names"] if item["name"] == "add")
    result = provider.run_mcp_probe(request={"action": "call", "connection": connection, "timeout_s": 20,
        "tool_name": "add", "input_schema": tool["input_schema"], "arguments": {"a": 17, "b": 25}},
        timeout=20, allow_hosts=set())
    assert result["status"] == "ok", result
    assert result["result"]["content"][0]["text"] == "42"


@pytest.mark.asyncio
async def test_service_account_revocation_fences_stale_graph_allow(monkeypatch):
    from vibecanvas_api.authorization.types import PrincipalRef, PrincipalType
    from vibecanvas_api.storage import repo_service_accounts
    account = PrincipalRef(PrincipalType.SERVICE_ACCOUNT, str(uuid4()))
    monkeypatch.setattr(repo_service_accounts, "ServiceAccountsRepo", lambda session:
        SimpleNamespace(resource_refs=AsyncMock(return_value=())))
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    hydrate = AsyncMock()
    monkeypatch.setattr(broker, "hydrate_connection_credentials", hydrate)
    with pytest.raises(HTTPException) as denied:
        await broker.resolve_call(session=object(), service=service, principal=account,
            authz_context=object(), capability=SimpleNamespace(server_id=str(uuid4())),
            body={"tool_name": "add", "arguments": {}})
    assert denied.value.status_code == 403
    service.check.assert_not_awaited()
    hydrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_call_resolves_rotated_credentials_and_rejects_expired_auth(monkeypatch):
    tools = [{'name': 'add', 'input_schema': {'type': 'object'}}]
    fingerprint = hashlib.sha256(json.dumps(tools, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    capability = SimpleNamespace(server_id=str(uuid4()), tools_fingerprint=fingerprint)
    row = {'last_tool_names': tools}
    monkeypatch.setattr(broker, 'authorize_selected_server', AsyncMock(return_value=row))
    hydrate = AsyncMock(side_effect=[{'auth_config': {'token': 'first'}},
                                    {'auth_config': {'token': 'rotated'}},
                                    {'auth_config': {'token': 'expired'}}])
    monkeypatch.setattr(broker, 'hydrate_connection_credentials', hydrate)
    monkeypatch.setattr(broker, 'resolve_oauth_auth_config', AsyncMock(side_effect=[{'token': 'first'}, {'token': 'rotated'}, None]))
    monkeypatch.setattr(broker, 'server_descriptor', lambda hydrated: {'connection': hydrated})
    destination = AsyncMock(return_value=[])
    monkeypatch.setattr(broker, 'validate_mcp_connection_destination', destination)
    kwargs = dict(session=object(), service=object(), principal=object(), authz_context=object(),
                  capability=capability, body={'tool_name': 'add', 'arguments': {}})
    first, _ = await broker.resolve_call(**kwargs)
    second, _ = await broker.resolve_call(**kwargs)
    assert first['connection']['auth_config']['token'] == 'first'
    assert second['connection']['auth_config']['token'] == 'rotated'
    with pytest.raises(HTTPException) as expired:
        await broker.resolve_call(**kwargs)
    assert expired.value.status_code == 403
    assert expired.value.detail['code'] == 'workflow_mcp_authorization_required'
    assert hydrate.await_count == 3
    assert destination.await_count == 2
