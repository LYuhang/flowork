"""Repository and state machine for durable Chat tool jobs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import case, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service

from .models import Chat
from .models_background_jobs import (
    ACTIVE_BACKGROUND_JOB_STATUSES,
    TERMINAL_BACKGROUND_JOB_STATUSES,
    ChatToolJob,
    ChatToolJobEvent,
)


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def project_background_job(row: ChatToolJob) -> dict:
    """Return the stable frontend/model projection; omit executor-private data."""

    delivery = row.delivery
    result = _sanitize_public_output(dict(row.result_snapshot or {}))
    output = _output_envelope(
        status=row.status,
        result=result,
        result_ref=row.result_ref,
        error=dict(row.error_json or {}),
    )
    return {
        "job_id": row.job_id,
        "chat_id": row.chat_id,
        "parent_run_id": row.parent_run_id,
        "runtime_type": row.runtime_type,
        "executor_type": row.executor_type,
        "tool_name": row.tool_name,
        "title": row.title,
        "status": row.status,
        "progress": {
            "current": row.progress_current,
            "total": row.progress_total,
            "message": row.progress_message,
        },
        "input": dict(row.input_snapshot or {}),
        "output": output,
        "result": result,
        "result_ref": row.result_ref,
        "error": dict(row.error_json or {}),
        "event_seq": row.event_seq,
        "cancel_requested": row.cancel_requested_at is not None,
        "delivery_status": "delivered" if delivery is not None else "pending",
        "delivered_at": (
            delivery.delivered_at.isoformat()
            if delivery is not None and delivery.delivered_at
            else None
        ),
        "delivery_batch_id": (
            delivery.delivery_batch_id if delivery is not None else None
        ),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


_PRIVATE_OUTPUT_KEYS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "password", "proxy",
    "secret", "token", "access_token", "refresh_token",
})


def _sanitize_public_output(value, *, depth: int = 0):
    if depth > 8:
        return "[nested output omitted]"
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]" if str(key).lower() in _PRIVATE_OUTPUT_KEYS
                else _sanitize_public_output(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, list):
        return [_sanitize_public_output(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return value[:100_000]
    return value


def _output_envelope(*, status: str, result: dict, result_ref: str | None, error: dict) -> dict:
    terminal = status in TERMINAL_BACKGROUND_JOB_STATUSES
    state = "final" if status == "completed" else ("partial" if result else "none")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    truncated = len(encoded.encode("utf-8")) > 12_000
    summary = ""
    for value in result.values():
        if isinstance(value, str) and value.strip():
            summary = value.strip()[:1000]
            break
    if not summary and error:
        summary = str(error.get("message") or error.get("code") or "")[:1000]
    return {
        "state": state if terminal or result else "none",
        "inline": None if truncated else result,
        "summary": summary,
        "ref": result_ref,
        "truncated": truncated,
    }


class BackgroundJobStateError(RuntimeError):
    pass


class BackgroundJobsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _store_job_private(self, row: ChatToolJob) -> None:
        encrypted = await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=row.tenant_id,
            resource_type="chat",
            resource_id=row.chat_id,
            purpose="background_job_private",
            record_id=row.job_id,
            value={
                "title": row.title,
                "progress_message": row.progress_message,
                "input_snapshot": row.input_snapshot,
                "result_snapshot": row.result_snapshot,
                "result_ref": row.result_ref,
                "error_json": row.error_json,
                "execution_handle_json": row.execution_handle_json,
            },
        )
        row.private_ciphertext = encrypted.ciphertext
        row.private_nonce = encrypted.nonce
        row.private_key_id = encrypted.key_id

    async def _materialize_job(
        self,
        row: ChatToolJob | None,
    ) -> ChatToolJob | None:
        if row is None:
            return None
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=row.private_key_id,
            tenant_id=row.tenant_id,
            resource_type="chat",
            resource_id=row.chat_id,
            purpose="background_job_private",
            record_id=row.job_id,
            ciphertext=row.private_ciphertext,
            nonce=row.private_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("background job ciphertext must contain an object")
        row.title = value.get("title") or ""
        row.progress_message = value.get("progress_message") or ""
        row.input_snapshot = value.get("input_snapshot") or {}
        row.result_snapshot = value.get("result_snapshot") or {}
        row.result_ref = value.get("result_ref")
        row.error_json = value.get("error_json") or {}
        row.execution_handle_json = value.get("execution_handle_json") or {}
        return row

    async def _new_event(
        self,
        *,
        row: ChatToolJob,
        event_type: str,
        payload: dict,
    ) -> ChatToolJobEvent:
        # A Chat stream combines multiple Jobs. Serialize event ID allocation
        # across those jobs so a later commit cannot be skipped by its cursor.
        await self.session.execute(text(
            "SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"
        ), {"scope": f"background-events:{row.tenant_id}:{row.chat_id}"})
        encrypted = await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=row.tenant_id,
            resource_type="chat",
            resource_id=row.chat_id,
            purpose="background_job_event",
            record_id=f"{row.job_id}:{row.event_seq}",
            value=dict(payload or {}),
        )
        event = ChatToolJobEvent(
            job_id=row.job_id,
            seq=row.event_seq,
            tenant_id=row.tenant_id,
            event_type=event_type,
            payload_ciphertext=encrypted.ciphertext,
            payload_nonce=encrypted.nonce,
            payload_key_id=encrypted.key_id,
        )
        event.payload = payload
        return event

    async def _materialize_event(
        self,
        event: ChatToolJobEvent,
        *,
        chat_id: str | None = None,
    ) -> ChatToolJobEvent:
        if chat_id is None:
            chat_id = (
                await self.session.execute(
                    select(ChatToolJob.chat_id).where(
                        ChatToolJob.job_id == event.job_id
                    )
                )
            ).scalar_one()
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=event.payload_key_id,
            tenant_id=event.tenant_id,
            resource_type="chat",
            resource_id=chat_id,
            purpose="background_job_event",
            record_id=f"{event.job_id}:{event.seq}",
            ciphertext=event.payload_ciphertext,
            nonce=event.payload_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("background job event ciphertext must contain an object")
        event.payload = value
        return event

    async def create_idempotent(
        self,
        *,
        job_id: str,
        tenant_id: str | uuid.UUID,
        chat_id: str,
        creator_user_id: str | uuid.UUID,
        parent_run_id: str | None,
        runtime_type: str,
        executor_type: str,
        tool_name: str,
        title: str,
        input_snapshot: dict,
        idempotency_key: str,
    ) -> tuple[ChatToolJob, bool]:
        candidate = ChatToolJob(
            job_id=job_id,
            tenant_id=_uuid(tenant_id),
            chat_id=chat_id,
            creator_user_id=_uuid(creator_user_id),
            parent_run_id=parent_run_id,
            runtime_type=runtime_type,
            executor_type=executor_type,
            tool_name=tool_name,
            status="queued",
            idempotency_key=idempotency_key,
        )
        candidate.title = title
        candidate.progress_message = ""
        candidate.input_snapshot = input_snapshot
        candidate.result_snapshot = {}
        candidate.result_ref = None
        candidate.error_json = {}
        candidate.execution_handle_json = {}
        await self._store_job_private(candidate)
        values = {
            "job_id": job_id,
            "tenant_id": _uuid(tenant_id),
            "chat_id": chat_id,
            "creator_user_id": _uuid(creator_user_id),
            "parent_run_id": parent_run_id,
            "runtime_type": runtime_type,
            "executor_type": executor_type,
            "tool_name": tool_name,
            "status": "queued",
            "private_ciphertext": candidate.private_ciphertext,
            "private_nonce": candidate.private_nonce,
            "private_key_id": candidate.private_key_id,
            "idempotency_key": idempotency_key,
        }
        inserted = (
            await self.session.execute(
                pg_insert(ChatToolJob)
                .values(**values)
                .on_conflict_do_nothing(
                    constraint="uq_chat_tool_jobs_chat_idempotency"
                )
                .returning(ChatToolJob.job_id)
            )
        ).scalar_one_or_none()
        created = inserted is not None
        row = (
            await self.session.execute(
                select(ChatToolJob).where(
                    ChatToolJob.chat_id == chat_id,
                    ChatToolJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
        await self._materialize_job(row)
        if created:
            await self._append_event_locked(
                row,
                "queued",
                {"status": "queued", "title": title},
            )
        return row, created

    async def get_for_user(
        self,
        *,
        chat_id: str,
        job_id: str,
        creator_user_id: str | uuid.UUID,
        for_update: bool = False,
    ) -> ChatToolJob | None:
        query = (
            select(ChatToolJob)
            .join(Chat, Chat.chat_id == ChatToolJob.chat_id)
            .where(
                ChatToolJob.chat_id == chat_id,
                ChatToolJob.job_id == job_id,
                Chat.creator_user_id == _uuid(creator_user_id),
                Chat.deleted_at.is_(None),
            )
        )
        if for_update:
            query = query.with_for_update()
        return await self._materialize_job(
            (await self.session.execute(query)).scalar_one_or_none()
        )

    async def get(self, job_id: str, *, for_update: bool = False) -> ChatToolJob | None:
        query = select(ChatToolJob).where(ChatToolJob.job_id == job_id)
        if for_update:
            query = query.with_for_update()
        return await self._materialize_job(
            (await self.session.execute(query)).scalar_one_or_none()
        )

    async def list_for_user(
        self,
        *,
        chat_id: str,
        creator_user_id: str | uuid.UUID,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[ChatToolJob]:
        query = (
            select(ChatToolJob)
            .join(Chat, Chat.chat_id == ChatToolJob.chat_id)
            .where(
                ChatToolJob.chat_id == chat_id,
                Chat.creator_user_id == _uuid(creator_user_id),
                Chat.deleted_at.is_(None),
            )
        )
        normalized = tuple(str(status) for status in (statuses or ()) if status)
        if normalized:
            query = query.where(ChatToolJob.status.in_(normalized))
        query = query.order_by(
            case(
                (
                    ChatToolJob.status.in_(ACTIVE_BACKGROUND_JOB_STATUSES),
                    0,
                ),
                else_=1,
            ),
            ChatToolJob.finished_at.desc().nulls_first(),
            ChatToolJob.created_at.desc(),
        ).limit(
            max(1, min(int(limit), 200))
        )
        rows = list((await self.session.execute(query)).scalars().all())
        for row in rows:
            await self._materialize_job(row)
        return rows

    async def list_chat_events_for_user(
        self,
        *,
        chat_id: str,
        creator_user_id: str | uuid.UUID,
        after_event_id: int = 0,
        limit: int = 500,
    ) -> list[ChatToolJobEvent]:
        query = (
            select(ChatToolJobEvent)
            .join(
                ChatToolJob,
                ChatToolJob.job_id == ChatToolJobEvent.job_id,
            )
            .join(Chat, Chat.chat_id == ChatToolJob.chat_id)
            .where(
                ChatToolJob.chat_id == chat_id,
                Chat.creator_user_id == _uuid(creator_user_id),
                Chat.deleted_at.is_(None),
                ChatToolJobEvent.event_id > max(0, int(after_event_id)),
            )
            .order_by(ChatToolJobEvent.event_id)
            .limit(max(1, min(int(limit), 1000)))
        )
        events = list((await self.session.execute(query)).scalars().all())
        for event in events:
            await self._materialize_event(event, chat_id=chat_id)
        return events

    async def list_events(
        self,
        *,
        job_id: str,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[ChatToolJobEvent]:
        events = list(
            (
                await self.session.execute(
                    select(ChatToolJobEvent)
                    .where(
                        ChatToolJobEvent.job_id == job_id,
                        ChatToolJobEvent.seq > max(0, int(after_seq)),
                    )
                    .order_by(ChatToolJobEvent.seq)
                    .limit(max(1, min(int(limit), 1000)))
                )
            )
            .scalars()
            .all()
        )
        chat_id = (
            await self.session.execute(
                select(ChatToolJob.chat_id).where(ChatToolJob.job_id == job_id)
            )
        ).scalar_one_or_none()
        if chat_id is None and events:
            raise LookupError(f"background job {job_id} not found")
        for event in events:
            await self._materialize_event(event, chat_id=chat_id)
        return events

    async def _append_event_locked(
        self,
        row: ChatToolJob,
        event_type: str,
        payload: dict,
    ) -> ChatToolJobEvent:
        row.event_seq = int(row.event_seq or 0) + 1
        row.updated_at = _now()
        event = await self._new_event(
            row=row,
            event_type=event_type,
            payload=payload,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def append_event(
        self,
        job_id: str,
        event_type: str,
        payload: dict,
    ) -> ChatToolJobEvent:
        row = await self.get(job_id, for_update=True)
        if row is None:
            raise LookupError(f"background job {job_id} not found")
        return await self._append_event_locked(row, event_type, payload)

    async def claim(
        self,
        *,
        job_id: str,
        owner: str,
        lease_seconds: int = 60,
        execution_handle: dict | None = None,
    ) -> ChatToolJob | None:
        row = await self.get(job_id, for_update=True)
        if row is None or row.status != "queued":
            return None
        now = _now()
        row.status = "running"
        row.started_at = row.started_at or now
        row.lease_owner = owner
        row.lease_expires_at = now + timedelta(seconds=max(5, lease_seconds))
        row.execution_handle_json = dict(execution_handle or {})
        await self._store_job_private(row)
        await self._append_event_locked(
            row,
            "started",
            {"status": "running"},
        )
        return row

    async def heartbeat(
        self,
        *,
        job_id: str,
        owner: str,
        lease_seconds: int = 60,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
    ) -> ChatToolJob:
        row = await self.get(job_id, for_update=True)
        if row is None:
            raise LookupError(f"background job {job_id} not found")
        if row.status not in {"running", "cancelling"}:
            raise BackgroundJobStateError(
                f"cannot heartbeat job in status {row.status}"
            )
        if row.lease_owner not in {None, owner}:
            raise BackgroundJobStateError("background job lease is owned elsewhere")
        row.lease_owner = owner
        row.lease_expires_at = _now() + timedelta(
            seconds=max(5, lease_seconds)
        )
        if current is not None:
            row.progress_current = max(0, int(current))
        if total is not None:
            row.progress_total = max(row.progress_current, int(total))
        if message is not None:
            row.progress_message = str(message)
            await self._store_job_private(row)
        await self._append_event_locked(
            row,
            "progress",
            {
                "status": row.status,
                "current": row.progress_current,
                "total": row.progress_total,
                "message": row.progress_message,
            },
        )
        return row

    async def complete(
        self,
        *,
        job_id: str,
        result: dict,
        result_ref: str | None = None,
    ) -> ChatToolJob:
        row = await self.get(job_id, for_update=True)
        if row is None:
            raise LookupError(f"background job {job_id} not found")
        if row.status in TERMINAL_BACKGROUND_JOB_STATUSES:
            return row
        if row.status == "cancelling":
            return await self._cancel_locked(row, reason="cancelled_before_result")
        row.status = "completed"
        row.result_snapshot = dict(result or {})
        row.result_ref = result_ref
        row.error_json = {}
        await self._store_job_private(row)
        row.finished_at = _now()
        row.lease_owner = None
        row.lease_expires_at = None
        await self._append_event_locked(
            row,
            "completed",
            {
                "status": "completed",
                "result": row.result_snapshot,
                "result_ref": result_ref,
            },
        )
        return row

    async def fail(self, *, job_id: str, error: dict) -> ChatToolJob:
        row = await self.get(job_id, for_update=True)
        if row is None:
            raise LookupError(f"background job {job_id} not found")
        if row.status in TERMINAL_BACKGROUND_JOB_STATUSES:
            return row
        if row.status == "cancelling":
            return await self._cancel_locked(row, reason="cancelled_during_failure")
        return await self._fail_locked(row, error=error)

    async def _fail_locked(
        self,
        row: ChatToolJob,
        *,
        error: dict,
    ) -> ChatToolJob:
        row.status = "failed"
        row.error_json = dict(error or {})
        await self._store_job_private(row)
        row.finished_at = _now()
        row.lease_owner = None
        row.lease_expires_at = None
        await self._append_event_locked(
            row,
            "failed",
            {"status": "failed", "error": row.error_json},
        )
        return row

    async def reconcile_stale_for_chat(
        self,
        *,
        chat_id: str,
        queued_grace_seconds: int = 120,
    ) -> list[ChatToolJob]:
        """Move abandoned jobs to explicit terminal states.

        A background executor may disappear after the browser disconnects, an
        API worker is restarted, or its sandbox process exits unexpectedly.
        Replaying a subagent automatically is unsafe because it may already
        have changed files before the control connection was lost.  We
        therefore preserve the uncertainty as durable job state instead of
        either leaving ``running`` forever or risking duplicate side effects.

        This method is tenant-scoped by its session and uses ``SKIP LOCKED`` so
        multiple API workers may reconcile the same Chat concurrently.
        """

        now = _now()
        queued_before = now - timedelta(
            seconds=max(30, int(queued_grace_seconds))
        )
        rows = list(
            (
                await self.session.execute(
                    select(ChatToolJob)
                    .where(
                        ChatToolJob.chat_id == chat_id,
                        or_(
                            (
                                (ChatToolJob.status == "queued")
                                & (ChatToolJob.created_at < queued_before)
                            ),
                            (
                                ChatToolJob.status.in_(("running", "cancelling"))
                                & (ChatToolJob.lease_expires_at.is_not(None))
                                & (ChatToolJob.lease_expires_at < now)
                            ),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await self._materialize_job(row)
            if row.status == "cancelling":
                await self._cancel_locked(
                    row,
                    reason="executor_disconnected_after_cancel",
                )
            elif row.status == "queued":
                await self._fail_locked(
                    row,
                    error={
                        "code": "executor_not_started",
                        "message": (
                            "The background executor was not started before "
                            "its dispatch window expired."
                        ),
                    },
                )
            else:
                await self._fail_locked(
                    row,
                    error={
                        "code": "executor_disconnected_state_unknown",
                        "message": (
                            "The background executor connection was lost. "
                            "Its final side effects are unknown; inspect the "
                            "workspace before starting another task."
                        ),
                    },
                )
        return rows

    async def _cancel_locked(
        self,
        row: ChatToolJob,
        *,
        reason: str,
    ) -> ChatToolJob:
        row.status = "cancelled"
        row.finished_at = _now()
        row.lease_owner = None
        row.lease_expires_at = None
        await self._append_event_locked(
            row,
            "cancelled",
            {"status": "cancelled", "reason": reason},
        )
        return row

    async def request_cancel(
        self,
        *,
        chat_id: str,
        job_id: str,
        creator_user_id: str | uuid.UUID,
        reason: str = "requested",
    ) -> ChatToolJob | None:
        row = await self.get_for_user(
            chat_id=chat_id,
            job_id=job_id,
            creator_user_id=creator_user_id,
            for_update=True,
        )
        if row is None:
            return None
        if row.status in TERMINAL_BACKGROUND_JOB_STATUSES:
            return row
        row.cancel_requested_at = row.cancel_requested_at or _now()
        if row.status == "queued":
            return await self._cancel_locked(row, reason=reason)
        row.status = "cancelling"
        await self._append_event_locked(
            row,
            "cancel_requested",
            {"status": "cancelling", "reason": reason},
        )
        return row

    async def mark_cancelled(self, job_id: str, *, reason: str) -> ChatToolJob:
        row = await self.get(job_id, for_update=True)
        if row is None:
            raise LookupError(f"background job {job_id} not found")
        if row.status in TERMINAL_BACKGROUND_JOB_STATUSES:
            return row
        return await self._cancel_locked(row, reason=reason)

    async def active_count(self, chat_id: str) -> int:
        rows = (
            await self.session.execute(
                select(ChatToolJob.job_id).where(
                    ChatToolJob.chat_id == chat_id,
                    ChatToolJob.status.in_(ACTIVE_BACKGROUND_JOB_STATUSES),
                )
            )
        ).all()
        return len(rows)
