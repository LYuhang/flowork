"""Deployment authorization, secret action, sharing, and revoke matrix."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from vibecanvas_api.app import build_app
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.config import config


class _RelationshipStore:
    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()

    async def read(self, *, tuple_key, **_kwargs):
        return OpenFgaReadPage(
            (tuple_key,) if tuple_key in self.tuples else ()
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

    def _role(self, user: str, object_: str, roles: set[str]) -> bool:
        return any(self._has(user, role, object_) for role in roles)

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
            if relation == "can_create_resource":
                return self._role(
                    user,
                    object_,
                    {"owner", "admin", "member"},
                )
            return False
        if object_.startswith("workflow:"):
            roles = {"viewer", "editor", "operator", "manager"}
            if relation == "can_view_metadata":
                return self._role(user, object_, roles)
            if relation in {"can_deploy", "can_manage_access"}:
                return self._role(user, object_, {"manager"})
            return False
        if not object_.startswith("deployment:"):
            return False
        content_roles = {"viewer", "editor", "operator", "manager"}
        if relation == "can_view_metadata":
            return self._role(user, object_, content_roles) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin", "auditor"},
                )
            )
        if relation in {"can_view", "can_inspect_runs"}:
            return self._role(user, object_, content_roles)
        if relation == "can_update":
            return self._role(user, object_, {"editor", "manager"})
        if relation in {"can_execute", "can_cancel"}:
            return self._role(user, object_, {"operator", "manager"})
        if relation == "can_manage_secret":
            return self._role(user, object_, {"manager"}) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin"},
                )
            )
        if relation in {"can_delete", "can_manage_access"}:
            return self._role(user, object_, {"manager"}) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin"},
                )
            )
        return False


from tests.test_workflow_authorization_integration import (
    _browser_sessions, _headers, _register,
)


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
async def test_deployment_roles_secret_action_and_revoke(
    pg_engine,
    monkeypatch,
):
    monkeypatch.setattr(config, "resource_sharing_enabled", True)
    store = _RelationshipStore()
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner = await _register(client, "deployment_owner")
        viewer_token, viewer = await _register(
            client,
            "deployment_viewer",
        )
        editor_token, editor = await _register(
            client,
            "deployment_editor",
        )
        operator_token, operator = await _register(
            client,
            "deployment_operator",
        )
        outsider_token, outsider = await _register(
            client,
            "deployment_outsider",
        )
        guest_token, guest = await _register(client, "deployment_guest")
        auditor_token, auditor = await _register(
            client,
            "deployment_auditor",
        )
        admin_token, admin = await _register(client, "deployment_admin")
        for member in (viewer, editor, operator, outsider):
            await _join_active_organization(
                pg_engine,
                user_id=member["user_id"],
                organization_id=owner["tenant_id"],
            )
        await _join_active_organization(
            pg_engine,
            user_id=guest["user_id"],
            organization_id=owner["tenant_id"],
            role="guest",
        )
        store.tuples.add(OpenFgaTuple(
            f"user:{guest['user_id']}",
            "guest",
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

        workflow_response = await client.post(
            "/api/v1/workflows",
            json={"name": "Deployment source workflow"},
            headers=_headers(owner_token),
        )
        assert workflow_response.status_code == 201, workflow_response.text
        workflow_id = workflow_response.json()["wf_id"]

        body = {
            "wf_id": workflow_id,
            "name": "Private deployment",
            "slug": f"private-{uuid.uuid4().hex[:10]}",
            "trigger_type": "api",
            "version_pin": "head",
        }
        guest_create = await client.post(
            "/api/v1/deployments",
            json=body,
            headers=_headers(guest_token),
        )
        assert guest_create.status_code == 404

        created = await client.post(
            "/api/v1/deployments",
            json=body,
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        deployment = created.json()
        deployment_id = deployment["id"]
        assert deployment["api_key"]
        assert deployment["access"]["effective_role"] == "manager"
        assert "manage_secret" in deployment["access"]["capabilities"]
        assert deployment["provenance"]["ownership_scope"] == "personal"
        assert deployment["provenance"]["origin_type"] == "created"
        assert deployment["provenance"]["owner"]["display_name"] == (
            "deployment_owner"
        )
        assert OpenFgaTuple(
            f"organization:{owner['tenant_id']}",
            "organization",
            f"deployment:{deployment_id}",
        ) in store.tuples

        for token, is_admin in (
            (auditor_token, False),
            (admin_token, True),
        ):
            inventory = await client.get(
                "/api/v1/deployments",
                headers=_headers(token),
            )
            assert inventory.status_code == 200, inventory.text
            item = inventory.json()["items"][0]
            assert item["id"] == deployment_id
            assert item["name"] == "Private deployment"
            assert "api_key" not in item
            assert "api_key_hash" not in item
            assert "hmac_secret_ref" not in item
            capabilities = set(item["access"]["capabilities"])
            assert "view_metadata" in capabilities
            assert "view" not in capabilities
            assert "inspect_runs" not in capabilities
            assert ("manage_access" in capabilities) is is_admin
            assert ("manage_secret" in capabilities) is is_admin
            assert (
                await client.get(
                    f"/api/v1/deployments/{deployment_id}",
                    headers=_headers(token),
                )
            ).status_code == 404

        assert (
            await client.get(
                "/api/v1/deployments",
                headers=_headers(viewer_token),
            )
        ).json()["items"] == []
        assert (
            await client.get(
                f"/api/v1/deployments/{deployment_id}",
                headers=_headers(outsider_token),
            )
        ).status_code == 404

        async def grant(relation: str, subject: dict) -> None:
            resolved = await client.post(
                f"/api/v1/resource-access/deployment/{deployment_id}/resolve-target",
                json={
                    "target_type": "user",
                    "identifier": subject["email"],
                },
                headers=_headers(owner_token),
            )
            assert resolved.status_code == 200, resolved.text
            target = resolved.json()["target"]
            assert target is not None
            response = await client.post(
                f"/api/v1/deployments/{deployment_id}/access",
                json={
                    "relation": relation,
                    "resolution_token": target["resolution_token"],
                },
                headers=_headers(
                    owner_token,
                    **{
                        "Idempotency-Key": (
                            f"grant-deployment-{relation}-{subject['user_id']}"
                        )
                    },
                ),
            )
            assert response.status_code == 201, response.text

        await grant("viewer", viewer)
        await grant("editor", editor)
        await grant("operator", operator)

        for token, expected_source in [(owner_token, 'created'), (viewer_token, 'shared')]:
            for source in ('all', 'created', 'shared'):
                listed = await client.get(f'/api/v1/deployments?source={source}&limit=1', headers=_headers(token))
                assert listed.status_code == 200, listed.text
                expected = source in ('all', expected_source)
                assert listed.json()['total'] == int(expected)
                assert [item['id'] for item in listed.json()['items']] == ([deployment_id] if expected else [])
                summary = listed.json()['summary']
                assert summary['active'] + summary['disabled'] == int(expected)
            beyond = await client.get('/api/v1/deployments?source=all&limit=1&offset=1', headers=_headers(token))
            assert beyond.json()['items'] == []
            assert beyond.json()['total'] == 1

        shared = await client.get(
            "/api/v1/resource-access/shared?resource_type=deployment",
            headers=_headers(viewer_token),
        )
        assert shared.status_code == 200, shared.text
        assert [item["resource_id"] for item in shared.json()["items"]] == [
            deployment_id
        ]
        assert shared.json()["items"][0]["name"] == "Private deployment"
        assert shared.json()["items"][0]["access"]["effective_role"] == (
            "viewer"
        )

        visible = await client.get(
            "/api/v1/deployments",
            headers=_headers(viewer_token),
        )
        assert [
            item["id"] for item in visible.json()["items"]
        ] == [deployment_id]
        access = visible.json()["items"][0]["access"]
        assert access["effective_role"] == "viewer"
        assert "view" in access["capabilities"]
        assert "update" not in access["capabilities"]
        assert "manage_secret" not in access["capabilities"]
        assert "api_key_hash" not in visible.json()["items"][0]

        for reader_token in (viewer_token, editor_token):
            history = await client.get(
                f"/api/v1/deployments/{deployment_id}/history",
                headers=_headers(reader_token),
            )
            assert history.status_code == 200, history.text
            detail = await client.get(
                f"/api/v1/deployments/{deployment_id}",
                headers=_headers(reader_token),
            )
            capabilities = set(detail.json()["access"]["capabilities"])
            assert "inspect_runs" in capabilities
            assert "execute" not in capabilities
            assert "manage_secret" not in capabilities

        updated = await client.patch(
            f"/api/v1/deployments/{deployment_id}",
            json={"name": "Editor changed name"},
            headers=_headers(editor_token),
        )
        assert updated.status_code == 200, updated.text
        operator_update = await client.patch(
            f"/api/v1/deployments/{deployment_id}",
            json={"name": "Operator cannot edit"},
            headers=_headers(operator_token),
        )
        assert operator_update.status_code == 404
        operator_detail = await client.get(
            f"/api/v1/deployments/{deployment_id}",
            headers=_headers(operator_token),
        )
        operator_capabilities = operator_detail.json()["access"][
            "capabilities"
        ]
        assert "execute" in operator_capabilities
        assert "manage_secret" not in operator_capabilities
        denied_rotate = await client.post(
            f"/api/v1/deployments/{deployment_id}/rotate-key",
            headers=_headers(operator_token),
        )
        assert denied_rotate.status_code == 404
        rotated = await client.post(
            f"/api/v1/deployments/{deployment_id}/rotate-key",
            headers=_headers(owner_token),
        )
        assert rotated.status_code == 200
        assert rotated.json()["api_key"]

        structural_revoke = await client.request(
            "DELETE",
            f"/api/v1/deployments/{deployment_id}/access",
            json={
                "relation": "manager",
                "subject_type": "user",
                "subject_id": owner["user_id"],
                "subject_relation": None,
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "cannot-revoke-deployment-creator"},
            ),
        )
        assert structural_revoke.status_code == 409

        binding = {
            "relation": "viewer",
            "subject_type": "user",
            "subject_id": viewer["user_id"],
            "subject_relation": None,
        }
        revoked = await client.request(
            "DELETE",
            f"/api/v1/deployments/{deployment_id}/access",
            json=binding,
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "revoke-deployment-viewer"},
            ),
        )
        assert revoked.status_code == 200, revoked.text
        assert (
            await client.get(
                f"/api/v1/deployments/{deployment_id}",
                headers=_headers(viewer_token),
            )
        ).status_code == 404

        deleted = await client.delete(
            f"/api/v1/deployments/{deployment_id}",
            headers=_headers(owner_token),
        )
        assert deleted.status_code == 204, deleted.text
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"deployment:{deployment_id}",
        ) not in store.tuples
        assert (
            await client.get(
                "/api/v1/deployments",
                headers=_headers(operator_token),
            )
        ).json()["items"] == []
