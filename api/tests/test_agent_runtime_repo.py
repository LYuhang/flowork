from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.agent_runtime_repo import AgentRuntimeRepo
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.db import session_scope


async def _seed(pg_engine) -> tuple[str, str]:
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:tenant, 'runtime')"),
            {"tenant": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:user, :tenant, :email)"
            ),
            {
                "user": user_id,
                "tenant": tenant_id,
                "email": f"runtime-{uuid.uuid4().hex[:8]}@example.test",
            },
        )
    return tenant_id, user_id


async def _insert_chat(tenant_id: str, user_id: str, chat_id: str) -> None:
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        project = await ChatProjectRepo(session, user_id).create(name="Runtime")
        await ChatRepo(session, user_id).register_session(
            "__chat_runtime",
            project_id=project["project_id"],
            name="Runtime",
            chat_id=chat_id,
            surface="chat",
        )
        await session.commit()


@pytest.mark.asyncio
async def test_project_runtime_shared_connection_independent_threads(pg_engine) -> None:
    tenant_id, user_id = await _seed(pg_engine)
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        project = await ChatProjectRepo(session, user_id).create(name="Shared runtime")
        chats = ChatRepo(session, user_id)
        first_id = await chats.register_session("__chat_runtime", project_id=project["project_id"])
        second_id = await chats.register_session("__chat_runtime", project_id=project["project_id"])
        repo = AgentRuntimeRepo(session, user_id)
        first = await repo.bind_chat(first_id)
        await repo.set_runtime_model_selection(
            first_id, runtime_type="codex", model_id="codex:account:terra",
            connection_id="codex:account", agent_settings={"reasoning_effort": "low"},
        )
        second = await repo.bind_chat(second_id)
        assert first["runtime_session_id"] == second["runtime_session_id"]
        assert second["runtime_connection_id"] == "codex:account"
        assert second["runtime_model_id"] == "codex:account:terra"
        for chat_id, thread_id in ((first_id, "thread-a"), (second_id, "thread-b")):
            await repo.set_runtime_state_ref(
                chat_id, runtime_type="codex", runtime_session_id=first["runtime_session_id"],
                state_ref=thread_id,
            )
        with pytest.raises(ValueError, match="runtime_connection_locked"):
            await repo.set_runtime_model_selection(
                second_id, runtime_type="codex", model_id="codex:managed:other:model",
                connection_id="codex:managed:other", agent_settings={},
            )
        await session.commit()
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        a = await repo.get_chat_binding(first_id)
        b = await repo.get_chat_binding(second_id)
        assert a["runtime_state_ref"] == "thread-a"
        assert b["runtime_state_ref"] == "thread-b"
        assert a["runtime_session_id"] == b["runtime_session_id"]
        assert a["runtime_connection_id"] == b["runtime_connection_id"]


@pytest.mark.asyncio
async def test_chat_runtime_binding_is_immutable_after_first_start(pg_engine) -> None:
    tenant_id, user_id = await _seed(pg_engine)
    first_chat = f"runtime_first_{uuid.uuid4().hex[:8]}"
    second_chat = f"runtime_second_{uuid.uuid4().hex[:8]}"
    await _insert_chat(tenant_id, user_id, first_chat)
    await _insert_chat(tenant_id, user_id, second_chat)

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        assert await repo.get_preferences() == {
            "default_runtime_type": "codex",
            "codex_managed_profile_id": None,
            "preferred_timezone": None,
        }
        await repo.set_default_runtime_type("codex")
        first = await repo.bind_chat(first_chat)
        await session.commit()

    assert first is not None
    assert first["runtime_type"] == "codex"
    assert first["runtime_session_id"].startswith("rt_codex_")
    assert first["runtime_state_ref"] is None

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        await repo.set_default_runtime_type("codex")
        rebound = await repo.bind_chat(first_chat)
        second = await repo.bind_chat(second_chat)
        await session.commit()

    assert rebound == first
    assert second is not None
    assert second["runtime_type"] == "codex"
    assert second["runtime_session_id"].startswith("rt_codex_")
    assert second["runtime_state_ref"] is None
    assert second["runtime_timezone"] == "UTC"
    assert second["runtime_started_at"] is not None


