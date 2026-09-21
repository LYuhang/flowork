"""Chats router smoke. Full SSE streaming gate lives in T17.

Auth: the legacy ``VIBECANVAS_API_DEV_TOKEN`` + sync ``TestClient`` +
``Bearer tok`` harness is DEAD (dev-token auth was removed from the app).
These route-contract tests now use the conftest async ``client`` fixture +
a real ``register → session_token`` (the same pattern as
``test_routes_workflows.py`` / ``test_routes_executions.py``).
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text, update

from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.background_jobs_repo import BackgroundJobsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo
from vibecanvas_api.storage.models import Chat
from vibecanvas_api.services.chat_workspace import chat_workspace_scope_id


async def _register(client) -> str:
    """Register a fresh user, return its bearer session token. Email is
    unique per call (uuid) so it never collides with another test's row."""
    email = f"chat_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_encrypted_chat(
    me: dict,
    *,
    scope_id: str,
    chat_id: str,
    name: str,
    surface: str,
    state: dict | None = None,
) -> None:
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        await ChatRepo(session, me["user_id"]).register_session(
            scope_id,
            name=name,
            chat_id=chat_id,
            surface=surface,
        )
        if state:
            await session.execute(
                update(Chat).where(Chat.chat_id == chat_id).values(**state)
            )


