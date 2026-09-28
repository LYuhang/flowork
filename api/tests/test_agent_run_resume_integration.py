from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.agent_runs_repo import AgentRunActiveError, AgentRunsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo


async def _register(client) -> tuple[dict[str, str], dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"resume_{uuid.uuid4().hex[:12]}@example.com",
            "username": "Resume User",
            "password": "pw12345678",
        },
    )
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return headers, me


def _parse_sse(text_body: str) -> list[tuple[int | None, str, dict]]:
    events: list[tuple[int | None, str, dict]] = []
    for block in text_body.replace("\r\n", "\n").split("\n\n"):
        event_id: int | None = None
        event_type = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("id:"):
                event_id = int(line[3:].strip())
            elif line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((event_id, event_type, json.loads("\n".join(data_lines))))
    return events


async def _seed_chat(
    me: dict,
    *,
    scope_id: str,
    chat_id: str,
    surface: str,
) -> None:
    async with session_scope(tenant_id=me["tenant_id"], user_id=me["user_id"]) as session:
        project = await ChatProjectRepo(session, me["user_id"]).create(name="Resume", surface=surface)
        await ChatRepo(session, me["user_id"]).register_session(
            scope_id,
            project_id=project["project_id"],
            chat_id=chat_id,
            surface=surface,
        )


@pytest.mark.asyncio
async def test_agent_run_reservation_is_idempotent_and_exclusive(client, app_engine):
    headers, me = await _register(client)
    boot = await client.get("/api/v1/chats/bootstrap?surface=chat", headers=headers)
    assert boot.status_code == 200, boot.text
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = f"c_run_reservation_{uuid.uuid4().hex[:8]}"

    await _seed_chat(me, scope_id=scope_id, chat_id=chat_id, surface="chat")

    common = {
        "tenant_id": me["tenant_id"],
        "chat_id": chat_id,
        "creator_user_id": me["user_id"],
        "input_snapshot": {"content": "hello"},
    }
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        first, created = await AgentRunsRepo(session).create_exclusive(
            run_id="turn_reservation_first",
            client_request_id="request_reservation_same",
            **common,
        )
    assert created is True
    assert first.run_id == "turn_reservation_first"

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        replay, created = await AgentRunsRepo(session).create_exclusive(
            run_id="turn_reservation_retry_should_not_win",
            client_request_id="request_reservation_same",
            **common,
        )
    assert created is False
    assert replay.run_id == "turn_reservation_first"

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        with pytest.raises(AgentRunActiveError) as conflict:
            await AgentRunsRepo(session).create_exclusive(
                run_id="turn_reservation_second",
                client_request_id="request_reservation_different",
                **common,
            )
    assert conflict.value.run_id == "turn_reservation_first"


@pytest.mark.asyncio
async def test_durable_chat_control_state_is_isolated_between_same_tenant_users(
    client, app_engine,
):
    """Tenant RLS is not a substitute for per-user Chat ownership checks."""
    headers, me = await _register(client)
    boot = await client.get("/api/v1/chats/bootstrap?surface=browser", headers=headers)
    assert boot.status_code == 200, boot.text

    suffix = uuid.uuid4().hex[:10]
    intruder_user_id = str(uuid.uuid4())
    chat_id = f"c_owner_isolation_{suffix}"
    run_id = f"turn_owner_isolation_{suffix}"
    hitl_request_id = f"hitl_owner_isolation_{suffix}"
    artifact_id = f"ia_owner_isolation_{suffix}"

    async with app_engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id',:tenant,false)"),
            {"tenant": me["tenant_id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email, display_name) "
                "VALUES (:user_id, :tenant_id, :email, 'Intruder')"
            ),
            {
                "user_id": intruder_user_id,
                "tenant_id": me["tenant_id"],
                "email": f"intruder_{suffix}@example.com",
            },
        )
        await connection.commit()

    await _seed_chat(
        me,
        scope_id=boot.json()["carrier_scope_id"],
        chat_id=chat_id,
        surface="browser",
    )
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        await session.execute(
            text(
                "UPDATE chats SET browser_control_status='attached', "
                "browser_session_id=:browser_session_id WHERE chat_id=:chat_id"
            ),
            {"browser_session_id": f"bs_{suffix}", "chat_id": chat_id},
        )

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        runs = AgentRunsRepo(session)
        run = await runs.create(
            run_id=run_id,
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            creator_user_id=me["user_id"],
            client_request_id=f"request_{suffix}",
            input_snapshot={},
        )
        hitl = HitlRepo(session)
        await hitl.create_interactive_artifact(
            artifact_id=artifact_id,
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            run_id=run_id,
            component_type="slider",
            completion_mode="wait_for_submit",
            title="Choose",
            definition_json={},
            artifact_ref=None,
            content_hash=None,
        )
        await hitl.create_request(
            hitl_request_id=hitl_request_id,
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            run_id=run_id,
            artifact_id=artifact_id,
            hitl_type="post_tool_review",
            title="Review",
            prompt_text="Review result",
            ui_payload_json={},
            agent_payload_json={},
            runtime_correlation_json={},
        )
        await hitl.link_artifact_hitl(artifact_id, hitl_request_id)
        assert run.status == "waiting_approval"

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        owner_runs = AgentRunsRepo(session)
        owner_hitl = HitlRepo(session)
        assert await owner_runs.get_for_chat(
            chat_id, run_id, creator_user_id=me["user_id"],
        ) is not None
        assert await owner_hitl.get_request_for_user(
            hitl_request_id, me["user_id"],
        ) is not None
        assert await owner_hitl.get_artifact_for_user(
            artifact_id, me["user_id"],
        ) is not None
        assert await ChatRepo(session, me["user_id"]).get_browser_binding(chat_id) is not None

        assert await owner_runs.get_for_chat(
            chat_id, run_id, creator_user_id=intruder_user_id,
        ) is None
        assert await owner_runs.get_by_client_request(
            chat_id,
            f"request_{suffix}",
            creator_user_id=intruder_user_id,
        ) is None
        assert await owner_runs.request_cancel(
            chat_id, run_id, creator_user_id=intruder_user_id,
        ) is False
        assert await owner_hitl.get_request_for_user(
            hitl_request_id, intruder_user_id,
        ) is None
        assert await owner_hitl.get_artifact_for_user(
            artifact_id, intruder_user_id,
        ) is None
        assert await ChatRepo(session, intruder_user_id).get_browser_binding(chat_id) is None


