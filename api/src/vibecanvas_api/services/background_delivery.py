"""Observe and deliver durable background results as independent Human Turns."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import structlog
from fastapi import HTTPException, Request
from sqlalchemy import exists, func, select

from vibecanvas_api.auth.deps import AuthContext
from vibecanvas_api.security.identity_protection import decrypt_user_profile
from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
)
from vibecanvas_api.schemas.chat import BackgroundResultsControl, MessagePostBody
from vibecanvas_api.storage.agent_runtime_repo import AgentRuntimeRepo
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.background_delivery_repo import BackgroundDeliveryRepo
from vibecanvas_api.storage.background_jobs_repo import BackgroundJobsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo
from vibecanvas_api.storage.models import Chat, Session, Tenant, User
from vibecanvas_api.storage.models_org import OrgMembership
from vibecanvas_api.storage.models_background_jobs import (
    ACTIVE_BACKGROUND_JOB_STATUSES,
    TERMINAL_BACKGROUND_JOB_STATUSES,
    ChatToolJob,
    ChatToolJobDelivery,
)
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


logger = structlog.get_logger(__name__)


def background_result_batch_id(job_ids: list[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(job_ids)).encode()).hexdigest()
    return f"bg_{digest[:24]}"


class BackgroundResultDeliveryCoordinator:
    """Cross-worker observer backed entirely by PostgreSQL state.

    Every API worker may run this loop. The ordinary chat Turn advisory lock,
    the unique client request id, and row-level delivery claims make those
    observers converge on exactly one result Turn.
    """

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._openfga_client = None

    async def start(self, *, openfga_client=None) -> None:
        self._openfga_client = openfga_client
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name="background-result-delivery",
        )

    async def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "background_result_delivery_sweep_failed",
                    exc_info=True,
                )

    def notify(self) -> None:
        """Wake the shared indexed sweep after a job/Turn terminal event."""

        self._wake.set()

    async def sweep_once(self) -> int:
        tenant_ids: list[str] = []
        async with session_scope() as session:
            tenant_ids = [
                str(value)
                for value in (
                    await session.execute(select(Tenant.tenant_id))
                ).scalars()
            ]
        candidates: list[tuple[str, str, str]] = []
        for tenant_id in tenant_ids:
            async with session_scope(tenant_id=tenant_id) as session:
                rows = (
                    await session.execute(
                        select(
                            ChatToolJob.chat_id,
                            ChatToolJob.creator_user_id,
                        )
                        .where(
                            (
                                ChatToolJob.status.in_(
                                    ACTIVE_BACKGROUND_JOB_STATUSES
                                )
                                | (
                                    ChatToolJob.status.in_(
                                        TERMINAL_BACKGROUND_JOB_STATUSES
                                    )
                                    & ~exists(
                                        select(ChatToolJobDelivery.job_id).where(
                                            ChatToolJobDelivery.job_id
                                            == ChatToolJob.job_id
                                        )
                                    )
                                )
                            ),
                        )
                        .distinct()
                        .limit(100)
                    )
                ).all()
                candidates.extend(
                    (tenant_id, str(chat_id), str(user_id))
                    for chat_id, user_id in rows
                )
        delivered = 0
        for tenant_id, chat_id, user_id in candidates:
            if self._stop.is_set():
                break
            if await self._deliver_one(
                tenant_id=tenant_id,
                chat_id=chat_id,
                user_id=user_id,
            ):
                delivered += 1
        return delivered

    async def _deliver_one(
        self,
        *,
        tenant_id: str,
        chat_id: str,
        user_id: str,
    ) -> bool:
        async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
            chat_user_membership = (
                await session.execute(
                    select(Chat, User, OrgMembership)
                    .join(User, User.user_id == Chat.creator_user_id)
                    .join(
                        OrgMembership,
                        (OrgMembership.user_id == Chat.creator_user_id)
                        & (OrgMembership.tenant_id == Chat.tenant_id),
                    )
                    .where(
                        Chat.chat_id == chat_id,
                        Chat.creator_user_id == uuid.UUID(user_id),
                        Chat.deleted_at.is_(None),
                        User.status == "active",
                        OrgMembership.status == "active",
                    )
                )
            ).one_or_none()
            if chat_user_membership is None:
                return False
            chat, user, membership = chat_user_membership
            # Result processing continues to represent the Chat user, not a
            # platform service account. Bind the internal Turn to the user's
            # newest live primary Session so model/MCP brokers retain their
            # normal generation and revocation checks. If the user has logged
            # out, leave the terminal jobs pending; a later login makes the
            # next observer sweep deliver them without losing or replaying the
            # background execution.
            user_session = (
                await session.execute(
                    select(Session)
                    .where(
                        Session.user_id == uuid.UUID(user_id),
                        Session.active_organization_id == uuid.UUID(tenant_id),
                        Session.audience.in_(("web", "api")),
                        Session.expires_at > func.now(),
                    )
                    .order_by(Session.last_seen_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if user_session is None:
                return False
            repo = BackgroundJobsRepo(session)
            # A worker/API restart does not replay business work. Once its
            # execution lease expires, make that loss explicit and deliver the
            # resulting durable failure as an ordinary result Turn.
            await repo.reconcile_stale_for_chat(chat_id=chat_id)
            jobs = await BackgroundDeliveryRepo(
                session
            ).list_pending_terminal_for_user(
                chat_id=chat_id,
                creator_user_id=user_id,
                limit=100,
            )
            if not jobs:
                return False
            job_ids = [job.job_id for job in jobs]
            profile = await decrypt_user_profile(session, user)
            auth = AuthContext(
                user_id=user_id,
                tenant_id=tenant_id,
                email=profile.email,
                display_name=profile.display_name,
                active_organization_id=tenant_id,
                membership_id=str(membership.membership_id),
                membership_role=membership.org_role,
                membership_status=membership.status,
                session_id=str(user_session.session_id),
                session_generation=int(user_session.generation),
                authentication_strength=user_session.authentication_strength,
            )
            body = MessagePostBody(
                control=BackgroundResultsControl(
                    type="background_results",
                    batch_id=background_result_batch_id(job_ids),
                    job_ids=job_ids,
                ),
                client_request_id=background_result_batch_id(job_ids),
            )

            # Reuse the ordinary message/Turn path so command state, MCP
            # selection, runtime history, durable events and product transcript
            # retain exactly the same invariants as a user-originated Turn.
            from vibecanvas_api.routes.chats import post_message
            from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo

            request = Request({
                "type": "http",
                "method": "INTERNAL",
                "path": "/api/v1/background-delivery",
                "headers": [],
                "query_string": b"",
                "app": SimpleNamespace(
                    state=SimpleNamespace(
                        openfga_client=self._openfga_client,
                    ),
                ),
                "state": {
                    "request_id": (
                        f"background-delivery:{chat_id}:"
                        f"{body.control.batch_id}"
                    ),
                },
            })
            authz_service = authz_service_for_session(
                session=session,
                organization_id=tenant_id,
                openfga_client=self._openfga_client,
            )
            try:
                await post_message(
                    str(chat.scope_id),
                    chat_id,
                    body,
                    request,
                    wf_repo=WorkflowRepo(session, user_id),
                    chat_repo=ChatRepo(session, user_id),
                    project_repo=ChatProjectRepo(session, user_id),
                    hitl_repo=HitlRepo(session),
                    runtime_repo=AgentRuntimeRepo(session, user_id),
                    agent_runs_repo=AgentRunsRepo(session),
                    session=session,
                    auth=auth,
                    authz_service=authz_service,
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                # An active foreground/result Turn is expected. The next sweep
                # will observe the same unclaimed rows after that Turn ends.
                if exc.status_code in {409, 503} or detail.get("code") in {
                    "chat_run_active",
                    "background_result_batch_not_available",
                }:
                    return False
                raise
            return True


background_result_delivery = BackgroundResultDeliveryCoordinator()
