"""Short-session durable writer used by the in-process Agent Turn runner."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.db import session_scope


class AgentRunWriter:
    """Persist every product event before it is published to an SSE subscriber.

    The event log is the recovery source for partial output, so an event that a
    browser can observe must already be durable. Any future write coalescing
    belongs below this contract (transaction batching or a snapshot table), not
    in process-local pending state.
    """

    def __init__(
        self,
        *,
        run_id: str,
        tenant_id: str,
        chat_id: str | None = None,
        user_id: str | None = None,
    ):
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.chat_id = chat_id
        self.user_id = user_id
        self._lock = asyncio.Lock()
        self._transcript = None
        if chat_id and user_id:
            from vibecanvas_api.services.chat_transcript_writer import (
                ChatTranscriptWriter,
            )
            self._transcript = ChatTranscriptWriter(
                tenant_id=tenant_id,
                user_id=user_id,
                chat_id=chat_id,
                turn_id=run_id,
            )

    async def _append(self, seq: int, event_type: str, payload: dict) -> None:
        async with session_scope(tenant_id=self.tenant_id) as session:
            if event_type == "HITL_REQUIRED" and self.chat_id and self.user_id:
                from vibecanvas_api.storage.chat_queue_repo import ChatQueueRepo
                await ChatQueueRepo(session, self.user_id).block_for_interaction(self.chat_id, self.run_id)
            await AgentRunsRepo(session).append_event(
                run_id=self.run_id,
                seq=seq,
                event_type=event_type,
                payload=payload,
                tenant_id=self.tenant_id,
            )

    async def emit(self, seq: int, event_type: str, payload: Any) -> None:
        raw_payload = payload if isinstance(payload, dict) else {"value": payload}
        # SSE already stringifies uncommon values. Mirror that tolerance before
        # binding JSONB so a Path/datetime in an artifact cannot kill the Turn.
        safe_payload = json.loads(json.dumps(raw_payload, default=str))
        async with self._lock:
            if self._transcript is not None:
                await self._transcript.consume(event_type, safe_payload)
            await self._append(seq, event_type, safe_payload)
            if event_type == "USER_INPUT_STATUS" and isinstance(payload, dict):
                # The in-memory SSE buffer publishes this same dictionary only
                # after emit returns. Match the durable acknowledgment exactly.
                payload.clear()
                payload.update(safe_payload)

    async def heartbeat(self) -> None:
        async with session_scope(tenant_id=self.tenant_id) as session:
            await AgentRunsRepo(session).heartbeat(self.run_id)

    async def cancel_requested(self) -> bool:
        async with session_scope(tenant_id=self.tenant_id) as session:
            return await AgentRunsRepo(session).cancel_requested(self.run_id)

    async def close(self) -> None:
        if self._transcript is not None:
            await self._transcript.close()
