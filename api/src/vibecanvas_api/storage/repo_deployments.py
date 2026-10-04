"""CRUD for ``deployments`` rows. Deployments T2.

All methods assume the session is already tenant-bound (via
``session_scope(tenant_id=...)``) — they do NOT set the ``app.tenant_id``
GUC themselves. The ONE exception is ``get_by_slug_admin`` /
``get_by_api_key``: these are admin-role lookups for the
``resolve_deployment_and_bind_tenant`` flow; slug/key is the
only thing the external caller knows BEFORE the tenant is known), so
they MUST be invoked through ``session_scope_admin``. Both are
RLS-unscoped on purpose — the api_key_hash / slug uniqueness invariants
(migrations 005 and 088) make them safe to look up cross-tenant.

Soft-delete is universal across this repo: every SELECT filters
``deleted_at IS NULL`` and the public "delete" path is an UPDATE that
sets ``deleted_at = now()`` plus ``enabled = FALSE``.

Returns: ``mappings()`` row dicts, NOT ORM ``Deployment`` instances.
This is deliberate forward-compat — Task 4+ route handlers consume the
dict shape uniformly across the create/list/get path; if a later task
needs ORM attribute access it can ``session.get(Deployment, id)``
explicitly.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class DeploymentsRepo:
    """Async CRUD over ``deployments``.

    Caller owns the transaction: mutating methods do NOT ``commit()``
    (the DI request session commits at request end; background writers
    commit explicitly via ``session_scope`` / ``session_scope_admin``).
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, dep_id: uuid.UUID) -> Optional[dict]:
        """Tenant-scoped fetch (session must already have
        ``app.tenant_id`` set, or RLS will hide the row). Filters
        soft-deleted rows."""
        row = (await self.session.execute(
            text(
                """
                SELECT d.*,
                       (SELECT 'v' || (r.spec->>'pinned_major') || '.sv' || (r.spec->>'pinned_sub')
                        FROM deployment_runtime_revisions r WHERE r.id = d.active_revision_id
                        AND r.deployment_id = d.id AND r.state = 'active') AS active_version,
                       COALESCE(inv.invoke_count, 0)::int AS invoke_count,
                       inv.last_invoked_at AS last_invoked_at
                FROM deployments d
                LEFT JOIN (
                    SELECT deployment_id,
                           count(*) AS invoke_count,
                           max(finished_at) AS last_invoked_at
                    FROM deployment_invocations
                    WHERE finished_at IS NOT NULL
                    GROUP BY deployment_id
                ) inv ON inv.deployment_id = d.id
                WHERE d.id = :id AND d.deleted_at IS NULL
                """
            ),
            {"id": dep_id},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def get_by_slug(
        self, tenant_id: uuid.UUID, slug: str,
    ) -> Optional[dict]:
        """Tenant-bound slug lookup — session MUST already have
        ``app.tenant_id`` set to ``tenant_id``. The explicit kwarg is
        defence-in-depth (the WHERE also pins ``tenant_id``)."""
        row = (await self.session.execute(
            text(
                "SELECT * FROM deployments "
                "WHERE tenant_id = :t AND slug = :s "
                "AND deleted_at IS NULL"
            ),
            {"t": tenant_id, "s": slug},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def get_by_slug_admin(self, slug: str) -> Optional[dict]:
        """Admin-role slug lookup — no tenant scope filter. ONLY
        callable from ``resolve_deployment_and_bind_tenant`` /
        ``session_scope_admin``. Used by the webhook flow
        where slug is the only identifier the external caller carries.
        Migration 088 makes every active public slug globally unique because
        webhook URLs carry no tenant identifier. Soft-deleted rows do not
        reserve a slug."""
        row = (await self.session.execute(
            text(
                "SELECT * FROM deployments "
                "WHERE slug = :s AND deleted_at IS NULL"
            ),
            {"s": slug},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def get_by_api_key(self, api_key: str) -> Optional[dict]:
        """Admin-role api_key lookup — no tenant scope filter. ONLY
        callable from ``resolve_deployment_and_bind_tenant`` /
        ``session_scope_admin``. The api_key is hashed (SHA-256) at
        rest; this method does the hashing so callers never touch the
        hash form. Partial UNIQUE on api_key_hash WHERE deleted_at IS
        NULL guarantees at most one live row per key (migration 005)."""
        h = hashlib.sha256(api_key.encode()).hexdigest()
        row = (await self.session.execute(
            text(
                "SELECT * FROM deployments "
                "WHERE api_key_hash = :h AND deleted_at IS NULL"
            ),
            {"h": h},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def list_for_tenant(
        self,
        *,
        deployment_ids: tuple[str, ...] | list[str] | None = None,
        creator_user_id: str | None = None,
        exclude_creator: bool = False,
        trigger_type: Optional[str] = None,
        enabled: Optional[bool] = None,
        wf_id: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List deployments visible under the current tenant
        (RLS-scoped). Soft-deleted rows are filtered. Newest-first by
        ``created_at``."""
        clauses = ["deleted_at IS NULL"]
        params: dict = {"limit": limit, "offset": offset}
        if deployment_ids is not None:
            normalized_ids = []
            for value in dict.fromkeys(str(item) for item in deployment_ids):
                try:
                    normalized_ids.append(uuid.UUID(value))
                except ValueError:
                    continue
            if not normalized_ids:
                return []
            clauses.append("id = ANY(CAST(:deployment_ids AS uuid[]))")
            params["deployment_ids"] = [
                str(value) for value in normalized_ids
            ]
        if creator_user_id is not None:
            clauses.append("d.user_id != :creator" if exclude_creator else "d.user_id = :creator")
            params["creator"] = uuid.UUID(str(creator_user_id))
        if trigger_type is not None:
            clauses.append("trigger_type = :tt")
            params["tt"] = trigger_type
        if enabled is not None:
            clauses.append("enabled = :en")
            params["en"] = enabled
        if wf_id is not None:
            clauses.append("wf_id = :wf")
            params["wf"] = wf_id
        normalized_query = (query or "").strip()
        if normalized_query:
            clauses.append(
                "(d.name ILIKE :query OR d.slug ILIKE :query "
                "OR d.wf_id ILIKE :query OR CAST(d.id AS text) ILIKE :query)"
            )
            params["query"] = f"%{normalized_query}%"
        where_sql = " AND ".join(f"d.{clause}" if clause in {"deleted_at IS NULL"} else clause for clause in clauses)
        rows = (await self.session.execute(
            text(
                """
                SELECT d.*,
                       (SELECT 'v' || (r.spec->>'pinned_major') || '.sv' || (r.spec->>'pinned_sub')
                        FROM deployment_runtime_revisions r WHERE r.id = d.active_revision_id
                        AND r.deployment_id = d.id AND r.state = 'active') AS active_version,
                       COALESCE(inv.invoke_count, 0)::int AS invoke_count,
                       inv.last_invoked_at AS last_invoked_at
                FROM deployments d
                LEFT JOIN (
                    SELECT deployment_id,
                           count(*) AS invoke_count,
                           max(finished_at) AS last_invoked_at
                    FROM deployment_invocations
                    WHERE finished_at IS NOT NULL
                    GROUP BY deployment_id
                ) inv ON inv.deployment_id = d.id
                WHERE """
                + where_sql
                + """
                ORDER BY d.created_at DESC, d.id
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )).mappings().all()
        return [dict(r) for r in rows]

    async def count_for_tenant(
        self,
        *,
        deployment_ids: tuple[str, ...] | list[str] | None = None,
        creator_user_id: str | None = None,
        exclude_creator: bool = False,
        trigger_type: Optional[str] = None,
        enabled: Optional[bool] = None,
        wf_id: Optional[str] = None,
        query: Optional[str] = None,
    ) -> int:
        """Count the same authorized/filterable set used by list_for_tenant."""
        clauses = ["d.deleted_at IS NULL"]
        params: dict = {}
        if deployment_ids is not None:
            normalized_ids = []
            for value in dict.fromkeys(str(item) for item in deployment_ids):
                try:
                    normalized_ids.append(uuid.UUID(value))
                except ValueError:
                    continue
            if not normalized_ids:
                return 0
            clauses.append("d.id = ANY(CAST(:deployment_ids AS uuid[]))")
            params["deployment_ids"] = [str(value) for value in normalized_ids]
        if creator_user_id is not None:
            clauses.append("d.user_id != :creator" if exclude_creator else "d.user_id = :creator")
            params["creator"] = uuid.UUID(str(creator_user_id))
        if trigger_type is not None:
            clauses.append("d.trigger_type = :tt")
            params["tt"] = trigger_type
        if enabled is not None:
            clauses.append("d.enabled = :en")
            params["en"] = enabled
        if wf_id is not None:
            clauses.append("d.wf_id = :wf")
            params["wf"] = wf_id
        normalized_query = (query or "").strip()
        if normalized_query:
            clauses.append(
                "(d.name ILIKE :query OR d.slug ILIKE :query "
                "OR d.wf_id ILIKE :query OR CAST(d.id AS text) ILIKE :query)"
            )
            params["query"] = f"%{normalized_query}%"
        total = (
            await self.session.execute(
                text(
                    "SELECT count(*) FROM deployments d WHERE "
                    + " AND ".join(clauses)
                ),
                params,
            )
        ).scalar_one()
        return int(total)

    async def summary_for_tenant(
        self,
        *,
        deployment_ids: tuple[str, ...] | list[str] | None = None,
        creator_user_id: str | None = None,
        exclude_creator: bool = False,
        wf_id: str | None = None,
    ) -> dict:
        """Return list-page totals without loading every deployment row."""
        params: dict = {}
        id_clause = ""
        if deployment_ids is not None:
            normalized_ids = []
            for value in dict.fromkeys(str(item) for item in deployment_ids):
                try:
                    normalized_ids.append(uuid.UUID(value))
                except ValueError:
                    continue
            if not normalized_ids:
                return {
                    "active": 0,
                    "disabled": 0,
                    "invocations": 0,
                    "last_invoked_at": None,
                }
            id_clause = " AND d.id = ANY(CAST(:deployment_ids AS uuid[]))"
            params["deployment_ids"] = [str(value) for value in normalized_ids]
        if creator_user_id is not None:
            id_clause += " AND d.user_id != :creator" if exclude_creator else " AND d.user_id = :creator"
            params["creator"] = uuid.UUID(str(creator_user_id))
        if wf_id:
            id_clause += " AND d.wf_id = :wf_id"
            params["wf_id"] = wf_id
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT
                        count(*) FILTER (WHERE d.enabled)::int AS active,
                        count(*) FILTER (WHERE NOT d.enabled)::int AS disabled,
                        COALESCE(sum(inv.invoke_count), 0)::bigint AS invocations,
                        max(inv.last_invoked_at) AS last_invoked_at
                    FROM deployments d
                    LEFT JOIN (
                        SELECT deployment_id,
                               count(*) AS invoke_count,
                               max(finished_at) AS last_invoked_at
                        FROM deployment_invocations
                        WHERE finished_at IS NOT NULL
                        GROUP BY deployment_id
                    ) inv ON inv.deployment_id = d.id
                    WHERE d.deleted_at IS NULL
                    """
                    + id_clause
                ),
                params,
            )
        ).mappings().one()
        return {
            "active": int(row["active"] or 0),
            "disabled": int(row["disabled"] or 0),
            "invocations": int(row["invocations"] or 0),
            "last_invoked_at": row["last_invoked_at"],
        }

    async def insert(self, **fields) -> uuid.UUID:
        """Generic INSERT with caller-supplied columns. Defaults an
        ``id`` UUID if absent. Returns the new id."""
        fields.setdefault("id", uuid.uuid4())
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(f":{k}" for k in fields.keys())
        await self.session.execute(
            text(f"INSERT INTO deployments ({cols}) VALUES ({placeholders})"),
            fields,
        )
        return fields["id"]

    async def update(self, dep_id: uuid.UUID, **fields) -> None:
        """Patch named columns. Always bumps ``updated_at``. Ignored on
        soft-deleted rows."""
        if not fields:
            return
        sets = ", ".join(f"{k} = :{k}" for k in fields.keys())
        params = {"id": dep_id, **fields}
        await self.session.execute(
            text(
                f"UPDATE deployments SET {sets}, updated_at = now() "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            params,
        )

    async def soft_delete(self, dep_id: uuid.UUID) -> None:
        """Soft delete: mark ``deleted_at`` and disable. Idempotent —
        already-deleted rows are a no-op (the WHERE filters them)."""
        await self.session.execute(
            text(
                "UPDATE deployments "
                "SET deleted_at = now(), enabled = FALSE, "
                "    updated_at = now() "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": dep_id},
        )
