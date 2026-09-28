"""Project storage ownership and persistent per-thread working directories."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.services.chat_workspace import chat_working_directory, project_workspace_scope_id
from vibecanvas_api.services.sandbox.manager import SandboxSession, _hydrate_run_folders
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import Chat


async def _user(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": f"project-{uuid.uuid4().hex}@example.com",
        "username": "Project tester", "password": "pw12345678",
    })
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['session_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()


async def _project(client, headers):
    response = await client.post("/api/v1/projects", headers=headers, json={"name": "Research"})
    assert response.status_code == 201, response.text
    return response.json()["project_id"]


@pytest.mark.asyncio
async def test_project_creates_durable_roots_before_any_chat_or_sandbox(client):
    headers, _me = await _user(client)
    project_id = await _project(client, headers)
    response = await client.get("/api/v1/storage/list", headers=headers,
                                params={"path": f"/project/{project_id}"})
    assert response.status_code == 200, response.text
    assert {item["name"] for item in response.json()["items"]} == {"data", "memory", "logs", "chats"}
    async with session_scope(tenant_id=_me["tenant_id"], user_id=_me["user_id"]) as session:
        scope = project_workspace_scope_id(project_id)
        rows = (await session.execute(text(
            "SELECT path FROM vfs_artifacts WHERE scope_id=:scope "
            "UNION SELECT path FROM vfs_scratch WHERE scope_id=:scope"
        ), {"scope": scope})).scalars().all()
        assert set(rows) == {"/data/.keep", "/logs/.keep", "/memory/.keep", "/chats/.keep"}
    other_headers, _ = await _user(client)
    denied = await client.get("/api/v1/storage/list", headers=other_headers,
                              params={"path": f"/project/{project_id}"})
    assert denied.status_code == 404
    retired = await client.get("/api/v1/storage/list", headers=headers, params={"path": "/chat"})
    assert retired.status_code == 404


@pytest.mark.asyncio
async def test_platform_binding_requires_user_rls_scope_and_active_project(client):
    from vibecanvas_api.storage.chat_repo import ChatRepo

    headers, me = await _user(client)
    project_id = await _project(client, headers)
    carrier = (await client.get("/api/v1/chats/bootstrap", headers=headers)).json()["carrier_scope_id"]
    chat_id = "binding_" + uuid.uuid4().hex
    response = await client.put(f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}",
        headers=headers, json={"project_id": project_id})
    assert response.status_code == 200
    # The old tenant-only write transaction could read Chat but its Project
    # was hidden by owner RLS, resulting in an AttributeError during a write.
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        assert await ChatRepo(session, me["user_id"]).get_platform_context_binding(
            chat_id, for_update=True,
        ) is None
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        binding = await ChatRepo(session, me["user_id"]).get_platform_context_binding(
            chat_id, for_update=True,
        )
        assert binding["project_id"] == project_id
        assert binding["runtime_session_id"]
        await session.execute(text("UPDATE chat_projects SET deleted_at=now() WHERE project_id=:id"),
                              {"id": project_id})
        assert await ChatRepo(session, me["user_id"]).get_platform_context_binding(chat_id) is None


@pytest.mark.asyncio
async def test_explicit_chat_creation_persists_directory_and_is_idempotent(client, monkeypatch):
    from vibecanvas_api.routes import chats as routes

    def no_sandbox():
        pytest.fail("Creating a Chat must not start or inspect a sandbox")

    monkeypatch.setattr(routes, "get_sandbox_manager", no_sandbox)
    headers, me = await _user(client)
    project_id = await _project(client, headers)
    carrier = (await client.get("/api/v1/chats/bootstrap", headers=headers)).json()["carrier_scope_id"]
    chat_id = "new_" + uuid.uuid4().hex
    url = f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}"
    for _ in range(2):
        response = await client.put(url, headers=headers, json={"project_id": project_id})
        assert response.status_code == 200, response.text
        assert response.json()["project_id"] == project_id
        assert response.json()["chat_context"] == "New chat"
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        chat = await session.get(Chat, chat_id)
        assert chat.runtime_state_ref is None
        rows = (await session.execute(text(
            "SELECT path FROM vfs_artifacts WHERE scope_id=:scope AND path=:path"
        ), {"scope": project_workspace_scope_id(project_id), "path": f"/chats/{chat_id}/.keep"})).scalars().all()
        assert rows == [f"/chats/{chat_id}/.keep"]
    listed = await client.get(f"/api/v1/chat-scopes/{carrier}/chats", headers=headers)
    assert listed.json()["items"] == []  # Allocated cwd is not a started conversation.
    other_project = await _project(client, headers)
    mismatch = await client.put(url, headers=headers, json={"project_id": other_project})
    assert mismatch.status_code == 404
    browser_carrier = (await client.get("/api/v1/chats/bootstrap", headers=headers,
                                       params={"surface": "browser"})).json()["carrier_scope_id"]
    browser_url = f"/api/v1/chat-scopes/{browser_carrier}/chats/browser_{uuid.uuid4().hex}"
    browser = await client.put(browser_url, headers=headers, json={})
    assert browser.status_code == 200, browser.text
    retried = await client.put(browser_url, headers=headers, json={})
    assert retried.json()["project_id"] == browser.json()["project_id"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_codex_cli")
async def test_project_mcp_settings_are_shared_by_new_threads_and_not_written_by_send(client, monkeypatch):
    from vibecanvas_api.routes import chats as routes
    from vibecanvas_api.storage.models import ProjectMcpBinding
    from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo
    from sqlalchemy import select

    requests = []

    class Runtime:
        async def stream_turn(self, **kwargs):
            requests.append(kwargs["turn_request"])
            yield ("NO_OP", {})

    monkeypatch.setattr(routes, "AgentRuntimeOrchestrator", Runtime)
    headers, me = await _user(client)
    project_id = await _project(client, headers)
    other_project = await _project(client, headers)
    carrier = (await client.get("/api/v1/chats/bootstrap", headers=headers)).json()["carrier_scope_id"]
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        server_id = await McpServersRepo(session).insert(
            tenant_id=uuid.UUID(me["tenant_id"]), user_id=uuid.UUID(me["user_id"]),
            name="Project tools", tool_prefix="project_tools", transport="stdio", endpoint="python",
            description="Shared test tools", auth_config={}, connection_config={"args": ["-m", "test_tools"]},
        )
    url = f"/api/v1/projects/{project_id}/mcp"
    assert (await client.get(url, headers=headers)).json() == {
        "mcp_server_ids": [], "mcp_config_revision": 0,
    }
    for _ in range(2):
        saved = await client.put(url, headers=headers, json={
            "mcp_server_ids": [str(server_id), str(server_id)], "mcp_config_revision": 0,
        })
        assert saved.status_code == 200, saved.text
        assert saved.json() == {"mcp_server_ids": [str(server_id)], "mcp_config_revision": 1}
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        assert len((await session.execute(select(ProjectMcpBinding))).scalars().all()) == 1
    untouched = await client.get(f"/api/v1/projects/{other_project}/mcp", headers=headers)
    assert untouched.json() == {"mcp_server_ids": [], "mcp_config_revision": 0}

    foreign_headers, foreign = await _user(client)
    assert (await client.get(url, headers=foreign_headers)).status_code == 404
    assert (await client.put(url, headers=foreign_headers, json={"mcp_server_ids": []})).status_code == 404
    # Even a session scoped to the owner's tenant cannot read another user's
    # Project binding. Tenant isolation alone is not sufficient here.
    async with session_scope(tenant_id=me["tenant_id"], user_id=foreign["user_id"]) as session:
        assert (await session.execute(select(ProjectMcpBinding))).scalars().all() == []
    conflict = await client.put(url, headers=headers, json={"mcp_server_ids": [], "mcp_config_revision": 0})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "mcp_config_revision_conflict"
    unavailable = await client.put(url, headers=headers, json={
        "mcp_server_ids": [str(uuid.uuid4())], "mcp_config_revision": 1,
    })
    assert unavailable.status_code == 409

    for chat_id in ("mcp_first_thread", "mcp_sibling_thread"):
        created = await client.put(f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}", headers=headers,
                                   json={"project_id": project_id})
        assert created.status_code == 200, created.text
        sent = await client.post(f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}/messages", headers=headers,
                                 json={"role": "user", "content": "hello"})
        assert sent.status_code == 200, sent.text
    assert len(requests) == 2
    assert requests[0].runtime_session_id == requests[1].runtime_session_id
    assert requests[0].chat_id != requests[1].chat_id
    for request in requests:
        assert request.mcp_config_revision == 1
        assert [s.server_id for s in request.mcp_host_servers if s.source == "custom"] == [str(server_id)]
    assert (await client.get(url, headers=headers)).json() == saved.json()
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        await McpServersRepo(session).soft_delete(server_id)
    assert (await client.get(url, headers=headers)).json() == {
        "mcp_server_ids": [], "mcp_config_revision": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_files", [False, True])
async def test_chat_files_write_back_and_restore_with_the_project(client, tmp_path, delete_files):
    headers, me = await _user(client)
    project_id = await _project(client, headers)
    scope_response = await client.get("/api/v1/chats/bootstrap", headers=headers)
    assert scope_response.status_code == 200, scope_response.text
    carrier = scope_response.json()["carrier_scope_id"]
    chat_id = "chat_" + uuid.uuid4().hex
    response = await client.post(f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}/attachments",
                                headers=headers, params={"project_id": project_id},
                                files={"file": ("input.txt", b"source", "text/plain")})
    assert response.status_code in (200, 201), response.text
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        chat = await session.get(Chat, chat_id)
        assert chat.project_id == project_id
        assert "runtime_session_id" not in Chat.__table__.columns
        assert "runtime_connection_id" not in Chat.__table__.columns

    # Exercise the actual hydration/writeback functions with fresh host mounts.
    scope = project_workspace_scope_id(project_id)
    first = tmp_path / "first-mount"
    await _hydrate_run_folders(str(first), scope, me["tenant_id"])
    workdir = first / chat_working_directory(chat_id).lstrip("/")
    assert (workdir / ".keep").is_file()
    assert next((workdir / "attachments").glob("*_input.txt")).read_bytes() == b"source"
    (workdir / "result.txt").write_text("durable result")
    sandbox = SandboxSession.__new__(SandboxSession)
    sandbox.run_dir = str(first)
    sandbox.wf_id = scope
    sandbox.tenant_id = me["tenant_id"]
    sandbox._external_vfs_lock = asyncio.Lock()
    sandbox._external_vfs_fenced_paths = set()
    assert await sandbox._sync_run_folder("chats") >= 3
    restored = tmp_path / "new-mount"
    await _hydrate_run_folders(str(restored), scope, me["tenant_id"])
    assert (restored / "chats" / chat_id / "result.txt").read_text() == "durable result"
    read = await client.get("/api/v1/storage/content", headers=headers,
                            params={"path": f"/project/{project_id}/chats/{chat_id}/result.txt"})
    assert read.status_code == 200, read.text
    assert read.json()["content"] == "durable result"
    file_ref = {"schemaVersion": 1, "scope": "project", "projectId": project_id,
                "path": f"/chats/{chat_id}/result.txt"}
    preview = await client.post("/api/v1/previews/resolve", headers=headers, json={"fileRef": file_ref})
    assert preview.status_code == 200, preview.text
    deleted = await client.delete(f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}", headers=headers,
                                   params={"delete_files": str(delete_files).lower()})
    assert deleted.status_code == 200, deleted.text
    preview = await client.post("/api/v1/previews/resolve", headers=headers, json={"fileRef": file_ref})
    assert preview.status_code == (404 if delete_files else 200), preview.text
    roots = await client.get("/api/v1/storage/list", headers=headers,
                             params={"path": f"/project/{project_id}"})
    assert roots.status_code == 200
    assert {row["name"] for row in roots.json()["items"]} == {"data", "memory", "logs", "chats"}


@pytest.mark.asyncio
async def test_browser_chat_automatically_creates_one_project_and_reuses_it(client):
    headers, me = await _user(client)
    bootstrap = await client.get("/api/v1/chats/bootstrap", headers=headers, params={"surface": "browser"})
    carrier = bootstrap.json()["carrier_scope_id"]
    projects = []
    for _ in range(2):
        chat_id = "browser_" + uuid.uuid4().hex
        for index in range(2):
            upload = await client.post(f"/api/v1/chat-scopes/{carrier}/chats/{chat_id}/attachments",
                                       headers=headers,
                                       files={"file": (f"input-{index}.txt", b"test", "text/plain")})
            assert upload.status_code in (200, 201), upload.text
        async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
            chat = await session.get(Chat, chat_id)
            assert chat.project.surface == "browser"
            projects.append(chat.project_id)
    assert len(set(projects)) == 2


@pytest.mark.parametrize("value", ["", "../escape", "a/b", "a\\b", ".", "..", "a\x00b"])
def test_working_directory_rejects_path_traversal(value):
    with pytest.raises(ValueError, match="invalid chat_id"):
        chat_working_directory(value)