@pytest.mark.asyncio
async def test_chat_runtime_model_selection_can_advance_between_turns(
    pg_engine,
) -> None:
    tenant_id, user_id = await _seed(pg_engine)
    chat_id = f"runtime_model_{uuid.uuid4().hex[:8]}"
    await _insert_chat(tenant_id, user_id, chat_id)

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        await repo.bind_chat(chat_id, runtime_type="codex")
        first = await repo.set_runtime_model_selection(
            chat_id,
            runtime_type="codex",
            model_id="codex:account:gpt-first",
            connection_id="codex:account",
            agent_settings={"reasoning_effort": "high"},
        )
        resumed = await repo.set_runtime_model_selection(
            chat_id,
            runtime_type="codex",
            model_id="codex:account:gpt-first",
            connection_id="codex:account",
            agent_settings={"reasoning_effort": "high"},
        )
        await session.commit()

    assert first is not None
    assert resumed is not None
    assert first["runtime_model_id"] == "codex:account:gpt-first"
    assert first["runtime_connection_id"] == "codex:account"
    assert first["runtime_agent_settings"] == {
        "model_id": "codex:account:gpt-first",
        "reasoning_effort": "high",
    }
    assert resumed == first

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        switched_model = await repo.set_runtime_model_selection(
            chat_id,
            runtime_type="codex",
            model_id="codex:account:gpt-second",
            connection_id="codex:account",
            agent_settings={"reasoning_effort": "low"},
        )
        with pytest.raises(
            ValueError,
            match="runtime_connection_locked",
        ):
            await repo.set_runtime_model_selection(
                chat_id,
                runtime_type="codex",
                model_id="codex:managed:company:gpt-default",
                connection_id="codex:managed:company",
                agent_settings={"reasoning_effort": "high"},
            )
        await session.commit()

    assert switched_model is not None
    assert switched_model["runtime_model_id"] == "codex:account:gpt-second"
    assert switched_model["runtime_agent_settings"]["reasoning_effort"] == "low"
    assert switched_model["runtime_connection_id"] == "codex:account"


@pytest.mark.asyncio
async def test_runtime_conversation_clock_is_fixed_across_resume(pg_engine) -> None:
    tenant_id, user_id = await _seed(pg_engine)
    chat_id = f"runtime_clock_{uuid.uuid4().hex[:8]}"
    await _insert_chat(tenant_id, user_id, chat_id)

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        first = await repo.bind_chat(
            chat_id,
            runtime_type="codex",
            user_timezone="Asia/Shanghai",
        )
        await session.commit()

    assert first is not None
    assert first["runtime_timezone"] == "Asia/Shanghai"
    assert first["runtime_started_at"] is not None

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        # A later browser/resume may report another zone.  The Chat clock is
        # immutable and must not follow it.
        resumed = await repo.bind_chat(
            chat_id,
            user_timezone="America/New_York",
        )

    assert resumed == first

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        prefs = await AgentRuntimeRepo(session, user_id).get_preferences()
    assert prefs["preferred_timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_codex_thread_ref_rotates_only_with_matching_previous_ref(
    pg_engine,
) -> None:
    tenant_id, user_id = await _seed(pg_engine)
    chat_id = f"runtime_codex_{uuid.uuid4().hex[:8]}"
    await _insert_chat(tenant_id, user_id, chat_id)

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        binding = await repo.bind_chat(chat_id, runtime_type="codex")
        assert binding is not None
        saved = await repo.set_runtime_state_ref(
            chat_id,
            runtime_type="codex",
            runtime_session_id=binding["runtime_session_id"],
            state_ref="codex-thread-1",
        )
        assert saved is not None
        assert saved["runtime_state_ref"] == "codex-thread-1"

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        rotated = await repo.set_runtime_state_ref(
            chat_id,
            runtime_type="codex",
            runtime_session_id=binding["runtime_session_id"],
            state_ref="codex-thread-2",
            previous_state_ref="codex-thread-1",
        )
        assert rotated is not None
        assert rotated["runtime_state_ref"] == "codex-thread-2"

    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        with pytest.raises(ValueError, match="runtime state ref conflict"):
            await repo.set_runtime_state_ref(
                chat_id,
                runtime_type="codex",
                runtime_session_id=binding["runtime_session_id"],
                state_ref="codex-thread-3",
                previous_state_ref="codex-thread-1",
            )


