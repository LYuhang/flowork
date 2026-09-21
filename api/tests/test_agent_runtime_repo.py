from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.agent_runtime_repo import AgentRuntimeRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
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
    async with session_scope(tenant_id=tenant_id) as session:
        await ChatRepo(session, user_id).register_session(
            "__chat_runtime",
            name="Runtime",
            chat_id=chat_id,
            surface="chat",
        )
        await session.commit()


@pytest.mark.asyncio
async def test_chat_runtime_binding_is_immutable_after_first_start(pg_engine) -> None:
    tenant_id, user_id = await _seed(pg_engine)
    first_chat = f"runtime_first_{uuid.uuid4().hex[:8]}"
    second_chat = f"runtime_second_{uuid.uuid4().hex[:8]}"
    await _insert_chat(tenant_id, user_id, first_chat)
    await _insert_chat(tenant_id, user_id, second_chat)

    async with session_scope(tenant_id=tenant_id) as session:
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

    async with session_scope(tenant_id=tenant_id) as session:
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

    async with session_scope(tenant_id=tenant_id) as session:
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

    async with session_scope(tenant_id=tenant_id) as session:
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
            match="runtime connection is fixed for the Chat",
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

    async with session_scope(tenant_id=tenant_id) as session:
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

    async with session_scope(tenant_id=tenant_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        # A later browser/resume may report another zone.  The Chat clock is
        # immutable and must not follow it.
        resumed = await repo.bind_chat(
            chat_id,
            user_timezone="America/New_York",
        )

    assert resumed == first

    async with session_scope(tenant_id=tenant_id) as session:
        prefs = await AgentRuntimeRepo(session, user_id).get_preferences()
    assert prefs["preferred_timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_codex_thread_ref_rotates_only_with_matching_previous_ref(
    pg_engine,
) -> None:
    tenant_id, user_id = await _seed(pg_engine)
    chat_id = f"runtime_codex_{uuid.uuid4().hex[:8]}"
    await _insert_chat(tenant_id, user_id, chat_id)

    async with session_scope(tenant_id=tenant_id) as session:
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

    async with session_scope(tenant_id=tenant_id) as session:
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

    async with session_scope(tenant_id=tenant_id) as session:
        repo = AgentRuntimeRepo(session, user_id)
        with pytest.raises(ValueError, match="runtime state ref conflict"):
            await repo.set_runtime_state_ref(
                chat_id,
                runtime_type="codex",
                runtime_session_id=binding["runtime_session_id"],
                state_ref="codex-thread-3",
                previous_state_ref="codex-thread-1",
            )