@pytest.mark.asyncio
async def test_completed_run_can_be_found_by_request_and_replayed_from_cursor(
    client, app_engine,
):
    headers, me = await _register(client)
    boot = await client.get("/api/v1/chats/bootstrap?surface=browser", headers=headers)
    assert boot.status_code == 200, boot.text
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = "c_resume_contract"
    run_id = "t_resume_contract"
    request_id = "client_request_resume_contract"
    private_marker = "SENSITIVE_REPLAY_MARKER_31f0"

    await _seed_chat(me, scope_id=scope_id, chat_id=chat_id, surface="browser")

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        runs = AgentRunsRepo(session)
        await runs.create(
            run_id=run_id,
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            creator_user_id=me["user_id"],
            client_request_id=request_id,
            input_snapshot={},
            input_message_id=f"{chat_id}:user:{run_id}",
        )
        for seq, event_type, payload in (
            (1, "started", {"turn_id": run_id}),
            (
                2,
                "CHAT_UPDATE",
                {
                    "message_id": f"{chat_id}:assistant:{run_id}",
                    "delta": f"hello {private_marker}",
                },
            ),
            (3, "done", {"ok": True}),
        ):
            await runs.append_event(
                run_id=run_id,
                seq=seq,
                tenant_id=me["tenant_id"],
                event_type=event_type,
                payload=payload,
            )

    async with app_engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id',:tenant,false)"),
            {"tenant": me["tenant_id"]},
        )
        stored = (
            await connection.execute(
                text(
                    "SELECT r.private_ciphertext, "
                    "string_agg(e.payload_ciphertext, '') AS event_ciphertext "
                    "FROM agent_runs r JOIN agent_run_events e "
                    "ON e.run_id=r.run_id WHERE r.run_id=:run_id "
                    "GROUP BY r.private_ciphertext"
                ),
                {"run_id": run_id},
            )
        ).mappings().one()
        old_columns = (
            await connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND ("
                    "(table_name='agent_runs' AND column_name IN "
                    "('input_snapshot','error_message')) OR "
                    "(table_name='agent_run_events' AND column_name='payload'))"
                )
            )
        ).all()
    assert private_marker not in stored["private_ciphertext"]
    assert private_marker not in stored["event_ciphertext"]
    assert old_columns == []

    recovered = await client.get(
        f"/api/v1/chats/{chat_id}/turns/by-client-request/{request_id}",
        headers=headers,
    )
    assert recovered.status_code == 200, recovered.text
    body = recovered.json()
    assert body["run_id"] == run_id
    assert body["input_message_id"] == f"{chat_id}:user:{run_id}"
    assert "base_checkpoint_id" not in body
    assert "result_checkpoint_id" not in body
    assert body["last_event_id"] == 3

    replay = await client.get(
        f"/api/v1/chats/{chat_id}/turns/{run_id}/stream",
        headers={**headers, "Last-Event-ID": "1"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers["x-turn-id"] == run_id
    events = _parse_sse(replay.text)
    assert [(event_id, event_type) for event_id, event_type, _ in events] == [
        (2, "CHAT_UPDATE"),
        (3, "done"),
    ]
    assert events[0][2]["message_id"] == f"{chat_id}:assistant:{run_id}"
    assert events[0][2]["delta"] == f"hello {private_marker}"


@pytest.mark.asyncio
async def test_run_lookup_is_scoped_to_its_chat(client, app_engine):
    headers, me = await _register(client)
    boot = await client.get("/api/v1/chats/bootstrap?surface=chat", headers=headers)
    scope_id = boot.json()["carrier_scope_id"]
    chat_id = "c_resume_scope_owner"
    run_id = "t_resume_scope_owner"

    await _seed_chat(me, scope_id=scope_id, chat_id=chat_id, surface="chat")

    async with session_scope(tenant_id=me["tenant_id"]) as session:
        run = await AgentRunsRepo(session).create(
            run_id=run_id,
            tenant_id=me["tenant_id"],
            chat_id=chat_id,
            creator_user_id=me["user_id"],
            client_request_id="request_scope",
            input_snapshot={},
        )
        run.status = "completed"

    response = await client.get(
        f"/api/v1/chats/not_the_owner/turns/{run_id}/stream",
        headers=headers,
    )
    assert response.status_code == 404