@pytest.mark.asyncio
async def test_runtime_inputs_are_encrypted_private_and_preserve_original(pg_engine):
    tenant_id, user_id = await _seed(pg_engine)
    me = {"tenant_id": tenant_id, "user_id": user_id}
    chat_id = f"c_audit_{uuid.uuid4().hex[:8]}"
    await _insert_chat(tenant_id, user_id, chat_id)
    context = dict(tenant_id=me["tenant_id"], user_id=me["user_id"])
    run_id = f"turn_audit_{uuid.uuid4().hex}"
    original = "/workflow 修改节点"
    expanded = "<system-reminder>private instructions</system-reminder>修改节点"
    async with session_scope(**context) as session:
        await AgentRunsRepo(session).create(
            run_id=run_id, tenant_id=me["tenant_id"], chat_id=chat_id,
            creator_user_id=me["user_id"], client_request_id=run_id,
            input_snapshot={"content": original, "resolved_runtime_instructions": [{"content": "private instructions"}]},
        )
    for event_id in ("audit-1", "audit-1", "audit-2"):
        async with session_scope(**context) as session:
            await AgentRunsRepo(session).record_runtime_input(
                run_id, chat_id=chat_id, creator_user_id=me["user_id"],
                event_id=event_id, payload={"input": [{"type": "text", "text": expanded}]},
            )
    async with session_scope(**context) as session:
        repo = AgentRunsRepo(session)
        run = await repo.get(run_id)
        assert run.input_snapshot["content"] == original
        assert len(run.input_snapshot["runtime_inputs"]) == 2
        assert run.input_snapshot["runtime_inputs"][0]["payload"]["input"][0]["text"] == expanded
        assert expanded not in run.private_ciphertext
        assert await repo.list_events(run_id, 0) == []
        diagnostics = await repo.list_debug_turns(chat_id, creator_user_id=me["user_id"])
        assert "private instructions" not in json.dumps(diagnostics, default=str)
    async with session_scope(**context) as session:
        with pytest.raises(LookupError):
            await AgentRunsRepo(session).record_runtime_input(
                run_id, chat_id="another_chat", creator_user_id=me["user_id"],
                event_id="wrong-chat", payload={},
            )

    async with session_scope(**context) as session:
        with pytest.raises(LookupError):
            await AgentRunsRepo(session).record_runtime_input(
                run_id, chat_id=chat_id, creator_user_id=str(uuid.uuid4()),
                event_id="wrong-owner", payload={},
            )
    async with session_scope(**context) as session:
        await AgentRunsRepo(session).append_event(
            run_id=run_id, seq=1, event_type="error", tenant_id=tenant_id,
            payload={"code": "model_failure", "message": "Model unavailable"},
        )
    async with session_scope(**context) as session:
        run = await AgentRunsRepo(session).get(run_id)
        assert run.error_message == "Model unavailable"
        assert run.input_snapshot["content"] == original
        assert len(run.input_snapshot["runtime_inputs"]) == 2


@pytest.mark.asyncio
async def test_workflow_history_binding_is_immutable_and_scoped(pg_engine):
    tenant_id, user_id = await _seed(pg_engine)
    scope = dict(tenant_id=tenant_id, user_id=user_id)
    binding = {"workflow_id": "wf-history", "major_version": 2,
               "initial_subversion": 3, "target": {"kind": "node", "node_id": "focus"}}
    async with session_scope(**scope) as session:
        project = await ChatProjectRepo(session, user_id).create(name="Canvas")
        repo = ChatRepo(session, user_id)
        chat_id = await repo.register_session("wf-history", project_id=project["project_id"],
                                             name="[v2.sv3] Code", chat_id="canvas-history")
        stored = await repo.bind_workflow_context(chat_id, binding)
        assert await repo.bind_workflow_context(chat_id, binding) == stored
    async with session_scope(**scope) as session:
        repo = ChatRepo(session, user_id)
        history = await repo.list_sessions("wf-history")
        assert history[0]["workflow_context"] == stored
        assert history[0]["major_version"] == 2
        assert history[0]["created_at"] and history[0]["updated_at"]
        assert await repo.list_sessions("another-workflow") == []
        with pytest.raises(ValueError, match="workflow_chat_binding_conflict"):
            await repo.bind_workflow_context(chat_id, {**binding, "major_version": 3})
    async with session_scope(**scope) as session:
        with pytest.raises(LookupError):
            await ChatRepo(session, user_id).bind_workflow_context(
                chat_id, {**binding, "workflow_id": "another-workflow"})
    async with session_scope(**scope) as session:
        assert await ChatRepo(session, str(uuid.uuid4())).list_sessions("wf-history") == []


