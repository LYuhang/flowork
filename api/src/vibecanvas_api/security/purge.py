"""Durable, phase-separated account erasure worker.

Postgres owns the state machine.  Each external store is an independent phase,
so a crash resumes from the last committed boundary and a job is never marked
completed while a required phase is missing.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
from typing import Awaitable, Callable
import uuid

import asyncpg
from sqlalchemy import delete, select, text, update

from vibecanvas_api.audit import actions
from vibecanvas_api.audit.service import record_auth_audit
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaTuple,
    openfga_client_from_config,
)
from vibecanvas_api.config import config
from vibecanvas_api.security.redaction import redact_text
from vibecanvas_api.services.agent_runtime.checkpoint_store import LangChainCheckpointStore
from vibecanvas_api.services.chat_workspace import chat_workspace_scope_id
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.services.user_mount_workspace import (
    host_mount_bridge,
    mount_scope_id,
)
from vibecanvas_api.services.vfs_volume import get_chat_runtime_volume_provider
from vibecanvas_api.storage.models import Base, Chat
from vibecanvas_api.storage.models_purge import DataPurgeJob


LEASE_SECONDS = 15 * 60
PHASES = (
    "runtime_state",
    "object_store",
    "redis",
    "authorization",
    "database",
    "backup_retention",
)

_DELETED_ACTOR_NAMESPACE = uuid.UUID("3cb170d4-f98d-4967-b45e-cda7fe668a76")

# OpenFGA 1.18 requires the object type when Read filters by user. Keep this
# set aligned with the checked-in authorization model so erasure also removes
# user tuples that drifted out of the SQL projection or belong to a shared
# organization outside the personal tenant.
_OPENFGA_USER_OBJECT_TYPES = frozenset({
    "chat",
    "deployment",
    "group",
    "knowledge_base",
    "llm_credential",
    "mcp_installation",
    "organization",
    "service_account",
    "skill_installation",
    "storage_root",
    "task",
    "template",
    "workflow",
})


@dataclass(frozen=True, slots=True)
class PurgeLease:
    job_id: uuid.UUID
    deletion_request_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    completed_phases: tuple[str, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def claim_due_purge_job() -> PurgeLease | None:
    """Claim one due job. Failed jobs require an explicit operator requeue."""
    async with session_scope_admin() as session:
        row = (
            await session.execute(
                select(DataPurgeJob)
                .where(
                    DataPurgeJob.status.in_(("queued", "running")),
                    DataPurgeJob.available_at <= _now(),
                    (
                        (DataPurgeJob.status == "queued")
                        | (DataPurgeJob.lease_expires_at < _now())
                    ),
                )
                .order_by(DataPurgeJob.available_at, DataPurgeJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        request_status = (
            await session.execute(
                text("SELECT status FROM account_deletion_requests WHERE id = :id"),
                {"id": row.deletion_request_id},
            )
        ).scalar_one_or_none()
        if request_status not in {"pending", "purging"}:
            row.status = "cancelled"
            row.lease_expires_at = None
            row.updated_at = _now()
            return None
        row.status = "running"
        row.current_phase = None
        row.attempt_count += 1
        row.started_at = row.started_at or _now()
        row.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
        row.updated_at = _now()
        await session.execute(
            text(
                "UPDATE account_deletion_requests SET status = 'purging', "
                "purging_at = coalesce(purging_at, now()), "
                "attempt_count = attempt_count + 1 WHERE id = :id"
            ),
            {"id": row.deletion_request_id},
        )
        await session.flush()
        return PurgeLease(
            job_id=row.job_id,
            deletion_request_id=row.deletion_request_id,
            user_id=row.user_id,
            tenant_id=row.tenant_id,
            completed_phases=tuple(row.completed_phases or []),
        )


@dataclass(frozen=True, slots=True)
class ChatRuntimeCoordinate:
    tenant_id: uuid.UUID
    chat_id: str
    thread_id: str


async def _chat_runtime_coordinates(
    lease: PurgeLease,
) -> tuple[ChatRuntimeCoordinate, ...]:
    async with session_scope_admin() as session:
        rows = (
            await session.execute(
                select(
                    Chat.tenant_id,
                    Chat.chat_id,
                    Chat.scope_id,
                    Chat.major_version,
                ).where(
                    (Chat.tenant_id == lease.tenant_id)
                    | (Chat.creator_user_id == lease.user_id)
                )
            )
        ).all()
    coordinates: list[ChatRuntimeCoordinate] = []
    for tenant_id, chat_id, scope_id, major_version in rows:
        thread_id = (
            f"{lease.user_id}__{scope_id}__v{major_version}__{chat_id}"
            if major_version
            else f"{lease.user_id}__{scope_id}__{chat_id}"
        )
        coordinates.append(
            ChatRuntimeCoordinate(
                tenant_id=uuid.UUID(str(tenant_id)),
                chat_id=str(chat_id),
                thread_id=thread_id,
            )
        )
    return tuple(coordinates)


async def _user_tenant_ids(lease: PurgeLease) -> tuple[uuid.UUID, ...]:
    async with session_scope_admin() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id FROM org_memberships WHERE user_id=:user_id "
                    "UNION SELECT tenant_id FROM chats WHERE creator_user_id=:user_id"
                ),
                {"user_id": lease.user_id},
            )
        ).scalars().all()
    values = {lease.tenant_id}
    values.update(uuid.UUID(str(value)) for value in rows)
    return tuple(sorted(values, key=str))


def _safe_remove_tenant_directory(root: str, tenant_id: uuid.UUID) -> None:
    if not root:
        return
    resolved_root = Path(root).resolve()
    target = (resolved_root / str(tenant_id)).resolve()
    if target.parent != resolved_root:
        raise RuntimeError("purge directory escaped its configured root")
    if target.exists():
        shutil.rmtree(target)


def _safe_remove_user_directory(
    root: str,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    if not root:
        return
    resolved_root = Path(root).resolve()
    tenant_directory = resolved_root / str(tenant_id)
    if tenant_directory.is_symlink():
        raise RuntimeError("purge tenant directory must not be a symbolic link")
    target = tenant_directory / str(user_id)
    if target.is_symlink():
        target.unlink()
        return
    if target.exists():
        resolved_target = target.resolve()
        if resolved_target.parent != tenant_directory.resolve():
            raise RuntimeError("purge user directory escaped its configured root")
        shutil.rmtree(resolved_target)


def _safe_remove_host_mount(user_id: uuid.UUID) -> None:
    root = config.storage.mount_path
    if root is None:
        return
    resolved_root = root.resolve(strict=True)
    users_root = resolved_root / "users"
    if users_root.is_symlink():
        raise RuntimeError("MOUNT_PATH/users must not be a symbolic link")
    target = users_root / str(user_id)
    if target.is_symlink():
        target.unlink()
        return
    if target.exists():
        resolved_target = target.resolve()
        if resolved_target.parent != users_root.resolve():
            raise RuntimeError("purge host mount escaped MOUNT_PATH")
        shutil.rmtree(resolved_target)


async def _purge_runtime_state(lease: PurgeLease) -> None:
    async with session_scope_admin() as session:
        celery_ids = (
            await session.execute(
                text(
                    "SELECT celery_id FROM tasks WHERE tenant_id=:tenant_id "
                    "AND status IN ('queued','running','cancelling','resuming') "
                    "AND celery_id IS NOT NULL"
                ),
                {"tenant_id": lease.tenant_id},
            )
        ).scalars().all()
        await session.execute(
            text(
                "UPDATE tasks SET status='cancelled', finished_at=now() "
                "WHERE tenant_id=:tenant_id "
                "AND status IN ('queued','running','cancelling','resuming')"
            ),
            {"tenant_id": lease.tenant_id},
        )
    if celery_ids:
        from vibecanvas_api.celery_app import celery_app

        for celery_id in celery_ids:
            await asyncio.to_thread(
                celery_app.control.revoke,
                str(celery_id),
                terminate=True,
                signal="SIGTERM",
            )
    tenant_ids = await _user_tenant_ids(lease)
    # Celery workers do not run the FastAPI lifespan, so their process-local
    # manager singleton may not exist yet.  Construct the configured service
    # proxy here instead of treating an uninitialized singleton as “no
    # sandbox”.  The daemon owns these mounted volumes and is the only process
    # that can reliably erase them in the Docker deployment topology.
    manager = get_sandbox_manager()
    await manager.close_user(str(lease.user_id), reason="account_purge")
    await manager.close_tenant(str(lease.tenant_id), reason="account_purge")
    await manager.purge_user_storage(
        str(lease.user_id),
        [str(tenant_id) for tenant_id in tenant_ids],
        str(lease.tenant_id),
    )
    await host_mount_bridge.unregister_user(user_id=str(lease.user_id))
    coordinates = await _chat_runtime_coordinates(lease)
    grouped: dict[uuid.UUID, list[ChatRuntimeCoordinate]] = defaultdict(list)
    for coordinate in coordinates:
        grouped[coordinate.tenant_id].append(coordinate)
    store = LangChainCheckpointStore()
    try:
        for tenant_id, tenant_coordinates in grouped.items():
            chats = [value.chat_id for value in tenant_coordinates]
            threads = [value.thread_id for value in tenant_coordinates]
            if tenant_id == lease.tenant_id:
                await store.purge_organization(
                    str(tenant_id), legacy_thread_ids=threads, chat_ids=chats
                )
            else:
                await store.purge_chats(
                    str(tenant_id), legacy_thread_ids=threads, chat_ids=chats
                )
    finally:
        await store.close()

    volume_provider = get_chat_runtime_volume_provider()
    for coordinate in coordinates:
        await asyncio.to_thread(
            volume_provider.delete,
            tenant_id=str(coordinate.tenant_id),
            user_id=str(lease.user_id),
            chat_scope_id=chat_workspace_scope_id(coordinate.chat_id),
        )

    roots = {
        os.path.realpath(root): root
        for root in (
            config.agent_overlay_root,
            config.agent_runtime_root,
            config.vfs_volume_root,
        )
        if root
    }
    for root in roots.values():
        for tenant_id in tenant_ids:
            await asyncio.to_thread(
                _safe_remove_user_directory,
                root,
                tenant_id,
                lease.user_id,
            )
        await asyncio.to_thread(
            _safe_remove_tenant_directory,
            root,
            lease.tenant_id,
        )
    await asyncio.to_thread(_safe_remove_host_mount, lease.user_id)


async def _purge_object_store(lease: PurgeLease) -> None:
    store = get_object_store()
    tenant = str(lease.tenant_id)
    user_mount_scope = mount_scope_id(str(lease.user_id))
    async with session_scope_admin() as session:
        task_ids = (
            await session.execute(
                text("SELECT id FROM tasks WHERE tenant_id=:tenant_id"),
                {"tenant_id": lease.tenant_id},
            )
        ).scalars().all()
        mount_object_keys = (
            await session.execute(
                text(
                    "SELECT object_key FROM vfs_artifacts "
                    "WHERE scope_id=:scope_id AND object_key IS NOT NULL "
                    "UNION SELECT object_key FROM vfs_scratch "
                    "WHERE scope_id=:scope_id AND object_key IS NOT NULL"
                ),
                {"scope_id": user_mount_scope},
            )
        ).scalars().all()
    # Every prefix is non-empty and includes the exact organization UUID.
    prefixes = (
        f"artifacts/{tenant}/",
        f"scratch/{tenant}/",
        f"run/{tenant}/",
        f"kb/{tenant}/",
        f"batch/{tenant}/",
        f"skills/{tenant}/",
        f"chat-runtime-v1/{tenant}/",
    )
    for prefix in prefixes:
        await asyncio.to_thread(store.delete_prefix, prefix)
    for task_id in task_ids:
        await asyncio.to_thread(store.delete_prefix, f"tasks/{task_id}/")
    for object_key in mount_object_keys:
        await asyncio.to_thread(store.delete_bytes, str(object_key))


async def _purge_redis(lease: PurgeLease) -> None:
    if not config.redis.url:
        return
    import redis.asyncio as aioredis

    client = aioredis.from_url(config.redis.url, decode_responses=False)
    tenant = str(lease.tenant_id)
    user = str(lease.user_id)
    patterns = (
        f"vibecanvas:*:organization:{tenant}:*",
        f"vibecanvas:*:tenant:{tenant}:*",
        f"vibecanvas:*:{tenant}:*",
        f"vibecanvas:auth:*:{user}:*",
    )
    try:
        for pattern in patterns:
            batch: list[bytes] = []
            async for key in client.scan_iter(match=pattern, count=250):
                batch.append(key)
                if len(batch) >= 250:
                    await client.delete(*batch)
                    batch.clear()
            if batch:
                await client.delete(*batch)
    finally:
        await client.aclose()


async def _read_openfga_tuples(
    client,
    *,
    tuple_key: OpenFgaTuple,
) -> set[OpenFgaTuple]:
    result: set[OpenFgaTuple] = set()
    continuation_token = ""
    while True:
        page = await client.read(
            tuple_key=tuple_key,
            continuation_token=continuation_token,
            page_size=100,
        )
        result.update(page.tuples)
        continuation_token = page.continuation_token
        if not continuation_token:
            return result


async def _read_user_openfga_tuples(
    client,
    *,
    user_id: uuid.UUID,
) -> set[OpenFgaTuple]:
    """Read every direct user tuple using OpenFGA's typed-object contract."""
    result: set[OpenFgaTuple] = set()
    for object_type in sorted(_OPENFGA_USER_OBJECT_TYPES):
        result.update(
            await _read_openfga_tuples(
                client,
                tuple_key=OpenFgaTuple(
                    user=f"user:{user_id}",
                    relation="",
                    object=f"{object_type}:",
                ),
            )
        )
    return result


