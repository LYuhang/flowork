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


@pytest.mark.asyncio
async def test_workflow_delete_removes_linked_chats_and_internal_project(client, web_cookies, monkeypatch):
    from sqlalchemy import select
    from unittest.mock import AsyncMock
    from vibecanvas_api.storage.models import Chat, ChatMessage, ChatProject
    monkeypatch.setattr("vibecanvas_api.services.workflow_deletion.enqueue_background_job_in_transaction", AsyncMock())
    me, headers, wf_id, _ = await setup(client)
    cid = "delete-canvas-" + uuid.uuid4().hex
    made = await client.put(f"/api/v1/chat-scopes/{wf_id}/chats/{cid}", headers=headers, json=payload(wf_id))
    assert made.status_code == 200, made.text
    pid = made.json()["project_id"]
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        ordinary = await ChatProjectRepo(session, me["user_id"]).create(name="Unaffected")
        other = await ChatRepo(session, me["user_id"]).register_session("__chat", project_id=ordinary["project_id"])
        await ChatRepo(session, me["user_id"]).persist_message(cid, {"role": "user", "content": {"text": "Delete fixture", "parts": [{"type": "text", "text": "Delete fixture"}]}, "message_id": "deletion-msg"})
    deleted = await client.delete(f"/api/v1/workflows/{wf_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        assert (await session.get(Chat, cid)).deleted_at is not None
        assert (await session.get(ChatProject, pid)).deleted_at is not None
        assert (await session.execute(select(ChatMessage).where(ChatMessage.chat_id == cid))).scalars().all() == []
        assert (await session.get(Chat, other)).deleted_at is None
        assert (await session.get(ChatProject, ordinary["project_id"])).deleted_at is None


@pytest.mark.asyncio
async def test_active_canvas_conversation_blocks_workflow_delete(client, web_cookies):
    from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
    from vibecanvas_api.storage.models import Chat, Workflow
    me, headers, wf_id, _ = await setup(client)
    cid = "active-canvas-" + uuid.uuid4().hex
    made = await client.put(f"/api/v1/chat-scopes/{wf_id}/chats/{cid}", headers=headers, json=payload(wf_id))
    assert made.status_code == 200, made.text
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        await AgentRunsRepo(session).create(run_id=uuid.uuid4().hex, tenant_id=me["tenant_id"],
            chat_id=cid, creator_user_id=me["user_id"], client_request_id=uuid.uuid4().hex, input_snapshot={})
    deleted = await client.delete(f"/api/v1/workflows/{wf_id}", headers=headers)
    assert deleted.status_code == 409, deleted.text
    assert "active canvas conversations" in deleted.text
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        assert (await session.get(Workflow, wf_id)).deleted_at is None
        assert (await session.get(Chat, cid)).deleted_at is None


@pytest.mark.asyncio
async def test_canvas_run_uses_the_same_private_project_as_chat(client, web_cookies, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from vibecanvas_api.routes import executions
    from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
    from vibecanvas_api.services.workflow_run_source import WorkflowRunSource
    me, headers, wf_id, _ = await setup(client)
    made = await client.put(f'/api/v1/chat-scopes/{wf_id}/chats/run-chat-shared', headers=headers, json=payload(wf_id))
    assert made.status_code == 200, made.text
    acquire = AsyncMock(return_value=object())
    monkeypatch.setattr(executions, 'get_sandbox_manager', lambda: SimpleNamespace(get_session=acquire))
    await executions._acquire_canvas_execution_session(me['tenant_id'], me['user_id'], wf_id, me['tenant_id'])
    acquire.assert_awaited_once_with(
        me['tenant_id'], project_workspace_scope_id(made.json()['project_id']), user_id=me['user_id'],
        expose_run=True, expose_runtime=True,
        workflow_run_source=WorkflowRunSource(tenant_id=me['tenant_id'], workflow_id=wf_id),
    )

@pytest.mark.asyncio
async def test_workflow_sandbox_lifecycle_targets_private_chat_workspace(client, web_cookies, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from vibecanvas_api.routes import workflows
    from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
    from vibecanvas_api.services.workflow_run_source import WorkflowRunSource
    me, headers, wf_id, _ = await setup(client)
    runtime = SimpleNamespace(prewarm_fileops=AsyncMock())
    manager = SimpleNamespace(get_session=AsyncMock(return_value=runtime),
        status=AsyncMock(return_value={'status': 'running'}), close_session=AsyncMock())
    monkeypatch.setattr(workflows, 'get_sandbox_manager', lambda: manager)
    response = await client.get(f'/api/v1/workflows/{wf_id}/sandbox', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'idle'
    manager.status.assert_not_awaited()
    listed = await client.get('/api/v1/workflows/sandboxes', headers=headers, params={'wf_id': wf_id})
    assert listed.status_code == 200, listed.text
    assert [(item['wf_id'], item['status']) for item in listed.json()['items']] == [(wf_id, 'idle')]
    manager.status.assert_not_awaited()
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        assert await ChatProjectRepo(db, me['user_id']).find_for_workflow(wf_id) is None
    response = await client.post(f'/api/v1/workflows/{wf_id}/sandbox', headers=headers)
    assert response.status_code == 200, response.text
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        project = await ChatProjectRepo(db, me['user_id']).find_for_workflow(wf_id)
        scope = project_workspace_scope_id(project.project_id)
    manager.get_session.assert_awaited_once_with(me['tenant_id'], scope, user_id=me['user_id'],
        expose_run=True, expose_runtime=True,
        workflow_run_source=WorkflowRunSource(tenant_id=me['tenant_id'], workflow_id=wf_id))
    assert response.json()['workspace_scope_id'] == scope
    manager.status.assert_awaited_with(me['tenant_id'], scope)
    response = await client.delete(f'/api/v1/workflows/{wf_id}/sandbox', headers=headers)
    assert response.status_code == 200, response.text
    manager.close_session.assert_awaited_once_with(me['tenant_id'], scope)

@pytest.mark.asyncio
async def test_sandbox_status_keeps_shared_workflow_actors_separate(client, web_cookies, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from vibecanvas_api.routes import workflows
    from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
    from vibecanvas_api.storage.execution_repo import ExecutionRepo
    from vibecanvas_api.storage import stop_registry
    owner, _, wf_id, _ = await setup(client)
    other, _, _, _ = await setup(client)
    states = []
    for actor in (owner, other):
        execution = str(uuid.uuid4())
        async with session_scope(tenant_id=actor['tenant_id'], user_id=actor['user_id']) as db:
            project = await ChatProjectRepo(db, actor['user_id']).for_workflow(wf_id)
            await ExecutionRepo(db, actor['user_id']).start_execution(wf_id, 'v1.sv0', execution,
                workflow_tenant_id=owner['tenant_id'])
            states.append((actor, execution, project_workspace_scope_id(project.project_id)))
    status = AsyncMock(return_value={'status': 'running'})
    monkeypatch.setattr(workflows, 'get_sandbox_manager', lambda: SimpleNamespace(status=status))
    try:
        # Even with the outer DB admitted to the owner, state resolution uses
        # the caller's private organization and identity.
        async with session_scope(tenant_id=owner['tenant_id']) as source_db:
            for actor, execution, scope in states:
                observed = await workflows._workflow_sandbox_status_payload(session=source_db,
                    tenant_id=actor['tenant_id'], user_id=actor['user_id'], wf_id=wf_id)
                assert observed['active_execution_ids'] == [execution]
                assert observed['workspace_scope_id'] == scope
                status.assert_awaited_with(actor['tenant_id'], scope)
        assert states[0][2] != states[1][2]
    finally:
        for _, execution, _ in states:
            stop_registry.discard(execution)