@pytest.mark.asyncio
async def test_agent_commit_notifications_and_bounded_replay(pg_engine):
    import asyncio
    from vibecanvas_api.services.state_notifications import agent_changes
    tenant_id, user_id = await _seed(pg_engine)
    chat_id = f'notify_{uuid.uuid4().hex}'
    run_id = f't_{uuid.uuid4().hex}'
    await _insert_chat(tenant_id, user_id, chat_id)
    scope = dict(tenant_id=tenant_id, user_id=user_id)
    async with session_scope(**scope) as session:
        await AgentRunsRepo(session).create(run_id=run_id, tenant_id=tenant_id,
            chat_id=chat_id, creator_user_id=user_id, client_request_id=run_id, input_snapshot={})
    async with agent_changes(run_id) as changed:
        await asyncio.wait_for(changed.wait(), 5)
        changed.clear()
        async with session_scope(**scope) as session:
            repo = AgentRunsRepo(session)
            for seq in range(1, 6):
                await repo.append_event(run_id=run_id, seq=seq, event_type='message_delta',
                    payload={'text': str(seq)}, tenant_id=tenant_id)
            await asyncio.sleep(0.02)
            assert not changed.is_set()
        await asyncio.wait_for(changed.wait(), 5)
        async with session_scope(**scope) as session:
            repo = AgentRunsRepo(session)
            cursor, found = 0, []
            while rows := await repo.list_events(run_id, cursor, limit=2):
                assert len(rows) <= 2
                found.extend(row.seq for row in rows)
                cursor = rows[-1].seq
            assert found == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_chat_activity_commits_broadcast_to_all_observers(pg_engine):
    import asyncio
    from vibecanvas_api.services.state_notifications import state_changes
    tenant_id, user_id = await _seed(pg_engine)
    chat_id = f'activity_{uuid.uuid4().hex}'
    await _insert_chat(tenant_id, user_id, chat_id)
    async with state_changes('flowork_chat_activity', chat_id) as first:
        async with state_changes('flowork_chat_activity', chat_id) as second:
            # Listener readiness also triggers reconciliation.
            await asyncio.wait_for(first.wait(), 3)
            await asyncio.wait_for(second.wait(), 3)
            first.clear()
            second.clear()
            async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
                await AgentRunsRepo(session).create(
                    run_id=f'turn_{uuid.uuid4().hex}', tenant_id=tenant_id,
                    chat_id=chat_id, creator_user_id=user_id,
                    client_request_id=uuid.uuid4().hex, input_snapshot={'content': 'test'},
                )
                await session.flush()
                assert not first.is_set() and not second.is_set()
            await asyncio.wait_for(first.wait(), 3)
            await asyncio.wait_for(second.wait(), 3)

@pytest.mark.asyncio
async def test_failed_turn_history_is_bounded_to_actor_chat_and_requested_runs(pg_engine):
    tenant_id, user_id = await _seed(pg_engine)
    chat_id = f'failed_history_{uuid.uuid4().hex}'
    await _insert_chat(tenant_id, user_id, chat_id)
    failed_id, success_id = f't_{uuid.uuid4().hex}', f't_{uuid.uuid4().hex}'
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRunsRepo(session)
        for run_id, kind, payload in [
            (failed_id, 'error', {'code': 'authorization_unavailable', 'message': 'Authorization unavailable'}),
            (success_id, 'done', {'ok': True}),
        ]:
            await repo.create(run_id=run_id, tenant_id=tenant_id, chat_id=chat_id,
                creator_user_id=user_id, client_request_id=run_id, input_snapshot={})
            await repo.append_event(run_id=run_id, seq=1, event_type=kind, payload=payload, tenant_id=tenant_id)
    async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
        repo = AgentRunsRepo(session)
        assert await repo.failed_turns_for_history(chat_id, [failed_id, success_id], creator_user_id=user_id) == {
            failed_id: {'code': 'authorization_unavailable', 'message': 'Authorization unavailable'},
        }
        assert not await repo.failed_turns_for_history(chat_id, [success_id], creator_user_id=user_id)
        assert not await repo.failed_turns_for_history('other_chat', [failed_id], creator_user_id=user_id)
        assert not await repo.failed_turns_for_history(chat_id, [failed_id], creator_user_id=str(uuid.uuid4()))
        assert not await repo.failed_turns_for_history(chat_id, [], creator_user_id=user_id)