async def _purge_openfga_change_history(
    *,
    subjects: set[str],
    object_ids: set[str],
) -> int:
    """Erase identity-bearing OpenFGA change-feed rows via a scoped function.

    OpenFGA's public API removes live tuples but deliberately retains their
    write/delete history for ``ReadChanges``.  A deployment that promises
    irreversible account erasure therefore provisions a database role that
    can execute only ``flowork_erase_changelog``; it cannot read or mutate
    live relationship tuples.
    """

    dsn = config.openfga_erasure_database_url
    if not dsn:
        raise RuntimeError(
            "OPENFGA_ERASURE_DATABASE_URL is required for account erasure "
            "when OpenFGA is enabled"
        )
    connection = await _connect_openfga_erasure_database(
        dsn.replace("+asyncpg", "")
    )
    try:
        removed = await connection.fetchval(
            "SELECT public.flowork_erase_changelog($1, $2::text[], $3::text[])",
            config.openfga_store_id,
            sorted(subjects),
            sorted(object_ids),
        )
        return int(removed or 0)
    finally:
        await connection.close()


async def _connect_openfga_erasure_database(dsn: str):
    return await asyncpg.connect(dsn=dsn)


async def _purge_authorization(lease: PurgeLease) -> None:
    async with session_scope_admin() as session:
        object_rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT object_type, object_id "
                    "FROM authz_edge_revisions "
                    "WHERE tenant_id=:tenant_id "
                    "   OR (subject_type='user' AND subject_id=:user_id)"
                ),
                {
                    "tenant_id": lease.tenant_id,
                    "user_id": str(lease.user_id),
                },
            )
        ).all()
        # Stop the reconciler from recreating an erased user's edges while the
        # external tuple deletion is running. The erasure-only trigger branch
        # permits only these identity fields to be scrubbed.
        await session.execute(text("SET LOCAL app.account_erasure = 'on'"))
        await session.execute(
            text(
                "UPDATE authz_mutations SET "
                "actor_type=CASE WHEN actor_type='user' AND actor_id=:user_id "
                "  THEN 'system' ELSE actor_type END, "
                "actor_id=CASE WHEN actor_type='user' AND actor_id=:user_id "
                "  THEN 'account-erasure' ELSE actor_id END, "
                "subject_id=CASE WHEN subject_type='user' AND subject_id=:user_id "
                "  THEN 'account-erasure' ELSE subject_id END, "
                "status=CASE WHEN subject_type='user' AND subject_id=:user_id "
                "  THEN 'superseded' ELSE status END, "
                "revocation_guard_active=CASE "
                "  WHEN subject_type='user' AND subject_id=:user_id "
                "  THEN false ELSE revocation_guard_active END, "
                "next_attempt_at=CASE "
                "  WHEN subject_type='user' AND subject_id=:user_id "
                "  THEN NULL ELSE next_attempt_at END "
                "WHERE actor_type='user' AND actor_id=:user_id "
                "   OR subject_type='user' AND subject_id=:user_id"
            ),
            {"user_id": str(lease.user_id)},
        )
        await session.execute(
            text(
                "DELETE FROM authz_edge_revisions "
                "WHERE tenant_id=:tenant_id "
                "   OR (subject_type='user' AND subject_id=:user_id)"
            ),
            {
                "tenant_id": lease.tenant_id,
                "user_id": str(lease.user_id),
            },
        )

    openfga_values = (
        config.openfga_api_url,
        config.openfga_store_id,
        config.openfga_authorization_model_id,
    )
    if not any(openfga_values):
        return
    if not all(openfga_values):
        raise RuntimeError("OpenFGA is only partially configured")

    client = openfga_client_from_config()
    try:
        doomed = await _read_user_openfga_tuples(
            client,
            user_id=lease.user_id,
        )
        for object_type, object_id in object_rows:
            doomed.update(
                await _read_openfga_tuples(
                    client,
                    tuple_key=OpenFgaTuple(
                        user="",
                        relation="",
                        object=f"{object_type}:{object_id}",
                    ),
                )
            )
        ordered = sorted(doomed, key=lambda item: (item.object, item.relation, item.user))
        for offset in range(0, len(ordered), 100):
            await client.write(deletes=ordered[offset:offset + 100])

        # Verify that the authoritative relationship set is empty before its
        # identity-bearing change history is removed.  This preserves
        # fail-closed revocation semantics if OpenFGA rejects any batch.
        remaining = await _read_user_openfga_tuples(
            client,
            user_id=lease.user_id,
        )
        for object_type, object_id in object_rows:
            remaining.update(
                await _read_openfga_tuples(
                    client,
                    tuple_key=OpenFgaTuple(
                        user="",
                        relation="",
                        object=f"{object_type}:{object_id}",
                    ),
                )
            )
        if remaining:
            raise RuntimeError("OpenFGA tuples remained after account erasure")

        subjects = {f"user:{lease.user_id}"}
        object_ids = {str(lease.user_id), str(lease.tenant_id)}
        for item in doomed:
            subjects.add(item.user)
            _object_type, separator, object_id = item.object.partition(":")
            if separator and object_id:
                object_ids.add(object_id)
        object_ids.update(str(object_id) for _object_type, object_id in object_rows)
        await _purge_openfga_change_history(
            subjects=subjects,
            object_ids=object_ids,
        )
    finally:
        await client.close()


