import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from vibecanvas_api.storage.models import Chat, ChatMessage, Workflow
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.agent_runtime_repo import AgentRuntimeRepo
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo
from vibecanvas_api.storage.background_delivery_repo import BackgroundDeliveryRepo
from vibecanvas_api.storage.background_jobs_repo import BackgroundJobsRepo

# ChatRepo/WorkflowRepo stamp rows with the authenticated user id, which in
# production is a UUID string (AuthContext.user_id = str(sessions.user_id)) and
# is an FK to users.user_id. The chats/workflows tenant_id columns resolve from
# the `app.tenant_id` GUC server-default. So a repo test must seed a tenant +
# the user row and bind the session's tenant GUC (mirrors
# test_workflow_repo_pg.py / test_execution_repo_pg.py).
TENANT = uuid.uuid4()
USER = uuid.uuid5(uuid.NAMESPACE_DNS, "chat-repo-user")
PROJECT = "project-repo-tests"


async def _seed_and_bind(session):
    await session.execute(
        text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'chat-repo-test') "
             "ON CONFLICT (tenant_id) DO NOTHING"),
        {"t": TENANT},
    )
    await session.execute(
        text("INSERT INTO users(user_id, tenant_id, email) "
             "VALUES (:u, :t, :e) ON CONFLICT (user_id) DO NOTHING"),
        {"u": USER, "t": TENANT, "e": "chat-repo@test"},
    )
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(TENANT)}
    )
    await session.execute(
        text("SELECT set_config('app.user_id', :u, false)"), {"u": str(USER)}
    )
    project_repo = ChatProjectRepo(session, str(USER))
    if await project_repo.get(PROJECT) is None:
        await project_repo.create(project_id=PROJECT, name="Test project")
        await project_repo.create(project_id=f"{PROJECT}-browser", name="Browser project", surface="browser")


@pytest.mark.asyncio
async def test_chat_metadata_rejects_retired_workflow_state_without_losing_browser_state(pg_session):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session("__chat_versions", project_id=PROJECT, name="No selection")
    chat = await pg_session.get(Chat, chat_id)
    await repo._store_chat_private(chat, name="Preserved", meta={
        "browser_last_release_reason": "manual", "notes": {"keep": True}})
    await pg_session.flush()
    await repo._materialize_chat_private(chat)
    assert chat.meta == {"browser_last_release_reason": "manual", "notes": {"keep": True}}
    assert chat.name == "Preserved"
    binding = await repo.get_platform_context_binding(chat_id)
    assert not any(key.startswith("current_workflow") for key in binding)
    assert await repo.get_platform_context_binding(chat_id, for_update=True) == binding


@pytest.mark.asyncio
async def test_history_only_lists_started_chats_regardless_of_title(pg_session):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    scope = "__chat_draft_history"
    blank = await repo.register_session(scope, project_id=PROJECT, name="Renamed but never sent")
    started = await repo.register_session(scope, project_id=PROJECT, name="New chat")
    assert await repo.list_authorized_sessions(scope, [blank, started], surface="chat") == []
    await repo.persist_message(started, {
        "message_id": "first_user_message", "role": "user", "content": {"text": "Hello"},
    })
    history = await repo.list_authorized_sessions(scope, [blank, started], surface="chat")
    assert [row["chat_id"] for row in history] == [started]
    assert history[0]["chat_context"] == "New chat"
    # Draft storage stays addressable; filtering history never deletes it.
    assert {row["chat_id"] for row in await repo.list_sessions(scope)} == {blank, started}
    projects = await ChatProjectRepo(pg_session, str(USER)).list()
    assert next(row for row in projects if row["project_id"] == PROJECT)["chat_count"] == 1