@pytest.mark.asyncio
async def test_post_message_404_unknown_workflow(client, pg_engine):
    tok = await _register(client)
    r = await client.post(
        "/api/v1/chat-scopes/no_such/chats/c1/messages",
        json={"role": "user", "content": "hi"}, headers=_hdr(tok),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_message_404_for_unowned_internal_scope(client, pg_engine):
    tok = await _register(client)
    r = await client.post(
        "/api/v1/chat-scopes/__chatws_someone_else_c1/chats/c1/messages",
        json={"role": "user", "content": "hi"},
        headers=_hdr(tok),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_resume_404_for_unknown_turn(client, pg_engine):
    tok = await _register(client)
    r = await client.get("/api/v1/chats/c1/turns/t_unknown/resume",
                         headers=_hdr(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cancel_404_for_unknown_turn(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/chats/c1/turns/t_unknown/cancel",
                          headers=_hdr(tok))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_history_for_unknown_chat_returns_404(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/workflows", json={"name": "wf"},
                          headers=_hdr(tok))
    wf_id = r.json()["wf_id"]
    r = await client.get(
        f"/api/v1/chat-scopes/{wf_id}/chats/nope/messages", headers=_hdr(tok),
    )
    # §5.4 gate (chats.py): the chat_id is resolved through the RLS-protected
    # `chats` table FIRST, so an unknown (or cross-tenant) chat → 404 before
    # the checkpoint read is ever reached. (The pre-gate behavior of "no
    # checkpoint → empty 200" is no longer the contract.)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_chats_for_workflow_empty(client, pg_engine):
    tok = await _register(client)
    r = await client.post("/api/v1/workflows", json={"name": "wf"},
                          headers=_hdr(tok))
    wf_id = r.json()["wf_id"]
    r = await client.get(f"/api/v1/chat-scopes/{wf_id}/chats",
                         headers=_hdr(tok))
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_browser_bootstrap_returns_browser_surface(client, pg_engine):
    tok = await _register(client)
    r = await client.get("/api/v1/chats/bootstrap?surface=browser",
                         headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["surface"] == "browser"
    assert "/browser" in {f"/{c}" for c in body["available_commands"]}


@pytest.mark.asyncio
async def test_chat_workspace_scopes_do_not_create_workflow_rows(client, pg_engine):
    tok = await _register(client)
    headers = _hdr(tok)

    r = await client.get("/api/v1/chats/bootstrap", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["carrier_scope_id"].startswith("__chat_")

    r = await client.get("/api/v1/chats/workspace?chat_id=c1", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["workspace_scope_id"].startswith("__chatws_")

    r = await client.get("/api/v1/workflows", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_chat_attachment_upload_is_durable_vfs_metadata(client, pg_engine):
    tok = await _register(client)
    headers = _hdr(tok)
    boot = await client.get("/api/v1/chats/bootstrap", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]

    uploaded = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/c_attachment/attachments",
        params={"attachment_type": "image"},
        files={"file": ("photo.png", b"png-bytes", "image/png")},
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert body["type"] == "image"
    assert body["name"] == "photo.png"
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(b"png-bytes")
    assert body["path"].startswith("/data/attachments/")

    workspace = await client.get(
        "/api/v1/chats/workspace?chat_id=c_attachment", headers=headers,
    )
    assert workspace.status_code == 200, workspace.text
    listing = await client.get(
        "/api/v1/vfs",
        params={"wf_id": workspace.json()["workspace_scope_id"]},
        headers=headers,
    )
    assert listing.status_code == 200, listing.text
    assert any(item["path"] == body["path"] for item in listing.json()["entries"])


@pytest.mark.asyncio
async def test_chat_can_be_renamed_without_changing_its_identity(client, pg_engine):
    tok = await _register(client)
    headers = _hdr(tok)
    boot = await client.get("/api/v1/chats/bootstrap", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = f"c_rename_{uuid.uuid4().hex[:8]}"
    created = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/attachments",
        files={"file": ("seed.txt", b"seed", "text/plain")},
        headers=headers,
    )
    assert created.status_code == 200, created.text

    renamed = await client.patch(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}",
        json={"name": "Architecture review"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["chat_id"] == chat_id
    assert renamed.json()["chat_context"] == "Architecture review"

    listed = await client.get(
        f"/api/v1/chat-scopes/{scope_id}/chats?surface=chat",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    item = next(row for row in listed.json()["items"] if row["chat_id"] == chat_id)
    assert item["chat_context"] == "Architecture review"



@pytest.mark.asyncio
async def test_chat_attachment_type_rejects_mismatched_media(client, pg_engine):
    tok = await _register(client)
    headers = _hdr(tok)
    scope_id = (await client.get("/api/v1/chats/bootstrap", headers=headers)).json()[
        "carrier_scope_id"
    ]
    response = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/c_bad_attachment/attachments",
        params={"attachment_type": "video"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "attachment_not_video"


@pytest.mark.asyncio
async def test_chat_sandboxes_batch_status_is_read_only(client, pg_engine):
    tok = await _register(client)
    headers = _hdr(tok)
    scope_id = (
        await client.get("/api/v1/chats/bootstrap", headers=headers)
    ).json()["carrier_scope_id"]
    for chat_id in ("c1", "c2"):
        created = await client.post(
            f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/attachments",
            files={"file": ("seed.txt", b"x", "text/plain")},
            headers=headers,
        )
        assert created.status_code == 200, created.text
    r = await client.get(
        "/api/v1/chats/sandboxes?chat_id=c1&chat_id=unknown&chat_id=c2&chat_id=c1",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [item["chat_id"] for item in body["items"]] == ["c1", "c2"]
    assert {item["status"] for item in body["items"]} == {"idle"}


@pytest.mark.asyncio
async def test_delete_chat_session_deletes_runtime_volume(
    client, app_engine, monkeypatch, tmp_path,
):
    from vibecanvas_api.config import config
    from vibecanvas_api.services.vfs_volume import get_chat_runtime_volume_provider
    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    boot = await client.get("/api/v1/chats/bootstrap", headers=headers)
    assert boot.status_code == 200, boot.text
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = "c_delete_cp"

    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=chat_id,
        name="Delete me",
        surface="chat",
        state={
            "runtime_type": "codex",
            "runtime_session_id": "runtime-delete",
            "runtime_state_ref": "thread-delete",
        },
    )
    monkeypatch.setattr(config, "agent_runtime_root", str(tmp_path))
    monkeypatch.setattr(config, "vfs_volume_root", str(tmp_path))
    monkeypatch.setattr(config.object_store, "provider", "filesystem")
    monkeypatch.setattr(
        config, "kms_local_master_key",
        base64.urlsafe_b64encode(b"test-chat-runtime-master-key!!!!").decode(),
    )
    monkeypatch.setattr(
        config.object_store, "fs_root", str(tmp_path / "encrypted-object-store")
    )
    monkeypatch.setattr(
        config.object_store,
        "fs_materialized_root",
        str(tmp_path / "materialized-object-store"),
    )
    runtime_volume = get_chat_runtime_volume_provider().ensure(
        tenant_id=me["tenant_id"],
        user_id=me["user_id"],
        chat_scope_id=chat_workspace_scope_id(chat_id),
    )
    runtime_marker = Path(runtime_volume.path) / "prepared-before-first-runtime"
    runtime_marker.write_text("delete with Chat", encoding="utf-8")

    r = await client.delete(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runtime_state_deleted"] is True
    assert not Path(runtime_volume.path).exists()


@pytest.mark.asyncio
async def test_delete_browser_chat_rejects_while_browser_session_is_active(
    client, app_engine,
):
    """Deleting an attached chat must not orphan Chrome debugger control."""
    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    boot = await client.get("/api/v1/chats/bootstrap?surface=browser", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = "c_active_browser_delete"

    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=chat_id,
        name="Controlled chat",
        surface="browser",
        state={
            "browser_control_status": "attached",
            "browser_session_id": "brs_delete_guard",
            "browser_session_generation": 4,
        },
    )

    response = await client.delete(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}?surface=browser",
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "browser_session_active"

    binding = await client.get(
        f"/api/v1/chats/{chat_id}/browser-binding",
        headers=headers,
    )
    assert binding.status_code == 200, binding.text
    assert binding.json()["status"] == "attached"


@pytest.mark.asyncio
async def test_delete_chat_rejects_while_agent_turn_is_active(
    client, app_engine,
):
    """Deleting a Chat must not revoke a running Turn's capabilities."""
    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    scope_id = (
        await client.get("/api/v1/chats/bootstrap", headers=headers)
    ).json()["carrier_scope_id"]
    chat_id = "c_active_turn_delete"

    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=chat_id,
        name="Running chat",
        surface="chat",
    )
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        await AgentRunsRepo(session).create(
            run_id="t_active_delete_guard",
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            creator_user_id=me["user_id"],
            client_request_id="req-active-delete-guard",
            input_snapshot={"message": "still running"},
        )

    response = await client.delete(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}",
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "chat_turn_active"

    visible = await client.get(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        headers=headers,
    )
    assert visible.status_code == 200, visible.text


@pytest.mark.asyncio
async def test_browser_release_is_fenced_by_session_generation_and_event_sequence(
    client, app_engine,
):
    from types import SimpleNamespace
    from vibecanvas_api.routes.browser import _handle_browser_event

    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    boot = await client.get("/api/v1/chats/bootstrap?surface=browser", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = "c_release_fence"

    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=chat_id,
        name="Release fence",
        surface="browser",
        state={
            "browser_control_status": "attached",
            "browser_session_id": "brs_current",
            "browser_session_generation": 7,
            "browser_last_event_seq": 12,
        },
    )

    scoped = SimpleNamespace(tenant_id=me["tenant_id"], user_id=me["user_id"])

    stale_generation = await _handle_browser_event(
        {"channel": f"chat:{chat_id}", "data": {
            "type": "browser_session_changed",
            "status": "released",
            "chat_id": chat_id,
            "browser_session_id": "brs_current",
            "session_generation": 6,
            "event_seq": 13,
            "reason": "delayed_old_release",
        }},
        scoped,
    )
    assert stale_generation is None

    stale_sequence = await _handle_browser_event(
        {"channel": f"chat:{chat_id}", "data": {
            "type": "browser_session_changed",
            "status": "released",
            "chat_id": chat_id,
            "browser_session_id": "brs_current",
            "session_generation": 7,
            "event_seq": 12,
            "reason": "duplicate_release",
        }},
        scoped,
    )
    assert stale_sequence == {
        "type": "browser_session_event_ack",
        "browser_session_id": "brs_current",
        "session_generation": 7,
        "event_seq": 12,
    }
    binding = await client.get(f"/api/v1/chats/{chat_id}/browser-binding", headers=headers)
    assert binding.json()["status"] == "attached"

    released = await _handle_browser_event(
        {"channel": f"chat:{chat_id}", "data": {
            "type": "browser_session_changed",
            "status": "released",
            "chat_id": chat_id,
            "browser_session_id": "brs_current",
            "session_generation": 7,
            "event_seq": 13,
            "reason": "user_cancelled",
        }},
        scoped,
    )
    assert released and released["event_seq"] == 13
    binding = await client.get(f"/api/v1/chats/{chat_id}/browser-binding", headers=headers)
    assert binding.json()["status"] == "inactive"


@pytest.mark.asyncio
async def test_browser_binding_lost_lease_expires_after_configured_grace(
    client, app_engine, monkeypatch,
):
    from vibecanvas_api.routes import chats as chats_routes

    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    boot = await client.get("/api/v1/chats/bootstrap?surface=browser", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = "c_lost_expiry"
    monkeypatch.setattr(chats_routes.app_config, "browser_lost_grace_seconds", 1)

    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=chat_id,
        name="Lost lease",
        surface="browser",
        state={
            "browser_control_status": "lost",
            "browser_session_id": "brs_lost",
            "browser_session_generation": 3,
            "browser_lost_at": datetime.now(timezone.utc) - timedelta(minutes=2),
        },
    )

    binding = await client.get(
        f"/api/v1/chats/{chat_id}/browser-binding",
        headers=headers,
    )
    assert binding.status_code == 200, binding.text
    assert binding.json()["status"] == "inactive"
    assert "browser_session_id" not in binding.json()
    assert "browser_session_generation" not in binding.json()
    assert "browser_last_event_seq" not in binding.json()
    assert "browser_window_id" not in binding.json()


@pytest.mark.asyncio
async def test_new_browser_reservation_expires_stale_lost_lease_without_frontend_poll(
    client, app_engine,
):
    from vibecanvas_api.storage.chat_repo import ChatRepo
    from vibecanvas_api.storage.db import session_scope

    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    boot = await client.get("/api/v1/chats/bootstrap?surface=browser", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]
    old_chat_id = "c_stale_lost_before_reserve"
    new_chat_id = "c_new_browser_reserve"

    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=old_chat_id,
        name="Old lost chat",
        surface="browser",
        state={
            "browser_control_status": "lost",
            "browser_session_id": "brs_old",
            "browser_session_generation": 2,
            "browser_lost_at": datetime.now(timezone.utc) - timedelta(minutes=2),
        },
    )
    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=new_chat_id,
        name="New chat",
        surface="browser",
    )

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        repo = ChatRepo(session, me["user_id"])
        reserved = await repo.reserve_browser_session(
            chat_id=new_chat_id,
            browser_session_id="brs_new",
            lost_grace_seconds=1,
        )
        await session.commit()

    assert reserved["ok"] is True
    assert reserved["binding"]["status"] == "attaching"
    old_binding = await client.get(
        f"/api/v1/chats/{old_chat_id}/browser-binding",
        headers=headers,
    )
    assert old_binding.status_code == 200
    assert old_binding.json()["status"] == "inactive"


@pytest.mark.asyncio
async def test_browser_reconnect_snapshot_restores_matching_lost_session(
    client, app_engine,
):
    from types import SimpleNamespace
    from vibecanvas_api.routes.browser import _handle_browser_event

    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    boot = await client.get("/api/v1/chats/bootstrap?surface=browser", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = "c_snapshot_restore"

    await _seed_encrypted_chat(
        me,
        scope_id=scope_id,
        chat_id=chat_id,
        name="Snapshot restore",
        surface="browser",
        state={
            "browser_control_status": "lost",
            "browser_session_id": "brs_snapshot",
            "browser_session_generation": 9,
            "browser_last_event_seq": 40,
            "browser_lost_at": datetime.now(timezone.utc),
        },
    )

    ack = await _handle_browser_event(
        {
            "channel": f"chat:{chat_id}",
            "data": {
                "type": "browser_session_snapshot",
                "chat_id": chat_id,
                "browser_session_id": "brs_snapshot",
                "session_generation": 9,
                "event_seq": 41,
                "controlled": True,
                "reason": "websocket_reconnected",
            },
        },
        SimpleNamespace(tenant_id=me["tenant_id"], user_id=me["user_id"]),
    )

    binding = await client.get(
        f"/api/v1/chats/{chat_id}/browser-binding",
        headers=headers,
    )
    assert binding.status_code == 200, binding.text
    assert binding.json()["status"] == "attached"
    assert binding.json()["browser_lost_at"] is None
    assert ack and ack["event_seq"] == 41


@pytest.mark.asyncio
async def test_existing_chat_workflow_command_commits_metadata_before_stream(
    client, pg_engine, monkeypatch, openfga_allow_all,
):
    """Regression for create_workflow hanging on persist_chat_binding.

    An existing chat that receives `/workflow` updates chats.meta.active_modes
    before the SSE producer starts. That write must be committed immediately;
    otherwise the later create_workflow tool writes current_workflow_id through a
    short-session repo and waits on this route transaction's row lock.
    """
    from vibecanvas_api.routes import chats as chats_route
    from vibecanvas_api.storage.chat_repo import ChatRepo

    dispatched_turns = []

    class FakeRuntimeOrchestrator:
        async def stream_turn(self, **kwargs):
            dispatched_turns.append(kwargs["turn_request"])
            yield ("NO_OP", {})

    commit_calls: list[str] = []
    original_commit = ChatRepo.commit

    async def recording_commit(self):
        commit_calls.append("commit")
        await original_commit(self)

    monkeypatch.setattr(
        chats_route,
        "AgentRuntimeOrchestrator",
        FakeRuntimeOrchestrator,
    )
    monkeypatch.setattr(ChatRepo, "commit", recording_commit)
    tok = await _register(client)
    headers = _hdr(tok)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    r = await client.post("/api/v1/workflows", json={"name": "wf"}, headers=headers)
    assert r.status_code in (200, 201), r.text
    wf_id = r.json()["wf_id"]

    r = await client.post(
        f"/api/v1/chat-scopes/{wf_id}/chats/c_build/messages",
        json={"role": "user", "content": "hello"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert len(commit_calls) == 1
    base_platform_mcps = [
        "config",
        "interactive",
    ]
    assert dispatched_turns[0].active_platform_mcps == base_platform_mcps
    from vibecanvas_api.config import config
    from vibecanvas_api.services.agent_runtime.model_capability import (
        verify_runtime_model_capability,
    )

    broker_model = dispatched_turns[0].model
    assert broker_model["base_url"].endswith("/api/internal/runtime-model/v1")
    assert broker_model["api_key"] != config.agent.api_key
    assert config.agent.api_key not in {
        str(value) for value in broker_model.values() if config.agent.api_key
    }
    capability = verify_runtime_model_capability(
        broker_model["api_key"],
        secret=config.signing_secret,
    )
    assert capability is not None
    assert capability.chat_id == "c_build"
    assert capability.turn_id == dispatched_turns[0].turn_id
    assert capability.actions == ("chat:execute", "model:invoke")

    r = await client.post(
        f"/api/v1/chat-scopes/{wf_id}/chats/c_build/messages",
        json={"role": "user", "content": "/workflow create something"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert len(commit_calls) == 2
    assert dispatched_turns[1].active_platform_mcps == [
        *base_platform_mcps,
        "workflow",
        "build",
    ]
    assert dispatched_turns[1].message["content"] == "create something"
    assert [item.name for item in dispatched_turns[1].instructions] == ["workflow"]
    assert dispatched_turns[1].instructions[0].activated_this_turn is True
    assert "WORKFLOW mode" in dispatched_turns[1].instructions[0].content
    assert dispatched_turns[1].command_context.active_modes == ["workflow"]
    build_server = next(
        server
        for server in dispatched_turns[1].mcp_host_servers
        if server.source == "platform" and server.name == "build"
    )
    assert build_server.connection["transport"] == "host_gateway"
    from vibecanvas_api.services.platform_mcp.capability import (
        verify_platform_mcp_capability,
    )

    build_capability = verify_platform_mcp_capability(
        build_server.connection["capability"],
        secret=config.signing_secret,
        server="build",
    )
    assert build_capability is not None
    assert build_capability.audience == "platform-mcp"
    assert build_capability.runtime_session_id == (
        dispatched_turns[1].runtime_session_id
    )
    assert build_capability.session_generation > 0
    assert build_capability.membership_id
    assert build_capability.authorization_generation
    assert "chat:execute" in build_capability.actions
    assert "workflow:update" in build_capability.actions
    assert "platform_mcp:call" in build_capability.actions
    assert "workflow:*" in build_capability.resources

    # Command activation is durable Chat metadata. A later plain message must
    # receive the Workflow MCP again without repeating `/workflow`.
    r = await client.post(
        f"/api/v1/chat-scopes/{wf_id}/chats/c_build/messages",
        json={"role": "user", "content": "continue"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert len(commit_calls) == 3
    assert dispatched_turns[2].active_platform_mcps == [
        *base_platform_mcps,
        "workflow",
        "build",
    ]
    assert dispatched_turns[2].message["content"] == "continue"
    assert [item.name for item in dispatched_turns[2].instructions] == ["workflow"]
    assert dispatched_turns[2].instructions[0].activated_this_turn is False

    # The first accepted Turn fixes the exact account/API connection. A stale
    # or malicious composer may not move this Chat to another credential; use
    # a new Chat for that connection instead.
    credential_ids: list[str] = []
    for label, model_name in (("Primary API", "gpt-audit-a"), ("Backup API", "gpt-audit-b")):
        created_credential = await client.post(
            "/api/v1/llm-credentials",
            json={
                "name": label,
                "provider": "openai",
                "model_name": model_name,
                "model_context_tokens": 128_000,
                "api_url": "https://api.openai.com/v1",
                "proxy": "",
                "api_key": f"fixture-{model_name}",
            },
            headers=headers,
        )
        assert created_credential.status_code == 201, created_credential.text
        credential_ids.append(created_credential.json()["id"])

    switched = await client.post(
        f"/api/v1/chat-scopes/{wf_id}/chats/c_build/messages",
        json={
            "role": "user",
            "content": "cross-connection switch",
            "agent_settings": {
                "model_id": f"codex:credential:{credential_ids[0]}",
                "reasoning_effort": "low",
            },
        },
        headers=headers,
    )
    assert switched.status_code == 409, switched.text
    assert switched.json()["detail"]["code"] == "runtime_connection_locked"
    assert len(dispatched_turns) == 3

    # Capability headers are private turn-transport data. Durable product Run
    # snapshots keep runtime/model choices, but never bearer tokens or MCP
    # connection descriptors.
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        run_ids = list(
            (
                await session.execute(
                    text(
                        "SELECT run_id FROM agent_runs "
                        "WHERE chat_id = 'c_build' ORDER BY created_at"
                    )
                )
            ).scalars()
        )
        run_repo = AgentRunsRepo(session)
        snapshots = [
            (await run_repo.get(run_id)).input_snapshot for run_id in run_ids
        ]
        chat_binding = (
            await session.execute(
                text(
                    "SELECT runtime_model_id, runtime_connection_id, "
                    "runtime_agent_settings FROM chats WHERE chat_id='c_build'"
                )
            )
        ).one()
    assert len(snapshots) == 3
    assert all("mcp_servers" not in snapshot for snapshot in snapshots)
    assert all("Authorization" not in str(snapshot) for snapshot in snapshots)
    assert chat_binding.runtime_model_id not in {
        f"codex:credential:{credential_id}"
        for credential_id in credential_ids
    }
    assert str(chat_binding.runtime_connection_id).startswith("codex:")

    # A real host-side Platform MCP request can rebuild the context while the
    # exact Turn is active, but the same already-issued descriptor is rejected
    # immediately after its browser Session generation changes.
    final_build_server = next(
        server
        for server in dispatched_turns[2].mcp_host_servers
        if server.source == "platform" and server.name == "build"
    )
    final_capability = verify_platform_mcp_capability(
        final_build_server.connection["capability"],
        secret=config.signing_secret,
        server="build",
    )
    assert final_capability is not None
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": final_capability.organization_id},
        )
        await connection.execute(
            text(
                "UPDATE agent_runs SET status='running' "
                "WHERE run_id=:run_id"
            ),
            {"run_id": final_capability.turn_id},
        )
    from vibecanvas_api.services.platform_mcp import invocation as platform_invocation

    monkeypatch.setattr(
        platform_invocation,
        "_OPENFGA_CLIENT",
        openfga_allow_all,
    )
    live_context = await platform_invocation._context_for(final_capability)
    assert live_context.chat_id == "c_build"
    assert live_context.runtime_session_id == final_capability.runtime_session_id
    assert live_context.authorization_session_generation == (
        final_capability.session_generation
    )

    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sessions SET generation=generation+1 "
                "WHERE session_id=:session_id"
            ),
            {"session_id": uuid.UUID(final_capability.session_id)},
        )
    with pytest.raises(PermissionError, match="identity has been revoked"):
        await platform_invocation._context_for(final_capability)

    from vibecanvas_api.services.platform_mcp.capability import (
        mint_platform_mcp_capability,
    )

    rotated_token = mint_platform_mcp_capability(
        organization_id=final_capability.organization_id,
        user_id=final_capability.user_id,
        chat_id=final_capability.chat_id,
        turn_id=final_capability.turn_id,
        workspace_scope_id=final_capability.workspace_scope_id,
        runtime_session_id=final_capability.runtime_session_id,
        session_id=final_capability.session_id,
        session_generation=final_capability.session_generation + 1,
        membership_id=final_capability.membership_id,
        server=final_capability.server,
        authorization_generation=final_capability.authorization_generation,
        secret=config.signing_secret,
        ttl_s=config.mcp.platform_capability_ttl_s,
    )
    rotated_capability = verify_platform_mcp_capability(
        rotated_token,
        secret=config.signing_secret,
        server="build",
    )
    assert rotated_capability is not None
    assert (await platform_invocation._context_for(rotated_capability)).chat_id == (
        "c_build"
    )

    async with pg_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": rotated_capability.organization_id},
        )
        await connection.execute(
            text(
                "UPDATE org_memberships SET status='suspended' "
                "WHERE membership_id=:membership_id"
            ),
            {"membership_id": uuid.UUID(rotated_capability.membership_id)},
        )
    with pytest.raises(PermissionError, match="identity has been revoked"):
        await platform_invocation._context_for(rotated_capability)

    async with pg_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": rotated_capability.organization_id},
        )
        await connection.execute(
            text(
                "UPDATE org_memberships SET status='active' "
                "WHERE membership_id=:membership_id"
            ),
            {"membership_id": uuid.UUID(rotated_capability.membership_id)},
        )
        await connection.execute(
            text(
                "UPDATE chats SET runtime_session_id='runtime-rebound' "
                "WHERE chat_id=:chat_id"
            ),
            {"chat_id": rotated_capability.chat_id},
        )
    with pytest.raises(PermissionError, match="Runtime binding is stale"):
        await platform_invocation._context_for(rotated_capability)


@pytest.mark.asyncio
async def test_hitl_continue_is_hidden_in_product_history_and_sent_as_new_human_turn(
    client, pg_engine, monkeypatch,
):
    from vibecanvas_api.routes import chats as chats_route

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
    token = await _register(client)
    headers = _hdr(token)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    bootstrap = await client.get(
        "/api/v1/chats/bootstrap?surface=chat",
        headers=headers,
    )
    scope_id = bootstrap.json()["carrier_scope_id"]
    chat_id = "c_hidden_hitl_continue"

    first = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        json={"role": "user", "content": "render a review"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert len(dispatched_turns) == 1
    first_run_id = dispatched_turns[0].turn_id

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        hitl = HitlRepo(session)
        await hitl.create_interactive_artifact(
            artifact_id="ia_hidden_continue",
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            run_id=first_run_id,
            component_type="html_preview",
            completion_mode="wait_for_submit",
            title="Review",
            definition_json={
                "kind": "interactive_artifact",
                "artifact_id": "ia_hidden_continue",
                "require_human_confirm": True,
                "interaction_schema": {"interaction_type": "continue"},
            },
            artifact_ref=None,
            content_hash=None,
        )
        await hitl.create_request(
            hitl_request_id="hitl_hidden_continue",
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            run_id=first_run_id,
            artifact_id="ia_hidden_continue",
            hitl_type="post_tool_review",
            title="Review",
            prompt_text="Continue",
            ui_payload_json={},
            agent_payload_json={},
            runtime_correlation_json={},
        )
        await hitl.link_artifact_hitl(
            "ia_hidden_continue",
            "hitl_hidden_continue",
        )
        await hitl.set_interaction_result(
            hitl_request_id="hitl_hidden_continue",
            interaction_result={
                "result_path": "/data/annotations/result.json",
                "content_type": "application/json",
                "hash": "sha256:saved",
            },
        )

    continued = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        json={
            "role": "user",
            "content": "",
            "control": {
                "type": "hitl_continue",
                "version": 1,
                "hitl_request_id": "hitl_hidden_continue",
                "artifact_id": "ia_hidden_continue",
                "action": "continue",
            },
        },
        headers=headers,
    )
    assert continued.status_code == 200, continued.text
    assert len(dispatched_turns) == 2
    runtime_message = dispatched_turns[1].message
    assert '<human-control type="hitl_continue"' in runtime_message["content"]
    assert "<interaction-result>" in runtime_message["content"]
    assert "/data/annotations/result.json" in runtime_message["content"]
    assert runtime_message["additional_kwargs"]["control"]["status"] == "submitted"

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        user_rows = [
            item["content"]
            for item in await ChatRepo(session, me["user_id"]).list_messages(
                chat_id
            )
            if item["role"] == "user"
        ]
        hitl_status = (
            await session.execute(
                text(
                    "SELECT status FROM hitl_requests "
                    "WHERE hitl_request_id='hitl_hidden_continue'"
                )
            )
        ).scalar_one()
        control_runs = list(
            (
                await session.execute(
                    text(
                        "SELECT run_id, client_request_id FROM agent_runs "
                        "WHERE chat_id=:chat_id "
                        "AND client_request_id='hitl_continue:hitl_hidden_continue'"
                    ),
                    {"chat_id": chat_id},
                )
            ).all()
        )
    assert hitl_status == "submitted"
    assert len(control_runs) == 1
    assert len(user_rows) == 2
    assert user_rows[0]["message_type"] == "text"
    assert user_rows[0]["visibility"] == "visible"
    assert user_rows[1]["message_type"] == "control"
    assert user_rows[1]["visibility"] == "hidden"
    assert user_rows[1]["control"]["hitl_request_id"] == "hitl_hidden_continue"

    history = await client.get(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        headers=headers,
    )
    assert history.status_code == 200, history.text
    user_messages = [
        item for item in history.json()["items"] if item["role"] == "user"
    ]
    assert [item["content"] for item in user_messages] == ["render a review"]

    # A second page/replayed click uses the stable HITL idempotency key and
    # attaches to the same durable Turn instead of dispatching another Runtime.
    replay = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        json={
            "role": "user",
            "content": "",
            "client_request_id": "a-different-browser-request-id",
            "control": {
                "type": "hitl_continue",
                "version": 1,
                "hitl_request_id": "hitl_hidden_continue",
                "artifact_id": "ia_hidden_continue",
                "action": "continue",
            },
        },
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers["x-turn-id"] == control_runs[0].run_id
    assert len(dispatched_turns) == 2


@pytest.mark.asyncio
async def test_background_results_are_claimed_as_one_hidden_turn_with_visible_notice(
    client, pg_engine, monkeypatch, openfga_allow_all,
):
    from vibecanvas_api.routes import chats as chats_route
    from vibecanvas_api.services.background_delivery import (
        background_result_batch_id,
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
    token = await _register(client)
    headers = _hdr(token)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    scope_id = (
        await client.get(
            "/api/v1/chats/bootstrap?surface=chat",
            headers=headers,
        )
    ).json()["carrier_scope_id"]
    chat_id = f"c_background_delivery_{uuid.uuid4().hex[:8]}"
    first = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        json={"role": "user", "content": "delegate research"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    job_ids = ["job_result_alpha", "job_result_beta"]
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        jobs_repo = BackgroundJobsRepo(session)
        for index, job_id in enumerate(job_ids):
            await jobs_repo.create_idempotent(
                job_id=job_id,
                tenant_id=me["tenant_id"],
                chat_id=chat_id,
                creator_user_id=me["user_id"],
                parent_run_id=None,
                runtime_type="codex",
                executor_type="runtime_task",
                tool_name="subagent",
                title=f"Research {index + 1}",
                input_snapshot={},
                idempotency_key=f"source:{index}",
            )
        await jobs_repo.complete(
            job_id=job_ids[0],
            result={"result": "finding 1"},
        )
        # Simulate a background executor lost during an API/worker restart.
        # The periodic delivery observer must reconcile the expired lease into
        # a durable failure and include it in the same automatic result Turn;
        # it must never replay the business task.
        lost = await jobs_repo.claim(job_id=job_ids[1], owner="lost-worker")
        assert lost is not None
        lost.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    held_close = await client.delete(
        "/api/v1/chats/sandbox",
        params={"chat_id": chat_id},
        headers=headers,
    )
    assert held_close.status_code == 409, held_close.text
    assert held_close.json()["detail"] == {
        "code": "sandbox_held_by_background_jobs",
        "job_ids": job_ids,
    }

    batch_id = background_result_batch_id(job_ids)
    from vibecanvas_api.services.background_delivery import (
        background_result_delivery,
    )
    background_result_delivery._openfga_client = openfga_allow_all
    assert await background_result_delivery._deliver_one(
        tenant_id=me["tenant_id"],
        chat_id=chat_id,
        user_id=me["user_id"],
    ) is True
    for _ in range(40):
        if len(dispatched_turns) == 2:
            break
        await asyncio.sleep(0.05)
    assert len(dispatched_turns) == 2
    runtime_message = dispatched_turns[1].message
    assert '<human-control type="background_results"' in runtime_message["content"]
    assert all(job_id in runtime_message["content"] for job_id in job_ids)
    assert runtime_message["additional_kwargs"]["control"]["status"] == "delivered"

    async with pg_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT job_id, delivered_at "
                    "FROM chat_tool_job_deliveries "
                    "WHERE job_id = ANY(:job_ids) ORDER BY job_id"
                ),
                {"job_ids": job_ids},
            )
        ).all()
        control_runs = (
            await connection.execute(
                text(
                    "SELECT run_id FROM agent_runs "
                    "WHERE chat_id=:chat_id AND client_request_id=:request_id"
                ),
                {
                    "chat_id": chat_id,
                    "request_id": f"background_results:{batch_id}",
                },
            )
        ).all()
    assert len(rows) == 2
    assert all(row.delivered_at is not None for row in rows)
    assert len(control_runs) == 1

    history = await client.get(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        headers=headers,
    )
    assert history.status_code == 200, history.text
    visible = history.json()["items"]
    assert [item["content"] for item in visible if item["role"] == "user"] == [
        "delegate research"
    ]
    notices = [item for item in visible if item["role"] == "system"]
    assert len(notices) == 1
    assert "2 background jobs have results" in notices[0]["content"]
    assert notices[0]["activity"] == {
        "type": "background_jobs_delivered",
        "delivery_batch_id": batch_id,
        "job_ids": job_ids,
        "summary": {"completed": 1, "failed": 1, "cancelled": 0},
    }

    replay = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages",
        headers=headers,
        json={
            "role": "user",
            "content": "",
            "control": {
                "type": "background_results",
                "version": 1,
                "batch_id": batch_id,
                "job_ids": job_ids,
            },
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers["x-turn-id"] == control_runs[0].run_id
    assert len(dispatched_turns) == 2