_PRESERVED_TENANT_TABLES = frozenset({
    "account_deletion_requests",
    "audit_log",
    "content_encryption_keys",
    "data_purge_jobs",
    "organizations",
    "tenants",
    "users",
})

_USER_SCOPED_TABLES = frozenset({
    "auth_identities",
    "enterprise_directory_users",
    "group_memberships",
    "mcp_oauth_connections",
    "mcp_oauth_transactions",
    "org_memberships",
    "password_reset_tokens",
    "platform_admin_eligibilities",
    "session_exchange_codes",
    "sessions",
    "user_agent_preferences",
    "user_webauthn_challenges",
    "user_webauthn_credentials",
})


async def _purge_database(lease: PurgeLease) -> None:
    async with session_scope_admin() as session:
        # Break the nullable sides of the schema's intentional FK cycles before
        # walking the ordinary reverse dependency order. Without this, a
        # published Skill keeps its current revision alive while the revision
        # still owns the Skill; privileged sessions have the same two-way
        # relationship with their access request.
        await session.execute(
            text(
                "UPDATE skills SET current_revision_id=NULL "
                "WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": lease.tenant_id},
        )
        await session.execute(
            text(
                "UPDATE sessions SET privileged_access_request_id=NULL "
                "WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": lease.tenant_id},
        )
        await session.execute(
            text(
                "UPDATE privileged_access_requests SET "
                "requested_session_id=NULL, activated_session_id=NULL "
                "WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": lease.tenant_id},
        )
        # Reverse FK order makes child rows disappear before their parents.
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in _PRESERVED_TENANT_TABLES or "tenant_id" not in table.c:
                continue
            await session.execute(
                delete(table).where(table.c.tenant_id == lease.tenant_id)
            )
        # Membership in other organizations is user data, not owned content.
        await session.execute(
            text("DELETE FROM vfs_artifacts WHERE scope_id=:scope_id"),
            {"scope_id": mount_scope_id(str(lease.user_id))},
        )
        await session.execute(
            text("DELETE FROM vfs_scratch WHERE scope_id=:scope_id"),
            {"scope_id": mount_scope_id(str(lease.user_id))},
        )
        for table in reversed(Base.metadata.sorted_tables):
            if table.name not in _USER_SCOPED_TABLES:
                continue
            user_columns = [
                foreign_key.parent
                for foreign_key in table.foreign_keys
                if foreign_key.column.table.name == "users"
            ]
            if not user_columns:
                continue
            predicate = user_columns[0] == lease.user_id
            for column in user_columns[1:]:
                predicate = predicate | (column == lease.user_id)
            await session.execute(delete(table).where(predicate))


