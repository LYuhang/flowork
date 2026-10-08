"""Read-only historical workflow graphs and authenticated human decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import aclosing
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.authorization.dependencies import authorize_resource, authorized_resource_scope, get_authz_service
from vibecanvas_api.authorization.service import AuthzService
from vibecanvas_api.authorization.types import Action, ResourceRef, ResourceType
from vibecanvas_api.storage.workflow_history_repo import HistoryConflict, WorkflowHistoryRepo

router = APIRouter(prefix="/api/v1/workflow-executions", tags=["executions"])
SourceType = Literal["workflow", "task", "deployment"]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: StrictBool


async def _authorize_source(request, auth, service, source_type, source_id, *, session, action=Action.INSPECT_RUNS):
    if source_type == "workflow":
        async with authorized_resource_scope(request=request, auth=auth, session=session,
                resource_type=ResourceType.WORKFLOW, resource_id=source_id, action=action):
            pass
        return
    await authorize_resource(
        request=request,
        auth=auth,
        service=service,
        resource=ResourceRef(ResourceType(source_type), source_id, auth.active_organization_id),
        action=action,
    )


async def _require_workflow_run_visibility(repo, auth, run):
    if (run["source_type"] == "workflow"
            and str(run["initiator_user_id"]) != auth.user_id
            and not await repo.is_workflow_owner(run["source_id"], auth.user_id)):
        raise HTTPException(404, "execution_not_found")


async def _authorize_detail(request, auth, service, repo, execution_id):
    run = await repo.get(execution_id)
    if run is None:
        raise HTTPException(404, "execution_not_found")
    if not await repo.is_assignee(execution_id, auth.user_id):
        await _require_workflow_run_visibility(repo, auth, run)
        await _authorize_source(request, auth, service, run["source_type"], run["source_id"], session=repo.session)
    return run


async def _stream_authorized(request, auth, execution_id):
    """Revalidate the same assignee/private-history rules as an ordinary read."""
    from vibecanvas_api.auth.deps import _admit_shared_resource
    from vibecanvas_api.auth.live_identity import resolve_live_authorization_identity
    from vibecanvas_api.storage.db import session_scope
    try:
        async with session_scope() as identity:
            fresh = await resolve_live_authorization_identity(identity, session_id=auth.session_id,
                user_id=auth.user_id, organization_id=auth.active_organization_id,
                session_generation=auth.session_generation, membership_id=auth.membership_id)
        async with session_scope(tenant_id=fresh.active_organization_id, user_id=fresh.user_id) as session:
            current = Request({**request.scope, 'state': {}})
            await _admit_shared_resource(current, fresh, session)
            repo = WorkflowHistoryRepo(session)
            run = await repo.get(execution_id)
            if run is None:
                return False
            if not await repo.is_assignee(execution_id, fresh.user_id):
                await _require_workflow_run_visibility(repo, fresh, run)
                async with authorized_resource_scope(request=current, auth=fresh, session=session,
                        resource_type=ResourceType(run['source_type']), resource_id=run['source_id'],
                        action=Action.INSPECT_RUNS):
                    pass
        return True
    except Exception:
        return False


@router.get("/{execution_id}/activity")
async def activity(
    request: Request, execution_id: uuid.UUID,
    auth: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    from vibecanvas_api.services.state_notifications import invalidations
    identifier = str(execution_id)
    await _authorize_detail(request, auth, service, WorkflowHistoryRepo(session), identifier)
    await session.commit()

    async def stream():
        async with aclosing(invalidations('flowork_execution_activity', identifier,
                lambda: _stream_authorized(request, auth, identifier))) as changes:
            async for changed in changes:
                yield 'event: changed\ndata: {}\n\n' if changed else ': heartbeat\n\n'

    return StreamingResponse(stream(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get("")
async def history(
    request: Request,
    source_type: SourceType,
    source_id: str,
    statuses: list[str] | None = Query(default=None),
    mine: bool = False,
    before_time: datetime | None = None,
    before_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=1000),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    # The personal pending view grants only the current assignee's records.
    # Browsing the source's full history still requires normal inspect authority.
    if not mine:
        await _authorize_source(request, auth, service, source_type, source_id, session=session)
    if (before_time is None) != (before_id is None):
        raise HTTPException(422, "both_cursor_fields_required")
    if before_time is not None and before_time.tzinfo is None:
        raise HTTPException(422, "cursor_timezone_required")
    repo = WorkflowHistoryRepo(session)
    own_runs_only = (source_type == "workflow" and not mine
                     and not await repo.is_workflow_owner(source_id, auth.user_id))
    try:
        result = await repo.history(
            source_type=source_type,
            source_id=source_id,
            statuses=statuses,
            pending_for_user_id=auth.user_id if mine else None,
            initiator_user_id=auth.user_id if own_runs_only else None,
            before=(before_time, str(before_id)) if before_time else None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Only visible-page assignees are resolved. Do not decrypt every historical
    # workflow or return its business inputs to render a pending-review summary.
    emails = {}
    users = AuthRepo(session)
    for item in result["items"]:
        for approval in item["pending_approvals"]:
            actor = approval["approver_user_id"]
            if actor not in emails:
                user = await users.get_user(uuid.UUID(actor))
                emails[actor] = user.email if user else None
            approval["approver_email"] = emails[actor]
    result["server_time"] = datetime.now(timezone.utc).isoformat()
    return result


@router.get("/{execution_id}")
async def detail(
    request: Request,
    execution_id: uuid.UUID,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    repo = WorkflowHistoryRepo(session)
    await _authorize_detail(request, auth, service, repo, str(execution_id))
    result = await repo.detail(str(execution_id))
    result["server_time"] = datetime.now(timezone.utc).isoformat()
    for approval in result["approvals"]:
        approval["can_decide"] = (
            approval["approver_user_id"] == auth.user_id
            and approval["status"] == "pending"
            and datetime.fromisoformat(approval["deadline"]) > datetime.now(timezone.utc)
            and result["status"] in {"running", "waiting_approval"}
        )
    return result


@router.get("/{execution_id}/events")
async def events(
    request: Request,
    execution_id: uuid.UUID,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    repo = WorkflowHistoryRepo(session)
    run = await _authorize_detail(request, auth, service, repo, str(execution_id))
    frames = await repo.events(str(execution_id), after=after, limit=limit)
    return {"events": frames, "last_seq": run["last_seq"], "status": run["status"]}


@router.post("/{execution_id}/approvals/{approval_id}", status_code=202)
async def decide(
    execution_id: uuid.UUID,
    approval_id: str,
    body: ApprovalDecision,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
):
    # current_user revalidates account/membership and browser CSRF. Assignment is
    # execution-scoped; graph editing or deployment privileges do not substitute.
    try:
        return await WorkflowHistoryRepo(session).request_decision(
            str(execution_id),
            approval_id,
            actor_user_id=auth.user_id,
            approved=body.approved,
        )
    except KeyError as exc:
        raise HTTPException(404, "approval_not_found") from exc
    except HistoryConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{execution_id}/cancel", status_code=202)
async def cancel(
    request: Request,
    execution_id: uuid.UUID,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    repo = WorkflowHistoryRepo(session)
    run = await repo.get(str(execution_id))
    if run is None:
        raise HTTPException(404, "execution_not_found")
    await _require_workflow_run_visibility(repo, auth, run)
    await _authorize_source(request, auth, service, run["source_type"], run["source_id"], session=session, action=Action.CANCEL)
    try:
        return await repo.request_cancel(str(execution_id))
    except HistoryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
