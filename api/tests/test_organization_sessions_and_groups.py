"""Server-owned organization context and generic group regressions."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models_authorization import AuthzMutation
from vibecanvas_api.storage.repo_org import GroupRepo


pytestmark = pytest.mark.asyncio


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


async def _register(client, *, prefix: str) -> tuple[str, dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{prefix}_{uuid.uuid4().hex[:12]}@example.com",
            "username": prefix,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session_token"], response.json()


async def test_business_organization_switch_is_server_owned_and_rotates_generation(
    client,
):
    token, registered = await _register(client, prefix="org-switch")
    personal_id = registered["session"]["active_organization_id"]
    assert registered["session"]["generation"] == 1

    created = await client.post(
        "/api/v1/organizations",
        headers=_headers(token),
        json={"name": "Acme Security", "slug": f"acme-{uuid.uuid4().hex[:8]}"},
    )
    assert created.status_code == 201, created.text
    business_id = created.json()["organization_id"]

    listed = await client.get(
        "/api/v1/organizations",
        headers=_headers(token),
    )
    assert listed.status_code == 200, listed.text
    assert {
        item["organization_id"] for item in listed.json()["items"]
    } == {personal_id, business_id}
    assert listed.json()["active_organization_id"] == personal_id
    assert "manage_members" in next(
        item for item in listed.json()["items"]
        if item["organization_id"] == personal_id
    )["access"]["capabilities"]

    # Arbitrary organization headers are ignored; only the Session switch API
    # can change the server-owned active organization.
    forged = await client.get(
        "/api/v1/auth/me",
        headers=_headers(
            token,
            **{"X-Organization-ID": business_id, "X-Tenant-ID": business_id},
        ),
    )
    assert forged.status_code == 200
    assert forged.json()["active_organization_id"] == personal_id

    wrong_path = await client.get(
        f"/api/v1/organizations/{business_id}/groups",
        headers=_headers(token),
    )
    assert wrong_path.status_code == 404

    missing = await client.post(
        "/api/v1/organizations/active",
        headers=_headers(token),
        json={"organization_id": str(uuid.uuid4())},
    )
    assert missing.status_code == 404
    still_personal = await client.get(
        "/api/v1/auth/me",
        headers=_headers(token),
    )
    assert still_personal.json()["session"]["generation"] == 1

    switched = await client.post(
        "/api/v1/organizations/active",
        headers=_headers(token),
        json={"organization_id": business_id},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["active"] is True
    assert switched.json()["role"] == "owner"
    assert switched.json()["session_generation"] == 2

    me = await client.get("/api/v1/auth/me", headers=_headers(token))
    assert me.status_code == 200
    assert me.json()["active_organization_id"] == business_id
    assert me.json()["membership"]["role"] == "owner"
    assert me.json()["session"]["generation"] == 2

    switched_back = await client.post(
        "/api/v1/organizations/active",
        headers=_headers(token),
        json={"organization_id": personal_id},
    )
    assert switched_back.status_code == 200
    assert switched_back.json()["session_generation"] == 3


async def test_inactive_membership_immediately_invalidates_active_session(
    client,
    app_engine,
):
    token, registered = await _register(client, prefix="suspended")
    created = await client.post(
        "/api/v1/organizations",
        headers=_headers(token),
        json={
            "name": "Suspended Organization",
            "slug": f"suspended-{uuid.uuid4().hex[:8]}",
        },
    )
    business_id = created.json()["organization_id"]
    switched = await client.post(
        "/api/v1/organizations/active",
        headers=_headers(token),
        json={"organization_id": business_id},
    )
    assert switched.status_code == 200

    async with app_engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE org_memberships NO FORCE ROW LEVEL SECURITY")
        )
        await connection.execute(
            text(
                "UPDATE org_memberships SET status = 'suspended' "
                "WHERE tenant_id = :organization_id "
                "AND user_id = :user_id"
            ),
            {
                "organization_id": business_id,
                "user_id": registered["user"]["user_id"],
            },
        )
        await connection.execute(
            text("ALTER TABLE org_memberships FORCE ROW LEVEL SECURITY")
        )

    denied = await client.get("/api/v1/auth/me", headers=_headers(token))
    assert denied.status_code == 401
    assert "membership is not active" in denied.json()["detail"]


async def test_owner_can_manage_member_role_but_cannot_remove_last_owner(
    client,
    app_engine,
):
    owner_token, owner = await _register(client, prefix="member-admin-owner")
    member_token, member = await _register(client, prefix="member-admin-user")
    organization_id = owner["session"]["active_organization_id"]
    owner_id = owner["user"]["user_id"]
    member_id = member["user"]["user_id"]

    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": organization_id},
        )
        await connection.execute(
            text(
                "INSERT INTO org_memberships("
                "membership_id, user_id, tenant_id, org_role, status"
                ") VALUES ("
                "gen_random_uuid(), :user_id, :tenant_id, 'member', 'active'"
                ")"
            ),
            {"user_id": member_id, "tenant_id": organization_id},
        )

    promoted = await client.patch(
        f"/api/v1/organizations/{organization_id}/members/{member_id}",
        headers=_headers(owner_token),
        json={"role": "admin", "status": "active"},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "admin"
    assert promoted.json()["status"] == "active"

    switched_admin = await client.post(
        "/api/v1/organizations/active",
        headers=_headers(member_token),
        json={"organization_id": organization_id},
    )
    assert switched_admin.status_code == 200, switched_admin.text

    admin_created_group = await client.post(
        f"/api/v1/organizations/{organization_id}/groups",
        headers=_headers(member_token),
        json={"name": "Admin managed", "kind": "team"},
    )
    assert admin_created_group.status_code == 201, admin_created_group.text

    # Administrators cannot change owners. An owner changing their own role
    # must also satisfy the separate last-active-owner invariant.
    admin_cannot_remove_last_owner = await client.patch(
        f"/api/v1/organizations/{organization_id}/members/{owner_id}",
        headers=_headers(member_token),
        json={"role": "member", "status": "active"},
    )
    assert admin_cannot_remove_last_owner.status_code == 403
    assert (
        admin_cannot_remove_last_owner.json()["detail"]
        == "organization_owner_required"
    )

    last_owner = await client.patch(
        f"/api/v1/organizations/{organization_id}/members/{owner_id}",
        headers=_headers(owner_token),
        json={"role": "member", "status": "active"},
    )
    assert last_owner.status_code == 409
    assert last_owner.json()["detail"] == "organization_requires_active_owner"


async def test_group_hierarchy_cycle_depth_and_active_organization_boundary(
    client,
):
    token, registered = await _register(client, prefix="groups")
    personal_id = registered["session"]["active_organization_id"]

    root_response = await client.post(
        f"/api/v1/organizations/{personal_id}/groups",
        headers=_headers(token),
        json={"name": "Engineering", "kind": "department"},
    )
    assert root_response.status_code == 201, root_response.text
    root = root_response.json()
    assert "manage_members" in root["access"]["capabilities"]

    child_response = await client.post(
        f"/api/v1/organizations/{personal_id}/groups",
        headers=_headers(token),
        json={
            "name": "Agent Platform",
            "kind": "team",
            "parent_group_id": root["group_id"],
        },
    )
    assert child_response.status_code == 201, child_response.text
    child = child_response.json()

    cycle = await client.patch(
        f"/api/v1/organizations/{personal_id}/groups/{root['group_id']}",
        headers=_headers(token),
        json={"parent_group_id": child["group_id"]},
    )
    assert cycle.status_code == 400
    assert cycle.json()["detail"] == "group_cycle"

    duplicate = await client.post(
        f"/api/v1/organizations/{personal_id}/groups",
        headers=_headers(token),
        json={"name": "engineering", "kind": "team"},
    )
    assert duplicate.status_code == 409

    archive_parent = await client.delete(
        f"/api/v1/organizations/{personal_id}/groups/{root['group_id']}",
        headers=_headers(token),
    )
    assert archive_parent.status_code == 409
    assert archive_parent.json()["detail"] == "group_has_active_children"

    groups = await client.get(
        f"/api/v1/organizations/{personal_id}/groups",
        headers=_headers(token),
    )
    assert [item["name"] for item in groups.json()["items"]] == [
        "Engineering",
        "Agent Platform",
    ]

    foreign_path = await client.get(
        f"/api/v1/organizations/{uuid.uuid4()}/groups",
        headers=_headers(token),
    )
    assert foreign_path.status_code == 404


async def test_group_direct_membership_inherits_active_ancestor_groups(
    client,
    app_engine,
):
    owner_token, owner = await _register(client, prefix="group-owner")
    _member_token, member = await _register(client, prefix="group-member")
    organization_id = owner["session"]["active_organization_id"]
    member_id = member["user"]["user_id"]

    # Company invitation remains product-gated. Seed the already-existing user
    # as an active organization member to exercise the generic group semantics.
    async with app_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": organization_id},
        )
        await connection.execute(
            text(
                "INSERT INTO org_memberships("
                "membership_id, user_id, tenant_id, org_role, status"
                ") VALUES ("
                "gen_random_uuid(), :user_id, :tenant_id, 'member', 'active'"
                ")"
            ),
            {"user_id": member_id, "tenant_id": organization_id},
        )

    root = (
        await client.post(
            f"/api/v1/organizations/{organization_id}/groups",
            headers=_headers(owner_token),
            json={"name": "Engineering", "kind": "department"},
        )
    ).json()
    child = (
        await client.post(
            f"/api/v1/organizations/{organization_id}/groups",
            headers=_headers(owner_token),
            json={
                "name": "Backend",
                "kind": "team",
                "parent_group_id": root["group_id"],
            },
        )
    ).json()
    added = await client.put(
        (
            f"/api/v1/organizations/{organization_id}/groups/"
            f"{child['group_id']}/members/{member_id}"
        ),
        headers=_headers(owner_token),
        json={"role": "member", "status": "active"},
    )
    assert added.status_code == 200, added.text

    self_summary = await client.get(
        f"/api/v1/organizations/{organization_id}/me",
        headers=_headers(owner_token),
    )
    assert self_summary.status_code == 200, self_summary.text
    assert self_summary.json()["membership"]["user_id"] == owner["user"]["user_id"]
    assert self_summary.json()["groups"] == []

    member_summary = await client.get(
        f"/api/v1/organizations/{organization_id}/me",
        headers=_headers(_member_token),
    )
    # The member's original Session is bound to their personal organization;
    # an explicit server-owned organization switch is required before the
    # company membership can be projected.
    assert member_summary.status_code == 404

    switched_member = await client.post(
        "/api/v1/organizations/active",
        headers=_headers(_member_token),
        json={"organization_id": organization_id},
    )
    assert switched_member.status_code == 200, switched_member.text
    member_summary = await client.get(
        f"/api/v1/organizations/{organization_id}/me",
        headers=_headers(_member_token),
    )
    assert member_summary.status_code == 200, member_summary.text
    assert member_summary.json()["membership"]["user_id"] == member_id
    assert member_summary.json()["membership"]["role"] == "member"
    assert member_summary.json()["groups"] == [{
        "group_id": child["group_id"],
        "kind": "team",
        "name": "Backend",
        "source": "native",
        "role": "member",
        "status": "active",
    }]

    async with session_scope(tenant_id=organization_id) as session:
        effective = await GroupRepo(session).effective_group_ids(
            uuid.UUID(member_id)
        )
    assert set(map(str, effective)) == {
        root["group_id"],
        child["group_id"],
    }

    revoked = await client.delete(
        (
            f"/api/v1/organizations/{organization_id}/groups/"
            f"{child['group_id']}/members/{member_id}"
        ),
        headers=_headers(owner_token),
    )
    assert revoked.status_code == 204
    async with session_scope(tenant_id=organization_id) as session:
        assert await GroupRepo(session).effective_group_ids(
            uuid.UUID(member_id)
        ) == []


async def test_org_and_group_writes_commit_structural_intents_atomically(
    client,
):
    token, registered = await _register(client, prefix="structural-intent")
    organization_id = registered["session"]["active_organization_id"]
    user_id = registered["user"]["user_id"]

    root = (
        await client.post(
            f"/api/v1/organizations/{organization_id}/groups",
            headers=_headers(token),
            json={"name": "Security", "kind": "department"},
        )
    ).json()
    child = (
        await client.post(
            f"/api/v1/organizations/{organization_id}/groups",
            headers=_headers(token),
            json={
                "name": "Authorization",
                "kind": "team",
                "parent_group_id": root["group_id"],
            },
        )
    ).json()
    added = await client.put(
        (
            f"/api/v1/organizations/{organization_id}/groups/"
            f"{child['group_id']}/members/{user_id}"
        ),
        headers=_headers(token),
        json={"role": "lead", "status": "active"},
    )
    assert added.status_code == 200, added.text
    suspended = await client.put(
        (
            f"/api/v1/organizations/{organization_id}/groups/"
            f"{child['group_id']}/members/{user_id}"
        ),
        headers=_headers(token),
        json={"role": "lead", "status": "suspended"},
    )
    assert suspended.status_code == 200, suspended.text

    async with session_scope(organization_id) as session:
        rows = list(
            (
                await session.execute(
                    select(AuthzMutation).order_by(
                        AuthzMutation.requested_at,
                        AuthzMutation.mutation_id,
                    )
                )
            ).scalars()
        )

    def matching(
        *,
        object_type: str,
        object_id: str,
        relation: str,
        subject_type: str,
        subject_id: str,
    ) -> list[AuthzMutation]:
        return sorted([
            row
            for row in rows
            if row.object_type == object_type
            and row.object_id == object_id
            and row.relation == relation
            and row.subject_type == subject_type
            and row.subject_id == subject_id
        ], key=lambda row: row.edge_revision)

    # Registration and its personal storage root were committed together.
    assert matching(
        object_type="organization",
        object_id=organization_id,
        relation="owner",
        subject_type="user",
        subject_id=user_id,
    )
    assert matching(
        object_type="storage_root",
        object_id=user_id,
        relation="manager",
        subject_type="user",
        subject_id=user_id,
    )
    # Child hierarchy and membership additions are durable structural facts.
    assert matching(
        object_type="group",
        object_id=root["group_id"],
        relation="descendant",
        subject_type="group",
        subject_id=child["group_id"],
    )
    direct = matching(
        object_type="group",
        object_id=child["group_id"],
        relation="direct_member",
        subject_type="user",
        subject_id=user_id,
    )
    lead = matching(
        object_type="group",
        object_id=child["group_id"],
        relation="lead",
        subject_type="user",
        subject_id=user_id,
    )
    assert [row.desired_state for row in direct] == ["present", "absent"]
    assert [row.desired_state for row in lead] == ["present", "absent"]
    # Each route synchronously applies its committed intent. Applied history
    # remains immutable; the later delete is a new applied revision rather
    # than rewriting an already-acknowledged grant as superseded.
    assert [row.status for row in direct] == ["applied", "applied"]
    assert [row.status for row in lead] == ["applied", "applied"]
    assert direct[-1].revocation_guard_active is False
    assert lead[-1].revocation_guard_active is False
