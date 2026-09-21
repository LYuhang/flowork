"""Postgres-to-OpenFGA projection and durable authorization reconciliation.

Postgres owns structural facts. OpenFGA owns effective relationship tuples.
This module converges the latter from the former through the single
``authz_mutations`` ledger; it never writes tuples directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import uuid

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.models_authorization import AuthzMutation
from vibecanvas_api.storage.sync_session import short_admin_connection

from .mutations import (
    AuthzMutationCoordinator,
    AuthzMutationSupersededError,
    MutationEdge,
)
from .openfga_client import (
    OpenFgaHttpClient,
    OpenFgaTuple,
    OpenFgaUnavailableError,
)


logger = structlog.get_logger(__name__)

_ORGANIZATION_ROLES = frozenset({
    "owner",
    "admin",
    "member",
    "guest",
    "auditor",
})
_PRIVATE_OWNER_RELATIONS = {
    "chat": "creator",
    "mcp_installation": "installer",
    "llm_credential": "owner",
}
@dataclass(frozen=True, slots=True)
class DesiredProjection:
    edge: MutationEdge
    source_revision: str


@dataclass(slots=True)
class ReconcileStats:
    organizations: int = 0
    skipped: int = 0
    pending_applied: int = 0
    repairs_requested: int = 0
    repairs_applied: int = 0
    failures: int = 0
    unexplained_tuples_removed: int = 0

    def merge(self, other: ReconcileStats) -> None:
        for field in self.__dataclass_fields__:
            setattr(self, field, getattr(self, field) + getattr(other, field))


def organization_membership_edges(
    *,
    organization_id: str,
    user_id: str,
    role: str,
    status: str,
) -> frozenset[MutationEdge]:
    """Return every structural edge owned by one organization membership."""
    if status != "active" or role not in _ORGANIZATION_ROLES:
        return frozenset()
    return frozenset({
        MutationEdge(
            organization_id,
            "organization",
            organization_id,
            role,
            "user",
            user_id,
        ),
        MutationEdge(
            organization_id,
            "storage_root",
            user_id,
            "organization",
            "organization",
            organization_id,
        ),
        MutationEdge(
            organization_id,
            "storage_root",
            user_id,
            "manager",
            "user",
            user_id,
        ),
    })


def group_edges(
    *,
    organization_id: str,
    group_id: str,
    parent_group_id: str | None,
    status: str,
) -> frozenset[MutationEdge]:
    if status != "active":
        return frozenset()
    result = {
        MutationEdge(
            organization_id,
            "group",
            group_id,
            "organization",
            "organization",
            organization_id,
        )
    }
    if parent_group_id:
        result.add(MutationEdge(
            organization_id,
            "group",
            parent_group_id,
            "descendant",
            "group",
            group_id,
            "member",
        ))
    return frozenset(result)


def group_membership_edges(
    *,
    organization_id: str,
    group_id: str,
    user_id: str,
    role: str,
    status: str,
) -> frozenset[MutationEdge]:
    if status != "active":
        return frozenset()
    result = {
        MutationEdge(
            organization_id,
            "group",
            group_id,
            "direct_member",
            "user",
            user_id,
        )
    }
    if role == "lead":
        result.add(MutationEdge(
            organization_id,
            "group",
            group_id,
            "lead",
            "user",
            user_id,
        ))
    return frozenset(result)


def resource_root_edges(
    *,
    organization_id: str,
    object_type: str,
    object_id: str,
    owner_relation: str,
    owner_type: str,
    owner_id: str,
    active: bool = True,
) -> frozenset[MutationEdge]:
    """Canonical organization and owner edges for one root resource.

    Creation/deletion routes and the periodic collector deliberately share
    this shape.  That prevents the synchronous write path and drift repair
    path from converging on different OpenFGA tuples.
    """
    if not active:
        return frozenset()
    return frozenset({
        MutationEdge(
            organization_id,
            object_type,
            object_id,
            "organization",
            "organization",
            organization_id,
        ),
        MutationEdge(
            organization_id,
            object_type,
            object_id,
            owner_relation,
            owner_type,
            owner_id,
        ),
    })


def service_account_edges(
    *,
    organization_id: str,
    service_account_id: str,
    created_by: str,
    owner_resource_type: str,
    owner_resource_id: str,
    workflow_id: str,
    status: str = "active",
    credential_ids: tuple[str, ...] = (),
) -> frozenset[MutationEdge]:
    """Canonical identity and execution grants for one Service Account.

    Disabled identities retain their graph edges so administrators can inspect
    and re-enable them. Runtime authorization additionally validates status and
    generation on every sensitive call. Deleted identities project no edges.
    """
    if status == "deleted":
        return frozenset()
    result = {
        MutationEdge(
            organization_id,
            "service_account",
            service_account_id,
            "organization",
            "organization",
            organization_id,
        ),
        MutationEdge(
            organization_id,
            "service_account",
            service_account_id,
            "manager",
            "user",
            created_by,
        ),
        MutationEdge(
            organization_id,
            owner_resource_type,
            owner_resource_id,
            "operator",
            "service_account",
            service_account_id,
        ),
        MutationEdge(
            organization_id,
            "workflow",
            workflow_id,
            "operator",
            "service_account",
            service_account_id,
        ),
    }
    result.update(
        MutationEdge(
            organization_id,
            "llm_credential",
            credential_id,
            "consumer",
            "service_account",
            service_account_id,
        )
        for credential_id in credential_ids
    )
    return frozenset(result)


async def enqueue_structural_delta(
    *,
    session: AsyncSession,
    coordinator: AuthzMutationCoordinator,
    actor_type: str,
    actor_id: str,
    before: frozenset[MutationEdge] | set[MutationEdge],
    after: frozenset[MutationEdge] | set[MutationEdge],
    operation_id: str,
    source: str,
) -> tuple[uuid.UUID, ...]:
    """Persist the exact edge delta in the caller's business transaction."""
    mutation_ids: list[uuid.UUID] = []
    changes = [
        *((edge, False) for edge in set(before) - set(after)),
        *((edge, True) for edge in set(after) - set(before)),
    ]
    for index, (edge, desired_present) in enumerate(
        sorted(
            changes,
            key=lambda item: (
                item[0].lock_key(),
                item[1],
            ),
        )
    ):
        mutation = await coordinator.enqueue_structural(
            session=session,
            actor_type=actor_type,
            actor_id=actor_id,
            edge=edge,
            desired_present=desired_present,
            idempotency_key=(
                f"structural:{operation_id}:{index}:"
                f"{'present' if desired_present else 'absent'}"
            ),
            source_revision=f"{source}:{operation_id}",
        )
        mutation_ids.append(mutation.mutation_id)
    return tuple(mutation_ids)


