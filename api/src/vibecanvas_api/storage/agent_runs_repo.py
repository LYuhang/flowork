"""CRUD for durable interactive agent runs and their ordered UI events."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service

from .models import Chat
from .models_agent_runs import (
    ACTIVE_AGENT_RUN_STATUSES,
    AgentRun,
    AgentRunEvent,
    HitlRequest,
)


class AgentRunActiveError(RuntimeError):
    """A different non-terminal Run already owns this Chat."""

    def __init__(self, run_id: str):
        super().__init__(f"chat already has active agent run {run_id}")
        self.run_id = run_id


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentRunsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def commit(self) -> None:
        await self.session.commit()

    async def _store_run_private(
        self,
        run: AgentRun,
        *,
        input_snapshot: dict,
        error_message: str | None,
    ) -> None:
        encrypted = await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=run.tenant_id,
            resource_type="chat",
            resource_id=run.chat_id,
            purpose="agent_run_private",
            record_id=run.run_id,
            value={
                "input_snapshot": dict(input_snapshot or {}),
                "error_message": error_message,
            },
        )
        run.private_ciphertext = encrypted.ciphertext
        run.private_nonce = encrypted.nonce
        run.private_key_id = encrypted.key_id
        run.input_snapshot = input_snapshot
        run.error_message = error_message

    async def _materialize_run(self, run: AgentRun | None) -> AgentRun | None:
        if run is None:
            return None
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=run.private_key_id,
            tenant_id=run.tenant_id,
            resource_type="chat",
            resource_id=run.chat_id,
            purpose="agent_run_private",
            record_id=run.run_id,
            ciphertext=run.private_ciphertext,
            nonce=run.private_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("Agent Run ciphertext must contain an object")
        run.input_snapshot = value.get("input_snapshot") or {}
        run.error_message = value.get("error_message")
        return run

    async def _new_event(
        self,
        *,
        run: AgentRun,
        seq: int,
        event_type: str,
        payload: dict,
        tenant_id: str | uuid.UUID,
    ) -> AgentRunEvent:
        encrypted = await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=tenant_id,
            resource_type="chat",
            resource_id=run.chat_id,
            purpose="agent_run_event",
            record_id=f"{run.run_id}:{seq}",
            value=dict(payload or {}),
        )
        event = AgentRunEvent(
            run_id=run.run_id,
            seq=seq,
            tenant_id=_uuid(tenant_id),
            event_type=event_type,
            payload_ciphertext=encrypted.ciphertext,
            payload_nonce=encrypted.nonce,
            payload_key_id=encrypted.key_id,
        )
        event.payload = payload
        return event

    async def _materialize_event(self, event: AgentRunEvent) -> AgentRunEvent:
        run = await self.session.get(AgentRun, event.run_id)
        if run is None:
            raise LookupError(f"agent run {event.run_id} not found")
        value = await content_encryption_service().decrypt_json(
            self.session,
            key_id=event.payload_key_id,
            tenant_id=event.tenant_id,
            resource_type="chat",
            resource_id=run.chat_id,
            purpose="agent_run_event",
            record_id=f"{event.run_id}:{event.seq}",
            ciphertext=event.payload_ciphertext,
            nonce=event.payload_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("Agent Run event ciphertext must contain an object")
        event.payload = value
        return event

    async def create(
        self,
        *,
        run_id: str,
        tenant_id: str | uuid.UUID,
        chat_id: str,
        creator_user_id: str | uuid.UUID,
        client_request_id: str,
        input_snapshot: dict,
        input_message_id: str | None = None,
    ) -> AgentRun:
        run = AgentRun(
            run_id=run_id,
            tenant_id=_uuid(tenant_id),
            chat_id=chat_id,
            creator_user_id=_uuid(creator_user_id),
            client_request_id=client_request_id,
            status="running",
            input_message_id=input_message_id,
        )
        await self._store_run_private(
            run,
            input_snapshot=input_snapshot,
            error_message=None,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def create_exclusive(
        self,
        *,
        run_id: str,
        tenant_id: str | uuid.UUID,
        chat_id: str,
        creator_user_id: str | uuid.UUID,
        client_request_id: str,
        input_snapshot: dict,
        input_message_id: str | None = None,
    ) -> tuple[AgentRun, bool]:
        """Atomically reserve the sole live Run for a Chat.

        The read-before-insert checks in an HTTP handler are only a latency fast
        path: two API workers can pass them concurrently.  A transaction-scoped
        PostgreSQL advisory lock serializes reservations for one Chat, while the
        partial unique index remains the final schema invariant.

        Returns ``(run, created)``.  Reusing the same ``client_request_id`` is an
        idempotent lookup (``created=False``); a different live request raises a
        stable ``AgentRunActiveError`` instead of leaking an IntegrityError/500.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"agent-run:{chat_id}"},
        )
        existing = await self.get_by_client_request(
            chat_id,
            client_request_id,
            creator_user_id=creator_user_id,
        )
        if existing is not None:
            return existing, False
        active = await self.get_active_for_chat(chat_id)
        if active is not None:
            raise AgentRunActiveError(active.run_id)
        return (
            await self.create(
                run_id=run_id,
                tenant_id=tenant_id,
                chat_id=chat_id,
                creator_user_id=creator_user_id,
                client_request_id=client_request_id,
                input_snapshot=input_snapshot,
                input_message_id=input_message_id,
            ),
            True,
        )

    async def record_runtime_input(
        self,
        run_id: str,
        *,
        chat_id: str,
        creator_user_id: str | uuid.UUID,
        event_id: str,
        payload: dict,
    ) -> None:
        """Append a private submission snapshot without changing display history."""
        row = (await self.session.execute(
            select(AgentRun).where(
                AgentRun.run_id == run_id,
                AgentRun.chat_id == chat_id,
                AgentRun.creator_user_id == _uuid(creator_user_id),
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        run = await self._materialize_run(row)
        if run is None:
            raise LookupError("runtime input audit Run not found")
        snapshot = dict(run.input_snapshot)
        submissions = list(snapshot.get("runtime_inputs") or [])
        if any(item.get("event_id") == event_id for item in submissions):
            return
        submissions.append({
            "event_id": event_id,
            "recorded_at": _now().isoformat(),
            "payload": payload,
        })
        snapshot["runtime_inputs"] = submissions
        await self._store_run_private(
            run, input_snapshot=snapshot, error_message=run.error_message,
        )
        await self.session.flush()

    async def get(self, run_id: str) -> AgentRun | None:
        return await self._materialize_run(
            await self.session.get(AgentRun, run_id)
        )

    async def failed_turns_for_history(
        self, chat_id: str, turn_ids: list[str], *, creator_user_id: str,
    ) -> dict[str, dict[str, str]]:
        if not turn_ids:
            return {}
        rows = (await self.session.execute(select(AgentRun).where(
            AgentRun.chat_id == chat_id,
            AgentRun.creator_user_id == _uuid(creator_user_id),
            AgentRun.run_id.in_(turn_ids),
            AgentRun.status == "failed",
        ))).scalars().all()
        result = {}
        for row in rows:
            run = await self._materialize_run(row)
            result[run.run_id] = {
                "code": run.error_code or "engine_error",
                "message": run.error_message or "",
            }
        return result

    async def get_for_chat(
        self,
        chat_id: str,
        run_id: str,
        *,
        creator_user_id: str | uuid.UUID,
    ) -> AgentRun | None:
        row = (
            await self.session.execute(
                select(AgentRun).where(
                    AgentRun.run_id == run_id,
                    AgentRun.chat_id == chat_id,
                    AgentRun.creator_user_id == _uuid(creator_user_id),
                )
            )
        ).scalar_one_or_none()
        return await self._materialize_run(row)

    async def get_active_for_chat(self, chat_id: str) -> AgentRun | None:
        row = (
            await self.session.execute(
                select(AgentRun)
                .where(
                    AgentRun.chat_id == chat_id,
                    AgentRun.status.in_(ACTIVE_AGENT_RUN_STATUSES),
                )
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return await self._materialize_run(row)

    async def get_active_for_chat_user(
        self,
        chat_id: str,
        *,
        creator_user_id: str | uuid.UUID,
    ) -> AgentRun | None:
        """Return the sole active Run only when the user owns the Chat Run."""
        row = (
            await self.session.execute(
                select(AgentRun)
                .where(
                    AgentRun.chat_id == chat_id,
                    AgentRun.creator_user_id == _uuid(creator_user_id),
                    AgentRun.status.in_(ACTIVE_AGENT_RUN_STATUSES),
                )
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return await self._materialize_run(row)

    async def get_by_client_request(
        self,
        chat_id: str,
        client_request_id: str,
        *,
        creator_user_id: str | uuid.UUID,
    ) -> AgentRun | None:
        row = (
            await self.session.execute(
                select(AgentRun).where(
                    AgentRun.chat_id == chat_id,
                    AgentRun.client_request_id == client_request_id,
                    AgentRun.creator_user_id == _uuid(creator_user_id),
                )
            )
        ).scalar_one_or_none()
        return await self._materialize_run(row)

    async def list_active_for_scope(
        self,
        scope_id: str,
        *,
        creator_user_id: str | uuid.UUID,
    ) -> list[AgentRun]:
        rows = list((
            await self.session.execute(
                select(AgentRun)
                .join(Chat, Chat.chat_id == AgentRun.chat_id)
                .where(
                    Chat.scope_id == scope_id,
                    Chat.creator_user_id == _uuid(creator_user_id),
                    Chat.deleted_at.is_(None),
                    AgentRun.status.in_(ACTIVE_AGENT_RUN_STATUSES),
                )
                .order_by(AgentRun.created_at)
            )
        ).scalars().all())
        for row in rows:
            await self._materialize_run(row)
        return rows

    async def list_debug_turns(self, chat_id: str, *, creator_user_id: str) -> list[dict]:
        """Owner-scoped diagnostic metadata; never expose a raw private envelope."""
        rows = (await self.session.execute(select(AgentRun).where(
            AgentRun.chat_id == chat_id,
            AgentRun.creator_user_id == _uuid(creator_user_id),
        ).order_by(AgentRun.created_at, AgentRun.run_id))).scalars().all()
        usage_by_run: dict[str, list[dict]] = {}
        if rows:
            usage_events = (await self.session.execute(select(AgentRunEvent).where(
                AgentRunEvent.run_id.in_([run.run_id for run in rows]),
                AgentRunEvent.event_type == "USAGE",
            ).order_by(AgentRunEvent.run_id, AgentRunEvent.seq))).scalars().all()
            for event in usage_events:
                await self._materialize_event(event)
                usage_by_run.setdefault(event.run_id, []).append(event.payload)
        result = []
        public_input_fields = {
            "mode", "surface", "agent_surface", "approval_mode", "runtime_type",
            "runtime_session_id", "runtime_version", "runtime_connection_id",
            "model_id", "provider_model_id", "model_provider", "api_source",
            "api_protocol", "reasoning_effort", "command", "runtime_instructions",
            "skill_snapshot", "mcp_snapshot",
        }
        for run in rows:
            await self._materialize_run(run)
            result.append({
                "run_id": run.run_id,
                "input_message_id": run.input_message_id,
                "client_request_id": run.client_request_id,
                "status": run.status,
                "created_at": run.created_at,
                "ended_at": run.ended_at,
                "heartbeat_at": run.heartbeat_at,
                "duration_ms": int((run.ended_at - run.created_at).total_seconds() * 1000) if run.ended_at else None,
                "cancel_requested_at": run.cancel_requested_at,
                "last_event_id": run.last_event_id,
                "error_code": run.error_code,
                "error_message": run.error_message,
                "usage": usage_by_run.get(run.run_id, []),
                "configuration": {key: value for key, value in run.input_snapshot.items() if key in public_input_fields},
            })
        return result

    async def append_event(
        self,
        *,
        run_id: str,
        seq: int,
        event_type: str,
        payload: dict,
        tenant_id: str | uuid.UUID,
    ) -> None:
        run = await self.get(run_id)
        if run is None:
            raise LookupError(f"agent run {run_id} not found")

        now = _now()
        self.session.add(await self._new_event(
            run=run,
            seq=seq,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
        ))
        run.last_event_id = max(run.last_event_id, seq)
        run.heartbeat_at = now
        run.updated_at = now

        if event_type == "done":
            run.status = "completed"
            run.ended_at = now
        elif event_type == "error":
            code = str(payload.get("code") or "engine_error")
            run.status = "cancelled" if code == "cancelled" else "failed"
            run.error_code = code
            await self._store_run_private(
                run,
                input_snapshot=run.input_snapshot,
                error_message=str(payload.get("message") or "")[:2000],
            )
            run.ended_at = now

        await self.session.flush()

    async def list_events(self, run_id: str, after_seq: int, *, limit: int = 256) -> list[AgentRunEvent]:
        rows = list((
            await self.session.execute(
                select(AgentRunEvent)
                .where(
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.seq > after_seq,
                )
                .order_by(AgentRunEvent.seq)
                .limit(max(1, min(limit, 256)))
            )
        ).scalars().all())
        for row in rows:
            await self._materialize_event(row)
        return rows

    async def heartbeat(self, run_id: str) -> None:
        run = await self.get(run_id)
        if run is None or run.status not in {"running", "waiting_approval", "cancel_requested"}:
            return
        now = _now()
        run.heartbeat_at = now
        run.updated_at = now
        await self.session.flush()

    async def cancel_requested(self, run_id: str) -> bool:
        status = (
            await self.session.execute(
                select(AgentRun.status).where(AgentRun.run_id == run_id)
            )
        ).scalar_one_or_none()
        return status == "cancel_requested"

    async def request_cancel(
        self,
        chat_id: str,
        run_id: str,
        *,
        creator_user_id: str | uuid.UUID,
    ) -> bool:
        run = await self.get_for_chat(
            chat_id,
            run_id,
            creator_user_id=creator_user_id,
        )
        if run is None or run.status not in {"running", "waiting_approval", "cancel_requested"}:
            return False
        if run.status != "cancel_requested":
            now = _now()
            run.status = "cancel_requested"
            run.cancel_requested_at = now
            run.updated_at = now
            await self.session.flush()
        return True

    async def mark_stale_for_scope(
        self,
        *,
        scope_id: str,
        stale_before: datetime,
        tenant_id: str | uuid.UUID,
        creator_user_id: str | uuid.UUID,
    ) -> int:
        """Close orphaned live runs whose worker heartbeat has expired.

        This MVP does not automatically transfer execution to another worker.
        Persisting an explicit terminal event is preferable to leaving every
        frontend permanently stuck in a synthetic streaming state.
        """
        rows = list((
            await self.session.execute(
                select(AgentRun)
                .join(Chat, Chat.chat_id == AgentRun.chat_id)
                .where(
                    Chat.scope_id == scope_id,
                    Chat.creator_user_id == _uuid(creator_user_id),
                    AgentRun.status.in_(("running", "waiting_approval", "cancel_requested")),
                    AgentRun.heartbeat_at < stale_before,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all())
        now = _now()
        # Local import avoids a module cycle; both repositories share the same
        # transaction so stale-Run closure and HITL ciphertext updates remain
        # atomic.
        from .hitl_repo import HitlRepo
        hitl_repo = HitlRepo(self.session)
        for run in rows:
            await self._materialize_run(run)
            cancelled = run.status == "cancel_requested"
            payload = {
                "code": "cancelled" if cancelled else "worker_lost",
                "message": (
                    "Turn cancelled after its worker stopped responding."
                    if cancelled else
                    "The agent worker stopped responding. Please retry the turn."
                ),
            }
            seq = int(run.last_event_id or 0)
            pending_hitl = list((
                await self.session.execute(
                    select(HitlRequest).where(
                        HitlRequest.run_id == run.run_id,
                        HitlRequest.status == "pending",
                    )
                )
            ).scalars().all())
            for req in pending_hitl:
                await hitl_repo._materialize_request(req)
                req.status = "cancelled" if cancelled else "expired"
                req.is_interacted = True
                req.decision_payload_json = {
                    "reason": payload["code"],
                    "message": payload["message"],
                }
                req.interaction_result_json = req.decision_payload_json
                req.resolved_at = now
                req.updated_at = now
                if req.artifact_id:
                    artifact = await hitl_repo.get_artifact(req.artifact_id)
                    if artifact is not None:
                        artifact.is_interacted = True
                        artifact.interaction_result_json = req.interaction_result_json
                        artifact.updated_at = now
                        await hitl_repo._store_artifact_private(artifact)
                await hitl_repo._store_request_private(req)
                seq += 1
                self.session.add(await self._new_event(
                    run=run,
                    seq=seq,
                    tenant_id=tenant_id,
                    event_type="HITL_RESOLVED",
                    payload={
                        "hitl_request_id": req.hitl_request_id,
                        "chat_id": req.chat_id,
                        "run_id": req.run_id,
                        "artifact_id": req.artifact_id,
                        "hitl_type": req.hitl_type,
                        "status": req.status,
                        "title": req.title,
                        "decision_payload": req.decision_payload_json,
                        "interaction_result": req.interaction_result_json,
                        "agent_payload": req.agent_payload_json,
                    },
                ))
            seq += 1
            self.session.add(await self._new_event(
                run=run,
                seq=seq,
                tenant_id=tenant_id,
                event_type="error",
                payload=payload,
            ))
            run.last_event_id = seq
            run.status = "cancelled" if cancelled else "failed"
            run.error_code = payload["code"]
            await self._store_run_private(
                run,
                input_snapshot=run.input_snapshot,
                error_message=payload["message"],
            )
            run.updated_at = now
            run.ended_at = now
        await self.session.flush()
        return len(rows)
