"""CRUD for ``mcp_servers`` rows. MCP T2.

All methods assume the session is already tenant-bound through the
``session_scope(tenant_id=...)`` pattern) — they do NOT set the
``app.tenant_id`` GUC themselves. FORCE RLS + the ``deleted_at IS NULL``
clause on every SELECT make cross-tenant / soft-deleted rows invisible.

The public "delete" path is a soft delete: an UPDATE that sets
``deleted_at = now()`` **and** ``enabled = FALSE``. The latter is
load-bearing — the dispatcher / loader scans for ``enabled = TRUE`` rows
without re-checking ``deleted_at``, so flipping ``enabled`` here makes a
delete take effect immediately for any later handshake / tool-load pass.

Returns: ``mappings()`` row dicts, NOT ORM ``McpServer`` instances. This
matches ``DeploymentsRepo`` (Deployments T2) so route handlers can
consume the dict shape uniformly; if a later task needs ORM attribute
access it can ``session.get(McpServer, id)`` explicitly.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# JSONB columns on ``mcp_servers``. asyncpg's text-protocol bind path
# (used by ``text()`` queries with named params) does NOT auto-encode
# Python ``dict`` / ``list`` into JSONB — it tries ``.encode()`` on the
# value and raises ``DataError``. We pre-serialize these specific columns
# in ``insert`` / ``update`` so callers can keep passing native Python
# objects (as the plan tests do).
_JSONB_FIELDS = frozenset({"auth_config", "connection_config", "last_tool_names"})


def _encode_jsonb(fields: dict) -> dict:
    """Return a shallow copy of ``fields`` with known JSONB columns
    serialized to JSON strings. Strings are passed through unchanged
    (already-encoded). ``None`` is passed through (NULL)."""
    out = dict(fields)
    for k in _JSONB_FIELDS & fields.keys():
        v = fields[k]
        if v is not None and not isinstance(v, str):
            out[k] = json.dumps(v)
    return out


class McpServersRepo:
    """Async CRUD over ``mcp_servers``.

    Caller owns the transaction: mutating methods do NOT ``commit()``
    (the DI request session commits at request end; background writers
    commit explicitly via ``session_scope``).
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, server_id: uuid.UUID) -> Optional[dict]:
        """Tenant-scoped fetch (session must already have
        ``app.tenant_id`` set, or RLS will hide the row). Filters
        soft-deleted rows."""
        row = (await self.session.execute(
            text(
                "SELECT * FROM mcp_servers "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": server_id},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def get_for_user(
        self,
        server_id: uuid.UUID,
        user_id: str | uuid.UUID,
    ) -> Optional[dict]:
        row = (
            await self.session.execute(
                text(
                    "SELECT * FROM mcp_servers "
                    "WHERE id = :id AND user_id = :user_id "
                    "AND deleted_at IS NULL"
                ),
                {"id": server_id, "user_id": user_id},
            )
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def list_for_tenant(self) -> list[dict]:
        """List every live server visible under the current tenant
        (RLS-scoped). Newest-first by ``created_at``."""
        rows = (await self.session.execute(
            text(
                "SELECT * FROM mcp_servers "
                "WHERE deleted_at IS NULL "
                "ORDER BY created_at DESC"
            ),
        )).mappings().all()
        return [dict(r) for r in rows]

    async def list_for_user(self, user_id: str | uuid.UUID) -> list[dict]:
        rows = (await self.session.execute(
            text(
                "SELECT * FROM mcp_servers "
                "WHERE user_id=:user_id AND deleted_at IS NULL "
                "ORDER BY created_at DESC"
            ),
            {"user_id": user_id},
        )).mappings().all()
        return [dict(row) for row in rows]

    async def list_authorized(
        self,
        authorized_ids: tuple[str, ...] | list[str] | set[str],
    ) -> list[dict]:
        ids = tuple(dict.fromkeys(str(item) for item in authorized_ids if item))
        if not ids:
            return []
        rows = (
            await self.session.execute(
                text(
                    "SELECT * FROM mcp_servers "
                    "WHERE id = ANY(CAST(:ids AS uuid[])) "
                    "AND deleted_at IS NULL ORDER BY created_at DESC"
                ),
                {"ids": list(ids)},
            )
        ).mappings().all()
        return [dict(row) for row in rows]

    async def list_enabled(self) -> list[dict]:
        """List every live AND enabled server — the set the loader /
        agent should connect to. Used at agent-build time."""
        rows = (await self.session.execute(
            text(
                "SELECT * FROM mcp_servers "
                "WHERE deleted_at IS NULL AND enabled = TRUE "
                "  AND connection_status IN ('not_required', 'connected') "
                "ORDER BY created_at DESC"
            ),
        )).mappings().all()
        return [dict(r) for r in rows]

    async def list_enabled_for_user(self, user_id: str | uuid.UUID) -> list[dict]:
        rows = (await self.session.execute(
            text(
                "SELECT * FROM mcp_servers "
                "WHERE user_id=:user_id AND deleted_at IS NULL AND enabled=TRUE "
                "AND connection_status IN ('not_required', 'connected') "
                "ORDER BY created_at DESC"
            ),
            {"user_id": user_id},
        )).mappings().all()
        return [dict(row) for row in rows]

    async def insert(self, **fields) -> uuid.UUID:
        """Generic INSERT with caller-supplied columns. Defaults an
        ``id`` UUID if absent. Returns the new id.

        Native Python ``dict`` / ``list`` values for JSONB columns
        (``auth_config``, ``last_tool_names``) are auto-serialized — see
        the module-level ``_encode_jsonb`` comment."""
        fields.setdefault("id", uuid.uuid4())
        params = _encode_jsonb(fields)
        cols = ", ".join(params.keys())
        placeholders = ", ".join(f":{k}" for k in params.keys())
        await self.session.execute(
            text(f"INSERT INTO mcp_servers ({cols}) VALUES ({placeholders})"),
            params,
        )
        return fields["id"]

    async def update(self, server_id: uuid.UUID, **fields) -> None:
        """Patch named columns. Always bumps ``updated_at``. Ignored on
        soft-deleted rows.

        JSONB columns are auto-serialized (see ``insert``)."""
        if not fields:
            return
        encoded = _encode_jsonb(fields)
        sets = ", ".join(f"{k} = :{k}" for k in encoded.keys())
        params = {"id": server_id, **encoded}
        await self.session.execute(
            text(
                f"UPDATE mcp_servers SET {sets}, updated_at = now() "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            params,
        )

    async def soft_delete(self, server_id: uuid.UUID) -> None:
        """Soft delete: mark ``deleted_at`` and disable. Idempotent —
        already-deleted rows are a no-op (the WHERE filters them).

        Flipping ``enabled = FALSE`` is required so the loader's
        enabled-only scan (``list_enabled``) stops yielding this row
        even before any future ``deleted_at`` filter is added. Project selections
        are removed in the same transaction and their CAS revisions advance so
        no Project continues advertising an uninstalled server."""
        await self.session.execute(
            text(
                "WITH affected AS ("
                "  DELETE FROM project_mcp_bindings "
                "  WHERE mcp_server_id = :id "
                "  RETURNING project_id"
                "), affected_projects AS ("
                "  SELECT DISTINCT project_id FROM affected"
                ") "
                "UPDATE chat_projects AS project "
                "SET mcp_config_revision = project.mcp_config_revision + 1, "
                "    updated_at = now() "
                "FROM affected_projects AS affected_project "
                "WHERE project.project_id = affected_project.project_id"
            ),
            {"id": server_id},
        )
        await self.session.execute(
            text(
                "UPDATE mcp_servers "
                "SET deleted_at = now(), enabled = FALSE, "
                "    updated_at = now() "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": server_id},
        )

    async def update_handshake(
        self, health: dict[uuid.UUID, dict],
    ) -> None:
        """Batch-update ``last_handshake_*`` for the given servers.

        ``health[server_id]`` is a dict ``{status, tool_count,
        tool_names}`` produced by the handshake helper (MCP T3).
        ``tool_count`` / ``tool_names`` are optional — a failed
        handshake records the status but leaves the tool snapshot
        alone (``info.get(...)`` → ``None``)."""
        for sid, info in health.items():
            encoded = _encode_jsonb(
                {"last_tool_names": info.get("tool_names")},
            )
            await self.session.execute(
                text(
                    "UPDATE mcp_servers SET "
                    "  last_handshake_at = now(), "
                    "  last_handshake_status = :status, "
                    "  last_tool_count = :count, "
                    "  last_tool_names = :names, "
                    "  updated_at = now() "
                    "WHERE id = :id AND deleted_at IS NULL"
                ),
                {
                    "id": sid,
                    "status": info["status"],
                    "count": info.get("tool_count"),
                    "names": encoded["last_tool_names"],
                },
            )

    async def list_other_tool_names(
        self, exclude_id: Optional[uuid.UUID] = None,
    ) -> set[str]:
        """Return all live MCP tools' prefixed names (``{prefix}__{name}``)
        from OTHER enabled servers in the current tenant. Used by the
        create / PATCH conflict pre-check so a new server cannot collide
        with names already in flight.

        ``last_tool_names`` is JSONB of the shape
        ``[{"name": ..., "description": ...}, ...]`` — we read each
        entry's ``name`` (with a fallback to the bare-string form so an
        older row schema doesn't break the helper)."""
        params: dict = {}
        sql = (
            "SELECT id, tool_prefix, last_tool_names FROM mcp_servers "
            "WHERE deleted_at IS NULL AND enabled = TRUE"
        )
        if exclude_id is not None:
            sql += " AND id != :ex"
            params["ex"] = exclude_id
        rows = (await self.session.execute(text(sql), params)).mappings().all()
        out: set[str] = set()
        for r in rows:
            prefix = r["tool_prefix"]
            for tn in (r["last_tool_names"] or []):
                name = tn["name"] if isinstance(tn, dict) else tn
                out.add(f"{prefix}__{name}")
        return out


class McpOAuthRepo:
    """Tenant-scoped persistence for OAuth tokens and short-lived PKCE state."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_connection(self, server_id: uuid.UUID) -> Optional[dict]:
        row = (await self.session.execute(
            text("SELECT * FROM mcp_oauth_connections WHERE server_id = :server_id"),
            {"server_id": server_id},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def upsert_connection(self, **fields) -> Optional[dict]:
        previous = (
            await self.session.execute(
                text(
                    "SELECT tenant_id, secret_ref, secret_version "
                    "FROM mcp_oauth_connections WHERE server_id = :server_id"
                ),
                {"server_id": fields["server_id"]},
            )
        ).mappings().one_or_none()
        columns = ", ".join(fields)
        values = ", ".join(f":{name}" for name in fields)
        updates = ", ".join(
            f"{name} = EXCLUDED.{name}"
            for name in fields
            if name not in {"id", "tenant_id", "user_id", "server_id", "created_at"}
        )
        await self.session.execute(
            text(
                f"INSERT INTO mcp_oauth_connections ({columns}) VALUES ({values}) "
                f"ON CONFLICT (server_id) DO UPDATE SET {updates}, updated_at = now()"
            ),
            fields,
        )
        return dict(previous) if previous else None

    async def delete_connection(self, server_id: uuid.UUID) -> Optional[dict]:
        row = (
            await self.session.execute(
                text(
                    "DELETE FROM mcp_oauth_connections "
                    "WHERE server_id = :server_id "
                    "RETURNING tenant_id, secret_ref, secret_version"
                ),
                {"server_id": server_id},
            )
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def create_transaction(self, **fields) -> list[dict]:
        old_rows = (
            await self.session.execute(
                text(
                    "DELETE FROM mcp_oauth_transactions "
                    "WHERE server_id = :server_id "
                    "RETURNING tenant_id, secret_ref, secret_version"
                ),
                {"server_id": fields["server_id"]},
            )
        ).mappings().all()
        columns = ", ".join(fields)
        values = ", ".join(f":{name}" for name in fields)
        await self.session.execute(
            text(f"INSERT INTO mcp_oauth_transactions ({columns}) VALUES ({values})"),
            fields,
        )
        return [dict(row) for row in old_rows]

    async def get_transaction(self, state_hash: str) -> Optional[dict]:
        row = (await self.session.execute(
            text(
                "SELECT * FROM mcp_oauth_transactions "
                "WHERE state_hash = :state_hash AND expires_at > now()"
            ),
            {"state_hash": state_hash},
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def delete_transaction(self, state_hash: str) -> Optional[dict]:
        row = (
            await self.session.execute(
                text(
                    "DELETE FROM mcp_oauth_transactions "
                    "WHERE state_hash = :state_hash "
                    "RETURNING tenant_id, secret_ref, secret_version"
                ),
                {"state_hash": state_hash},
            )
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def delete_transactions_for_server(
        self, server_id: uuid.UUID,
    ) -> list[dict]:
        rows = (
            await self.session.execute(
                text(
                    "DELETE FROM mcp_oauth_transactions "
                    "WHERE server_id = :server_id "
                    "RETURNING tenant_id, secret_ref, secret_version"
                ),
                {"server_id": server_id},
            )
        ).mappings().all()
        return [dict(row) for row in rows]
