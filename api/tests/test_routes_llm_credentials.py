"""LLM credential route secrecy, ownership, and lifecycle."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


def _body(
    *,
    name: str = "OpenAI Prod",
    api_key: str = "fixture-key-original",
    proxy: str | None = None,
) -> dict:
    return {
        "name": name,
        "description": "prod key",
        "provider": "openai",
        "model_name": "gpt-4o",
        "model_context_tokens": 128000,
        "api_url": "https://api.openai.com/v1",
        "proxy": proxy,
        "api_key": api_key,
    }


async def _register(client, label: str) -> tuple[str, dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{label}_{uuid.uuid4().hex[:12]}@example.com",
            "username": label,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return token, me


async def _join_active_organization(
    pg_engine,
    *,
    user_id: str,
    organization_id: str,
) -> None:
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO org_memberships(
                    membership_id, user_id, tenant_id, org_role, status
                ) VALUES (
                    gen_random_uuid(), :user_id, :organization_id,
                    'member', 'active'
                )
                """
            ),
            {
                "user_id": uuid.UUID(user_id),
                "organization_id": uuid.UUID(organization_id),
            },
        )
        await connection.execute(
            text(
                """
                UPDATE sessions
                SET tenant_id = :organization_id,
                    active_organization_id = :organization_id,
                    generation = generation + 1
                WHERE user_id = :user_id
                """
            ),
            {
                "user_id": uuid.UUID(user_id),
                "organization_id": uuid.UUID(organization_id),
            },
        )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_llm_credential_crud_never_leaks_from_normal_views(
    client,
    pg_engine,
):
    token, me = await _register(client, "credential_owner")
    headers = _headers(token)
    secret = "fixture-key-exact"
    created = await client.post(
        "/api/v1/llm-credentials",
        json=_body(
            api_key=secret,
            proxy="http://user:pass@proxy.example:8080",
        ),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    owner = created.json()
    credential_id = owner["id"]
    assert owner["api_key_set"] is True
    assert owner["runtime_scope"] == "codex"
    assert owner["model_name"] == "gpt-4o"
    assert owner["access"]["effective_role"] == "owner"
    assert "manage_secret" in owner["access"]["capabilities"]
    assert secret not in created.text

    listed = await client.get("/api/v1/llm-credentials", headers=headers)
    assert listed.status_code == 200
    public = listed.json()[0]
    assert public["id"] == credential_id
    assert public["runtime_scope"] == "codex"
    for forbidden in ("api_key", "model_name", "api_url", "proxy"):
        assert forbidden not in public
    assert secret not in listed.text
    assert "proxy.example" not in listed.text

    detail = await client.get(
        f"/api/v1/llm-credentials/{credential_id}",
        headers=headers,
    )
    assert detail.status_code == 200
    assert detail.json()["proxy"] == "http://proxy.example:8080"
    assert "api_key" not in detail.json()
    assert "user:pass" not in detail.text
    assert secret not in detail.text

    reveal_removed = await client.get(
        f"/api/v1/llm-credentials/{credential_id}/reveal",
        headers=headers,
    )
    assert reveal_removed.status_code == 404

    updated = await client.put(
        f"/api/v1/llm-credentials/{credential_id}",
        json={
            "name": "Renamed",
            "model_name": "gpt-4o-mini",
            "model_context_tokens": 200000,
            "proxy": "http://new-proxy:3128",
        },
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["model_context_tokens"] == 200000
    assert updated.json()["proxy"] == "http://new-proxy:3128"
    rotated = await client.put(
        f"/api/v1/llm-credentials/{credential_id}",
        json={"api_key": "fixture-key-rotated"},
        headers=headers,
    )
    assert rotated.status_code == 200
    kept = await client.put(
        f"/api/v1/llm-credentials/{credential_id}",
        json={"api_key": ""},
        headers=headers,
    )
    assert kept.status_code == 200
    deleted = await client.delete(
        f"/api/v1/llm-credentials/{credential_id}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert (
        await client.get(
            f"/api/v1/llm-credentials/{credential_id}",
            headers=headers,
        )
    ).status_code == 404
    assert (
        await client.get("/api/v1/llm-credentials", headers=headers)
    ).json() == []

    async with pg_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT c.enabled, c.deleted_at, "
                    "s.status AS secret_status, s.ciphertext, s.wrapped_dek "
                    "FROM llm_credentials AS c "
                    "JOIN encrypted_secrets AS s ON s.secret_id = c.secret_ref "
                    "WHERE c.id = CAST(:id AS uuid)"
                ),
                {"id": credential_id},
            )
        ).one()
    assert row.enabled is False
    assert row.deleted_at is not None
    assert row.secret_status == "destroyed"
    assert row.ciphertext is None
    assert row.wrapped_dek is None


