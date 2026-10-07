"""Bridge one live RPC execution to durable history and approval commands.

The driver is owned by sandboxd, not by an HTTP request. It never replays a
lost process. Acknowledgements follow database commits and artifact storage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time

import structlog
from sqlalchemy.exc import DBAPIError, TimeoutError as DatabasePoolTimeout

from vibecanvas_api.services.workflow_approvers import notify_approval_requested
from vibecanvas_api.services.workflow_resume import refresh_execution_context
from vibecanvas_api.services.deployment_completion import complete_before_cancelling
from vibecanvas_api.storage.db import short_session_scope
from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES, WorkflowHistoryRepo

from .workflow_rpc import WorkflowRpcError
from .workflow_event_wait import wait_for_execution_event
from ..state_notifications import execution_changes

logger = structlog.get_logger(__name__)
TRANSPORT_ERRORS = (OSError, TimeoutError, asyncio.IncompleteReadError)
PERSISTENCE_RETRY_SECONDS = 10


def _retryable_database_error(exc: Exception) -> bool:
    if isinstance(exc, DatabasePoolTimeout):
        return True
    if not isinstance(exc, DBAPIError):
        return False
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", "") or ""
    return exc.connection_invalidated or code.startswith("08") or code in {
        "40001", "40P01", "53300", "57P01", "57P02", "57P03",
    }


class WorkflowExecutionDriver:
    def __init__(
        self,
        *,
        tenant_id: str,
        execution_id: str,
        slot,
        persist_artifacts: Callable[[], Awaitable[None]],
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.tenant_id = tenant_id
        self.execution_id = execution_id
        self.slot = slot
        self.persist_artifacts = persist_artifacts
        self.on_event = on_event
        self._timeout = None
        self._remaining_timeout = None

    def _session(self):
        return short_session_scope(tenant_id=self.tenant_id)

    async def _persist_frames(self, generation: str, frames: list[dict]):
        """Retry the same ordered batch before ACK, including an uncertain commit.

        The runtime retains unacknowledged events. Sequence deduplication makes
        a retry safe even if the previous commit succeeded but its reply was lost.
        A prolonged outage follows the normal confirmed-stop failure path.
        """
        deadline = time.monotonic() + PERSISTENCE_RETRY_SECONDS
        reported = False
        while True:
            try:
                async with self._session() as session:
                    repo = WorkflowHistoryRepo(session)
                    before = await repo.get(self.execution_id, lock=True)
                    if before.get("timeout_requested_at") is not None:
                        return before["last_seq"], before, [], None
                    through = await repo.persist_events(self.execution_id, generation, frames)
                    run = await repo.get(self.execution_id)
                    commands = await repo.pending_commands(self.execution_id)
                    result = (
                        (await repo.detail(self.execution_id))["result"]
                        if run["status"] in TERMINAL_STATUSES else None
                    )
                return through, run, commands, result
            except (DBAPIError, DatabasePoolTimeout) as exc:
                if not _retryable_database_error(exc) or time.monotonic() >= deadline or not self.slot.alive:
                    raise
                if not reported:
                    logger.warning("workflow_history_write_retry", execution_id=self.execution_id)
                    reported = True
                await asyncio.sleep(0.25)

    @complete_before_cancelling
    async def _lost(self, error_code="execution_lost") -> dict:
        # Invocation-stop confirmation precedes terminal history and capacity
        # release. The worker is shared and remains resident for sibling calls.
        # An RPC timeout alone is never evidence that an execution stopped.
        await self.slot.close()
        async with self._session() as session:
            repo = WorkflowHistoryRepo(session)
            run = await repo.get(self.execution_id)
            if error_code == "execution_timeout" or run.get("timeout_requested_at") is not None:
                await repo.confirm_timed_out(self.execution_id)
            elif run["cancel_requested_at"] is not None:
                await repo.confirm_cancelled(self.execution_id)
            else:
                await repo.fail(self.execution_id, error_code=error_code)
            detail = await repo.detail(self.execution_id)
        return detail["result"]

    async def run(self, *, inputs: dict, context: dict, timeout_seconds: float | None = None) -> dict:
        try:
            async with asyncio.timeout(timeout_seconds) as budget:
                self._timeout = budget
                async with execution_changes(self.execution_id) as changed:
                    changed.set()  # Reconcile commands committed before subscribing.
                    return await self._run(inputs=inputs, context=context, changed=changed)
        except TimeoutError:
            if self._timeout is not None and self._timeout.expired():
                return await self._lost(error_code="execution_timeout")
            await self._lost()
            raise
        except BaseException:
            await self._lost()
            raise

    async def _run(self, *, inputs: dict, context: dict, changed: asyncio.Event) -> dict:
        generation = self.slot.client.generation
        async with self._session() as session:
            await WorkflowHistoryRepo(session).bind_runtime(
                self.execution_id,
                generation,
                process=self.slot.process_identity(),
            )
        # One submission per execution. Propagate dispatch failures to run(),
        # which stops this invocation and records failure; never resubmit.
        await self.slot.invoke(self.execution_id, inputs, context, require_approval_resume=True)
        after = 0
        disconnected_since = None
        sent_commands: set[str] = set()
        pending_resumes: dict[str, dict | None] = {}
        command_disconnected_since = None
        while True:
            if not self.slot.alive:
                return await self._lost()
            try:
                if command_disconnected_since is not None:
                    # Retry uncertain delivery only while the control transport
                    # is failing; normal idle executions perform no DB polling.
                    await asyncio.sleep(0.25)
                    changed.set()
                frames, reconcile = await wait_for_execution_event(
                    self.slot.client, self.execution_id, after, changed,
                )
                disconnected_since = None
            except TRANSPORT_ERRORS:
                disconnected_since = disconnected_since or time.monotonic()
                if time.monotonic() - disconnected_since > 10:
                    return await self._lost()
                await asyncio.sleep(0.25)
                continue
            except WorkflowRpcError as exc:
                if exc.code in {"execution_lost", "not_found"}:
                    return await self._lost()
                raise

            if not frames and not reconcile:
                continue  # Transport heartbeat: no state change to persist/read.
            if any(frame["type"] in {"result", "approval_requested"} for frame in frames):
                # Reviewers need prior node artifacts while the workflow waits,
                # before either the approval or final result becomes visible.
                await self.persist_artifacts()
            # The short transaction closes before ACK and before any RPC.
            through, run, commands, result = await self._persist_frames(generation, frames)
            after = through

            if run.get("timeout_requested_at") is not None:
                return await self._lost(error_code="execution_timeout")
            for frame in frames:
                if self._timeout is not None:
                    if frame["type"] == "approval_requested" and self._timeout.when() is not None:
                        self._remaining_timeout = max(0, self._timeout.when() - asyncio.get_running_loop().time())
                        self._timeout.reschedule(None)
                    elif frame["type"] == "approval_resolved" and frame.get("reason") != "timeout" and self._remaining_timeout is not None:
                        self._timeout.reschedule(asyncio.get_running_loop().time() + self._remaining_timeout)
                        self._remaining_timeout = None
                if frame["type"] == "approval_ready":
                    pending_resumes.setdefault(frame["approval_id"], None)
                if frame["type"] == "approval_requested":
                    try:
                        await notify_approval_requested(
                            execution_id=self.execution_id,
                            approval_id=frame["approval_id"],
                        )
                    except Exception:
                        # Notification delivery never decides an approval.
                        logger.warning(
                            "workflow_approval_notification_failed",
                            execution_id=self.execution_id,
                            approval_id=frame["approval_id"],
                        )
                if self.on_event:
                    await self.on_event(frame)

            if run["status"] in TERMINAL_STATUSES:
                await self.slot.release(self.execution_id, through)
                return result

            try:
                if frames:
                    await self.slot.client.call("acknowledge", invocation_id=self.execution_id, through=through)
                if run["cancel_requested_at"] is not None:
                    await self.slot.client.call("cancel", invocation_id=self.execution_id)
                    continue
                for approval_id, refreshed in list(pending_resumes.items()):
                    if refreshed is None:
                        try:
                            refreshed = await refresh_execution_context(
                                tenant_id=self.tenant_id,
                                execution_id=self.execution_id,
                                workflow=self.slot.workflow,
                                context=context,
                            )
                        except Exception as exc:
                            # Do not log exception text or locals: they can
                            # contain credentials returned by a dependency.
                            detail = getattr(exc, "detail", None)
                            reason = detail.get("code") if isinstance(detail, dict) else None
                            logger.warning(
                                "workflow_resume_authorization_failed",
                                execution_id=self.execution_id,
                                error_type=type(exc).__name__,
                                reason=reason if isinstance(reason, str) and reason.startswith("runtime_model_") else None,
                            )
                            return await self._lost(error_code="execution_resume_failed")
                        pending_resumes[approval_id] = refreshed
                    await self.slot.client.call(
                        "resume",
                        invocation_id=self.execution_id,
                        approval_id=approval_id,
                        context=refreshed,
                    )
                    del pending_resumes[approval_id]
                for command in commands:
                    if command["id"] in sent_commands:
                        continue
                    try:
                        await self.slot.client.call(
                            "decide",
                            invocation_id=self.execution_id,
                            approval_id=command["id"],
                            approved=command["requested_decision"],
                        )
                        sent_commands.add(command["id"])
                    except WorkflowRpcError as exc:
                        if exc.code != "state_conflict":
                            raise
                        # The runtime clock wins a deadline race. Its next
                        # event records timeout instead of the stale command.
                        sent_commands.add(command["id"])
                command_disconnected_since = None
            except TRANSPORT_ERRORS:
                command_disconnected_since = command_disconnected_since or time.monotonic()
                if time.monotonic() - command_disconnected_since > 10:
                    return await self._lost()
                # Read from the committed watermark next time. Uncertain
                # decisions are idempotently redelivered to the same runtime.
                continue
