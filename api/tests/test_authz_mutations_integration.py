from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select, text

from vibecanvas_api.authorization.mutations import (
    AuthzIdempotencyConflictError,
    AuthzMutationCoordinator,
    AuthzMutationSupersededError,
    MutationEdge,
    RevocationGuard,
)
from vibecanvas_api.authorization.openfga import OpenFgaAuthzService
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
    OpenFgaUnavailableError,
)
from vibecanvas_api.authorization.projection import reconcile_due_mutations
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    PrincipalRef,
    PrincipalType,
    RelationshipBinding,
    RelationshipSubject,
    RelationshipSubjectType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage import db as db_module
from vibecanvas_api.storage.models_authorization import AuthzMutation


pytestmark = pytest.mark.asyncio


class _TupleStore:
    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()
        self.fail_write = False
        self.write_calls = 0

    async def read(self, *, tuple_key, **_kwargs):
        return OpenFgaReadPage(
            (tuple_key,) if tuple_key in self.tuples else ()
        )

    async def write(self, *, writes=(), deletes=()):
        self.write_calls += 1
        if self.fail_write:
            raise OpenFgaUnavailableError()
        self.tuples.update(writes)
        self.tuples.difference_update(deletes)

    async def list_objects(
        self,
        *,
        user,
        relation,
        object_type,
        **_kwargs,
    ):
        accepted = {
            "can_view_metadata": {"viewer", "editor", "operator", "manager"},
            "can_view": {"viewer", "editor", "operator", "manager"},
        }.get(relation, set())
        return tuple(
            item.object.split(":", 1)[1]
            for item in sorted(
                self.tuples,
                key=lambda value: (value.object, value.relation, value.user),
            )
            if item.user == user
            and item.relation in accepted
            and item.object.startswith(f"{object_type}:")
        )


async def _organization(pg_engine) -> str:
    organization_id = uuid.uuid4()
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants(tenant_id, name) "
                "VALUES (:tenant_id, 'Authz Mutation Test')"
            ),
            {"tenant_id": organization_id},
        )
    return str(organization_id)


async def _actor(pg_engine, organization_id: str) -> PrincipalRef:
    user_id = uuid.uuid4()
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:user_id, :tenant_id, :email)"
            ),
            {
                "user_id": user_id,
                "tenant_id": uuid.UUID(organization_id),
                "email": f"authz-actor-{user_id.hex}@example.test",
            },
        )
    return PrincipalRef(PrincipalType.USER, str(user_id))


async def _binding(pg_engine, organization_id: str) -> RelationshipBinding:
    recipient = await _actor(pg_engine, organization_id)
    return RelationshipBinding(
        subject=RelationshipSubject(
            RelationshipSubjectType.USER,
            recipient.id,
        ),
        relation="viewer",
        resource=ResourceRef(
            ResourceType.WORKFLOW,
            f"wf-{uuid.uuid4().hex}",
            organization_id,
        ),
    )


async def test_grant_commits_intent_applies_and_is_idempotent(pg_engine):
    organization_id = await _organization(pg_engine)
    store = _TupleStore()
    coordinator = AuthzMutationCoordinator(
        client=store,
        organization_id=organization_id,
    )
    binding = await _binding(pg_engine, organization_id)
    actor = await _actor(pg_engine, organization_id)

    first = await coordinator.request_binding(
        actor=actor,
        binding=binding,
        desired_present=True,
        idempotency_key="grant-1",
    )
    second = await coordinator.request_binding(
        actor=actor,
        binding=binding,
        desired_present=True,
        idempotency_key="grant-1",
    )

    assert first.mutation_id == second.mutation_id
    assert first.status == "applied"
    assert first.edge_revision == 1
    assert first.revocation_guard_active is False
    assert store.write_calls == 1
    assert len(store.tuples) == 1
    async with pg_engine.connect() as connection:
        delivery = (
            await connection.execute(
                text(
                    "SELECT name, status FROM dbos.workflow_status "
                    "WHERE workflow_uuid = :workflow_id"
                ),
                {"workflow_id": f"authz-{first.mutation_id}"},
            )
        ).one()
    assert delivery.name == "authorization.apply_mutation"
    assert delivery.status == "ENQUEUED"
    async with session_scope(organization_id) as session:
        rows = list(
            (
                await session.execute(select(AuthzMutation))
            ).scalars()
        )
    assert [row.idempotency_key for row in rows] == ["grant-1"]


async def test_idempotency_key_cannot_change_immutable_intent(pg_engine):
    organization_id = await _organization(pg_engine)
    coordinator = AuthzMutationCoordinator(
        client=_TupleStore(),
        organization_id=organization_id,
    )
    binding = await _binding(pg_engine, organization_id)
    actor = await _actor(pg_engine, organization_id)
    await coordinator.request_binding(
        actor=actor,
        binding=binding,
        desired_present=True,
        idempotency_key="same-key",
    )
    with pytest.raises(AuthzIdempotencyConflictError):
        await coordinator.request_binding(
            actor=actor,
            binding=binding,
            desired_present=False,
            idempotency_key="same-key",
        )