@pytest.mark.asyncio
async def test_session_and_per_message_persist(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    wf_id = wf["wf_id"]
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session(wf_id, project_id=PROJECT, name="chat A", major_version=1)
    await repo.persist_message(
        chat_id, {"message_id": "m_user", "role": "user", "content": {"text": "hi"}}
    )
    await repo.persist_message(
        chat_id,
        {"message_id": "m_assistant", "role": "assistant", "content": {"text": "hello"}},
    )
    msgs = await repo.list_messages(chat_id)
    assert [m["role"] for m in msgs] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_project_chat_write_fence_uses_live_project_binding(pg_session, monkeypatch):
    from vibecanvas_api.services.agent_resources import authorization
    from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo

    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session("__chat_write_fence", project_id=PROJECT)
    binding = await repo.get_platform_context_binding(chat_id)
    run_id = "write_fence_" + uuid.uuid4().hex
    await AgentRunsRepo(pg_session).create(
        run_id=run_id, tenant_id=TENANT, chat_id=chat_id,
        creator_user_id=USER, client_request_id=run_id, input_snapshot={},
    )
    decision = AsyncMock()
    monkeypatch.setattr(authorization, "_decision", decision)
    monkeypatch.setattr(authorization, "_service", lambda *_: None)
    context = SimpleNamespace(
        tenant_id=str(TENANT), username=str(USER), chat_id=chat_id,
        turn_id=run_id, runtime_session_id=binding["runtime_session_id"],
    )
    await authorization._require_active_chat_write(pg_session, context)
    decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_rename_session_and_tail_page(pg_session):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session(
        "__rename_chat", project_id=PROJECT,
        name="Original title",
        chat_id=f"rename_{uuid.uuid4().hex[:8]}",
    )
    await repo.set_active_modes(chat_id, {"diagram"})
    for index in range(5):
        await repo.persist_message(
            chat_id,
            {
                "message_id": f"message_{index}",
                "role": "user",
                "content": {"text": f"message {index}"},
            },
        )

    renamed = await repo.rename_session("__rename_chat", chat_id, "Architecture review")
    assert renamed is not None
    assert renamed["chat_context"] == "Architecture review"
    restored = next(
        item for item in await repo.list_sessions("__rename_chat")
        if item["chat_id"] == chat_id
    )
    assert restored["name"] == "Architecture review"
    assert restored["active_modes"] == ["diagram"]

    page, total, offset = await repo.list_message_page(chat_id, limit=2, tail=True)
    assert total == 5
    assert offset == 3
    assert [message["message_id"] for message in page] == ["message_3", "message_4"]


@pytest.mark.asyncio
async def test_chat_messages_are_ciphertext_only_without_stream_shape_change(
    pg_session,
):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session(
        "__encrypted_chat", project_id=PROJECT,
        name="encrypted",
        chat_id=f"encrypted_{uuid.uuid4().hex[:8]}",
    )
    original = {
        "message_id": "encrypted-message",
        "turn_id": "turn-1",
        "role": "assistant",
        "content": {"text": "private answer", "parts": [{"type": "text"}]},
        "meta": {"control": {"kind": "continue"}},
    }
    await repo.persist_message(chat_id, original)

    row = (
        await pg_session.execute(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id)
        )
    ).scalar_one()
    assert row.content_ciphertext
    assert row.content_nonce
    assert row.content_key_id is not None

    restored = (await repo.list_messages(chat_id))[0]
    assert restored["content"] == original["content"]
    assert restored["meta"] == original["meta"]
    assert restored["message_id"] == original["message_id"]


@pytest.mark.asyncio
async def test_active_diagram_context_is_an_ordinary_file_reference(
    pg_session,
):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session(
        "__diagram_context", project_id=PROJECT,
        name="diagram context",
        chat_id=f"diagram_{uuid.uuid4().hex[:8]}",
    )
    file_ref = {
        "path": "/data/diagrams/system.drawio",
        "revision": "sha256:revision-1",
        "source_hash": "sha256:source-1",
    }
    await repo.set_active_diagram(chat_id, file_ref)
    assert (await repo.get_active_diagram(chat_id)) == {"file_ref": file_ref}


@pytest.mark.asyncio
async def test_chat_display_metadata_is_ciphertext_only(pg_session):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    title = "private-chat-title-sentinel"
    chat_id = await repo.register_session(
        "__encrypted_chat_metadata", project_id=PROJECT,
        name=title,
        chat_id=f"metadata_{uuid.uuid4().hex[:8]}",
    )
    await repo.set_active_modes(chat_id, {"workflow"})

    row = await pg_session.get(Chat, chat_id)
    assert row is not None and row.metadata_ciphertext
    assert title not in row.metadata_ciphertext
    columns = set((await pg_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='chats'"
    ))).scalars())
    assert {"name", "meta"}.isdisjoint(columns)

    sessions = await repo.list_sessions("__encrypted_chat_metadata")
    restored = next(item for item in sessions if item["chat_id"] == chat_id)
    assert restored["name"] == title
    assert restored["active_modes"] == ["workflow"]