@pytest.mark.asyncio
async def test_llm_credential_cross_organization_is_indistinguishable(
    client,
    pg_engine,
):
    owner_token, _owner = await _register(client, "credential_owner")
    outsider_token, _outsider = await _register(
        client,
        "credential_outsider",
    )
    created = await client.post(
        "/api/v1/llm-credentials",
        json=_body(),
        headers=_headers(owner_token),
    )
    assert created.status_code == 201
    credential_id = created.json()["id"]

    outsider_headers = _headers(outsider_token)
    assert (
        await client.get(
            "/api/v1/llm-credentials",
            headers=outsider_headers,
        )
    ).json() == []
    response = await client.get(
        f"/api/v1/llm-credentials/{credential_id}",
        headers=outsider_headers,
    )
    assert response.status_code == 404
    assert (
        await client.delete(
            f"/api/v1/llm-credentials/{credential_id}",
            headers=outsider_headers,
        )
    ).status_code == 404

    owner_detail = await client.get(
        f"/api/v1/llm-credentials/{credential_id}",
        headers=_headers(owner_token),
    )
    assert owner_detail.status_code == 200
    assert "api_key" not in owner_detail.json()


@pytest.mark.asyncio
async def test_llm_credential_is_not_a_tenant_wide_runtime_model(
    client,
    pg_engine,
    openfga_allow_all,
    monkeypatch,
):
    # The shared route-test double intentionally permits every authorization
    # decision.  This test exercises the private-resource boundary itself, so
    # make only LLM credential decisions relationship-aware while preserving
    # the permissive organization bootstrap used by the fixture.
    def _owns(user: str, object_: str) -> bool:
        return any(
            item.user == user
            and item.object == object_
            and item.relation in {"owner", "manager", "consumer"}
            for item in openfga_allow_all.tuples
        )

    async def _check(*, user, object_, **_kwargs):
        if object_.startswith("llm_credential:"):
            return _owns(user, object_)
        return True

    async def _batch_check(checks, **_kwargs):
        return tuple(
            _owns(user, object_)
            if object_.startswith("llm_credential:")
            else True
            for user, _relation, object_ in checks
        )

    async def _list_objects(*, user, object_type, **_kwargs):
        prefix = f"{object_type}:"
        return tuple(sorted({
            item.object.removeprefix(prefix)
            for item in openfga_allow_all.tuples
            if item.object.startswith(prefix)
            and item.user == user
            and item.relation in {"owner", "manager", "consumer"}
        }))

    monkeypatch.setattr(openfga_allow_all, "check", _check, raising=False)
    monkeypatch.setattr(openfga_allow_all, "batch_check", _batch_check)
    monkeypatch.setattr(openfga_allow_all, "list_objects", _list_objects)

    owner_token, owner = await _register(client, "credential_owner")
    outsider_token, outsider = await _register(
        client,
        "same_org_outsider",
    )
    await _join_active_organization(
        pg_engine,
        user_id=outsider["user_id"],
        organization_id=owner["tenant_id"],
    )
    created = await client.post(
        "/api/v1/llm-credentials",
        json=_body(name="Owner private model"),
        headers=_headers(owner_token),
    )
    assert created.status_code == 201, created.text
    credential_id = created.json()["id"]

    outsider_headers = _headers(outsider_token)
    assert (
        await client.get(
            "/api/v1/llm-credentials",
            headers=outsider_headers,
        )
    ).json() == []
    assert (
        await client.get(
            f"/api/v1/llm-credentials/{credential_id}",
            headers=outsider_headers,
        )
    ).status_code == 404

    owner_capabilities = await client.get(
        "/api/v1/agent-runtime/capabilities",
        headers=_headers(owner_token),
    )
    outsider_capabilities = await client.get(
        "/api/v1/agent-runtime/capabilities",
        headers=outsider_headers,
    )
    assert owner_capabilities.status_code == 200
    assert outsider_capabilities.status_code == 200
    owner_model_ids = {
        model["id"] for model in owner_capabilities.json()["models"]
    }
    outsider_model_ids = {
        model["id"] for model in outsider_capabilities.json()["models"]
    }
    private_model_id = f"codex:credential:{credential_id}"
    assert private_model_id in owner_model_ids
    assert private_model_id not in outsider_model_ids
