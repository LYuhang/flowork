import json

import httpx
import pytest
from jsonschema import ValidationError

from vibecanvas_api.agents.tools.subagent import resources


def test_selected_dependencies_cannot_be_silently_ignored_or_expanded():
    config = {"skills": [{"id": "one", "name": "Audit"}]}
    with pytest.raises(RuntimeError, match="snapshot is missing"):
        resources.resolved_node_resources("worker", config, {})
    snapshot = {"skills": [{"id": "one", "name": "Audit", "root_path": "/skills/one/version"}], "mcp_servers": []}
    extra = {"workflow_resources": {"nodes": {"worker": snapshot}}}
    assert resources.resolved_node_resources("worker", config, extra) == snapshot
    prompt = resources.skill_instructions(snapshot)
    assert '/skills/one/version/SKILL.md' in prompt
    snapshot["skills"].append({"id": "two", "name": "Unselected"})
    with pytest.raises(RuntimeError, match="does not match"):
        resources.resolved_node_resources("worker", config, extra)


@pytest.mark.asyncio
async def test_mcp_tools_have_namespaces_validate_arguments_and_use_broker_only(monkeypatch):
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "found"}],
                                                  "structuredContent": {"count": 1}}})
    factory = httpx.AsyncClient
    clients = []
    def client(**kwargs):
        value = factory(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(value)
        return value
    monkeypatch.setattr(resources.httpx, "AsyncClient", client)
    definition = {"name": "lookup", "description": "Look up order", "input_schema": {
        "type": "object", "properties": {"order": {"type": "integer"}},
        "required": ["order"], "additionalProperties": False}}
    snapshot = {"mcp_servers": [{"id": identifier, "name": identifier, "tools": [definition],
        "broker_url": "https://broker.test/mcp/" + identifier, "capability": "scoped-token"} for identifier in ("one", "two")]}
    async with resources.resource_tools(snapshot) as tools:
        assert tools[0].name != tools[1].name
        assert all(len(tool.name) <= 64 for tool in tools)
        assert "scoped-token" not in str([tool.args_schema for tool in tools])
        with pytest.raises(ValidationError):
            await tools[0].ainvoke({"order": "invalid"})
        assert requests == []
        output = await tools[0].ainvoke({"order": 5})
        assert output[0] == {"type": "text", "text": "found"}
        assert json.loads(requests[0].content) == {"tool_name": "lookup", "arguments": {"order": 5}}
        assert requests[0].headers["authorization"] == "Bearer scoped-token"
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_resource_error_is_not_retried_and_closes_connection(monkeypatch):
    requests = []
    factory = httpx.AsyncClient
    clients = []
    def handler(request):
        requests.append(request)
        return httpx.Response(403, json={"detail": "private backend error"})
    def client(**kwargs):
        value = factory(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(value)
        return value
    monkeypatch.setattr(resources.httpx, "AsyncClient", client)
    snapshot = {"mcp_servers": [{"id": "one", "name": "One", "tools": [{"name": "write"}],
                                  "broker_url": "https://broker.test/call", "capability": "scoped-token"}]}
    with pytest.raises(RuntimeError, match="HTTP 403") as caught:
        async with resources.resource_tools(snapshot) as tools:
            await tools[0].ainvoke({})
    assert "private" not in str(caught.value)
    assert len(requests) == 1
    assert clients[0].is_closed