async def test_failed_revoke_stays_guarded_until_reconciled(pg_engine):
    organization_id = await _organization(pg_engine)
    store = _TupleStore()
    coordinator = AuthzMutationCoordinator(
        client=store,
        organization_id=organization_id,
    )
    binding = await _binding(pg_engine, organization_id)
    actor = await _actor(pg_engine, organization_id)
    await coordinator.request_binding(
        actor=actor,
        binding=binding,
        desired_present=True,
        idempotency_key="grant-before-revoke",
    )

    store.fail_write = True
    with pytest.raises(OpenFgaUnavailableError):
        await coordinator.request_binding(
            actor=actor,
            binding=binding,
            desired_present=False,
            idempotency_key="revoke-fails",
        )
    async with session_scope(organization_id) as session:
        failed = (
            await session.execute(
                select(AuthzMutation).where(
                    AuthzMutation.idempotency_key == "revoke-fails"
                )
            )
        ).scalar_one()
        assert failed.status == "failed"
        assert failed.revocation_guard_active is True
        denied = await RevocationGuard(session).denies(
            principal=PrincipalRef(
                PrincipalType.USER,
                binding.subject.id,
            ),
            action=Action.VIEW,
            resource=binding.resource,
            membership_role="member",
        )
        listed = await OpenFgaAuthzService(
            session,
            store,
        ).list_authorized_ids(
            PrincipalRef(
                PrincipalType.USER,
                binding.subject.id,
            ),
            Action.VIEW,
            ResourceType.WORKFLOW,
            AuthzRequestContext(
                active_organization_id=organization_id,
                membership_role="member",
                membership_status="active",
            ),
        )
        mutation_id = failed.mutation_id
    assert denied is True
    assert listed == ()

    store.fail_write = False
    applied = await coordinator.apply_mutation(mutation_id)
    assert applied.status == "applied"
    assert applied.revocation_guard_active is False
    assert store.tuples == set()


async def test_due_failed_mutation_is_reapplied_from_durable_ledger(
    pg_engine,
    monkeypatch,
):
    monkeypatch.setattr(db_module, "_admin_engine", pg_engine)
    organization_id = await _organization(pg_engine)
    store = _TupleStore()
    store.fail_write = True
    coordinator = AuthzMutationCoordinator(
        client=store,
        organization_id=organization_id,
    )
    binding = await _binding(pg_engine, organization_id)
    actor = await _actor(pg_engine, organization_id)

    with pytest.raises(OpenFgaUnavailableError):
        await coordinator.request_binding(
            actor=actor,
            binding=binding,
            desired_present=True,
            idempotency_key="grant-recovered-by-reconciler",
        )

    async with session_scope(organization_id) as session:
        failed = (
            await session.execute(
                select(AuthzMutation).where(
                    AuthzMutation.idempotency_key
                    == "grant-recovered-by-reconciler"
                )
            )
        ).scalar_one()
        failed.next_attempt_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        mutation_id = failed.mutation_id

    store.fail_write = False
    stats = await reconcile_due_mutations(store)
    assert stats.pending_applied == 1
    assert stats.failures == 0
    assert len(store.tuples) == 1
    async with session_scope(organization_id) as session:
        recovered = await session.get(AuthzMutation, mutation_id)
        assert recovered is not None
        assert recovered.status == "applied"
        assert recovered.attempt_count == 1
        assert recovered.error_code is None
        assert recovered.next_attempt_at is None


async def test_older_revision_is_superseded_and_cannot_replay(pg_engine):
    organization_id = await _organization(pg_engine)
    store = _TupleStore()
    coordinator = AuthzMutationCoordinator(
        client=store,
        organization_id=organization_id,
    )
    binding = await _binding(pg_engine, organization_id)
    edge = MutationEdge.from_binding(binding)

    async with session_scope(organization_id) as session:
        old = await coordinator.enqueue_structural(
            session=session,
            actor_type="system",
            actor_id="projection",
            edge=edge,
            desired_present=True,
            idempotency_key="projection-revision-1",
            source_revision="1",
        )
        old_id = old.mutation_id
    async with session_scope(organization_id) as session:
        new = await coordinator.enqueue_structural(
            session=session,
            actor_type="system",
            actor_id="projection",
            edge=edge,
            desired_present=False,
            idempotency_key="projection-revision-2",
            source_revision="2",
        )
        new_id = new.mutation_id

    with pytest.raises(AuthzMutationSupersededError):
        await coordinator.apply_mutation(old_id)
    applied = await coordinator.apply_mutation(new_id)
    assert applied.edge_revision == 2
    assert applied.status == "applied"
    assert store.tuples == set()


async def test_mutation_payload_trigger_and_rls_are_enforced(pg_engine):
    organization_id = await _organization(pg_engine)
    coordinator = AuthzMutationCoordinator(
        client=_TupleStore(),
        organization_id=organization_id,
    )
    binding = await _binding(pg_engine, organization_id)
    mutation = await coordinator.request_binding(
        actor=await _actor(pg_engine, organization_id),
        binding=binding,
        desired_present=True,
        idempotency_key="immutable-payload",
    )
    other_organization = await _organization(pg_engine)

    async with session_scope(other_organization) as session:
        assert await session.get(
            AuthzMutation,
            mutation.mutation_id,
        ) is None

    async with pg_engine.connect() as connection:
        forced = (
            await connection.execute(
                text(
                    "SELECT relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'authz_mutations'"
                )
            )
        ).scalar_one()
    assert forced is True

    async with session_scope(organization_id) as session:
        with pytest.raises(Exception, match="payload is immutable"):
            await session.execute(
                text(
                    "UPDATE authz_mutations SET object_id = 'tampered' "
                    "WHERE mutation_id = :mutation_id"
                ),
                {"mutation_id": mutation.mutation_id},
            )