@pytest.mark.asyncio
async def test_message_identity_is_idempotent_and_active_turn_can_be_excluded(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session(wf["wf_id"], project_id=PROJECT, name="chat", major_version=1)
    await repo.persist_message(
        chat_id,
        {
            "message_id": "m_previous",
            "turn_id": "turn_previous",
            "role": "assistant",
            "content": {"text": "previous"},
        },
    )
    current = {
        "message_id": "m_current_user",
        "turn_id": "turn_current",
        "role": "user",
        "content": {"text": "current"},
    }
    await repo.persist_message(chat_id, current)
    await repo.persist_message(chat_id, current)

    assert [m["message_id"] for m in await repo.list_messages(chat_id)] == [
        "m_previous",
        "m_current_user",
    ]
    assert [
        m["message_id"]
        for m in await repo.list_messages(chat_id, before_turn_id="turn_current")
    ] == ["m_previous"]


@pytest.mark.asyncio
async def test_todo_snapshot_is_revisioned_with_immutable_runtime_binding(pg_session):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    chat_id = await repo.register_session(
        "__chat_todo", project_id=PROJECT,
        name="Todo chat",
        chat_id="todo_chat",
    )
    binding = await AgentRuntimeRepo(
        pg_session, str(USER)
    ).bind_chat(chat_id, runtime_type="codex")
    assert binding is not None

    await repo.set_todo_items(
        chat_id,
        [{"id": 1, "text": "Inspect files", "status": "in_progress"}],
    )
    first = await repo.get_todo_state(chat_id)
    assert first == {
        "items": [{"id": 1, "text": "Inspect files", "status": "in_progress"}],
        "revision": 1,
        "runtime_type": "codex",
        "runtime_session_id": binding["runtime_session_id"],
    }

    await repo.set_todo_items(
        chat_id,
        [{"id": 1, "text": "Inspect files", "status": "done"}],
    )
    second = await repo.get_todo_state(chat_id)
    assert second["revision"] == 2
    assert second["items"] == [
        {"id": 1, "text": "Inspect files", "status": "done"}
    ]
    assert second["runtime_type"] == "codex"
    assert second["runtime_session_id"] == binding["runtime_session_id"]


@pytest.mark.asyncio
async def test_list_sessions_filters_major_version(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    wf_id = wf["wf_id"]
    repo = ChatRepo(pg_session, str(USER))
    await repo.register_session(wf_id, project_id=PROJECT, name="v1 chat", major_version=1)
    await repo.register_session(wf_id, project_id=PROJECT, name="v2 chat", major_version=2)
    v1 = await repo.list_sessions(wf_id, major_version=1)
    assert [s["name"] for s in v1] == ["v1 chat"]


@pytest.mark.asyncio
async def test_list_sessions_filters_surface(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    wf_id = wf["wf_id"]
    repo = ChatRepo(pg_session, str(USER))
    await repo.register_session(wf_id, project_id=PROJECT, name="main chat", surface="chat")
    await repo.register_session(wf_id, project_id=f"{PROJECT}-browser", name="browser chat", surface="browser")

    main = await repo.list_sessions(wf_id, surface="chat")
    browser = await repo.list_sessions(wf_id, surface="browser")

    assert [s["name"] for s in main] == ["main chat"]
    assert [s["name"] for s in browser] == ["browser chat"]
    assert browser[0]["surface"] == "browser"


@pytest.mark.asyncio
async def test_internal_chat_scope_does_not_require_workflow_row(pg_session):
    await _seed_and_bind(pg_session)
    scope_id = "__chatws_user_chat1"
    repo = ChatRepo(pg_session, str(USER))

    chat_id = await repo.register_session(
        scope_id, project_id=PROJECT,
        chat_id="chat1",
        name="hello",
        surface="chat",
    )

    assert chat_id == "chat1"
    assert await pg_session.get(Workflow, scope_id) is None
    sessions = await repo.list_sessions(scope_id, surface="chat")
    assert [s["chat_id"] for s in sessions] == ["chat1"]


@pytest.mark.asyncio
async def test_chat_command_and_workflow_context_are_user_scoped(pg_session):
    await _seed_and_bind(pg_session)
    other_user = uuid.uuid5(uuid.NAMESPACE_DNS, "chat-repo-other-user")
    await pg_session.execute(
        text(
            "INSERT INTO users(user_id, tenant_id, email) "
            "VALUES (:u, :t, :e) ON CONFLICT (user_id) DO NOTHING"
        ),
        {"u": other_user, "t": TENANT, "e": "chat-repo-other@test"},
    )
    await WorkflowRepo(pg_session, str(USER)).create_workflow(name="Owned")
    owner = ChatRepo(pg_session, str(USER))
    chat_id = await owner.register_session(
        "__chat_owner", project_id=PROJECT, name="owned chat", chat_id="owned_chat"
    )
    await owner.set_active_modes(chat_id, {"workflow"})

    other = ChatRepo(pg_session, str(other_user))
    assert await other.list_sessions("__chat_owner") == []
    assert await other.get_active_modes(chat_id) == set()
    assert await other.get_platform_context_binding(chat_id) is None

    await other.set_active_modes(chat_id, {"browser"})

    assert await owner.get_active_modes(chat_id) == {"workflow"}
    project = await ChatProjectRepo(pg_session, str(USER)).get(PROJECT)
    assert await owner.get_platform_context_binding(chat_id) == {
        "chat_id": chat_id,
        "project_id": PROJECT,
        "carrier_scope_id": "__chat_owner",
        "runtime_session_id": project.runtime_session_id,
        "runtime_type": project.runtime_type,
        "workflow_id": None,
    }


@pytest.mark.asyncio
async def test_drop_session_cascades_messages(pg_session):
    await _seed_and_bind(pg_session)
    wf = await WorkflowRepo(pg_session, str(USER)).create_workflow(name="W")
    repo = ChatRepo(pg_session, str(USER))
    cid = await repo.register_session(wf["wf_id"], project_id=PROJECT, name="c", major_version=1)
    await repo.persist_message(
        cid, {"message_id": "m_drop", "role": "user", "content": {"t": 1}}
    )
    await repo.drop_session(cid)
    assert await repo.list_messages(cid) == []


@pytest.mark.asyncio
async def test_project_mcp_selection_is_shared_idempotent_and_revision_guarded(pg_session):
    await _seed_and_bind(pg_session)
    chat_repo = ChatRepo(pg_session, str(USER))
    await chat_repo.register_session(
        "__chat_mcp", project_id=PROJECT, name="MCP chat", chat_id="mcp_chat"
    )
    project_repo = ChatProjectRepo(pg_session, str(USER))
    server_id = await McpServersRepo(pg_session).insert(
        tenant_id=TENANT,
        user_id=USER,
        name="Local tools",
        tool_prefix=f"local_{uuid.uuid4().hex[:6]}",
        transport="stdio",
        endpoint="python",
        description="test",
        auth_config={},
        connection_config={"args": ["-m", "server"]},
    )

    initial = await project_repo.get_mcp_selection(PROJECT)
    assert initial == {"mcp_server_ids": [], "mcp_config_revision": 0}
    selected = await project_repo.set_mcp_selection(
        PROJECT,
        mcp_server_ids=[server_id],
        expected_revision=0,
    )
    assert selected["ok"] is True
    assert selected["mcp_config_revision"] == 1
    assert selected["mcp_server_ids"] == [str(server_id)]

    await chat_repo.register_session(
        "__chat_mcp", project_id=PROJECT, name="Sibling", chat_id="mcp_sibling"
    )
    same_from_stale_tab = await project_repo.set_mcp_selection(
        PROJECT,
        mcp_server_ids=[server_id, server_id],
        expected_revision=0,
    )
    assert same_from_stale_tab["ok"] is True
    assert same_from_stale_tab["mcp_config_revision"] == 1
    assert "mcp_config_revision" not in Chat.__table__.columns
    other_project = await project_repo.create(name="Independent tools")
    assert await project_repo.get_mcp_selection(other_project["project_id"]) == initial
    conflict = await project_repo.set_mcp_selection(
        PROJECT,
        mcp_server_ids=[],
        expected_revision=0,
    )
    assert conflict["ok"] is False
    assert conflict["error_code"] == "mcp_config_revision_conflict"

    await McpServersRepo(pg_session).soft_delete(server_id)
    removed = await project_repo.get_mcp_selection(PROJECT)
    assert removed == {
        "mcp_server_ids": [],
        "mcp_config_revision": 2,
    }


@pytest.mark.asyncio
async def test_background_job_state_machine_is_idempotent_and_reconciles_stale(
    pg_session,
):
    private_marker = "SENSITIVE_BACKGROUND_MARKER_774a"
    await _seed_and_bind(pg_session)
    chat_id = await ChatRepo(pg_session, str(USER)).register_session(
        "__chat_jobs", project_id=PROJECT,
        name="Background jobs",
        chat_id=f"jobs_{uuid.uuid4().hex[:10]}",
    )
    repo = BackgroundJobsRepo(pg_session)
    kwargs = {
        "job_id": f"job_{uuid.uuid4().hex}",
        "tenant_id": TENANT,
        "chat_id": chat_id,
        "creator_user_id": USER,
        "parent_run_id": None,
        "runtime_type": "codex",
        "executor_type": "runtime_task",
        "tool_name": "subagent",
        "title": f"Inspect data {private_marker}",
        "input_snapshot": {"prompt": f"Inspect the data {private_marker}"},
        "idempotency_key": "turn_1:call_1",
    }
    row, created = await repo.create_idempotent(**kwargs)
    replay, replay_created = await repo.create_idempotent(
        **{**kwargs, "job_id": f"ignored_{uuid.uuid4().hex}"}
    )

    assert created is True
    assert replay_created is False
    assert replay.job_id == row.job_id
    delivery_repo = BackgroundDeliveryRepo(pg_session)
    assert await delivery_repo.has_sandbox_hold(chat_id) is True
    holds = await delivery_repo.list_sandbox_holds_for_user(
        chat_id=chat_id,
        creator_user_id=USER,
    )
    assert [item.job_id for item in holds] == [row.job_id]
    assert [event.event_type for event in await repo.list_events(job_id=row.job_id)] == [
        "queued"
    ]

    claimed = await repo.claim(
        job_id=row.job_id,
        owner="worker-a",
        lease_seconds=30,
    )
    assert claimed is not None
    assert claimed.status == "running"
    await repo.heartbeat(
        job_id=row.job_id,
        owner="worker-a",
        lease_seconds=30,
        current=2,
        total=4,
        message="Inspecting",
    )
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await pg_session.flush()

    reconciled = await repo.reconcile_stale_for_chat(chat_id=chat_id)

    assert [item.job_id for item in reconciled] == [row.job_id]
    terminal = await repo.get(row.job_id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.error_json["code"] == "executor_disconnected_state_unknown"
    assert terminal.lease_owner is None
    # Terminal results retain the sandbox until their durable delivery Turn
    # has claimed them.
    assert await delivery_repo.has_sandbox_hold(chat_id) is True
    assert [event.event_type for event in await repo.list_events(job_id=row.job_id)] == [
        "queued",
        "started",
        "progress",
        "failed",
    ]
    pending = await delivery_repo.list_pending_terminal_for_user(
        chat_id=chat_id,
        creator_user_id=USER,
    )
    assert [item.job_id for item in pending] == [row.job_id]
    delivered = await delivery_repo.claim_batch(
        chat_id=chat_id,
        creator_user_id=USER,
        job_ids=[row.job_id],
        delivery_batch_id="bg_test_delivery",
    )
    assert [item.job_id for item in delivered] == [row.job_id]
    assert delivered[0].delivery is not None
    assert delivered[0].delivery.delivery_batch_id == "bg_test_delivery"
    assert await delivery_repo.has_sandbox_hold(chat_id) is False
    assert await delivery_repo.list_sandbox_holds_for_user(
        chat_id=chat_id,
        creator_user_id=USER,
    ) == []
    assert await delivery_repo.claim_batch(
        chat_id=chat_id,
        creator_user_id=USER,
        job_ids=[row.job_id],
        delivery_batch_id="bg_test_delivery",
    ) == []
    assert [
        event.event_type for event in await repo.list_events(job_id=row.job_id)
    ][-1] == "delivered"
    chat_events = await repo.list_chat_events_for_user(
        chat_id=chat_id,
        creator_user_id=USER,
        after_event_id=0,
    )
    assert [event.event_type for event in chat_events] == [
        "queued",
        "started",
        "progress",
        "failed",
        "delivered",
    ]
    assert [event.event_id for event in chat_events] == sorted(
        event.event_id for event in chat_events
    )
    resumed_events = await repo.list_chat_events_for_user(
        chat_id=chat_id,
        creator_user_id=USER,
        after_event_id=chat_events[-2].event_id,
    )
    assert [event.event_type for event in resumed_events] == ["delivered"]
    stored = (
        await pg_session.execute(
            text(
                "SELECT j.private_ciphertext, "
                "string_agg(e.payload_ciphertext, '') AS event_ciphertext "
                "FROM chat_tool_jobs j JOIN chat_tool_job_events e "
                "ON e.job_id=j.job_id WHERE j.job_id=:job_id "
                "GROUP BY j.private_ciphertext"
            ),
            {"job_id": row.job_id},
        )
    ).mappings().one()
    old_columns = (
        await pg_session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND (("
                "table_name='chat_tool_jobs' AND column_name IN ("
                "'title','progress_message','input_snapshot','result_snapshot',"
                "'result_ref','error_json','execution_handle_json')) OR ("
                "table_name='chat_tool_job_events' AND column_name='payload'))"
            )
        )
    ).all()
    assert private_marker not in stored["private_ciphertext"]
    assert private_marker not in stored["event_ciphertext"]
    assert old_columns == []


@pytest.mark.asyncio
async def test_goal_snapshot_is_private_encrypted_and_preserves_other_chat_metadata(pg_session):
    await _seed_and_bind(pg_session)
    repo = ChatRepo(pg_session, str(USER))
    cid = await repo.register_session('__goal_test', project_id=PROJECT)
    await repo.set_active_modes(cid, {'workflow'})
    goal = {'objective': 'private-goal-example', 'status': 'paused', 'timeUsedSeconds': 42}
    await repo.set_goal(cid, goal)
    assert await repo.get_goal(cid) == goal
    assert await repo.get_active_modes(cid) == {'workflow'}
    assert await repo.get_goal_pause_requested(cid) is True
    await repo.set_goal(cid, {**goal, 'status':'active'})
    assert await repo.get_goal_pause_requested(cid) is False
    await repo.request_goal_pause(cid)
    assert await repo.get_goal_pause_requested(cid) is True
    assert (await repo.get_goal(cid))['status'] == 'paused'
    await repo.set_goal(cid, goal)
    ciphertext = (await pg_session.execute(text('SELECT metadata_ciphertext FROM chats WHERE chat_id=:id'), {'id':cid})).scalar_one()
    assert goal['objective'] not in ciphertext
    other = ChatRepo(pg_session, str(uuid.uuid4()))
    assert await other.get_goal(cid) is None
    await other.set_goal(cid, None)
    assert await repo.get_goal(cid) == goal
    await repo.set_goal(cid, None)
    assert await repo.get_goal(cid) is None
    assert await repo.get_active_modes(cid) == {'workflow'}


@pytest.mark.asyncio
async def test_chat_event_cursor_orders_concurrent_jobs(pg_session):
    import asyncio
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.services.state_notifications import state_changes

    await _seed_and_bind(pg_session)
    chat = await ChatRepo(pg_session, str(USER)).register_session('__chat_jobs', project_id=PROJECT)
    jobs = [f'job_{uuid.uuid4().hex}' for _ in range(2)]
    for job in jobs:
        await BackgroundJobsRepo(pg_session).create_idempotent(job_id=job,
            tenant_id=TENANT, chat_id=chat, creator_user_id=USER, parent_run_id=None,
            runtime_type='codex', executor_type='runtime_task', tool_name='subagent',
            title='Ordering test', input_snapshot={}, idempotency_key=job)
    await pg_session.commit()
    entered = asyncio.Event()
    async def append_second():
        async with session_scope(tenant_id=str(TENANT)) as session:
            entered.set()
            event = await BackgroundJobsRepo(session).append_event(jobs[1], 'progress', {'i': 2})
            return event.event_id
    pending = None
    try:
        async with state_changes('flowork_background_state', chat) as changed:
            await asyncio.wait_for(changed.wait(), 5)
            changed.clear()
            async with session_scope(tenant_id=str(TENANT)) as session:
                event = await BackgroundJobsRepo(session).append_event(jobs[0], 'progress', {'i': 1})
                first = event.event_id
                pending = asyncio.create_task(append_second())
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.sleep(0.2)
                assert not pending.done() and not changed.is_set()
            await asyncio.wait_for(changed.wait(), 5)
            second = await asyncio.wait_for(pending, 5)
        assert first < second
        async with session_scope(tenant_id=str(TENANT)) as session:
            events = await BackgroundJobsRepo(session).list_chat_events_for_user(
                chat_id=chat, creator_user_id=USER, after_event_id=first)
            assert [e.event_id for e in events] == [second]
    finally:
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