async def _record_backup_retention(_lease: PurgeLease) -> None:
    # Active stores have already been erased. Immutable encrypted backups age
    # out under the deployment retention policy; production startup separately
    # requires BACKUP_ENCRYPTION_VERIFIED before this worker may run.
    return None


_PHASE_HANDLERS: dict[str, Callable[[PurgeLease], Awaitable[None]]] = {
    "runtime_state": _purge_runtime_state,
    "object_store": _purge_object_store,
    "redis": _purge_redis,
    "authorization": _purge_authorization,
    "database": _purge_database,
    "backup_retention": _record_backup_retention,
}


async def _mark_phase(job_id: uuid.UUID, phase: str) -> None:
    async with session_scope_admin() as session:
        row = (
            await session.execute(
                select(DataPurgeJob).where(DataPurgeJob.job_id == job_id).with_for_update()
            )
        ).scalar_one()
        completed = list(row.completed_phases or [])
        if phase not in completed:
            completed.append(phase)
        row.completed_phases = completed
        row.current_phase = None
        row.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
        row.updated_at = _now()


async def _ensure_deleted_actor(session, tenant_id: uuid.UUID) -> uuid.UUID:
    actor_id = uuid.uuid5(_DELETED_ACTOR_NAMESPACE, str(tenant_id))
    await session.execute(
        text(
            "INSERT INTO users "
            "(user_id, tenant_id, email, display_name, status) "
            "VALUES (:user_id, :tenant_id, :email, '', 'disabled') "
            "ON CONFLICT (user_id) DO NOTHING"
        ),
        {
            "user_id": actor_id,
            "tenant_id": tenant_id,
            "email": f"deleted-actor-{tenant_id.hex}@invalid.local",
        },
    )
    row = (
        await session.execute(
            text(
                "SELECT tenant_id, status, profile_ciphertext, profile_key_id "
                "FROM users WHERE user_id=:user_id"
            ),
            {"user_id": actor_id},
        )
    ).one()
    if (
        row.tenant_id != tenant_id
        or row.status != "disabled"
        or row.profile_ciphertext is not None
        or row.profile_key_id is not None
    ):
        raise RuntimeError("organization deleted-actor identity is invalid")
    return actor_id


