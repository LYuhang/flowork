"""Workflow HTTP authorization, capabilities, sharing, and revoke matrix."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from vibecanvas_api.audit import actions as audit_actions
from vibecanvas_api.audit.repo import AuditRepo
from vibecanvas_api.app import build_app
from vibecanvas_api.auth.ratelimit import LoginRateLimitExceeded
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.config import config
from vibecanvas_api.routes import resource_access as resource_access_routes
from vibecanvas_api.storage.db import session_scope


@pytest.fixture(autouse=True)
def _browser_sessions(monkeypatch):
    monkeypatch.setattr(config, "web_session_cookie_enabled", True)
    # Isolate fixture registrations from deployment Redis and other tests.
    from vibecanvas_api.auth import ratelimit
    monkeypatch.setattr(config, "distributed_auth_rate_limit_enabled", False)
    monkeypatch.setattr(ratelimit, "_local_limiter", ratelimit.LoginRateLimiter(max_attempts=1000, window_seconds=300))
    monkeypatch.setattr(ratelimit, "_local_action_limiters", {})


class _RelationshipStore:
    """Small tuple evaluator; the pinned model itself is tested separately."""

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

    def _role(self, user: str, object_: str, roles: set[str]) -> bool:
        if any(self._has(user, role, object_) for role in roles):
            return True
        for item in self.tuples:
            if item.object != object_ or item.relation not in roles:
                continue
            subject, separator, subject_relation = item.user.partition("#")
            if not separator:
                continue
            if subject.startswith("group:") and subject_relation in {
                "direct_member",
                "member",
            }:
                if self._has(user, "direct_member", subject):
                    return True
            if subject.startswith("organization:") and (
                subject_relation == "member"
                and self._has(user, "member", subject)
            ):
                return True
        return False

    def _allowed(self, user: str, relation: str, object_: str) -> bool:
        if object_.startswith("organization:"):
            if relation == "can_create_resource":
                return self._role(
                    user,
                    object_,
                    {"owner", "admin", "member"},
                )
            if relation == "can_view_metadata":
                return self._role(
                    user,
                    object_,
                    {"owner", "admin", "member", "auditor"},
                )
            if relation == "can_manage_members":
                return self._role(user, object_, {"owner", "admin"})
            return False
        if object_.startswith("group:"):
            if relation == "can_view_metadata":
                return self._role(
                    user,
                    object_,
                    {"manager", "lead", "direct_member"},
                ) or self._organization_role(
                    user,
                    object_,
                    {"owner", "admin", "auditor"},
                )
            return False
        if not object_.startswith("workflow:"):
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
        if relation in {"can_view", "can_export", "can_use", "can_mount"}:
            return self._role(user, object_, content_roles)
        if relation == "can_update":
            return self._role(user, object_, {"editor", "manager"})
        if relation in {
            "can_execute",
            "can_cancel",
            "can_inspect_runs",
        }:
            return self._role(user, object_, {"operator", "manager"})
        if relation == "can_deploy":
            return self._role(user, object_, {"manager"})
        if relation in {"can_delete", "can_manage_access"}:
            return self._role(user, object_, {"manager"}) or (
                self._organization_role(
                    user,
                    object_,
                    {"owner", "admin"},
                )
            )
        return False


def _headers(token: dict[str, str], **extra: str) -> dict[str, str]:
    return {**token, **extra}


def _session_headers(response, client):
    cookies = dict(response.cookies)
    client.cookies.clear()
    return {
        "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
        "X-CSRF-Token": next(value for key, value in cookies.items() if key.endswith("-csrf")),
        "Origin": config.public_urls.public_url or "http://testserver",
    }


async def _register(client: AsyncClient, label: str) -> tuple[dict[str, str], dict]:
    response = await client.post(
        "/api/v1/auth/register",
        headers={"Origin": config.public_urls.public_url or "http://testserver"},
        json={
            "email": f"{label}_{uuid.uuid4().hex[:12]}@example.com",
            "username": label,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    token = _session_headers(response, client)
    me = (
        await client.get(
            "/api/v1/auth/me",
            headers=_headers(token),
        )
    ).json()
    return token, me


async def _register_exact(
    client: AsyncClient,
    label: str,
) -> tuple[dict[str, str], dict, str]:
    email = f"{label}_{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        headers={"Origin": config.public_urls.public_url or "http://testserver"},
        json={
            "email": email,
            "username": label,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    token = _session_headers(response, client)
    me_response = await client.get(
        "/api/v1/auth/me",
        headers=_headers(token),
    )
    assert me_response.status_code == 200, me_response.text
    return token, me_response.json(), email


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
async def test_workflow_direct_share_capabilities_and_revoke(
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
        owner_token, owner = await _register(client, "owner")
        viewer_token, viewer = await _register(client, "viewer")
        outsider_token, outsider = await _register(client, "outsider")
        guest_token, guest = await _register(client, "guest")
        auditor_token, auditor = await _register(client, "auditor")
        admin_token, admin = await _register(client, "admin")
        async with pg_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE organizations SET kind = 'business', "
                    "name = 'Workflow Company' WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": uuid.UUID(owner["tenant_id"])},
            )
        await _join_active_organization(
            pg_engine,
            user_id=viewer["user_id"],
            organization_id=owner["tenant_id"],
        )
        await _join_active_organization(
            pg_engine,
            user_id=outsider["user_id"],
            organization_id=owner["tenant_id"],
        )
        await _join_active_organization(
            pg_engine,
            user_id=guest["user_id"],
            organization_id=owner["tenant_id"],
            role="guest",
        )
        await _join_active_organization(
            pg_engine,
            user_id=auditor["user_id"],
            organization_id=owner["tenant_id"],
            role="auditor",
        )
        await _join_active_organization(
            pg_engine,
            user_id=admin["user_id"],
            organization_id=owner["tenant_id"],
            role="admin",
        )
        store.tuples.add(OpenFgaTuple(
            f"user:{guest['user_id']}",
            "guest",
            f"organization:{owner['tenant_id']}",
        ))
        store.tuples.add(OpenFgaTuple(
            f"user:{auditor['user_id']}",
            "auditor",
            f"organization:{owner['tenant_id']}",
        ))
        store.tuples.add(OpenFgaTuple(
            f"user:{admin['user_id']}",
            "admin",
            f"organization:{owner['tenant_id']}",
        ))
        store.tuples.add(OpenFgaTuple(
            f"user:{viewer['user_id']}",
            "member",
            f"organization:{owner['tenant_id']}",
        ))

        guest_create = await client.post(
            "/api/v1/workflows",
            json={"name": "Guest cannot create"},
            headers=_headers(guest_token),
        )
        assert guest_create.status_code == 404

        created = await client.post(
            "/api/v1/workflows",
            json={
                "name": "Shared workflow",
                "description": "private workflow description",
                "tags": ["private-tag"],
            },
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        workflow = created.json()
        wf_id = workflow["wf_id"]
        assert workflow["access"]["effective_role"] == "manager"
        assert "manage_access" in workflow["access"]["capabilities"]

        group = await client.post(
            f"/api/v1/organizations/{owner['tenant_id']}/groups",
            json={"name": "Platform", "kind": "team"},
            headers=_headers(owner_token),
        )
        assert group.status_code == 201, group.text
        resolved_group = await client.post(
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
            json={"target_type": "group", "identifier": "Platform"},
            headers=_headers(owner_token),
        )
        assert resolved_group.status_code == 200, resolved_group.text
        assert resolved_group.json()["target"]["display_name"] == "Platform"
        assert resolved_group.json()["target"]["detail"] == "Platform"

        resolved_organization = await client.post(
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
            json={"target_type": "organization", "identifier": "ignored"},
            headers=_headers(owner_token),
        )
        assert resolved_organization.status_code == 200
        assert resolved_organization.json()["target"]["display_name"] == (
            "Workflow Company"
        )
        assert resolved_organization.json()["target"]["allowed_relations"] == [
            "viewer", "editor", "operator", "manager"
        ]

        # Relationship-scope grants remain one userset tuple. Membership
        # changes take effect immediately without copying per-user grants.
        group_target = resolved_group.json()["target"]
        group_binding = {
            "relation": "viewer",
            "subject_type": "group",
            "subject_id": str(group.json()["group_id"]),
            "subject_relation": "member",
        }
        group_grant = await client.post(
            f"/api/v1/workflows/{wf_id}/access",
            json={
                "relation": "viewer",
                "resolution_token": group_target["resolution_token"],
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "grant-platform-team"},
            ),
        )
        assert group_grant.status_code == 201, group_grant.text
        group_ref = f"group:{group.json()['group_id']}"
        viewer_ref = f"user:{viewer['user_id']}"
        store.tuples.add(OpenFgaTuple(viewer_ref, "direct_member", group_ref))
        group_visible = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(viewer_token),
        )
        assert group_visible.status_code == 200, group_visible.text
        store.tuples.discard(OpenFgaTuple(
            viewer_ref,
            "direct_member",
            group_ref,
        ))
        group_hidden = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(viewer_token),
        )
        assert group_hidden.status_code == 404
        group_revoke = await client.request(
            "DELETE",
            f"/api/v1/workflows/{wf_id}/access",
            json=group_binding,
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "revoke-platform-team"},
            ),
        )
        assert group_revoke.status_code == 200, group_revoke.text

        organization_target = resolved_organization.json()["target"]
        organization_binding = {
            "relation": "viewer",
            "subject_type": "organization",
            "subject_id": owner["tenant_id"],
            "subject_relation": "member",
        }
        organization_grant = await client.post(
            f"/api/v1/workflows/{wf_id}/access",
            json={
                "relation": "viewer",
                "resolution_token": organization_target["resolution_token"],
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "grant-entire-organization"},
            ),
        )
        assert organization_grant.status_code == 201, organization_grant.text
        organization_visible = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(viewer_token),
        )
        assert organization_visible.status_code == 200
        organization_revoke = await client.request(
            "DELETE",
            f"/api/v1/workflows/{wf_id}/access",
            json=organization_binding,
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "revoke-entire-organization"},
            ),
        )
        assert organization_revoke.status_code == 200

        # Auditors and organization admins can inventory resources without
        # receiving private workflow content.  Their list projection contains
        # only safe inventory metadata and the detail endpoint remains hidden.
        for token, expected_admin_capabilities in (
            (auditor_token, False),
            (admin_token, True),
        ):
            metadata = await client.get(
                "/api/v1/workflows",
                headers=_headers(token),
            )
            assert metadata.status_code == 200, metadata.text
            assert [item["wf_id"] for item in metadata.json()["items"]] == [wf_id]
            item = metadata.json()["items"][0]
            assert item["workflow_name"] == "Shared workflow"
            assert item["description"] == ""
            assert item["tags"] == []
            assert "view_metadata" in item["access"]["capabilities"]
            assert "view" not in item["access"]["capabilities"]
            assert (
                "manage_access" in item["access"]["capabilities"]
            ) is expected_admin_capabilities
            hidden_detail = await client.get(
                f"/api/v1/workflows/{wf_id}",
                headers=_headers(token),
            )
            assert hidden_detail.status_code == 404

        guest_empty_list = await client.get(
            "/api/v1/workflows",
            headers=_headers(guest_token),
        )
        assert guest_empty_list.status_code == 200
        assert guest_empty_list.json()["items"] == []

        empty_list = await client.get(
            "/api/v1/workflows",
            headers=_headers(viewer_token),
        )
        assert empty_list.status_code == 200
        assert empty_list.json()["items"] == []

        binding = {
            "relation": "viewer",
            "subject_type": "user",
            "subject_id": viewer["user_id"],
            "subject_relation": None,
        }
        resolved_viewer = await client.post(
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
            json={
                "target_type": "user",
                "identifier": viewer["email"],
            },
            headers=_headers(owner_token),
        )
        assert resolved_viewer.status_code == 200, resolved_viewer.text
        viewer_target = resolved_viewer.json()["target"]
        assert viewer_target is not None
        granted = await client.post(
            f"/api/v1/workflows/{wf_id}/access",
            json={
                "relation": "viewer",
                "resolution_token": viewer_target["resolution_token"],
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "grant-viewer-1"},
            ),
        )
        assert granted.status_code == 201, granted.text

        visible = await client.get(
            "/api/v1/workflows",
            headers=_headers(viewer_token),
        )
        assert visible.status_code == 200, visible.text
        assert [item["wf_id"] for item in visible.json()["items"]] == [wf_id]
        access = visible.json()["items"][0]["access"]
        assert access["effective_role"] == "viewer"
        assert "view" in access["capabilities"]
        assert "update" not in access["capabilities"]

        guest_binding = {
            **binding,
            "subject_id": guest["user_id"],
        }
        resolved_guest = await client.post(
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
            json={
                "target_type": "user",
                "identifier": guest["email"],
            },
            headers=_headers(owner_token),
        )
        assert resolved_guest.status_code == 200, resolved_guest.text
        guest_target = resolved_guest.json()["target"]
        assert guest_target is not None
        guest_granted = await client.post(
            f"/api/v1/workflows/{wf_id}/access",
            json={
                "relation": "viewer",
                "resolution_token": guest_target["resolution_token"],
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "grant-guest-viewer-1"},
            ),
        )
        assert guest_granted.status_code == 201, guest_granted.text
        guest_visible = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(guest_token),
        )
        assert guest_visible.status_code == 200, guest_visible.text
        assert guest_visible.json()["meta"]["description"] == (
            "private workflow description"
        )

        detail = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(viewer_token),
        )
        assert detail.status_code == 200, detail.text
        denied_update = await client.patch(
            f"/api/v1/workflows/{wf_id}",
            json={"name": "not allowed"},
            headers=_headers(viewer_token),
        )
        assert denied_update.status_code == 404
        outsider_detail = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(outsider_token),
        )
        assert outsider_detail.status_code == 404

        listed = await client.get(
            f"/api/v1/workflows/{wf_id}/access",
            headers=_headers(owner_token),
        )
        assert listed.status_code == 200, listed.text
        assert [
            {
                key: item[key]
                for key in (
                    "relation",
                    "subject_type",
                    "subject_id",
                    "subject_relation",
                    "source",
                )
            }
            for item in listed.json()["items"]
        ] == [
            {**binding, "source": "direct"},
            {**guest_binding, "source": "direct"},
        ]
        assert [item["display_name"] for item in listed.json()["items"]] == [
            "viewer",
            "guest",
        ]
        assert [item["detail"] for item in listed.json()["items"]] == [
            viewer["email"],
            guest["email"],
        ]

        foreign_token, foreign = await _register(client, "foreign")
        out_of_scope = await client.post(
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
            json={"target_type": "user", "identifier": foreign["email"]},
            headers=_headers(owner_token),
        )
        unknown = await client.post(
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
            json={
                "target_type": "user",
                "identifier": f"unknown-{uuid.uuid4().hex}@example.com",
            },
            headers=_headers(owner_token),
        )
        assert out_of_scope.status_code == unknown.status_code == 200
        assert out_of_scope.json() == unknown.json() == {"target": None}
        foreign_grant = await client.post(
            f"/api/v1/workflows/{wf_id}/access",
            json={**binding, "subject_id": foreign["user_id"]},
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "foreign-user-denied"},
            ),
        )
        assert foreign_grant.status_code == 422
        assert foreign_token

        structural_revoke = await client.request(
            "DELETE",
            f"/api/v1/workflows/{wf_id}/access",
            json={
                "relation": "manager",
                "subject_type": "user",
                "subject_id": owner["user_id"],
                "subject_relation": None,
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "cannot-revoke-creator"},
            ),
        )
        assert structural_revoke.status_code == 409

        revoked = await client.request(
            "DELETE",
            f"/api/v1/workflows/{wf_id}/access",
            json=binding,
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "revoke-viewer-1"},
            ),
        )
        assert revoked.status_code == 200, revoked.text
        after_revoke = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(viewer_token),
        )
        assert after_revoke.status_code == 404
        after_revoke_list = await client.get(
            "/api/v1/workflows",
            headers=_headers(viewer_token),
        )
        assert after_revoke_list.json()["items"] == []

        guest_revoked = await client.request(
            "DELETE",
            f"/api/v1/workflows/{wf_id}/access",
            json=guest_binding,
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "revoke-guest-viewer-1"},
            ),
        )
        assert guest_revoked.status_code == 200, guest_revoked.text
        guest_after_revoke = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(guest_token),
        )
        assert guest_after_revoke.status_code == 404
        guest_after_revoke_list = await client.get(
            "/api/v1/workflows",
            headers=_headers(guest_token),
        )
        assert guest_after_revoke_list.status_code == 200
        assert guest_after_revoke_list.json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_role", ["viewer", "editor", "operator", "manager"])
async def test_cross_personal_shared_with_me_rechecks_authoritative_access(
    pg_engine,
    shared_role,
    monkeypatch,
):
    """A projection locates a cross-tenant root but never grants it."""
    monkeypatch.setattr(config, "resource_sharing_enabled", True)
    store = _RelationshipStore()
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner, _ = await _register_exact(client, "share_owner")
        recipient_token, recipient, recipient_email = await _register_exact(
            client,
            "share_recipient",
        )

        created = await client.post(
            "/api/v1/workflows",
            json={
                "name": "Cross-personal workflow",
                "description": "Visible only after current authorization",
            },
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        wf_id = created.json()["wf_id"]

        for unavailable_target in ("group", "organization"):
            unavailable = await client.post(
                f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
                json={
                    "target_type": unavailable_target,
                    "identifier": "Platform",
                },
                headers=_headers(owner_token),
            )
            assert unavailable.status_code == 200
            assert unavailable.json() == {"target": None}

        resolved = await client.post(
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
            json={"target_type": "user", "identifier": recipient_email},
            headers=_headers(owner_token),
        )
        assert resolved.status_code == 200, resolved.text
        target = resolved.json()["target"]
        assert target is not None
        assert target["target_type"] == "user"
        assert set(target["allowed_relations"]) == {"viewer", "editor", "operator", "manager"}

        granted = await client.post(
            f"/api/v1/workflows/{wf_id}/access",
            json={
                "relation": shared_role,
                "resolution_token": target["resolution_token"],
            },
            headers=_headers(
                owner_token,
                **{"Idempotency-Key": "cross-personal-viewer"},
            ),
        )
        assert granted.status_code == 201, granted.text

        shared = await client.get(
            "/api/v1/resource-access/shared?resource_type=workflow",
            headers=_headers(recipient_token),
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["next_offset"] is None
        assert len(shared.json()["items"]) == 1
        item = shared.json()["items"][0]
        assert item["resource_type"] == "workflow"
        assert item["resource_id"] == wf_id
        assert item["name"] == "Cross-personal workflow"
        assert item["description"] == (
            "Visible only after current authorization"
        )
        assert item["access"]["effective_role"] == shared_role
        assert item["access"]["source"] == "shared"
        assert item["provenance"]["ownership_scope"] == "personal"
        assert item["provenance"]["origin_type"] == "created"
        assert "tenant" not in item

        detail = await client.get(
            f"/api/v1/workflows/{wf_id}",
            headers=_headers(recipient_token),
        )
        assert detail.status_code == 200, detail.text

        from types import SimpleNamespace
        from vibecanvas_api.services.agent_resources.authorization import require_workflow_action, list_authorized_workflows
        from vibecanvas_api.services.agent_resources.workflow_transfer import read_workflow_snapshot
        from vibecanvas_api.authorization.types import Action
        from vibecanvas_api.agents.tools.decorator import ToolError
        agent = SimpleNamespace(tenant_id=recipient["active_organization_id"], username=recipient["user_id"],
            turn_id="shared-workflow-test", authorization_client=store,
            authorization_membership_role="owner", authorization_membership_status="active")
        discovered = await list_authorized_workflows(agent)
        assert [row["wf_id"] for row in discovered] == [wf_id]
        assert discovered[0]["access"]["effective_role"] == shared_role
        assert await list_authorized_workflows(agent, limit=1, offset=1) == []
        snapshot = await read_workflow_snapshot(agent, workflow_id=wf_id)
        assert snapshot["id"] == wf_id
        for action, roles in [(Action.UPDATE, {"editor", "manager"}), (Action.EXECUTE, {"operator", "manager"})]:
            if shared_role in roles:
                assert (await require_workflow_action(agent, wf_id, action)).allowed
            else:
                with pytest.raises(ToolError, match="permission_denied"):
                    await require_workflow_action(agent, wf_id, action)

        chat_id = "shared-canvas-" + uuid.uuid4().hex
        chat_body = {"workflow_context": {"workflow_id": wf_id, "major_version": 1,
                     "initial_subversion": 0, "target": {"kind": "workflow"}}}
        chat_response = await client.put(f"/api/v1/chat-scopes/{wf_id}/chats/{chat_id}",
                                         json=chat_body, headers=_headers(recipient_token))
        assert chat_response.status_code == (200 if shared_role in {"editor", "manager"} else 404), chat_response.text
        if chat_response.status_code == 200:
            from vibecanvas_api.storage.db import session_scope
            from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
            project_id = chat_response.json()["project_id"]
            async with session_scope(tenant_id=recipient["active_organization_id"], user_id=recipient["user_id"]) as session:
                project = await ChatProjectRepo(session, recipient["user_id"]).get(project_id)
                assert project is not None
                assert str(project.tenant_id) == recipient["active_organization_id"]
                assert str(project.creator_user_id) == recipient["user_id"]
            async with session_scope(tenant_id=owner["active_organization_id"], user_id=owner["user_id"]) as session:
                assert await ChatProjectRepo(session, owner["user_id"]).get(project_id) is None

        if shared_role == "manager":
            third_token, third, third_email = await _register_exact(client, "reshare_recipient")
            target_response = await client.post(
                f"/api/v1/resource-access/workflow/{wf_id}/resolve-target",
                json={"target_type": "user", "identifier": third_email},
                headers=_headers(recipient_token),
            )
            assert target_response.status_code == 200, target_response.text
            target = target_response.json()["target"]
            assert target is not None
            reshared = await client.post(
                f"/api/v1/workflows/{wf_id}/access",
                json={"relation": "viewer", "resolution_token": target["resolution_token"]},
                headers=_headers(recipient_token, **{"Idempotency-Key": "manager-reshare"}),
            )
            assert reshared.status_code == 201, reshared.text
            bindings = await client.get(f"/api/v1/workflows/{wf_id}/access", headers=_headers(recipient_token))
            assert bindings.status_code == 200, bindings.text
            assert any(item["subject_id"] == third["user_id"] for item in bindings.json()["items"])
            third_detail = await client.get(f"/api/v1/workflows/{wf_id}", headers=_headers(third_token))
            assert third_detail.status_code == 200, third_detail.text
            revoked = await client.request(
                "DELETE", f"/api/v1/workflows/{wf_id}/access",
                json={"relation": "viewer", "subject_type": "user", "subject_id": third["user_id"]},
                headers=_headers(recipient_token, **{"Idempotency-Key": "manager-revoke"}),
            )
            assert revoked.status_code == 200, revoked.text
            assert (await client.get(f"/api/v1/workflows/{wf_id}", headers=_headers(third_token))).status_code == 404

        # Simulate an authoritative tuple already removed while a stale
        # recipient locator remains. The list must fail closed and expose no
        # resource metadata from the projection alone.
        store.tuples.discard(OpenFgaTuple(
            f"user:{recipient['user_id']}",
            shared_role,
            f"workflow:{wf_id}",
        ))
        assert await list_authorized_workflows(agent) == []
        with pytest.raises(ToolError, match="workflow_unavailable"):
            await read_workflow_snapshot(agent, workflow_id=wf_id)
        stale_projection = await client.get(
            "/api/v1/resource-access/shared?resource_type=workflow",
            headers=_headers(recipient_token),
        )
        assert stale_projection.status_code == 200, stale_projection.text
        assert stale_projection.json()["items"] == []


@pytest.mark.asyncio
async def test_share_lookup_rate_limit_is_audited_outside_failed_request(
    pg_engine,
    monkeypatch,
):
    assert pg_engine is not None
    monkeypatch.setattr(config, "resource_sharing_enabled", True)
    store = _RelationshipStore()
    app = build_app()
    app.state.openfga_client = store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        owner_token, owner, _ = await _register_exact(client, "lookup_audit")
        created = await client.post(
            "/api/v1/workflows",
            json={"name": "Lookup audit workflow"},
            headers=_headers(owner_token),
        )
        assert created.status_code == 201, created.text
        wf_id = created.json()["wf_id"]
        lookup_url = (
            f"/api/v1/resource-access/workflow/{wf_id}/resolve-target"
        )
        not_found = await client.post(
            lookup_url,
            json={
                "target_type": "user",
                "identifier": f"missing-{uuid.uuid4().hex}@example.com",
            },
            headers=_headers(owner_token),
        )
        assert not_found.status_code == 200
        assert not_found.json() == {"target": None}

        async def reject_lookup(*_args, **_kwargs):
            raise LoginRateLimitExceeded

        monkeypatch.setattr(
            resource_access_routes,
            "consume_rate_limited_action",
            reject_lookup,
        )
        limited = await client.post(
            lookup_url,
            json={
                "target_type": "user",
                "identifier": f"limited-{uuid.uuid4().hex}@example.com",
            },
            headers=_headers(owner_token),
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        assert limited.json()["detail"] == "share_target_lookup_rate_limited"

    async with session_scope(
        tenant_id=owner["tenant_id"],
        user_id=owner["user_id"],
    ) as session:
        records = await AuditRepo(session).list_for_tenant(
            action=audit_actions.SHARE_LOOKUP,
            limit=10,
        )
    relevant = [item for item in records if item.target_id == wf_id]
    assert {item.meta.get("lookup_outcome") for item in relevant} >= {
        "not_found",
        "rate_limited",
    }
    assert all("identifier_hash" in item.meta for item in relevant)
    assert all("identifier" not in item.meta for item in relevant)
