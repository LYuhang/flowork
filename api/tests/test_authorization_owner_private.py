"""Owner-private authorization seam and same-organization IDOR regressions."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from vibecanvas_api.auth.tokens import new_token
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> tuple[str, dict]:
    email = f"authz_owner_{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "username": "Resource Owner",
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()["session_token"]
    me = (await client.get("/api/v1/auth/me", headers=_headers(token))).json()
    return token, me


async def _same_org_member_token(app_engine, owner: dict) -> tuple[str, str]:
    raw, token_hash = new_token()
    user_id = str(uuid.uuid4())
    suffix = uuid.uuid4().hex[:10]
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": owner["tenant_id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email, display_name) "
                "VALUES (:user_id, :tenant_id, :email, 'Same org member')"
            ),
            {
                "user_id": user_id,
                "tenant_id": owner["tenant_id"],
                "email": f"same_org_{suffix}@example.com",
            },
        )
        await connection.execute(
            text(
                "INSERT INTO org_memberships("
                "membership_id, user_id, tenant_id, org_role"
                ") VALUES (gen_random_uuid(), :user_id, :tenant_id, 'member')"
            ),
            {"user_id": user_id, "tenant_id": owner["tenant_id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO sessions("
                "token_hash, user_id, tenant_id, active_organization_id, "
                "expires_at"
                ") VALUES ("
                ":token_hash, :user_id, :tenant_id, :tenant_id, :expires_at"
                ")"
            ),
            {
                "token_hash": token_hash,
                "user_id": user_id,
                "tenant_id": owner["tenant_id"],
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            },
        )
    return raw, user_id


async def _seed_private_run(
    *,
    app_engine,
    owner: dict,
    chat_id: str,
    run_id: str,
) -> None:
    key = f"run/{owner['tenant_id']}/{run_id}/result.txt"
    get_object_store().put_bytes(key, b"owner only", "text/plain")
    async with session_scope(tenant_id=owner["tenant_id"], user_id=owner["user_id"]) as session:
        from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
        project = await ChatProjectRepo(session, owner["user_id"]).create(name="Private")
        await ChatRepo(session, owner["user_id"]).register_session(
            f"__chat_{owner['user_id'].replace('-', '')[:24]}",
            project_id=project["project_id"],
            name="Private Chat",
            chat_id=chat_id,
        )
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": owner["tenant_id"]},
        )
        await connection.execute(
            text(
                "INSERT INTO vfs_run("
                "run_id, path, object_key, content_type, size_bytes"
                ") VALUES (:run_id, '/run/result.txt', :key, 'text/plain', 10)"
            ),
            {"run_id": run_id, "key": key},
        )
    async with session_scope(tenant_id=owner["tenant_id"]) as session:
        runs = AgentRunsRepo(session)
        await runs.create(
            run_id=run_id,
            tenant_id=owner["tenant_id"],
            chat_id=chat_id,
            creator_user_id=owner["user_id"],
            client_request_id=f"request_{run_id}",
            input_snapshot={},
        )
        await runs.append_event(
            run_id=run_id,
            seq=1,
            event_type="done",
            payload={},
            tenant_id=owner["tenant_id"],
        )


@pytest.mark.asyncio
async def test_registration_creates_personal_organization_owner(
    client, app_engine,
):
    _, me = await _register(client)
    async with app_engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": me["tenant_id"]},
        )
        organization = (
            await connection.execute(
                text(
                    "SELECT kind, name FROM organizations "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": me["tenant_id"]},
            )
        ).one()
        role = (
            await connection.execute(
                text(
                    "SELECT org_role FROM org_memberships "
                    "WHERE tenant_id = :tenant_id AND user_id = :user_id"
                ),
                {"tenant_id": me["tenant_id"], "user_id": me["user_id"]},
            )
        ).scalar_one()
    assert organization.kind == "personal"
    # A personal organization name is fixed product copy; the user's display
    # name lives only in the encrypted identity profile and is not duplicated
    # into another plaintext table.
    assert organization.name == "Personal workspace"
    assert role == "owner"


@pytest.mark.asyncio
async def test_same_org_member_cannot_read_owner_run_vfs(
    client, app_engine, openfga_allow_all, monkeypatch,
):
    owner_token, owner = await _register(client)
    intruder_token, _ = await _same_org_member_token(app_engine, owner)
    suffix = uuid.uuid4().hex[:10]
    chat_id = f"chat_authz_{suffix}"
    run_id = f"run_authz_{suffix}"
    await _seed_private_run(
        app_engine=app_engine,
        owner=owner,
        chat_id=chat_id,
        run_id=run_id,
    )

    async def relationship_batch(checks, **_kwargs):
        return tuple(
            user == f"user:{owner['user_id']}"
            for user, _relation, _object in checks
        )

    async def relationship_list(*, user, object_type, **_kwargs):
        if user != f"user:{owner['user_id']}":
            return ()
        return (chat_id,) if object_type == "chat" else ()

    monkeypatch.setattr(openfga_allow_all, "batch_check", relationship_batch)
    monkeypatch.setattr(openfga_allow_all, "list_objects", relationship_list)

    owner_vfs = await client.get(
        f"/api/v1/vfs/runs/{run_id}",
        headers=_headers(owner_token),
    )
    assert owner_vfs.status_code == 200, owner_vfs.text
    owner_content = await client.get(
        "/api/v1/vfs/content",
        params={"run_id": run_id, "path": "/run/result.txt"},
        headers=_headers(owner_token),
    )
    assert owner_content.status_code == 200, owner_content.text
    assert owner_content.json()["content"] == "owner only"
    owner_sign = await client.post(
        "/api/v1/vfs/sign",
        json={"run_id": run_id, "path": "/run/result.txt"},
        headers=_headers(owner_token),
    )
    assert owner_sign.status_code == 200, owner_sign.text

    denied_requests = (
        await client.get(
            f"/api/v1/vfs/runs/{run_id}",
            headers=_headers(intruder_token),
        ),
        await client.get(
            "/api/v1/vfs/content",
            params={"run_id": run_id, "path": "/run/result.txt"},
            headers=_headers(intruder_token),
        ),
        await client.post(
            "/api/v1/vfs/sign",
            json={"run_id": run_id, "path": "/run/result.txt"},
            headers=_headers(intruder_token),
        ),
    )
    assert [response.status_code for response in denied_requests] == [404] * 3
