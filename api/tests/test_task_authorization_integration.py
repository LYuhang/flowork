"""Task HTTP authorization, capabilities, sharing, and revoke matrix."""

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
                return self._role(user, object_, roles) or (
                    self._organization_role(
                        user,
                        object_,
                        {"owner", "admin", "auditor"},
                    )
                )
            if relation in {"can_view", "can_use"}:
                return self._role(user, object_, roles)
            return False
        if not object_.startswith("task:"):
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
        if relation in {
            "can_view",
            "can_export",
            "can_inspect_runs",
        }:
            return self._role(user, object_, content_roles)
        if relation == "can_update":
            return self._role(user, object_, {"editor", "manager"})
        if relation in {"can_execute", "can_cancel", "can_resume"}:
            return self._role(user, object_, {"operator", "manager"})
        if relation in {"can_delete", "can_manage_access"}:
            return self._role(user, object_, {"manager"}) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin"},
                )
            )
        return False


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


async def _register(client: AsyncClient, label: str) -> tuple[str, dict]:
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
    me = (
        await client.get(
            "/api/v1/auth/me",
            headers=_headers(token),
        )
    ).json()
    return token, me


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
async def test_task_direct_roles_capabilities_and_revoke(
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
        owner_token, owner = await _register(client, "task_owner")
        viewer_token, viewer = await _register(client, "task_viewer")
        operator_token, operator = await _register(client, "task_operator")
        outsider_token, outsider = await _register(client, "task_outsider")
        guest_token, guest = await _register(client, "task_guest")
        auditor_token, auditor = await _register(client, "task_auditor")
        admin_token, admin = await _register(client, "task_admin")
        for member in (viewer, operator, outsider):
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
            json={"name": "Task source workflow"},
            headers=_headers(owner_token),
        )
        assert workflow_response.status_code == 201, workflow_response.text
        workflow_id = workflow_response.json()["wf_id"]

        guest_create = await client.post(
            "/api/v1/tasks/scheduled-runs",
            json={
                "name": "Guest schedule",
                "workflow_id": workflow_id,
                "schedule_type": "interval",
                "interval_seconds": 3600,
            },
            headers=_headers(guest_token),
        )
        assert guest_create.status_code == 404

        created = await client.post(
            "/api/v1/tasks/scheduled-runs",
            json={
                "name": "Shared schedule",
                "workflow_id": workflow_id,
                "schedule_type": "interval",
                "interval_seconds": 3600,
            },
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        task = created.json()["task"]
        task_id = task["id"]
        assert task["access"]["effective_role"] == "manager"
        assert "manage_access" in task["access"]["capabilities"]
        assert task["provenance"]["ownership_scope"] == "personal"
        assert task["provenance"]["origin_type"] == "created"
        assert task["provenance"]["owner"]["display_name"] == "task_owner"
        assert OpenFgaTuple(
            f"organization:{owner['tenant_id']}",
            "organization",
            f"task:{task_id}",
        ) in store.tuples
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"task:{task_id}",
        ) in store.tuples

        for token, is_admin in (
            (auditor_token, False),
            (admin_token, True),
        ):
            inventory = await client.get(
                "/api/v1/tasks",
                headers=_headers(token),
            )
            assert inventory.status_code == 200, inventory.text
            item = inventory.json()["items"][0]
            assert item["id"] == task_id
            assert item["status"] == "enabled"
            assert item["payload"] == {}
            assert item["result"] is None
            assert item["results_uri"] is None
            assert item["error"] is None
            assert item["background_job_id"] is None
            assert item["sandbox_status"] is None
            capabilities = set(item["access"]["capabilities"])
            assert "view_metadata" in capabilities
            assert "view" not in capabilities
            assert "inspect_runs" not in capabilities
            assert ("manage_access" in capabilities) is is_admin
            assert (
                await client.get(
                    f"/api/v1/tasks/{task_id}",
                    headers=_headers(token),
                )
            ).status_code == 404

        hidden = await client.get(
            "/api/v1/tasks",
            headers=_headers(viewer_token),
        )
        assert hidden.status_code == 200
        assert hidden.json()["items"] == []
        hidden_summary = await client.get(
            "/api/v1/tasks/summary",
            headers=_headers(outsider_token),
        )
        assert hidden_summary.status_code == 200
        assert hidden_summary.json()["enabled"] == 0

        async def grant(relation: str, subject: dict) -> None:
            resolved = await client.post(
                f"/api/v1/resource-access/task/{task_id}/resolve-target",
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
                f"/api/v1/tasks/{task_id}/access",
                json={
                    "relation": relation,
                    "resolution_token": target["resolution_token"],
                },
                headers=_headers(
                    owner_token,
                    **{
                        "Idempotency-Key": (
                            f"grant-task-{relation}-{subject['user_id']}"
                        )
                    },
                ),
            )
            assert response.status_code == 201, response.text

        await grant("viewer", viewer)
        await grant("operator", operator)

        shared = await client.get(
            "/api/v1/resource-access/shared?resource_type=task",
            headers=_headers(viewer_token),
        )
        assert shared.status_code == 200, shared.text
        assert [item["resource_id"] for item in shared.json()["items"]] == [
            task_id
        ]
        assert shared.json()["items"][0]["name"] == "Shared schedule"
        assert shared.json()["items"][0]["access"]["effective_role"] == (
            "viewer"
        )

        visible = await client.get(
            "/api/v1/tasks",
            headers=_headers(viewer_token),
        )
        assert [item["id"] for item in visible.json()["items"]] == [task_id]
        access = visible.json()["items"][0]["access"]
        assert access["effective_role"] == "viewer"
        assert "view" in access["capabilities"]
        assert "export" in access["capabilities"]
        assert "cancel" not in access["capabilities"]

        viewer_cancel = await client.post(
            f"/api/v1/tasks/{task_id}/cancel",
            json={"mode": "soft"},
            headers=_headers(viewer_token),
        )
        assert viewer_cancel.status_code == 404
        operator_cancel = await client.post(
            f"/api/v1/tasks/{task_id}/cancel",
            json={"mode": "soft"},
            headers=_headers(operator_token),
        )
        # The schedule itself is enabled rather than queued/running; reaching
        # the state conflict proves authorization passed for the operator.
        assert operator_cancel.status_code == 409

        structural_revoke = await client.request(
            "DELETE",
            f"/api/v1/tasks/{task_id}/access",
            json={
                "relation": "manager",
                "subject_type": "user",
                "subject_id": owner["user_id"],
                "subject_relation": None,
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "cannot-revoke-task-creator"},
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
            f"/api/v1/tasks/{task_id}/access",
            json=binding,
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "revoke-task-viewer"},
            ),
        )
        assert revoked.status_code == 200, revoked.text
        assert (
            await client.get(
                f"/api/v1/tasks/{task_id}",
                headers=_headers(viewer_token),
            )
        ).status_code == 404
        assert (
            await client.get(
                "/api/v1/tasks",
                headers=_headers(viewer_token),
            )
        ).json()["items"] == []

        deleted = await client.delete(
            f"/api/v1/tasks/scheduled-runs/{task_id}",
            headers=_headers(owner_token),
        )
        assert deleted.status_code == 200, deleted.text
        assert OpenFgaTuple(
            f"organization:{owner['tenant_id']}",
            "organization",
            f"task:{task_id}",
        ) not in store.tuples
        assert OpenFgaTuple(
            f"user:{owner['user_id']}",
            "manager",
            f"task:{task_id}",
        ) not in store.tuples
        # Direct bindings are reconciler-owned after root deletion, while the
        # tenant SQL intersection immediately prevents stale tuple exposure.
        assert (
            await client.get(
                "/api/v1/tasks",
                headers=_headers(operator_token),
            )
        ).json()["items"] == []
