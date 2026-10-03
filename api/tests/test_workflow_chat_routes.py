"""Canvas Chat resource creation on the shared Chat HTTP/storage path."""
import uuid
from datetime import datetime

import pytest

from vibecanvas_api.config import config
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


@pytest.fixture
def web_cookies(monkeypatch):
    monkeypatch.setattr(config, "environment", "test")
    monkeypatch.setattr(config, "web_session_cookie_secure", False)
    monkeypatch.setattr(config, "distributed_auth_rate_limit_enabled", False)
    monkeypatch.setattr(config, "web_session_cookie_enabled", True)
    monkeypatch.setattr(config.public_urls, "public_url", "")


async def setup(client):
    registered = await client.post("/api/v1/auth/register", headers={"Origin": "http://testserver"},
        json={"email": f"canvas-{uuid.uuid4().hex}@example.com", "username": "Canvas", "password": "pw12345678"})
    assert registered.status_code == 201, registered.text
    me_response = await client.get("/api/v1/auth/me")
    assert me_response.status_code == 200, me_response.text
    me = me_response.json()
    headers = {"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("vibecanvas-web-csrf")}
    wf_id = "wf-" + uuid.uuid4().hex[:12]
    graph = {"focus": {"node_id": "focus", "node_type": "CodeNode", "node_name": "Clean data",
                       "node_config": {"code": "return {}"}, "children": []}}
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        await WorkflowRepo(session, me["user_id"]).create_workflow(
            wf_id=wf_id, name="Canvas workflow", initial_workflow=graph)
    return me, headers, wf_id, graph


def payload(wf_id, sub=0):
    return {"workflow_context": {"workflow_id": wf_id, "major_version": 1,
            "initial_subversion": sub, "target": {"kind": "node", "node_id": "focus"}}}


@pytest.mark.asyncio
async def test_create_retry_history_metadata_and_hidden_project(client, web_cookies):
    me, headers, wf_id, graph = await setup(client)
    path = f"/api/v1/chat-scopes/{wf_id}/chats/canvas-history"
    created = await client.put(path, headers=headers, json=payload(wf_id))
    assert created.status_code == 200, created.text
    item = created.json()
    assert item["workflow_context"]["workflow_id"] == wf_id
    assert item["workflow_context"]["initial_subversion"] == 0
    assert "Clean data" in item["chat_context"]
    assert item["created_at"] and item["updated_at"]
    workspace = await client.get("/api/v1/chats/workspace", params={"chat_id": "canvas-history"})
    assert workspace.status_code == 200, workspace.text
    from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
    private_scope = project_workspace_scope_id(item["project_id"])
    assert private_scope != wf_id
    assert workspace.json()["workspace_scope_id"] == private_scope
    project_workspace = await client.get(f"/api/v1/projects/{item['project_id']}/workspace")
    assert project_workspace.status_code == 200, project_workspace.text
    assert project_workspace.json()["workspace_scope_id"] == private_scope
    from vibecanvas_api.services.sandbox.manager import SandboxManager
    manager = SandboxManager(max_resident=1, idle_ttl_s=10)
    assert await manager._workflow_chat_owner(me["tenant_id"], private_scope, me["user_id"]) == wf_id
    with pytest.raises((LookupError, PermissionError)):
        await manager._workflow_chat_owner(me["tenant_id"], private_scope, str(uuid.uuid4()))
    # The same Project identifier in another tenant cannot attach this workspace.
    with pytest.raises(LookupError):
        await manager._workflow_chat_owner(str(uuid.uuid4()), private_scope, me["user_id"])
    from sqlalchemy import text
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        rows = (await session.execute(text(
            "SELECT scope_id, path FROM vfs_artifacts WHERE path = '/chats/canvas-history/.keep'"
        ))).all()
        assert rows == [(private_scope, "/chats/canvas-history/.keep")]

    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        await WorkflowRepo(session, me["user_id"]).commit(wf_id, graph, target_major=1)
        assert await ChatProjectRepo(session, me["user_id"]).list() == []
    # A retried successful create stays idempotent even after the graph advances.
    replay = await client.put(path, headers=headers, json=payload(wf_id))
    assert replay.status_code == 200, replay.text
    assert replay.json()["project_id"] == item["project_id"]
    assert replay.json()["workflow_context"] == item["workflow_context"]
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        sessions = await ChatRepo(session, me["user_id"]).list_sessions(wf_id)
        assert len(sessions) == 1
        assert sessions[0]["workflow_context"]["initial_subversion"] == 0
    stale = await client.put(f"/api/v1/chat-scopes/{wf_id}/chats/stale", headers=headers, json=payload(wf_id))
    assert stale.status_code == 409, stale.text
    fresh = await client.put(f"/api/v1/chat-scopes/{wf_id}/chats/fresh", headers=headers, json=payload(wf_id, 1))
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["project_id"] == item["project_id"]
    rebound = await client.put(path, headers=headers, json=payload(wf_id, 1))
    assert rebound.status_code == 409


@pytest.mark.asyncio
async def test_rejects_wrong_workflow_missing_target_and_manual_project(client, web_cookies):
    _, headers, wf_id, _ = await setup(client)
    path = f"/api/v1/chat-scopes/{wf_id}/chats/canvas-invalid"
    wrong = await client.put(path, headers=headers, json=payload("other-workflow"))
    assert wrong.status_code == 422
    missing = payload(wf_id)
    missing["workflow_context"]["target"]["node_id"] = "deleted-node"
    response = await client.put(path, headers=headers, json=missing)
    assert response.status_code == 409
    response = await client.put(path, headers=headers, json={**payload(wf_id), "project_id": "arbitrary"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_concurrent_canvas_chats_share_internal_project(client, web_cookies):
    import asyncio
    me, headers, wf_id, _ = await setup(client)
    responses = await asyncio.gather(*(
        client.put(f"/api/v1/chat-scopes/{wf_id}/chats/parallel-{i}", headers=headers, json=payload(wf_id))
        for i in range(3)
    ))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert len({response.json()["project_id"] for response in responses}) == 1
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        sessions = await ChatRepo(session, me["user_id"]).list_sessions(wf_id)
        assert len(sessions) == 3
        assert {item["workflow_context"]["target"]["node_id"] for item in sessions} == {"focus"}


@pytest.mark.asyncio
async def test_history_spans_major_versions_but_not_other_workflows(client, web_cookies):
    me, headers, wf_id, graph = await setup(client)
    other_wf = "wf-" + uuid.uuid4().hex[:12]
    first = await client.put(f"/api/v1/chat-scopes/{wf_id}/chats/major-one",
                             headers=headers, json=payload(wf_id))
    assert first.status_code == 200, first.text
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        workflows = WorkflowRepo(session, me["user_id"])
        assert await workflows.new_version(wf_id, graph) == 2
        await workflows.create_workflow(wf_id=other_wf, name="Other", initial_workflow=graph)
    second_payload = payload(wf_id)
    second_payload["workflow_context"]["major_version"] = 2
    for scope, chat, body in [(wf_id, "major-two", second_payload),
                              (other_wf, "unrelated", payload(other_wf)),
                              (wf_id, "unsent", second_payload)]:
        response = await client.put(f"/api/v1/chat-scopes/{scope}/chats/{chat}", headers=headers, json=body)
        assert response.status_code == 200, response.text
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        repo = ChatRepo(session, me["user_id"])
        for chat in ["major-one", "major-two", "unrelated"]:
            await repo.persist_message(chat, {"message_id": str(uuid.uuid4()), "role": "user",
                                              "content": {"text": "Inspect this node"}})
        # Exercise the same authorized-ID intersection used by the History API.
        rows = await repo.list_authorized_sessions(wf_id,
            ["major-one", "major-two", "unrelated", "unsent"], surface="chat")
        assert {row["chat_id"] for row in rows} == {"major-one", "major-two"}
        assert {row["workflow_context"]["major_version"] for row in rows} == {1, 2}
        assert all(row["workflow_context"]["workflow_id"] == wf_id for row in rows)
        assert all(datetime.fromisoformat(row["last_message_at"]) for row in rows)
        binding = await repo.get_workflow_context("major-one")
        assert binding["major_version"] == 1
        assert binding["initial_subversion"] == 0
        assert binding["target"]["node_id"] == "focus"


@pytest.mark.asyncio
async def test_canvas_execution_records_selected_version_not_editable_metadata(client, web_cookies, monkeypatch):
    import vibecanvas_api.routes.executions as execution_routes
    me, headers, wf_id, graph = await setup(client)
    graph["__meta__"] = {"workflow_version": 99, "workflow_subversion": 99}
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        await WorkflowRepo(session, me["user_id"]).commit(wf_id, graph, target_major=1)
    observed = []

    async def sandbox(*args, **kwargs):
        observed.append((args[4], kwargs["workflow_version"]))
        yield "EXEC_UPDATE", {"status": "completed", "wf_id": wf_id}

    monkeypatch.setattr(execution_routes, "_produce_execution_sandbox", sandbox)
    response = await client.post(f"/api/v1/workflows/{wf_id}/executions", headers=headers, json={"input": {}})
    assert response.status_code == 200, response.text
    assert observed == [(graph, "v1.sv1")]
