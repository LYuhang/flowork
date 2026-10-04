from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from vibecanvas_api.flowork_cli import cli, resource_cli
from vibecanvas_api.services.agent_runtime import cli_resources as host


def test_discovery_commands_are_read_only_and_latest(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "request", lambda endpoint, args, **kw: calls.append((args, kw)) or {"items": []})
    identifier = str(uuid4())
    assert cli.main(["skill", "get", "--skill-id", identifier], socket_path="test") == 0
    assert calls[-1] == ({"skill_id": identifier}, {"operation": "skill.get"})
    assert cli.main(["skill", "get", "--skill-id", identifier, "--revision_hash", "a" * 64], socket_path="test") == 2
    assert cli.main(["mcp", "tools", "--server-id", identifier], socket_path="test") == 0
    assert resource_cli.OPERATIONS <= cli.READ_OPERATIONS
    assert not resource_cli.OPERATIONS & cli.WRITE_OPERATIONS
    capsys.readouterr()


@pytest.mark.parametrize("operation,args", [
    ("skill.read", {"skill_id": str(uuid4()), "path": "../secret"}),
    ("skill.read", {"skill_id": str(uuid4()), "path": "/etc/passwd"}),
    ("skill.read", {"skill_id": str(uuid4()), "limit": 64001}),
    ("skill.list", {"offset": True}),
    ("mcp.list", {"limit": 0}),
    ("mcp.tools", {"server_id": str(uuid4()), "token": "secret"}),
    ("skill.get", {"skill_id": str(uuid4()), "revision_hash": "a" * 64}),
])
def test_untrusted_discovery_arguments(operation, args):
    with pytest.raises(ValueError):
        cli.validate_arguments(operation, args)


def setup_host(monkeypatch):
    @asynccontextmanager
    async def session_scope(**kwargs):
        yield object()
    monkeypatch.setattr(host, "session_scope", session_scope)
    monkeypatch.setattr(host, "resource_route_params", lambda context, session: {"session": session})
    monkeypatch.setattr(host, "admitted_resource_route_params", AsyncMock(return_value={}))
    monkeypatch.setattr(host, "shared_resource_cards", AsyncMock(return_value=[]))
    return SimpleNamespace(tenant_id="test", username=str(uuid4()), chat_id="test-chat")


@pytest.mark.asyncio
async def test_mcp_discovery_drops_connection_secrets_and_checks_use(monkeypatch):
    context = setup_host(monkeypatch)
    authorize = AsyncMock()
    monkeypatch.setattr(host.mcp_servers, "_authorize_mcp", authorize)
    identifier = str(uuid4())
    repo = SimpleNamespace(get=AsyncMock(return_value={"id": identifier, "name": "Orders",
        "endpoint": "https://example.test/?token=SECRET", "auth_config": {"token": "SECRET"},
        "connection_config": {"env": {"SECRET": "value"}},
        "last_tool_names": [{"name": "lookup", "input_schema": {"type": "object"}}]}))
    monkeypatch.setattr(host, "McpServersRepo", lambda session: repo)
    result = await host.read(context, "mcp.tools", {"server_id": identifier})
    assert result["id"] == identifier
    assert result["tools"][0]["name"] == "lookup"
    assert "SECRET" not in str(result)
    assert authorize.await_args.kwargs["action"] == host.Action.USE
    authorize.side_effect = HTTPException(404, "mcp server not found")
    repo.get.reset_mock()
    assert (await host.read(context, "mcp.get", {"server_id": identifier}))["error"] == "resource_unavailable"
    repo.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_skill_metadata_reports_latest_publication_and_runtime_location(monkeypatch):
    context = setup_host(monkeypatch)
    monkeypatch.setattr(host.skills, "_authorize_skill", AsyncMock())
    monkeypatch.setattr(host.skills, "_authorize_skill_revision", AsyncMock())
    identifier = str(uuid4())
    repo = SimpleNamespace(
        get=AsyncMock(side_effect=[{"skill_id": identifier, "name": "Audit", "installed": True, "revision_hash": h} for h in ("a" * 64, "b" * 64)]),
        list_revisions=AsyncMock(return_value=[{"revision_id": "r1", "revision_hash": "a" * 64, "version": 1, "is_latest": False},
                                                {"revision_id": "r2", "revision_hash": "b" * 64, "version": 2, "is_latest": True}]),
        read_revision_files=AsyncMock(side_effect=[[('SKILL.md', 'text/markdown', b'First')], [('SKILL.md', 'text/markdown', b'Second')]]))
    monkeypatch.setattr(host, "SkillsRepo", lambda session: repo)
    first = await host.read(context, "skill.get", {"skill_id": identifier})
    second = await host.read(context, "skill.get", {"skill_id": identifier})
    assert (first["version"], second["version"]) == (1, 2)
    assert (first["revision_hash"], second["revision_hash"]) == ("a" * 64, "b" * 64)
    repo.read_revision_files.assert_not_awaited()
    assert "files" not in first and "content" not in first
    from vibecanvas_api.services.runtime_skills import runtime_skill_root
    assert first["runtime_path"] == second["runtime_path"] == runtime_skill_root("test-chat", identifier)
    assert first["entrypoint"] == first["runtime_path"] + "/SKILL.md"
    assert "refresh" in first["runtime_hint"]
    context.chat_id = "other-chat"
    assert host.runtime_location(context, identifier)["runtime_path"] != first["runtime_path"]


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_skill_list_includes_runtime_paths_without_reading_files(monkeypatch, installed):
    context = setup_host(monkeypatch)
    identifier = str(uuid4())
    monkeypatch.setattr(host.skills, "list_skills", AsyncMock(return_value={"items": [
        {"id": identifier, "name": "Visible", "access": {"capabilities": ["use"]}},
        {"id": str(uuid4()), "name": "Not usable", "access": {"capabilities": ["view"]}},
    ]}))
    from vibecanvas_api.storage import repo_skill_installations
    monkeypatch.setattr(repo_skill_installations.SkillInstallationsRepo, "installed_ids",
                        AsyncMock(return_value={identifier} if installed else set()))
    result = await host.read(context, "skill.list", {})
    assert result["total"] == 1
    assert result["items"][0]["installed"] is installed
    if installed:
        assert result["items"][0]["runtime_path"].endswith("/" + identifier)
    else:
        assert "runtime_path" not in result["items"][0]
    assert "skill refresh" in result["runtime_hint"]
