"""Single durable-intent ledger for OpenFGA relationship mutations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.audit import actions as audit_actions
from vibecanvas_api.audit.service import record_audit
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.models_authorization import (
    AuthzEdgeRevision,
    AuthzMutation,
    SharedResourceProjection,
)
from vibecanvas_api.storage.models_org import GroupMembership
from vibecanvas_api.storage.repo_org import GroupRepo

from .openfga_client import (
    OpenFgaHttpClient,
    OpenFgaTuple,
    OpenFgaUnavailableError,
)
from .openfga_model import (
    OPENFGA_OBJECT_TYPES,
    ROLE_CAPABILITIES,
    validate_share_binding,
)
from .types import (
    Action,
    PrincipalRef,
    PrincipalType,
    RelationshipBinding,
    ResourceRef,
    ResourceType,
)


class AuthzMutationError(RuntimeError):
    pass


class AuthzIdempotencyConflictError(AuthzMutationError):
    pass


class AuthzMutationSupersededError(AuthzMutationError):
    pass


@dataclass(frozen=True, slots=True)
class MutationEdge:
    organization_id: str
    object_type: str
    object_id: str
    relation: str
    subject_type: str
    subject_id: str
    subject_relation: str = ""

    @classmethod
    def from_binding(cls, binding: RelationshipBinding) -> MutationEdge:
        object_type = OPENFGA_OBJECT_TYPES.get(binding.resource.type)
        if object_type is None:
            raise ValueError("unsupported authorization resource type")
        return cls(
            organization_id=binding.resource.organization_id,
            object_type=object_type,
            object_id=binding.resource.id,
            relation=binding.relation,
            subject_type=binding.subject.type.value,
            subject_id=binding.subject.id,
            subject_relation=binding.subject.relation or "",
        )

    def lock_key(self) -> str:
        return "\x1f".join((
            self.organization_id,
            self.object_type,
            self.object_id,
            self.relation,
            self.subject_type,
            self.subject_id,
            self.subject_relation,
        ))


class AuthzMutationCoordinator:
    """Commit intent, serialize one edge, apply, then acknowledge."""

    def __init__(
        self,
        *,
        client: OpenFgaHttpClient | None,
        organization_id: str,
    ) -> None:
        self._client = client
        self.organization_id = organization_id

    @property
    def can_apply(self) -> bool:
        return self._client is not None

    async def request_binding(
        self,
        *,
        actor: PrincipalRef,
        binding: RelationshipBinding,
        desired_present: bool,
        idempotency_key: str,
    ) -> AuthzMutation:
        validate_share_binding(binding)
        if binding.resource.organization_id != self.organization_id:
            raise ValueError("authorization mutation organization mismatch")
        _validate_idempotency_key(idempotency_key)
        edge = MutationEdge.from_binding(binding)

        # This transaction commits before any OpenFGA call. It is the durable
        # recovery and audit intent if the process dies at any later boundary.
        async with short_session_scope(self.organization_id) as session:
            mutation = await self._insert_intent(
                session=session,
                actor_type=actor.type.value,
                actor_id=actor.id,
                kind="direct_binding",
                edge=edge,
                desired_present=desired_present,
                idempotency_key=idempotency_key,
                source_revision=None,
            )
            mutation_id = mutation.mutation_id

        return await self.apply_mutation(mutation_id)

    async def enqueue_structural(
        self,
        *,
        session: AsyncSession,
        actor_type: str,
        actor_id: str,
        edge: MutationEdge,
        desired_present: bool,
        idempotency_key: str,
        source_revision: str,
    ) -> AuthzMutation:
        """Write a structural projection in the caller's business transaction."""
        if edge.organization_id != self.organization_id:
            raise ValueError("authorization mutation organization mismatch")
        _validate_idempotency_key(idempotency_key)
        return await self._insert_intent(
            session=session,
            actor_type=actor_type,
            actor_id=actor_id,
            kind="structural_projection",
            edge=edge,
            desired_present=desired_present,
            idempotency_key=idempotency_key,
            source_revision=source_revision,
        )

    async def enqueue_repair(
        self,
        *,
        session: AsyncSession,
        edge: MutationEdge,
        desired_present: bool,
        kind: str,
        source_revision: str,
    ) -> AuthzMutation:
        """Create the next durable revision for observed projection drift.

        The idempotency key is derived after locking the canonical edge and
        includes the next monotonic revision. A repeated reconciler pass after
        an already-applied tuple is externally deleted therefore creates a new
        repair instead of returning the old applied mutation.
        """
        if edge.organization_id != self.organization_id:
            raise ValueError("authorization mutation organization mismatch")
        if kind not in {"structural_projection", "direct_binding"}:
            raise ValueError("invalid authorization mutation kind")
        await _lock_edge(session, edge)
        current_revision = (
            await session.execute(
                select(AuthzEdgeRevision.current_revision).where(
                    AuthzEdgeRevision.tenant_id
                    == uuid.UUID(edge.organization_id),
                    AuthzEdgeRevision.object_type == edge.object_type,
                    AuthzEdgeRevision.object_id == edge.object_id,
                    AuthzEdgeRevision.relation == edge.relation,
                    AuthzEdgeRevision.subject_type == edge.subject_type,
                    AuthzEdgeRevision.subject_id == edge.subject_id,
                    AuthzEdgeRevision.subject_relation
                    == edge.subject_relation,
                )
            )
        ).scalar_one_or_none()
        next_revision = int(current_revision or 0) + 1
        edge_digest = hashlib.sha256(
            edge.lock_key().encode("utf-8")
        ).hexdigest()[:32]
        idempotency_key = (
            f"reconcile:{edge_digest}:{next_revision}:"
            f"{'present' if desired_present else 'absent'}"
        )
        return await self._insert_intent(
            session=session,
            actor_type="system",
            actor_id="authorization-reconciler",
            kind=kind,
            edge=edge,
            desired_present=desired_present,
            idempotency_key=idempotency_key,
            source_revision=source_revision,
        )

    async def apply_mutation(self, mutation_id: uuid.UUID) -> AuthzMutation:
        if self._client is None:
            # OpenFGA is mandatory. Never pretend the external edge was
            # written when its client is unavailable.
            raise OpenFgaUnavailableError()
        failure: OpenFgaUnavailableError | None = None
        superseded = False
        async with short_session_scope(self.organization_id) as session:
            mutation = (
                await session.execute(
                    select(AuthzMutation)
                    .where(AuthzMutation.mutation_id == mutation_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if mutation is None:
                raise AuthzMutationError("authorization_mutation_not_found")
            edge = _edge_from_mutation(mutation)
            await _lock_edge(session, edge)
            await session.refresh(mutation, with_for_update=True)

            if mutation.status == "applied":
                return mutation
            if mutation.status == "superseded":
                superseded = True
            elif not await _is_latest_revision(session, mutation):
                mutation.status = "superseded"
                mutation.revocation_guard_active = False
                mutation.error_code = None
                mutation.next_attempt_at = None
                superseded = True
            else:
                tuple_ = _tuple_from_mutation(mutation)
                try:
                    page = await self._client.read(
                        tuple_key=tuple_,
                        page_size=1,
                    )
                    exists = bool(page.tuples)
                    if mutation.desired_state == "present" and not exists:
                        await self._client.write(writes=(tuple_,))
                    elif mutation.desired_state == "absent" and exists:
                        await self._client.write(deletes=(tuple_,))
                except OpenFgaUnavailableError as exc:
                    mutation.status = "failed"
                    mutation.error_code = exc.reason_code
                    mutation.attempt_count += 1
                    mutation.next_attempt_at = (
                        datetime.now(timezone.utc)
                        + _retry_delay(mutation.attempt_count)
                    )
                    # Revoke remains fail-closed until it is applied or
                    # superseded. A grant never activates a local allow.
                    mutation.revocation_guard_active = (
                        mutation.desired_state == "absent"
                    )
                    failure = exc
                else:
                    # The edge advisory lock is held across the external write
                    # and this acknowledgement. A newer revision cannot be
                    # allocated in between and make an older write win.
                    mutation.status = "applied"
                    mutation.revocation_guard_active = False
                    mutation.error_code = None
                    mutation.next_attempt_at = None
                    mutation.applied_at = datetime.now(timezone.utc)
                    if mutation.kind == "direct_binding":
                        await _sync_shared_resource_projection(
                            session,
                            mutation,
                        )
            await session.flush()

        if failure is not None:
            raise failure
        if superseded:
            raise AuthzMutationSupersededError(
                "authorization_mutation_superseded"
            )

        async with short_session_scope(self.organization_id) as session:
            result = await session.get(AuthzMutation, mutation_id)
            if result is None:
                raise AuthzMutationError("authorization_mutation_not_found")
            return result

    async def _insert_intent(
        self,
        *,
        session: AsyncSession,
        actor_type: str,
        actor_id: str,
        kind: str,
        edge: MutationEdge,
        desired_present: bool,
        idempotency_key: str,
        source_revision: str | None,
    ) -> AuthzMutation:
        await _lock_idempotency(
            session,
            organization_id=edge.organization_id,
            idempotency_key=idempotency_key,
        )
        await _lock_edge(session, edge)

        existing = (
            await session.execute(
                select(AuthzMutation).where(
                    AuthzMutation.tenant_id
                    == uuid.UUID(edge.organization_id),
                    AuthzMutation.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not _same_intent(
                existing,
                actor_type=actor_type,
                actor_id=actor_id,
                kind=kind,
                edge=edge,
                desired_present=desired_present,
                source_revision=source_revision,
            ):
                raise AuthzIdempotencyConflictError(
                    "authorization_idempotency_conflict"
                )
            return existing

        revision = (
            await session.execute(
                text(
                    """
                    INSERT INTO authz_edge_revisions (
                        tenant_id, object_type, object_id, relation,
                        subject_type, subject_id, subject_relation,
                        current_revision
                    ) VALUES (
                        CAST(:tenant_id AS uuid), :object_type, :object_id,
                        :relation, :subject_type, :subject_id,
                        :subject_relation, 1
                    )
                    ON CONFLICT (
                        tenant_id, object_type, object_id, relation,
                        subject_type, subject_id, subject_relation
                    ) DO UPDATE SET
                        current_revision =
                            authz_edge_revisions.current_revision + 1,
                        updated_at = now()
                    RETURNING current_revision
                    """
                ),
                _edge_params(edge),
            )
        ).scalar_one()

        previous = (
            await session.execute(
                select(AuthzMutation)
                .where(
                    AuthzMutation.tenant_id
                    == uuid.UUID(edge.organization_id),
                    AuthzMutation.object_type == edge.object_type,
                    AuthzMutation.object_id == edge.object_id,
                    AuthzMutation.relation == edge.relation,
                    AuthzMutation.subject_type == edge.subject_type,
                    AuthzMutation.subject_id == edge.subject_id,
                    AuthzMutation.subject_relation
                    == (edge.subject_relation or None),
                )
                .order_by(AuthzMutation.edge_revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if (
            kind == "direct_binding"
            and previous is not None
            and previous.kind == "structural_projection"
            and previous.desired_state == "present"
        ):
            # Creator/organization and other structural tuples belong to the
            # Postgres projection. A share API call must never convert or
            # revoke them as though they were a mutable direct grant.
            raise AuthzMutationError(
                "authorization_binding_is_structural"
            )

        # Older incomplete work may never execute after the new revision
        # commits. Applied rows remain immutable history.
        await session.execute(
            update(AuthzMutation)
            .where(
                AuthzMutation.tenant_id == uuid.UUID(edge.organization_id),
                AuthzMutation.object_type == edge.object_type,
                AuthzMutation.object_id == edge.object_id,
                AuthzMutation.relation == edge.relation,
                AuthzMutation.subject_type == edge.subject_type,
                AuthzMutation.subject_id == edge.subject_id,
                AuthzMutation.subject_relation
                == (edge.subject_relation or None),
                AuthzMutation.status.in_(("requested", "failed")),
            )
            .values(
                status="superseded",
                revocation_guard_active=False,
                error_code=None,
                next_attempt_at=None,
            )
        )

        mutation = AuthzMutation(
            tenant_id=uuid.UUID(edge.organization_id),
            actor_type=actor_type,
            actor_id=actor_id,
            kind=kind,
            operation="write" if desired_present else "delete",
            desired_state="present" if desired_present else "absent",
            object_type=edge.object_type,
            object_id=edge.object_id,
            relation=edge.relation,
            subject_type=edge.subject_type,
            subject_id=edge.subject_id,
            subject_relation=edge.subject_relation or None,
            source_revision=source_revision,
            edge_revision=revision,
            supersedes_mutation_id=(
                previous.mutation_id if previous is not None else None
            ),
            status="requested",
            revocation_guard_active=not desired_present,
            idempotency_key=idempotency_key,
        )
        session.add(mutation)
        if (
            kind == "direct_binding"
            and not desired_present
            and edge.subject_type == "user"
        ):
            # Recipient discovery fails closed as soon as revoke intent is
            # durable. OpenFGA's revocation guard independently denies a stale
            # tuple until the external delete is acknowledged.
            await session.execute(
                delete(SharedResourceProjection).where(
                    SharedResourceProjection.owner_tenant_id
                    == uuid.UUID(edge.organization_id),
                    SharedResourceProjection.resource_type == edge.object_type,
                    SharedResourceProjection.resource_id == edge.object_id,
                    SharedResourceProjection.recipient_user_id
                    == uuid.UUID(edge.subject_id),
                    SharedResourceProjection.relation == edge.relation,
                )
            )
        if kind == "direct_binding":
            await record_audit(
                session,
                action=(
                    audit_actions.SHARE_GRANT
                    if desired_present
                    else audit_actions.SHARE_REVOKE
                ),
                actor_user_id=(
                    uuid.UUID(actor_id) if actor_type == "user" else None
                ),
                actor_email=None,
                target_type=edge.object_type,
                target_id=edge.object_id,
                target_name=None,
                outcome="success",
                meta={
                    "relation": edge.relation,
                    "subject_type": edge.subject_type,
                    "subject_id": edge.subject_id,
                    "subject_relation": edge.subject_relation,
                    "idempotency_key": idempotency_key,
                },
            )
        await session.flush()
        # Flowork and DBOS share PostgreSQL, so persist delivery in the same
        # transaction as the mutation intent.  Creation, update, deletion and
        # sharing now drive OpenFGA directly; no inventory polling is needed.
        from vibecanvas_api.services.background_queue import (
            enqueue_background_job_in_transaction,
        )

        await enqueue_background_job_in_transaction(
            session,
            "authorization.apply_mutation",
            job_id=f"authz-{mutation.mutation_id}",
            queue="control",
            kwargs={"mutation_id": str(mutation.mutation_id)},
        )
        return mutation


_RECIPIENT_PROJECTED_TYPES = frozenset({
    "workflow",
    "task",
    "deployment",
    "knowledge_base",
})


async def _sync_shared_resource_projection(
    session: AsyncSession,
    mutation: AuthzMutation,
) -> None:
    """Project one applied direct User edge for recipient-side discovery."""
    if (
        mutation.subject_type != "user"
        or mutation.object_type not in _RECIPIENT_PROJECTED_TYPES
    ):
        return
    key = {
        "owner_tenant_id": mutation.tenant_id,
        "resource_type": mutation.object_type,
        "resource_id": mutation.object_id,
        "recipient_user_id": uuid.UUID(mutation.subject_id),
        "relation": mutation.relation,
    }
    if mutation.desired_state == "absent":
        await session.execute(
            delete(SharedResourceProjection).where(
                SharedResourceProjection.owner_tenant_id
                == key["owner_tenant_id"],
                SharedResourceProjection.resource_type
                == key["resource_type"],
                SharedResourceProjection.resource_id == key["resource_id"],
                SharedResourceProjection.recipient_user_id
                == key["recipient_user_id"],
                SharedResourceProjection.relation == key["relation"],
            )
        )
        return
    statement = pg_insert(SharedResourceProjection).values(
        **key,
        source_mutation_id=mutation.mutation_id,
        edge_revision=mutation.edge_revision,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=(
                SharedResourceProjection.owner_tenant_id,
                SharedResourceProjection.resource_type,
                SharedResourceProjection.resource_id,
                SharedResourceProjection.recipient_user_id,
                SharedResourceProjection.relation,
            ),
            set_={
                "source_mutation_id": statement.excluded.source_mutation_id,
                "edge_revision": statement.excluded.edge_revision,
                "updated_at": func.now(),
            },
        )
    )


class RevocationGuard:
    """Temporary local deny while a tuple deletion is uncertain."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def denies(
        self,
        *,
        principal: PrincipalRef,
        action: Action,
        resource: ResourceRef,
        membership_role: str,
    ) -> bool:
        return (
            await self.denies_many(
                checks=((
                    principal,
                    action,
                    resource,
                    membership_role,
                ),),
            )
        )[0]

    async def denies_many(
        self,
        *,
        checks: tuple[
            tuple[PrincipalRef, Action, ResourceRef, str],
            ...,
        ],
    ) -> tuple[bool, ...]:
        """Evaluate a page/batch using two guard queries, never per resource."""
        if not checks:
            return ()

        by_object_type: dict[str, set[str]] = {}
        for _principal, _action, resource, _membership_role in checks:
            object_type = OPENFGA_OBJECT_TYPES.get(resource.type)
            if object_type is not None:
                by_object_type.setdefault(object_type, set()).add(resource.id)

        resource_guards: list[AuthzMutation] = []
        conditions = [
            and_(
                AuthzMutation.object_type == object_type,
                AuthzMutation.object_id.in_(sorted(object_ids)),
            )
            for object_type, object_ids in by_object_type.items()
        ]
        if conditions:
            resource_guards = list(
                (
                    await self._session.execute(
                        select(AuthzMutation).where(
                            AuthzMutation.revocation_guard_active.is_(True),
                            or_(*conditions),
                        )
                    )
                ).scalars()
            )
        guards_by_resource: dict[
            tuple[str, str],
            list[AuthzMutation],
        ] = {}
        for guard in resource_guards:
            guards_by_resource.setdefault(
                (guard.object_type, guard.object_id),
                [],
            ).append(guard)

        # A stale group-membership/hierarchy tuple can grant unrelated
        # resources, so these guards are organization-wide for affected users.
        group_guards = list(
            (
                await self._session.execute(
                    select(AuthzMutation).where(
                        AuthzMutation.object_type == "group",
                        AuthzMutation.revocation_guard_active.is_(True),
                        AuthzMutation.kind == "structural_projection",
                    )
                )
            ).scalars()
        )
        group_cache: dict[
            tuple[PrincipalType, str],
            tuple[set[str] | None, set[str] | None],
        ] = {}

        async def groups_for(
            principal: PrincipalRef,
        ) -> tuple[set[str] | None, set[str] | None]:
            key = (principal.type, principal.id)
            if key in group_cache:
                return group_cache[key]
            if principal.type is not PrincipalType.USER:
                result = (set(), set())
            else:
                try:
                    user_id = uuid.UUID(principal.id)
                except ValueError:
                    # A malformed user principal must not bypass a pending
                    # group revoke. None means conservatively unknown.
                    result = (None, None)
                else:
                    effective = {
                        str(value)
                        for value in await GroupRepo(
                            self._session
                        ).effective_group_ids(user_id)
                    }
                    direct = {
                        str(value)
                        for value in (
                            await self._session.execute(
                                select(GroupMembership.group_id).where(
                                    GroupMembership.user_id == user_id,
                                    GroupMembership.status == "active",
                                )
                            )
                        ).scalars()
                    }
                    result = (effective, direct)
            group_cache[key] = result
            return result

        results: list[bool] = []
        for principal, action, resource, membership_role in checks:
            object_type = OPENFGA_OBJECT_TYPES.get(resource.type)
            guards = guards_by_resource.get(
                (object_type or "", resource.id),
                [],
            )
            needs_groups = (
                principal.type is PrincipalType.USER
                and (
                    any(guard.subject_type == "group" for guard in guards)
                    or any(
                        guard.subject_type == "group"
                        for guard in group_guards
                    )
                )
            )
            effective_groups: set[str] | None = set()
            direct_groups: set[str] | None = set()
            if needs_groups:
                effective_groups, direct_groups = await groups_for(principal)

            denied = _direct_guard_matches(
                guards=guards,
                principal=principal,
                action=action,
                resource_type=resource.type,
                organization_id=resource.organization_id,
                membership_role=membership_role,
                effective_groups=effective_groups,
                direct_groups=direct_groups,
            )
            if not denied and principal.type is PrincipalType.USER:
                denied = _group_guard_matches(
                    guards=group_guards,
                    principal=principal,
                    effective_groups=effective_groups,
                )
            results.append(denied)
        return tuple(results)

    async def denied_resource_ids(
        self,
        *,
        principal: PrincipalRef,
        action: Action,
        resource_type: ResourceType,
        resource_ids: tuple[str, ...],
        organization_id: str,
        membership_role: str,
    ) -> frozenset[str]:
        """Return list results hidden by pending failed revocations."""
        checks = tuple(
            (
                principal,
                action,
                ResourceRef(resource_type, resource_id, organization_id),
                membership_role,
            )
            for resource_id in resource_ids
        )
        denied = await self.denies_many(checks=checks)
        return frozenset(
            resource_id
            for resource_id, is_denied in zip(
                resource_ids,
                denied,
                strict=True,
            )
            if is_denied
        )


def _direct_guard_matches(
    *,
    guards: list[AuthzMutation],
    principal: PrincipalRef,
    action: Action,
    resource_type: ResourceType,
    organization_id: str,
    membership_role: str,
    effective_groups: set[str] | None,
    direct_groups: set[str] | None,
) -> bool:
    for guard in guards:
        role_actions = ROLE_CAPABILITIES.get(
            resource_type, {}
        ).get(guard.relation, frozenset())
        if action not in role_actions:
            continue
        if (
            guard.subject_type == principal.type.value
            and guard.subject_id == principal.id
            and not guard.subject_relation
        ):
            return True
        if (
            principal.type is PrincipalType.USER
            and guard.subject_type == "organization"
            and guard.subject_id == organization_id
            and guard.subject_relation == "member"
            and membership_role == "member"
        ):
            return True
        if (
            principal.type is not PrincipalType.USER
            or guard.subject_type != "group"
        ):
            continue
        if guard.subject_relation == "member":
            if (
                effective_groups is None
                or guard.subject_id in effective_groups
            ):
                return True
        elif guard.subject_relation == "direct_member":
            if (
                direct_groups is None
                or guard.subject_id in direct_groups
            ):
                return True
    return False


def _group_guard_matches(
    *,
    guards: list[AuthzMutation],
    principal: PrincipalRef,
    effective_groups: set[str] | None,
) -> bool:
    for guard in guards:
        if (
            guard.subject_type == "user"
            and guard.subject_id == principal.id
        ):
            return True
        if guard.subject_type == "group" and (
            effective_groups is None
            or guard.subject_id in effective_groups
        ):
            return True
    return False


async def _lock_idempotency(
    session: AsyncSession,
    *,
    organization_id: str,
    idempotency_key: str,
) -> None:
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:lock_key, 0))"
        ),
        {"lock_key": f"authz-idempotency:{organization_id}:{idempotency_key}"},
    )


async def _lock_edge(session: AsyncSession, edge: MutationEdge) -> None:
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:lock_key, 0))"
        ),
        {"lock_key": f"authz-edge:{edge.lock_key()}"},
    )


async def _is_latest_revision(
    session: AsyncSession,
    mutation: AuthzMutation,
) -> bool:
    current = (
        await session.execute(
            text(
                """
                SELECT current_revision
                FROM authz_edge_revisions
                WHERE tenant_id = :tenant_id
                  AND object_type = :object_type
                  AND object_id = :object_id
                  AND relation = :relation
                  AND subject_type = :subject_type
                  AND subject_id = :subject_id
                  AND subject_relation = :subject_relation
                """
            ),
            {
                **_edge_params(_edge_from_mutation(mutation)),
                "tenant_id": mutation.tenant_id,
            },
        )
    ).scalar_one_or_none()
    return current == mutation.edge_revision


def _edge_from_mutation(mutation: AuthzMutation) -> MutationEdge:
    return MutationEdge(
        organization_id=str(mutation.tenant_id),
        object_type=mutation.object_type,
        object_id=mutation.object_id,
        relation=mutation.relation,
        subject_type=mutation.subject_type,
        subject_id=mutation.subject_id,
        subject_relation=mutation.subject_relation or "",
    )


def _tuple_from_mutation(mutation: AuthzMutation) -> OpenFgaTuple:
    subject = f"{mutation.subject_type}:{mutation.subject_id}"
    if mutation.subject_relation:
        subject = f"{subject}#{mutation.subject_relation}"
    return OpenFgaTuple(
        user=subject,
        relation=mutation.relation,
        object=f"{mutation.object_type}:{mutation.object_id}",
    )


def _edge_params(edge: MutationEdge) -> dict[str, object]:
    return {
        "tenant_id": edge.organization_id,
        "object_type": edge.object_type,
        "object_id": edge.object_id,
        "relation": edge.relation,
        "subject_type": edge.subject_type,
        "subject_id": edge.subject_id,
        "subject_relation": edge.subject_relation,
    }


def _same_intent(
    mutation: AuthzMutation,
    *,
    actor_type: str,
    actor_id: str,
    kind: str,
    edge: MutationEdge,
    desired_present: bool,
    source_revision: str | None,
) -> bool:
    return (
        mutation.actor_type == actor_type
        and mutation.actor_id == actor_id
        and mutation.kind == kind
        and mutation.desired_state
        == ("present" if desired_present else "absent")
        and mutation.object_type == edge.object_type
        and mutation.object_id == edge.object_id
        and mutation.relation == edge.relation
        and mutation.subject_type == edge.subject_type
        and mutation.subject_id == edge.subject_id
        and (mutation.subject_relation or "") == edge.subject_relation
        and mutation.source_revision == source_revision
    )


def _retry_delay(attempt_count: int) -> timedelta:
    return timedelta(seconds=min(300, 2 ** min(attempt_count, 8)))


def _validate_idempotency_key(value: str) -> None:
    if (
        not value
        or len(value) > 200
        or "\n" in value
        or "\r" in value
        or "\x00" in value
    ):
        raise ValueError("invalid authorization idempotency key")
