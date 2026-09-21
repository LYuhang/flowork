from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.authorization.mutations import MutationEdge
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.authorization.projection import (
    collect_structural_projection,
    reconcile_organization,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models_org import Group, GroupMembership
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


pytestmark = pytest.mark.asyncio


class _TupleStore:
    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()
        self.read_calls = 0

    async def read(self, *, tuple_key, continuation_token="", **_kwargs):
        self.read_calls += 1
        assert not continuation_token
        matches = tuple(
            item
            for item in sorted(
                self.tuples,
                key=lambda value: (
                    value.object,
                    value.relation,
                    value.user,
                ),
            )
            if (
                (not tuple_key.user or item.user == tuple_key.user)
                and (
                    not tuple_key.relation
                    or item.relation == tuple_key.relation
                )
                and (
                    not tuple_key.object
                    or item.object == tuple_key.object
                )
            )
        )
        return OpenFgaReadPage(matches)

    async def write(self, *, writes=(), deletes=()):
        self.tuples.update(writes)
        self.tuples.difference_update(deletes)


async def _seed() -> tuple[str, str, str, str, str]:
    email = f"projection-{uuid.uuid4().hex}@example.com"
    async with session_scope() as session:
        user = await AuthRepo(session).register(
            email,
            "not-a-real-password-hash",
            "Projection",
        )
        organization_id = str(user.tenant_id)
        user_id = str(user.user_id)

    group_id = uuid.uuid4()
    child_group_id = uuid.uuid4()
    workflow_id = f"wf-{uuid.uuid4().hex}"
    async with session_scope(organization_id) as session:
        session.add_all([
            Group(
                group_id=group_id,
                tenant_id=uuid.UUID(organization_id),
                kind="department",
                name="Engineering",
                source="native",
                status="active",
                created_by=uuid.UUID(user_id),
            ),
            Group(
                group_id=child_group_id,
                tenant_id=uuid.UUID(organization_id),
                parent_group_id=group_id,
                kind="team",
                name="Backend",
                source="native",
                status="active",
                created_by=uuid.UUID(user_id),
            ),
        ])
        await session.flush()
        session.add(GroupMembership(
            tenant_id=uuid.UUID(organization_id),
            group_id=child_group_id,
            user_id=uuid.UUID(user_id),
            group_role="lead",
            status="active",
        ))
        await WorkflowRepo(session, user_id).create_workflow(
            wf_id=workflow_id,
            name="Projection",
        )
    return (
        organization_id,
        user_id,
        str(group_id),
        str(child_group_id),
        workflow_id,
    )


async def test_structural_projection_covers_org_groups_resources_and_mount(
    pg_engine,
):
    (
        organization_id,
        user_id,
        parent_group_id,
        _child_group_id,
        workflow_id,
    ) = await _seed()
    async with session_scope(organization_id) as session:
        projection = await collect_structural_projection(
            session,
            organization_id=organization_id,
        )

    assert MutationEdge(
        organization_id,
        "organization",
        organization_id,
        "owner",
        "user",
        user_id,
    ) in projection
    assert MutationEdge(
        organization_id,
        "group",
        parent_group_id,
        "descendant",
        "group",
        next(
            edge.subject_id
            for edge in projection
            if edge.object_type == "group"
            and edge.object_id == parent_group_id
            and edge.relation == "descendant"
        ),
        "member",
    ) in projection
    assert any(
        edge.object_type == "group"
        and edge.relation == "lead"
        and edge.subject_id == user_id
        for edge in projection
    )
    assert MutationEdge(
        organization_id,
        "workflow",
        workflow_id,
        "organization",
        "organization",
        organization_id,
    ) in projection
    assert MutationEdge(
        organization_id,
        "workflow",
        workflow_id,
        "manager",
        "user",
        user_id,
    ) in projection
    assert MutationEdge(
        organization_id,
        "storage_root",
        user_id,
        "manager",
        "user",
        user_id,
    ) in projection


async def test_reconciler_repairs_missing_and_removes_unexplained_tuples(
    pg_engine,
):
    (
        organization_id,
        user_id,
        _group_id,
        _child_group_id,
        workflow_id,
    ) = await _seed()
    store = _TupleStore()

    first = await reconcile_organization(store, organization_id)
    assert first.repairs_requested > 0
    assert first.repairs_requested == first.repairs_applied

    store.read_calls = 0
    clean = await reconcile_organization(store, organization_id)
    assert clean.repairs_requested == 0
    assert store.read_calls == 1

    owner_tuple = OpenFgaTuple(
        f"user:{user_id}",
        "manager",
        f"workflow:{workflow_id}",
    )
    store.tuples.remove(owner_tuple)
    repaired = await reconcile_organization(store, organization_id)
    assert repaired.repairs_requested == 1
    assert owner_tuple in store.tuples

    unexplained = OpenFgaTuple(
        f"user:{uuid.uuid4()}",
        "viewer",
        f"workflow:{workflow_id}",
    )
    store.tuples.add(unexplained)
    quarantined = await reconcile_organization(store, organization_id)
    assert quarantined.unexplained_tuples_removed == 1
    assert unexplained not in store.tuples


async def test_reconciler_removes_suspended_membership_and_old_hierarchy(
    pg_engine,
):
    (
        organization_id,
        user_id,
        parent_group_id,
        child_group_id,
        _workflow_id,
    ) = await _seed()
    store = _TupleStore()
    await reconcile_organization(store, organization_id)

    direct_member = OpenFgaTuple(
        f"user:{user_id}",
        "direct_member",
        f"group:{child_group_id}",
    )
    lead = OpenFgaTuple(
        f"user:{user_id}",
        "lead",
        f"group:{child_group_id}",
    )
    old_parent = OpenFgaTuple(
        f"group:{child_group_id}#member",
        "descendant",
        f"group:{parent_group_id}",
    )
    assert {direct_member, lead, old_parent} <= store.tuples

    new_parent_id = uuid.uuid4()
    async with session_scope(organization_id) as session:
        session.add(Group(
            group_id=new_parent_id,
            tenant_id=uuid.UUID(organization_id),
            kind="team",
            name="Platform",
            source="native",
            status="active",
            created_by=uuid.UUID(user_id),
        ))
        await session.flush()
        await session.execute(
            text(
                "UPDATE groups SET parent_group_id = :parent_group_id, "
                "updated_at = now() WHERE group_id = :group_id"
            ),
            {
                "parent_group_id": new_parent_id,
                "group_id": uuid.UUID(child_group_id),
            },
        )
        await session.execute(
            text(
                "UPDATE group_memberships SET status = 'suspended', "
                "updated_at = now() "
                "WHERE group_id = :group_id AND user_id = :user_id"
            ),
            {
                "group_id": uuid.UUID(child_group_id),
                "user_id": uuid.UUID(user_id),
            },
        )

    changed = await reconcile_organization(store, organization_id)
    assert changed.repairs_requested >= 4
    assert direct_member not in store.tuples
    assert lead not in store.tuples
    assert old_parent not in store.tuples
    assert OpenFgaTuple(
        f"group:{child_group_id}#member",
        "descendant",
        f"group:{new_parent_id}",
    ) in store.tuples