async def apply_committed_structural_mutations(
    coordinator: AuthzMutationCoordinator,
    mutation_ids: tuple[uuid.UUID, ...],
) -> None:
    """Apply committed intents synchronously when OpenFGA is configured."""
    if not coordinator.can_apply:
        return
    for mutation_id in mutation_ids:
        try:
            await coordinator.apply_mutation(mutation_id)
        except AuthzMutationSupersededError:
            # A concurrent newer structural fact is already authoritative.
            continue


async def list_organization_ids() -> tuple[str, ...]:
    """Return the cross-tenant work inventory through the admin-only path."""
    async with short_admin_connection() as connection:
        await _require_admin_rls_bypass(connection)
        rows = (
            await connection.execute(
                text(
                    "SELECT tenant_id::text FROM organizations "
                    "ORDER BY tenant_id"
                )
            )
        ).scalars()
        return tuple(rows)


async def reconcile_due_mutations(
    client: OpenFgaHttpClient,
    *,
    batch_size: int = 200,
) -> ReconcileStats:
    """Apply due durable intents before performing drift discovery."""
    stats = ReconcileStats()
    async with short_admin_connection() as connection:
        await _require_admin_rls_bypass(connection)
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT mutation_id, tenant_id::text AS organization_id
                    FROM authz_mutations
                    WHERE status IN ('requested', 'failed')
                      AND (
                        next_attempt_at IS NULL
                        OR next_attempt_at <= now()
                      )
                    ORDER BY
                        COALESCE(next_attempt_at, requested_at),
                        requested_at,
                        mutation_id
                    LIMIT :batch_size
                    """
                ),
                {"batch_size": max(1, min(batch_size, 1000))},
            )
        ).mappings().all()

    for row in rows:
        coordinator = AuthzMutationCoordinator(
            client=client,
            organization_id=row["organization_id"],
        )
        try:
            await coordinator.apply_mutation(row["mutation_id"])
        except AuthzMutationSupersededError:
            continue
        except OpenFgaUnavailableError:
            stats.failures += 1
            # One unavailable response predicts the same result for the rest
            # of this short pass. Their durable intents remain untouched.
            break
        else:
            stats.pending_applied += 1
    return stats


async def _require_admin_rls_bypass(connection: Any) -> None:
    """Refuse a silently tenant-blind control-plane connection.

    FORCE RLS also applies to the table owner. A reconciler accidentally
    pointed at the application DSN would otherwise see an empty inventory and
    report success forever, leaving failed revocations and grants stranded.
    """
    bypasses_rls = (
        await connection.execute(
            text(
                """
                SELECT rolsuper OR rolbypassrls
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
        )
    ).scalar_one_or_none()
    if bypasses_rls is not True:
        raise RuntimeError(
            "authorization_admin_database_requires_rls_bypass"
        )