async def _reassign_organization_content(session, lease: PurgeLease) -> None:
    skip_tables = _USER_SCOPED_TABLES | {
        "account_deletion_requests",
        "audit_log",
        "data_purge_jobs",
        "users",
    }
    for table in Base.metadata.sorted_tables:
        if table.name in skip_tables or "tenant_id" not in table.c:
            continue
        user_columns = [
            foreign_key.parent
            for foreign_key in table.foreign_keys
            if foreign_key.column.table.name == "users"
        ]
        for column in user_columns:
            tenant_ids = (
                await session.execute(
                    select(table.c.tenant_id)
                    .where(
                        column == lease.user_id,
                        table.c.tenant_id != lease.tenant_id,
                    )
                    .distinct()
                )
            ).scalars().all()
            for tenant_id_value in tenant_ids:
                tenant_id = uuid.UUID(str(tenant_id_value))
                actor_id = await _ensure_deleted_actor(session, tenant_id)
                await session.execute(
                    update(table)
                    .where(
                        table.c.tenant_id == tenant_id,
                        column == lease.user_id,
                    )
                    .values({column.name: actor_id})
                )


async def _finalize_hard_delete(lease: PurgeLease) -> None:
    async with session_scope_admin() as session:
        row = (
            await session.execute(
                select(DataPurgeJob).where(DataPurgeJob.job_id == lease.job_id).with_for_update()
            )
        ).scalar_one()
        missing = [phase for phase in PHASES if phase not in (row.completed_phases or [])]
        if missing:
            raise RuntimeError("purge phases are incomplete")

        await session.execute(
            text("SELECT erase_account_audit(:user_id, :tenant_id)"),
            {"user_id": lease.user_id, "tenant_id": lease.tenant_id},
        )
        await _reassign_organization_content(session, lease)

        # These ledgers are not foreign-keyed to users, so remove or scrub the
        # erased UUID explicitly before deleting the personal tenant.
        await session.execute(text("SET LOCAL app.account_erasure = 'on'"))
        await session.execute(
            text(
                "UPDATE authz_mutations SET "
                "actor_type=CASE WHEN actor_type='user' AND actor_id=:user_id "
                "  THEN 'system' ELSE actor_type END, "
                "actor_id=CASE WHEN actor_type='user' AND actor_id=:user_id "
                "  THEN 'account-erasure' ELSE actor_id END, "
                "subject_id=CASE WHEN subject_type='user' AND subject_id=:user_id "
                "  THEN 'account-erasure' ELSE subject_id END "
                "WHERE tenant_id<>:tenant_id AND ("
                "  actor_type='user' AND actor_id=:user_id OR "
                "  subject_type='user' AND subject_id=:user_id)"
            ),
            {"user_id": str(lease.user_id), "tenant_id": lease.tenant_id},
        )
        await session.execute(
            delete(DataPurgeJob).where(DataPurgeJob.job_id == lease.job_id)
        )
        await session.execute(
            text("DELETE FROM account_deletion_requests WHERE id=:request_id"),
            {"request_id": lease.deletion_request_id},
        )
        await session.execute(
            text("DELETE FROM users WHERE user_id=:user_id"),
            {"user_id": lease.user_id},
        )
        await session.execute(
            text("DELETE FROM content_encryption_keys WHERE tenant_id=:tenant_id"),
            {"tenant_id": lease.tenant_id},
        )
        await session.execute(
            text("DELETE FROM tenants WHERE tenant_id=:tenant_id"),
            {"tenant_id": lease.tenant_id},
        )


