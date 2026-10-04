"""OpenFGA enforcement for private LLM and MCP installations."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from tests.test_workflow_authorization_integration import _headers, _register, _browser_sessions
from sqlalchemy import text

from vibecanvas_api.app import build_app
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.services.agent_runtime.mcp_host_resolution import (
    McpSelectionError,
    resolve_custom_mcp_authority,
)


class _RelationshipStore:
    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()

    async def read(self, *, tuple_key, **_kwargs):
        return OpenFgaReadPage(
            (tuple_key,) if tuple_key in self.tuples else (),
        )

    async def write(self, *, writes=(), deletes=()):
        self.tuples.update(writes)
        self.tuples.difference_update(deletes)

    async def batch_check(self, checks, **_kwargs):
        return tuple(
            self._allowed(user, relation, object_)
            for user, relation, object_ in checks
        )

    async def list_objects(
        self,
        *,
        user,
        relation,
        object_type,
        **_kwargs,
    ):
        objects = {
            item.object
            for item in self.tuples
            if item.object.startswith(f"{object_type}:")
        }
        return tuple(
            object_.split(":", 1)[1]
            for object_ in sorted(objects)
            if self._allowed(user, relation, object_)
        )

    def _has(self, user: str, relation: str, object_: str) -> bool:
        return OpenFgaTuple(user, relation, object_) in self.tuples

    def _organization_for(self, object_: str) -> str | None:
        for item in self.tuples:
            if item.object == object_ and item.relation == "organization":
                return item.user
        return None

    def _organization_role(
        self,
        user: str,
        object_: str,
        roles: set[str],
    ) -> bool:
        organization = self._organization_for(object_)
        return bool(
            organization
            and any(self._has(user, role, organization) for role in roles)
        )

    def _allowed(self, user: str, relation: str, object_: str) -> bool:
        if object_.startswith("organization:"):
            return relation == "can_create_resource" and any(
                self._has(user, role, object_)
                for role in {"owner", "admin", "member"}
            )
        if object_.startswith("mcp_installation:"):
            if relation == "can_view_metadata":
                return self._has(user, "installer", object_) or (
                    self._organization_role(
                        user,
                        object_,
                        {"owner", "admin", "auditor"},
                    )
                )
            if relation == "can_delete":
                return self._has(user, "installer", object_) or (
                    self._organization_role(
                        user,
                        object_,
                        {"owner", "admin"},
                    )
                )
            return relation.startswith("can_") and self._has(
                user, "installer", object_,
            )
        if object_.startswith("llm_credential:"):
            if relation == "can_view_metadata":
                return self._has(user, "owner", object_) or (
                    self._organization_role(
                        user,
                        object_,
                        {"owner", "admin", "auditor"},
                    )
                )
            if relation == "can_use":
                return any(
                    self._has(user, role, object_)
                    for role in {"owner", "consumer"}
                )
            if relation in {"can_manage_secret", "can_delete"}:
                return self._has(user, "owner", object_) or (
                    self._organization_role(
                        user,
                        object_,
                        {"owner", "admin"},
                    )
                )
            return False
        return False





async def _join_active_organization(
    pg_engine,
    *,
    user_id: str,
    organization_id: str,
    role: str = "member",
) -> None:
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO org_memberships(
                    membership_id, user_id, tenant_id, org_role, status
                ) VALUES (
                    gen_random_uuid(), :user_id, :organization_id,
                    :role, 'active'
                )
                """
            ),
            {
                "user_id": uuid.UUID(user_id),
                "organization_id": uuid.UUID(organization_id),
                "role": role,
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


@pytest.mark.asyncio
async def test_private_installations_never_become_tenant_wide(
    pg_engine,
    monkeypatch,
):

    async def fake_handshake(**_kwargs):
        return {
            "status": "ok",
            "tool_count": 1,
            "tool_names": [{"name": "ping", "description": "ping"}],
            "tools": [],
        }

    monkeypatch.setattr(
        "vibecanvas_api.routes.mcp_servers.handshake_one",
        fake_handshake,
    )
    store = _RelationshipStore()
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner = await _register(client, "private_owner")
        outsider_token, outsider = await _register(
            client,
            "same_org_outsider",
        )
        auditor_token, auditor = await _register(client, "private_auditor")
        admin_token, admin = await _register(client, "private_admin")
        await _join_active_organization(
            pg_engine,
            user_id=outsider["user_id"],
            organization_id=owner["tenant_id"],
        )
        store.tuples.add(OpenFgaTuple(
            f"user:{outsider['user_id']}",
            "member",
            f"organization:{owner['tenant_id']}",
        ))
        for role, member in (("auditor", auditor), ("admin", admin)):
            await _join_active_organization(
                pg_engine,
                user_id=member["user_id"],
                organization_id=owner["tenant_id"],
                role=role,
            )
            store.tuples.add(OpenFgaTuple(
                f"user:{member['user_id']}",
                role,
                f"organization:{owner['tenant_id']}",
            ))

        llm = await client.post(
            "/api/v1/llm-credentials",
            json={
                "name": "Private model",
                "provider": "openai",
                "model_name": "private-model",
                "api_key": "fixture-private-key",
            },
            headers=_headers(owner_token),
        )
        assert llm.status_code == 201, llm.text
        credential_id = llm.json()["id"]
        assert llm.json()["access"]["effective_role"] == "owner"

        mcp = await client.post(
            "/api/v1/mcp-servers",
            json={
                "name": "Private MCP",
                "tool_prefix": "private_mcp",
                "transport": "sse",
                "endpoint": "https://mcp.example.com/sse",
                "auth_config": {
                    "type": "bearer",
                    "token": "fixture-private-token",
                },
            },
            headers=_headers(owner_token),
        )
        assert mcp.status_code == 201, mcp.text
        server_id = mcp.json()["id"]
        assert mcp.json()["access"]["effective_role"] == "installer"

        expected_edges = {
            OpenFgaTuple(
                f"organization:{owner['tenant_id']}",
                "organization",
                f"llm_credential:{credential_id}",
            ),
            OpenFgaTuple(
                f"user:{owner['user_id']}",
                "owner",
                f"llm_credential:{credential_id}",
            ),
            OpenFgaTuple(
                f"organization:{owner['tenant_id']}",
                "organization",
                f"mcp_installation:{server_id}",
            ),
            OpenFgaTuple(
                f"user:{owner['user_id']}",
                "installer",
                f"mcp_installation:{server_id}",
            ),
        }
        assert expected_edges <= store.tuples

        owner_mcp_list = await client.get(
            "/api/v1/mcp-servers",
            headers=_headers(owner_token),
        )
        assert owner_mcp_list.status_code == 200
        assert owner_mcp_list.json()["items"][0]["id"] == server_id
        assert (
            owner_mcp_list.json()["items"][0]["access"]["effective_role"]
            == "installer"
        )

        for token, is_admin in (
            (auditor_token, False),
            (admin_token, True),
        ):
            credential_inventory = await client.get(
                "/api/v1/llm-credentials",
                headers=_headers(token),
            )
            assert credential_inventory.status_code == 200
            credential = credential_inventory.json()[0]
            assert credential["id"] == credential_id
            assert credential["name"] == "Private model"
            assert "model_name" not in credential
            assert "api_url" not in credential
            assert "api_key" not in credential
            credential_caps = set(
                credential["access"]["capabilities"]
            )
            assert "view_metadata" in credential_caps
            assert "use" not in credential_caps
            assert ("manage_secret" in credential_caps) is is_admin
            credential_detail = await client.get(
                f"/api/v1/llm-credentials/{credential_id}",
                headers=_headers(token),
            )
            assert credential_detail.status_code == (200 if is_admin else 404)
            if is_admin:
                assert "api_key" not in credential_detail.json()

            mcp_inventory = await client.get(
                "/api/v1/mcp-servers",
                headers=_headers(token),
            )
            assert mcp_inventory.status_code == 200
            server = mcp_inventory.json()["items"][0]
            assert server["id"] == server_id
            assert server["name"] == "Private MCP"
            assert server["auth_config"].get("token") != (
                "fixture-private-token"
            )
            server_caps = set(server["access"]["capabilities"])
            assert "view_metadata" in server_caps
            assert "view" not in server_caps
            assert "use" not in server_caps
            assert (
                await client.get(
                    f"/api/v1/mcp-servers/{server_id}",
                    headers=_headers(token),
                )
            ).status_code == 404

        outsider_headers = _headers(outsider_token)
        assert (
            await client.get(
                "/api/v1/llm-credentials",
                headers=outsider_headers,
            )
        ).json() == []
        assert (
            await client.get(
                "/api/v1/mcp-servers",
                headers=outsider_headers,
            )
        ).json()["items"] == []
        assert (
            await client.get(
                f"/api/v1/llm-credentials/{credential_id}/reveal",
                headers=outsider_headers,
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/v1/mcp-servers/{server_id}",
                headers=outsider_headers,
            )
        ).status_code == 404

        with pytest.raises(McpSelectionError):
            await resolve_custom_mcp_authority(
                owner["tenant_id"],
                user_id=outsider["user_id"],
                chat_id="chat-private-mcp-test",
                turn_id="turn-private-mcp-test",
                runtime_session_id="runtime-private-mcp-test",
                session_id=str(uuid.uuid4()),
                session_generation=1,
                membership_id=str(uuid.uuid4()),
                server_ids=[server_id],
            )

        assert (
            await client.delete(
                f"/api/v1/llm-credentials/{credential_id}",
                headers=_headers(owner_token),
            )
        ).status_code == 204
        assert (
            await client.delete(
                f"/api/v1/mcp-servers/{server_id}",
                headers=_headers(owner_token),
            )
        ).status_code == 204
        assert not (expected_edges & store.tuples)