async def reconcile_organization(
    client: OpenFgaHttpClient,
    organization_id: str,
) -> ReconcileStats:
    """Converge one organization's structural and direct relationship edges."""
    organization_uuid = uuid.UUID(organization_id)
    stats = ReconcileStats(organizations=1)
    mutation_ids: list[uuid.UUID] = []

    # The reconciler is invoked by background worker through a fresh ``asyncio.run`` on
    # every tick. A process-global asyncpg pool is bound to the first event
    # loop and leaks unusable/idle transactions across later ticks. Use a
    # per-call engine whose connection is always disposed on the same loop.
    async with short_session_scope(organization_id) as session:
        acquired = (
            await session.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended(:key, 0))"
                ),
                {"key": f"authorization-reconcile:{organization_id}"},
            )
        ).scalar_one()
        if not acquired:
            stats.skipped = 1
            return stats

        structural = await collect_structural_projection(
            session,
            organization_id=organization_id,
        )
        latest = await _latest_mutations(session, organization_uuid)

        object_keys = {
            (item.edge.object_type, item.edge.object_id)
            for item in structural.values()
        }
        object_keys.update(
            (edge.object_type, edge.object_id) for edge in latest
        )
        actual = await _read_object_tuples(
            client,
            organization_id=organization_id,
            object_keys=object_keys,
        )

        desired: dict[MutationEdge, tuple[bool, str, str]] = {
            edge: (True, "structural_projection", item.source_revision)
            for edge, item in structural.items()
        }
        active_resource_objects = {
            (edge.object_type, edge.object_id)
            for edge in structural
            if edge.relation == "organization"
        }
        for edge, mutation in latest.items():
            if mutation.kind == "structural_projection":
                if edge not in desired:
                    desired[edge] = (
                        False,
                        "structural_projection",
                        mutation.source_revision or "structural-removed",
                    )
            else:
                # A direct grant cannot keep an already-deleted resource alive
                # in the authorization graph. Root-resource deletion removes
                # its structural organization tuple in the business
                # transaction; the reconciler then converts every remaining
                # direct binding into an explicit newer delete revision.
                resource_is_active = (
                    edge.object_type,
                    edge.object_id,
                ) in active_resource_objects
                desired[edge] = (
                    (
                        mutation.desired_state == "present"
                        and resource_is_active
                    ),
                    "direct_binding",
                    mutation.source_revision or (
                        f"direct:{mutation.mutation_id}"
                    ),
                )
        coordinator = AuthzMutationCoordinator(
            client=client,
            organization_id=organization_id,
        )
        all_edges = set(desired) | actual
        for edge in sorted(all_edges, key=MutationEdge.lock_key):
            desired_present, kind, revision = desired.get(
                edge,
                (
                    False,
                    _inferred_kind(edge, structural, latest),
                    "security-drift-unexplained-tuple",
                ),
            )
            actual_present = edge in actual
            if desired_present == actual_present:
                continue
            mutation = await coordinator.enqueue_repair(
                session=session,
                edge=edge,
                desired_present=desired_present,
                kind=kind,
                source_revision=revision,
            )
            mutation_ids.append(mutation.mutation_id)
            stats.repairs_requested += 1
            if actual_present and not desired_present and edge not in desired:
                stats.unexplained_tuples_removed += 1

    coordinator = AuthzMutationCoordinator(
        client=client,
        organization_id=organization_id,
    )
    for mutation_id in mutation_ids:
        try:
            await coordinator.apply_mutation(mutation_id)
        except AuthzMutationSupersededError:
            continue
        except OpenFgaUnavailableError:
            stats.failures += 1
        else:
            stats.repairs_applied += 1
    return stats


