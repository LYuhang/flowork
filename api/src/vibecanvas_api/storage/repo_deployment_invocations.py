"""Deployment invocation history and metrics."""
from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service


_OPERATIONAL_ERROR_RE = re.compile(r"^[a-z0-9_.:-]{1,128}$")


def _operational_error(value: str | None) -> str | None:
    """Keep only stable machine codes in plaintext invocation metadata."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if _OPERATIONAL_ERROR_RE.fullmatch(normalized):
        return normalized
    return "execution_failed"


def _encode_cursor(submitted_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"submitted_at": submitted_at.isoformat(), "id": str(row_id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        data = json.loads(raw)
        submitted_at = data.get("submitted_at")
        row_id = data.get("id")
        if not isinstance(submitted_at, str) or not isinstance(row_id, str):
            return None
        return datetime.fromisoformat(submitted_at), uuid.UUID(row_id)
    except Exception:
        return None


def _jsonable(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


class DeploymentInvocationsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        invocation_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID,
        deployment_id: uuid.UUID,
        wf_id: str,
        trigger_type: str,
        source: str,
        status: str,
        inputs: dict | None = None,
    ) -> uuid.UUID:
        invocation_id = invocation_id or uuid.uuid4()
        private = None
        if inputs is not None:
            private = await content_encryption_service().encrypt_json(
                self.session,
                tenant_id=tenant_id,
                resource_type="deployment_invocation",
                resource_id=str(invocation_id),
                purpose="deployment_invocation_private",
                record_id=str(invocation_id),
                value={"inputs": inputs},
            )
        await self.session.execute(
            text(
                """
                INSERT INTO deployment_invocations (
                    id, tenant_id, deployment_id, wf_id, trigger_type, source,
                    status, started_at, private_ciphertext, private_nonce,
                    private_key_id
                )
                VALUES (
                    :id, :tenant_id, :deployment_id, :wf_id, :trigger_type,
                    :source, :status,
                    CASE WHEN :status = 'running' THEN now() ELSE NULL END,
                    :private_ciphertext, :private_nonce, :private_key_id
                )
                """
            ),
            {
                "id": invocation_id,
                "tenant_id": tenant_id,
                "deployment_id": deployment_id,
                "wf_id": wf_id,
                "trigger_type": trigger_type,
                "source": source,
                "status": status,
                "private_ciphertext": private.ciphertext if private else None,
                "private_nonce": private.nonce if private else None,
                "private_key_id": private.key_id if private else None,
            },
        )
        return invocation_id

    async def load_worker_payload(self, invocation_id: uuid.UUID) -> dict | None:
        """Load one queued invocation and decrypt its executor-only inputs.

        This method is intended for an admin worker session. DBOS receives only
        the opaque invocation id; user input remains in Flowork's encrypted
        business storage rather than DBOS workflow arguments.
        """
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, deployment_id, private_ciphertext,
                           private_nonce, private_key_id
                    FROM deployment_invocations
                    WHERE id = :id
                    """
                ),
                {"id": invocation_id},
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        if not all(
            (
                row["private_ciphertext"],
                row["private_nonce"],
                row["private_key_id"],
            )
        ):
            raise ValueError("deployment invocation input ciphertext is missing")
        private = await content_encryption_service().decrypt_json(
            self.session,
            key_id=row["private_key_id"],
            tenant_id=row["tenant_id"],
            resource_type="deployment_invocation",
            resource_id=str(row["id"]),
            purpose="deployment_invocation_private",
            record_id=str(row["id"]),
            ciphertext=row["private_ciphertext"],
            nonce=row["private_nonce"],
        )
        if not isinstance(private, dict) or not isinstance(
            private.get("inputs"), dict
        ):
            raise ValueError("deployment invocation ciphertext is invalid")
        return {
            "task_id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "deployment_id": str(row["deployment_id"]),
            "inputs": private["inputs"],
        }

    async def mark_running(self, invocation_id: uuid.UUID) -> None:
        await self.session.execute(
            text(
                """
                UPDATE deployment_invocations
                SET status = 'running', started_at = COALESCE(started_at, now())
                WHERE id = :id
                """
            ),
            {"id": invocation_id},
        )

    async def mark_terminal(
        self,
        invocation_id: uuid.UUID,
        *,
        status: str,
        latency_ms: float | None,
        error: str | None = None,
        result_summary: dict | None = None,
    ) -> None:
        await self.session.execute(
            text(
                """
                UPDATE deployment_invocations
                SET status = :status,
                    finished_at = now(),
                    latency_ms = :latency_ms,
                    error = :error,
                    result_summary = CAST(:result_summary AS jsonb)
                WHERE id = :id
                """
            ),
            {
                "id": invocation_id,
                "status": status,
                "latency_ms": latency_ms,
                "error": _operational_error(error),
                "result_summary": json.dumps(result_summary or {}),
            },
        )

    async def history(
        self,
        *,
        deployment_id: uuid.UUID,
        limit: int,
        cursor: str | None = None,
        statuses: Iterable[str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
        descending: bool = True,
    ) -> dict:
        cursor_parts = _decode_cursor(cursor)
        clauses = ["deployment_id = :deployment_id"]
        params: dict = {"deployment_id": deployment_id, "limit": limit + 1}
        if cursor_parts:
            operator = "<" if descending else ">"
            clauses.append(f"(submitted_at, id) {operator} (:cursor_submitted_at, :cursor_id)")
            params["cursor_submitted_at"] = cursor_parts[0]
            params["cursor_id"] = cursor_parts[1]
        statuses_list = [s for s in (statuses or []) if s]
        if statuses_list:
            clauses.append("status = ANY(:statuses)")
            params["statuses"] = statuses_list
        if from_ is not None:
            clauses.append("submitted_at >= :from_")
            params["from_"] = from_
        if to is not None:
            clauses.append("submitted_at <= :to")
            params["to"] = to
        direction = "DESC" if descending else "ASC"
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, status, source, trigger_type, submitted_at,
                           started_at, finished_at, latency_ms, error
                    FROM deployment_invocations
                    WHERE """
                    + " AND ".join(clauses)
                    + """
                    ORDER BY submitted_at """
                    + direction
                    + ", id "
                    + direction
                    + """
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
        page_rows = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page_rows:
            last = page_rows[-1]
            next_cursor = _encode_cursor(last["submitted_at"], last["id"])
        items = []
        for row in page_rows:
            item = {k: _jsonable(v) for k, v in dict(row).items()}
            item["task_type"] = "deployment_invoke"
            items.append(item)
        return {"items": items, "next_cursor": next_cursor, "limit": limit}

    async def metrics(
        self,
        *,
        deployment_id: uuid.UUID,
        from_: datetime,
        to: datetime,
        bucket: str,
    ) -> list[dict]:
        trunc = "hour" if bucket == "hour" else "day"
        rows = (
            await self.session.execute(
                text(
                    f"""
                    SELECT date_trunc('{trunc}', finished_at) AS ts,
                           count(*)::int AS calls,
                           count(*) FILTER (WHERE status = 'failed')::int AS errors,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                               FILTER (WHERE latency_ms IS NOT NULL) AS latency_p50,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                               FILTER (WHERE latency_ms IS NOT NULL) AS latency_p95
                    FROM deployment_invocations
                    WHERE deployment_id = :deployment_id
                      AND finished_at >= :from_
                      AND finished_at <= :to
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {"deployment_id": deployment_id, "from_": from_, "to": to},
            )
        ).mappings().all()
        return [
            {
                "ts": row["ts"].isoformat(),
                "calls": row["calls"],
                "errors": row["errors"],
                "latency_p50": row["latency_p50"],
                "latency_p95": row["latency_p95"],
            }
            for row in rows
        ]