async def _fail(lease: PurgeLease, phase: str, exc: Exception) -> None:
    message = redact_text(str(exc))[:1000] or "purge phase failed"
    async with session_scope_admin() as session:
        await session.execute(
            update(DataPurgeJob)
            .where(DataPurgeJob.job_id == lease.job_id)
            .values(
                status="failed",
                current_phase=phase,
                lease_expires_at=None,
                last_error_code=type(exc).__name__[:128],
                last_error_message=message,
                updated_at=_now(),
            )
        )
        await session.execute(
            text(
                "UPDATE account_deletion_requests SET status = 'failed', "
                "last_error = :error WHERE id = :id"
            ),
            {"error": message, "id": lease.deletion_request_id},
        )


async def run_purge_job(lease: PurgeLease) -> None:
    await record_auth_audit(
        action=actions.PURGE_STARTED,
        actor_user_id=lease.user_id,
        actor_email=None,
        tenant_id=lease.tenant_id,
        outcome="success",
        meta={"job_id": str(lease.job_id)},
    )
    completed = set(lease.completed_phases)
    current = "claim"
    erased = False
    try:
        for phase in PHASES:
            if phase in completed:
                continue
            current = phase
            async with session_scope_admin() as session:
                await session.execute(
                    update(DataPurgeJob)
                    .where(DataPurgeJob.job_id == lease.job_id)
                    .values(current_phase=phase, updated_at=_now())
                )
            await _PHASE_HANDLERS[phase](lease)
            await _mark_phase(lease.job_id, phase)
        current = "hard_delete"
        await _finalize_hard_delete(lease)
        erased = True
        await record_auth_audit(
            action=actions.PURGE_COMPLETED,
            actor_user_id=None,
            actor_email=None,
            tenant_id=None,
            outcome="success",
            meta={},
        )
    except Exception as exc:
        if not erased:
            await _fail(lease, current, exc)
        await record_auth_audit(
            action=actions.PURGE_FAILED,
            actor_user_id=None if erased else lease.user_id,
            actor_email=None,
            tenant_id=None if erased else lease.tenant_id,
            outcome="failure",
            meta={} if erased else {"job_id": str(lease.job_id), "phase": current},
        )
        raise


async def run_one_due_purge() -> bool:
    lease = await claim_due_purge_job()
    if lease is None:
        return False
    await run_purge_job(lease)
    return True