async def reconcile_all(
    client: OpenFgaHttpClient,
) -> ReconcileStats:
    """Run one bounded-by-current-inventory authorization convergence pass."""
    stats = await reconcile_due_mutations(client)
    if stats.failures:
        return stats
    for organization_id in await list_organization_ids():
        try:
            result = await reconcile_organization(client, organization_id)
        except OpenFgaUnavailableError:
            stats.failures += 1
            break
        except Exception:
            logger.exception("authorization_reconcile_organization_failed")
            stats.failures += 1
        else:
            stats.merge(result)
    logger.info(
        "authorization_reconcile_finished",
        organizations=stats.organizations,
        skipped=stats.skipped,
        pending_applied=stats.pending_applied,
        repairs_requested=stats.repairs_requested,
        repairs_applied=stats.repairs_applied,
        failures=stats.failures,
        unexplained_tuples_removed=stats.unexplained_tuples_removed,
    )
    return stats


async def collect_structural_projection(
    session: AsyncSession,
    *,
    organization_id: str,
) -> dict[MutationEdge, DesiredProjection]:
    """Read canonical Postgres facts for one organization."""
    result: dict[MutationEdge, DesiredProjection] = {}

    memberships = (
        await session.execute(
            text(
                """
                SELECT user_id::text AS user_id, org_role, status, updated_at
                FROM org_memberships
                """
            )
        )
    ).mappings()
    for row in memberships:
        source = _source_revision("org-membership", row)
        for edge in organization_membership_edges(
            organization_id=organization_id,
            user_id=row["user_id"],
            role=row["org_role"],
            status=row["status"],
        ):
            result[edge] = DesiredProjection(edge, source)

    groups = (
        await session.execute(
            text(
                """
                SELECT group_id::text AS group_id,
                       parent_group_id::text AS parent_group_id,
                       status,
                       updated_at
                FROM groups
                """
            )
        )
    ).mappings().all()
    active_group_ids = {
        row["group_id"] for row in groups if row["status"] == "active"
    }
    for row in groups:
        parent_id = row["parent_group_id"]
        source = _source_revision("group", row)
        for edge in group_edges(
            organization_id=organization_id,
            group_id=row["group_id"],
            parent_group_id=(
                parent_id if parent_id in active_group_ids else None
            ),
            status=row["status"],
        ):
            result[edge] = DesiredProjection(edge, source)

    group_memberships = (
        await session.execute(
            text(
                """
                SELECT membership.user_id::text AS user_id,
                       membership.group_id::text AS group_id,
                       membership.group_role,
                       membership.status,
                       membership.updated_at
                FROM group_memberships AS membership
                JOIN groups AS group_row
                  ON group_row.group_id = membership.group_id
                WHERE group_row.status = 'active'
                """
            )
        )
    ).mappings()
    for row in group_memberships:
        source = _source_revision("group-membership", row)
        for edge in group_membership_edges(
            organization_id=organization_id,
            group_id=row["group_id"],
            user_id=row["user_id"],
            role=row["group_role"],
            status=row["status"],
        ):
            result[edge] = DesiredProjection(edge, source)

    for spec in _RESOURCE_QUERIES:
        rows = (await session.execute(text(spec.sql))).mappings()
        for row in rows:
            source = _source_revision(spec.object_type, row)
            for edge in resource_root_edges(
                organization_id=organization_id,
                object_type=spec.object_type,
                object_id=row["object_id"],
                owner_relation=spec.owner_relation,
                owner_type="user",
                owner_id=row["owner_id"],
            ):
                result[edge] = DesiredProjection(edge, source)

    service_accounts = (
        await session.execute(
            text(
                """
                SELECT sa.service_account_id::text AS service_account_id,
                       sa.created_by::text AS created_by,
                       sa.owner_resource_type,
                       sa.owner_resource_id,
                       sa.status,
                       sa.updated_at,
                       COALESCE(t.workflow_id, d.wf_id) AS workflow_id,
                       COALESCE(
                           array_agg(sac.credential_id::text)
                               FILTER (WHERE sac.credential_id IS NOT NULL),
                           ARRAY[]::text[]
                       ) AS credential_ids
                FROM service_accounts AS sa
                LEFT JOIN tasks AS t
                  ON sa.owner_resource_type = 'task'
                 AND sa.owner_resource_id = t.id::text
                 AND t.service_account_id = sa.service_account_id
                LEFT JOIN deployments AS d
                  ON sa.owner_resource_type = 'deployment'
                 AND sa.owner_resource_id = d.id::text
                 AND d.service_account_id = sa.service_account_id
                 AND d.deleted_at IS NULL
                LEFT JOIN service_account_credentials AS sac
                  ON sac.service_account_id = sa.service_account_id
                WHERE sa.status != 'deleted'
                  AND COALESCE(t.workflow_id, d.wf_id) IS NOT NULL
                GROUP BY sa.service_account_id, sa.created_by,
                         sa.owner_resource_type, sa.owner_resource_id,
                         sa.status, sa.updated_at,
                         COALESCE(t.workflow_id, d.wf_id)
                """
            )
        )
    ).mappings()
    for row in service_accounts:
        source = _source_revision("service-account", row)
        for edge in service_account_edges(
            organization_id=organization_id,
            service_account_id=row["service_account_id"],
            created_by=row["created_by"],
            owner_resource_type=row["owner_resource_type"],
            owner_resource_id=row["owner_resource_id"],
            workflow_id=row["workflow_id"],
            status=row["status"],
            credential_ids=tuple(row["credential_ids"] or ()),
        ):
            result[edge] = DesiredProjection(edge, source)

    return result


@dataclass(frozen=True, slots=True)
class _ResourceQuery:
    object_type: str
    owner_relation: str
    sql: str


_RESOURCE_QUERIES = (
    _ResourceQuery(
        "workflow",
        "manager",
        """
        SELECT wf_id AS object_id,
               owner_id::text AS owner_id,
               updated_at
        FROM workflows WHERE deleted_at IS NULL
        """,
    ),
    _ResourceQuery(
        "task",
        "manager",
        """
        SELECT id::text AS object_id,
               owner_id::text AS owner_id,
               submitted_at AS updated_at
        FROM tasks
        """,
    ),
    _ResourceQuery(
        "deployment",
        "manager",
        """
        SELECT id::text AS object_id,
               owner_id::text AS owner_id,
               updated_at
        FROM deployments WHERE deleted_at IS NULL
        """,
    ),
    _ResourceQuery(
        "knowledge_base",
        "manager",
        """
        SELECT id::text AS object_id, user_id::text AS owner_id, updated_at
        FROM knowledge_bases WHERE deleted_at IS NULL
        """,
    ),
    _ResourceQuery(
        "skill_installation",
        "manager",
        """
        SELECT skill_id::text AS object_id,
               user_id::text AS owner_id,
               updated_at
        FROM skills WHERE deleted_at IS NULL
        """,
    ),
    _ResourceQuery(
        "chat",
        "creator",
        """
        SELECT chat_id AS object_id,
               creator_user_id::text AS owner_id,
               updated_at
        FROM chats WHERE deleted_at IS NULL
        """,
    ),
    _ResourceQuery(
        "mcp_installation",
        "installer",
        """
        SELECT id::text AS object_id, user_id::text AS owner_id, updated_at
        FROM mcp_servers WHERE deleted_at IS NULL
        """,
    ),
    _ResourceQuery(
        "llm_credential",
        "owner",
        """
        SELECT id::text AS object_id, user_id::text AS owner_id, updated_at
        FROM llm_credentials WHERE deleted_at IS NULL
        """,
    ),
)


async def _latest_mutations(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> dict[MutationEdge, AuthzMutation]:
    rows = (
        await session.execute(
            select(AuthzMutation)
            .where(AuthzMutation.tenant_id == organization_id)
            .order_by(AuthzMutation.edge_revision.desc())
        )
    ).scalars()
    result: dict[MutationEdge, AuthzMutation] = {}
    for mutation in rows:
        edge = MutationEdge(
            organization_id=str(mutation.tenant_id),
            object_type=mutation.object_type,
            object_id=mutation.object_id,
            relation=mutation.relation,
            subject_type=mutation.subject_type,
            subject_id=mutation.subject_id,
            subject_relation=mutation.subject_relation or "",
        )
        result.setdefault(edge, mutation)
    return result


async def _read_object_tuples(
    client: OpenFgaHttpClient,
    *,
    organization_id: str,
    object_keys: set[tuple[str, str]],
) -> set[MutationEdge]:
    """Read current tuples without issuing one HTTP request per resource.

    OpenFGA's ``read`` endpoint can page over the store when the tuple key is
    empty.  The previous implementation filtered by object and therefore made
    an N+1 request for every workflow, task, chat, and installation on every
    reconciliation pass.  That saturated the worker and OpenFGA even for a
    modest personal workspace.  Read each page once and retain only objects
    from this tenant's canonical inventory; this preserves the same drift
    boundary while making request count proportional to tuple pages.
    """
    result: set[MutationEdge] = set()
    if not object_keys:
        return result

    continuation = ""
    while True:
        page = await client.read(
            tuple_key=OpenFgaTuple(user="", relation="", object=""),
            continuation_token=continuation,
        )
        for item in page.tuples:
            object_type, separator, object_id = item.object.partition(":")
            if not separator or not object_type or not object_id:
                raise OpenFgaUnavailableError(
                    "authorization_invalid_response"
                )
            if (object_type, object_id) not in object_keys:
                continue
            subject_type, subject_id, subject_relation = _parse_subject(
                item.user
            )
            result.add(MutationEdge(
                # Only objects discovered from this tenant's canonical data
                # or durable ledger cross the organization boundary here.
                organization_id=organization_id,
                object_type=object_type,
                object_id=object_id,
                relation=item.relation,
                subject_type=subject_type,
                subject_id=subject_id,
                subject_relation=subject_relation,
            ))
        continuation = page.continuation_token
        if not continuation:
            break
    return result


def _inferred_kind(
    edge: MutationEdge,
    structural: dict[MutationEdge, DesiredProjection],
    latest: dict[MutationEdge, AuthzMutation],
) -> str:
    if edge in structural:
        return "structural_projection"
    existing = latest.get(edge)
    if existing is not None:
        return existing.kind
    if (
        edge.object_type in {"organization", "group"}
        or edge.relation == "organization"
        or (
            edge.object_type in _PRIVATE_OWNER_RELATIONS
            and edge.relation == _PRIVATE_OWNER_RELATIONS[edge.object_type]
        )
    ):
        return "structural_projection"
    return "direct_binding"


def _parse_subject(value: str) -> tuple[str, str, str]:
    subject, separator, relation = value.partition("#")
    subject_type, colon, subject_id = subject.partition(":")
    if (
        not colon
        or subject_type
        not in {"user", "service_account", "group", "organization"}
        or not subject_id
        or ":" in subject_id
        or "#" in subject_id
        or (separator and not relation)
    ):
        raise OpenFgaUnavailableError("authorization_invalid_response")
    return subject_type, subject_id, relation


def _source_revision(prefix: str, row: Any) -> str:
    updated_at = row.get("updated_at")
    if isinstance(updated_at, datetime):
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        timestamp = updated_at.isoformat()
    else:
        timestamp = str(updated_at or "")
    values = ":".join(
        str(row.get(key) or "")
        for key in (
            "user_id",
            "group_id",
            "parent_group_id",
            "org_role",
            "group_role",
            "status",
            "object_id",
            "owner_id",
        )
    )
    return f"{prefix}:{timestamp}:{values}"
