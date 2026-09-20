"""Two-person privileged-support request, approval, and activation APIs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select

from vibecanvas_api.audit import actions as audit_actions
from vibecanvas_api.audit.context import extract_request_audit_context
from vibecanvas_api.audit.service import record_audit, record_auth_audit
from vibecanvas_api.auth.deps import (
    AuthContext,
    current_user,
    require_webauthn_step_up,
)
from vibecanvas_api.auth.email_sender import get_email_sender
from vibecanvas_api.auth.privileged_access import (
    SENSITIVE_ACTIONS,
    decrypt_request_private_payload,
    encrypt_request_private_payload,
    operator_is_eligible,
    validate_requested_scope,
)
from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.auth.session_security import (
    clear_session_cookie,
    cookie_credential,
    set_session_cookies,
)
from vibecanvas_api.auth.tokens import hash_token, new_token
from vibecanvas_api.authorization.types import Action, ResourceType
from vibecanvas_api.config import config
from vibecanvas_api.security.identity_protection import decrypt_user_profile
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import Session, User
from vibecanvas_api.storage.models_org import Organization, OrgMembership
from vibecanvas_api.storage.models_privileged_access import (
    PlatformAdminEligibility,
    PrivilegedAccessRequest,
)


router = APIRouter(
    prefix="/api/v1/auth/privileged-access",
    tags=["privileged-access"],
)
_REQUEST_TTL = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PrivilegedAccessRequestIn(BaseModel):
    resource_type: ResourceType | None = None
    resource_id: str | None = Field(default=None, min_length=1, max_length=256)
    actions: list[Action] = Field(min_length=1, max_length=16)
    duration_seconds: int = Field(default=900, ge=60, le=1800)
    justification: str = Field(min_length=20, max_length=2000)
    ticket_reference: str = Field(min_length=1, max_length=200)
    sensitive_scope_confirmed: bool = False

    @model_validator(mode="after")
    def validate_scope(self):
        try:
            validate_requested_scope(
                resource_type=self.resource_type,
                resource_id=self.resource_id,
                actions=set(self.actions),
                sensitive_scope_confirmed=self.sensitive_scope_confirmed,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class PrivilegedApprovalIn(BaseModel):
    sensitive_scope_confirmed: bool = False


class PlatformEligibilityIn(BaseModel):
    role: str = Field(pattern="^(platform_support|platform_security_admin)$")
    review_ttl_days: int = Field(default=30, ge=1, le=90)


class PlatformEligibilityReviewIn(BaseModel):
    review_ttl_days: int = Field(default=30, ge=1, le=90)


def _require_feature() -> None:
    if not config.privileged_access_enabled:
        raise HTTPException(404, "privileged_access_disabled")


async def _require_operator(ctx: AuthContext) -> None:
    _require_feature()
    async with session_scope() as session:
        if not await operator_is_eligible(session, ctx.user_id):
            raise HTTPException(404, "privileged_access_not_found")


async def _current_operator_eligibility(ctx: AuthContext) -> bool:
    if not config.privileged_access_enabled:
        return False
    async with session_scope() as session:
        return await operator_is_eligible(session, ctx.user_id)


def _require_bootstrap_admin(ctx: AuthContext) -> None:
    _require_feature()
    bootstrap_ids = getattr(
        config,
        "privileged_access_bootstrap_admin_ids",
        config.privileged_support_operator_ids,
    )
    if str(ctx.user_id) not in bootstrap_ids:
        raise HTTPException(404, "privileged_access_not_found")


def _eligibility_descriptor(row: PlatformAdminEligibility) -> dict:
    return {
        "eligibility_id": str(row.eligibility_id),
        "platform_user_id": str(row.platform_user_id),
        "role": row.role,
        "status": row.status,
        "granted_by_user_id": str(row.granted_by_user_id),
        "reviewed_by_user_id": str(row.reviewed_by_user_id),
        "reviewed_at": row.reviewed_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def _eligibility_audit(
    *,
    request: Request,
    ctx: AuthContext,
    row: PlatformAdminEligibility,
    action: str,
    operation: str,
) -> None:
    await record_auth_audit(
        action=action,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        tenant_id=ctx.active_organization_id,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={
            "target_platform_user_id": str(row.platform_user_id),
            "eligibility_id": str(row.eligibility_id),
            "role": row.role,
            "status": row.status,
            "operation": operation,
            "expires_at": row.expires_at.isoformat(),
        },
    )


@router.get("/eligibilities")
async def list_platform_eligibilities(
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    _require_bootstrap_admin(ctx)
    async with session_scope() as session:
        rows = list((await session.execute(
            select(PlatformAdminEligibility).order_by(
                PlatformAdminEligibility.created_at,
                PlatformAdminEligibility.eligibility_id,
            )
        )).scalars())
        for row in rows:
            if row.status == "active" and row.expires_at <= _now():
                row.status = "expired"
                row.updated_at = _now()
        await session.flush()
        result = [_eligibility_descriptor(row) for row in rows]
    return {"items": result}


@router.put("/eligibilities/{platform_user_id}")
async def grant_platform_eligibility(
    platform_user_id: uuid.UUID,
    body: PlatformEligibilityIn,
    request: Request,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    _require_bootstrap_admin(ctx)
    if platform_user_id == uuid.UUID(ctx.user_id):
        raise HTTPException(409, "privileged_eligibility_self_review_forbidden")
    now = _now()
    async with session_scope() as session:
        user = await session.get(User, platform_user_id)
        if user is None or user.status != "active":
            raise HTTPException(404, "platform_user_not_found")
        row = (await session.execute(
            select(PlatformAdminEligibility)
            .where(PlatformAdminEligibility.platform_user_id == platform_user_id)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            row = PlatformAdminEligibility(
                platform_user_id=platform_user_id,
                role=body.role,
                status="active",
                granted_by_user_id=uuid.UUID(ctx.user_id),
                reviewed_by_user_id=uuid.UUID(ctx.user_id),
                reviewed_at=now,
                expires_at=now + timedelta(days=body.review_ttl_days),
            )
            session.add(row)
        else:
            row.role = body.role
            row.status = "active"
            row.granted_by_user_id = uuid.UUID(ctx.user_id)
            row.reviewed_by_user_id = uuid.UUID(ctx.user_id)
            row.reviewed_at = now
            row.expires_at = now + timedelta(days=body.review_ttl_days)
            row.revoked_at = None
            row.updated_at = now
        await session.flush()
        result = _eligibility_descriptor(row)
    await _eligibility_audit(
        request=request,
        ctx=ctx,
        row=row,
        action=audit_actions.PRIVILEGED_ELIGIBILITY_CHANGE,
        operation="grant",
    )
    return result


@router.post("/eligibilities/{platform_user_id}/review")
async def review_platform_eligibility(
    platform_user_id: uuid.UUID,
    body: PlatformEligibilityReviewIn,
    request: Request,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    _require_bootstrap_admin(ctx)
    if platform_user_id == uuid.UUID(ctx.user_id):
        raise HTTPException(409, "privileged_eligibility_self_review_forbidden")
    now = _now()
    async with session_scope() as session:
        row = (await session.execute(
            select(PlatformAdminEligibility)
            .where(PlatformAdminEligibility.platform_user_id == platform_user_id)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "privileged_eligibility_not_found")
        if row.status == "revoked":
            raise HTTPException(409, "privileged_eligibility_revoked")
        row.status = "active"
        row.reviewed_by_user_id = uuid.UUID(ctx.user_id)
        row.reviewed_at = now
        row.expires_at = now + timedelta(days=body.review_ttl_days)
        row.updated_at = now
        await session.flush()
        result = _eligibility_descriptor(row)
    await _eligibility_audit(
        request=request,
        ctx=ctx,
        row=row,
        action=audit_actions.PRIVILEGED_ELIGIBILITY_REVIEW,
        operation="review",
    )
    return result


@router.delete("/eligibilities/{platform_user_id}", status_code=200)
async def revoke_platform_eligibility(
    platform_user_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    _require_bootstrap_admin(ctx)
    now = _now()
    async with session_scope() as session:
        row = (await session.execute(
            select(PlatformAdminEligibility)
            .where(PlatformAdminEligibility.platform_user_id == platform_user_id)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "privileged_eligibility_not_found")
        row.status = "revoked"
        row.revoked_at = now
        row.updated_at = now
        await session.execute(
            delete(Session).where(
                Session.user_id == platform_user_id,
                Session.audience == "support",
            )
        )
        await session.flush()
        result = _eligibility_descriptor(row)
    await _eligibility_audit(
        request=request,
        ctx=ctx,
        row=row,
        action=audit_actions.PRIVILEGED_ELIGIBILITY_CHANGE,
        operation="revoke",
    )
    return result


def _descriptor(row: PrivilegedAccessRequest) -> dict:
    return {
        "request_id": str(row.request_id),
        "organization_id": str(row.tenant_id),
        "operator_user_id": str(row.operator_user_id),
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "actions": list(row.allowed_actions),
        "duration_seconds": row.requested_duration_seconds,
        "status": row.status,
        "approved_by_user_id": (
            str(row.approved_by_user_id)
            if row.approved_by_user_id is not None
            else None
        ),
        "request_expires_at": row.request_expires_at,
        "active_expires_at": row.active_expires_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def _audit(
    session,
    *,
    request: Request,
    ctx: AuthContext,
    row: PrivilegedAccessRequest,
    action: str,
    outcome: str = "success",
) -> None:
    await record_audit(
        session,
        action=action,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        target_type=audit_actions.TARGET_PRIVILEGED_ACCESS,
        target_id=str(row.request_id),
        outcome=outcome,
        audit_ctx=extract_request_audit_context(request),
        meta={
            "operator_user_id": str(row.operator_user_id),
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "actions": list(row.allowed_actions),
            "duration_seconds": row.requested_duration_seconds,
        },
    )


async def _notify_customer_owners(
    session,
    *,
    request: Request,
    ctx: AuthContext,
    row: PrivilegedAccessRequest,
    event: str,
) -> None:
    """Deliver a content-free security notice to every active org owner.

    Activation is fail-closed if the customer cannot be notified. This keeps
    the notification part of the same security decision instead of treating
    it as best-effort application telemetry. Justification and ticket text are
    intentionally excluded from email and audit payloads.
    """
    owner_rows = (
        await session.execute(
            select(User)
            .join(OrgMembership, OrgMembership.user_id == User.user_id)
            .where(
                OrgMembership.tenant_id == row.tenant_id,
                OrgMembership.org_role == "owner",
                OrgMembership.status == "active",
                User.status == "active",
            )
            .order_by(User.user_id)
        )
    ).scalars().all()
    recipients: list[str] = []
    for owner in owner_rows:
        profile = await decrypt_user_profile(session, owner)
        if profile.email:
            recipients.append(profile.email)
    if not recipients:
        raise HTTPException(409, "privileged_access_owner_notification_unavailable")

    resource_scope = (
        f"{row.resource_type}:{row.resource_id}"
        if row.resource_type and row.resource_id
        else "organization metadata"
    )
    subject = f"Flowork privileged support {event}"
    body = (
        "A time-bounded privileged support session was "
        f"{event} for your organization.\n"
        f"Request: {row.request_id}\n"
        f"Scope: {resource_scope}\n"
        f"Actions: {', '.join(row.allowed_actions)}\n"
        f"Operator: {row.operator_user_id}\n"
        f"Approver: {row.approved_by_user_id}\n"
        f"Expires: {row.active_expires_at.isoformat() if row.active_expires_at else 'n/a'}\n"
        "No customer secret or content is included in this notice."
    )
    sender = get_email_sender()
    try:
        for recipient in recipients:
            await asyncio.to_thread(sender.send, recipient, subject, body)
    except Exception as exc:
        raise HTTPException(
            503,
            "privileged_access_owner_notification_failed",
        ) from exc
    await _audit(
        session,
        request=request,
        ctx=ctx,
        row=row,
        action=audit_actions.PRIVILEGED_ACCESS_NOTIFY_OWNER,
    )


@router.post("/organizations/{organization_id}/requests", status_code=201)
async def create_privileged_access_request(
    organization_id: uuid.UUID,
    body: PrivilegedAccessRequestIn,
    request: Request,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    await _require_operator(ctx)
    request_id = uuid.uuid4()
    now = _now()
    async with session_scope(tenant_id=str(organization_id)) as session:
        if await session.get(Organization, organization_id) is None:
            raise HTTPException(404, "organization_not_found")
        encrypted = await encrypt_request_private_payload(
            session,
            request_id=request_id,
            organization_id=organization_id,
            justification=body.justification,
            ticket_reference=body.ticket_reference,
        )
        row = PrivilegedAccessRequest(
            request_id=request_id,
            tenant_id=organization_id,
            operator_user_id=uuid.UUID(ctx.user_id),
            requested_session_id=uuid.UUID(ctx.session_id),
            resource_type=(
                body.resource_type.value if body.resource_type is not None else None
            ),
            resource_id=body.resource_id,
            allowed_actions=sorted({action.value for action in body.actions}),
            requested_duration_seconds=body.duration_seconds,
            private_ciphertext=encrypted.ciphertext,
            private_nonce=encrypted.nonce,
            private_key_id=encrypted.key_id,
            request_expires_at=now + _REQUEST_TTL,
        )
        session.add(row)
        await session.flush()
        await _audit(
            session,
            request=request,
            ctx=ctx,
            row=row,
            action=audit_actions.PRIVILEGED_ACCESS_REQUEST,
        )
        result = _descriptor(row)
    return result


@router.get("/organizations/{organization_id}/requests")
async def list_privileged_access_requests(
    organization_id: uuid.UUID,
    ctx: AuthContext = Depends(current_user),
) -> dict:
    await _require_operator(ctx)
    async with session_scope(tenant_id=str(organization_id)) as session:
        rows = list((await session.execute(
            select(PrivilegedAccessRequest)
            .where(PrivilegedAccessRequest.tenant_id == organization_id)
            .order_by(
                PrivilegedAccessRequest.created_at.desc(),
                PrivilegedAccessRequest.request_id.desc(),
            )
            .limit(100)
        )).scalars())
    return {"items": [_descriptor(row) for row in rows]}


@router.get("/organizations/{organization_id}/requests/{request_id}")
async def get_privileged_access_request(
    organization_id: uuid.UUID,
    request_id: uuid.UUID,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    await _require_operator(ctx)
    async with session_scope(tenant_id=str(organization_id)) as session:
        row = await session.get(PrivilegedAccessRequest, request_id)
        if row is None or row.tenant_id != organization_id:
            raise HTTPException(404, "privileged_access_not_found")
        private = await decrypt_request_private_payload(session, row)
        result = _descriptor(row)
        result["justification"] = private["justification"]
        result["ticket_reference"] = private["ticket_reference"]
    return result


@router.post("/organizations/{organization_id}/requests/{request_id}/approve")
async def approve_privileged_access_request(
    organization_id: uuid.UUID,
    request_id: uuid.UUID,
    body: PrivilegedApprovalIn,
    request: Request,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    await _require_operator(ctx)
    now = _now()
    async with session_scope(tenant_id=str(organization_id)) as session:
        row = (await session.execute(
            select(PrivilegedAccessRequest)
            .where(
                PrivilegedAccessRequest.request_id == request_id,
                PrivilegedAccessRequest.tenant_id == organization_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "privileged_access_not_found")
        if row.operator_user_id == uuid.UUID(ctx.user_id):
            raise HTTPException(409, "privileged_access_self_approval_forbidden")
        if row.status != "requested" or row.request_expires_at <= now:
            raise HTTPException(409, "privileged_access_request_not_approvable")
        scoped_actions = {Action(value) for value in row.allowed_actions}
        if scoped_actions & SENSITIVE_ACTIONS and not body.sensitive_scope_confirmed:
            raise HTTPException(
                409,
                "privileged_access_sensitive_scope_confirmation_required",
            )
        row.status = "approved"
        row.approved_by_user_id = uuid.UUID(ctx.user_id)
        row.approved_at = now
        row.updated_at = now
        await _audit(
            session,
            request=request,
            ctx=ctx,
            row=row,
            action=audit_actions.PRIVILEGED_ACCESS_APPROVE,
        )
        result = _descriptor(row)
    return result


@router.post("/organizations/{organization_id}/requests/{request_id}/deny")
async def deny_privileged_access_request(
    organization_id: uuid.UUID,
    request_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    await _require_operator(ctx)
    async with session_scope(tenant_id=str(organization_id)) as session:
        row = (await session.execute(
            select(PrivilegedAccessRequest)
            .where(
                PrivilegedAccessRequest.request_id == request_id,
                PrivilegedAccessRequest.tenant_id == organization_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "privileged_access_not_found")
        if row.operator_user_id == uuid.UUID(ctx.user_id):
            raise HTTPException(409, "privileged_access_self_approval_forbidden")
        if row.status != "requested":
            raise HTTPException(409, "privileged_access_request_not_deniable")
        row.status = "denied"
        row.approved_by_user_id = uuid.UUID(ctx.user_id)
        row.approved_at = _now()
        row.updated_at = _now()
        await _audit(
            session,
            request=request,
            ctx=ctx,
            row=row,
            action=audit_actions.PRIVILEGED_ACCESS_DENY,
        )
        result = _descriptor(row)
    return result


@router.post("/organizations/{organization_id}/requests/{request_id}/activate")
async def activate_privileged_access_request(
    organization_id: uuid.UUID,
    request_id: uuid.UUID,
    response: Response,
    request: Request,
    ctx: AuthContext = Depends(require_webauthn_step_up),
) -> dict:
    await _require_operator(ctx)
    now = _now()
    raw_session, token_hash = new_token()
    raw_csrf, csrf_hash = new_token()
    async with session_scope(tenant_id=str(organization_id)) as session:
        row = (await session.execute(
            select(PrivilegedAccessRequest)
            .where(
                PrivilegedAccessRequest.request_id == request_id,
                PrivilegedAccessRequest.tenant_id == organization_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "privileged_access_not_found")
        if (
            row.operator_user_id != uuid.UUID(ctx.user_id)
            or row.requested_session_id != uuid.UUID(ctx.session_id)
        ):
            raise HTTPException(404, "privileged_access_not_found")
        if (
            row.status != "approved"
            or row.request_expires_at <= now
            or row.approved_by_user_id is None
            or row.approved_by_user_id == row.operator_user_id
        ):
            raise HTTPException(409, "privileged_access_request_not_activatable")
        expires_at = now + timedelta(seconds=row.requested_duration_seconds)
        session_row = await AuthRepo(session).create_session(
            token_hash,
            row.operator_user_id,
            organization_id,
            expires_at,
            audience="support",
            parent_session_id=uuid.UUID(ctx.session_id),
            csrf_token_hash=csrf_hash,
            active_organization_id=organization_id,
            authentication_strength="webauthn",
            step_up_expires_at=expires_at,
            privileged_access_request_id=row.request_id,
        )
        row.status = "active"
        row.activated_session_id = session_row.session_id
        row.active_expires_at = expires_at
        row.updated_at = now
        await session.flush()
        await _audit(
            session,
            request=request,
            ctx=ctx,
            row=row,
            action=audit_actions.PRIVILEGED_ACCESS_ACTIVATE,
        )
        await _notify_customer_owners(
            session,
            request=request,
            ctx=ctx,
            row=row,
            event="activated",
        )
        result = _descriptor(row)
    set_session_cookies(
        response,
        audience="support",
        raw_session=raw_session,
        raw_csrf=raw_csrf,
        max_age=max(1, int((expires_at - now).total_seconds())),
    )
    return result


@router.post("/organizations/{organization_id}/requests/{request_id}/revoke")
async def revoke_privileged_access_request(
    organization_id: uuid.UUID,
    request_id: uuid.UUID,
    response: Response,
    request: Request,
    ctx: AuthContext = Depends(current_user),
) -> dict:
    _require_feature()
    if ctx.session_audience == "web":
        await _require_operator(ctx)
        await require_webauthn_step_up(ctx)
    elif (
        ctx.session_audience != "support"
        or ctx.privileged_access_request_id != str(request_id)
    ):
        raise HTTPException(404, "privileged_access_not_found")
    async with session_scope(tenant_id=str(organization_id)) as session:
        row = (await session.execute(
            select(PrivilegedAccessRequest)
            .where(
                PrivilegedAccessRequest.request_id == request_id,
                PrivilegedAccessRequest.tenant_id == organization_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        if row is None or uuid.UUID(ctx.user_id) not in {
            row.operator_user_id,
            row.approved_by_user_id,
        }:
            raise HTTPException(404, "privileged_access_not_found")
        if row.status in {"revoked", "denied", "expired"}:
            raise HTTPException(409, "privileged_access_request_not_revocable")
        row.status = "revoked"
        row.revoked_by_user_id = uuid.UUID(ctx.user_id)
        row.revoked_at = _now()
        row.updated_at = _now()
        if row.activated_session_id is not None:
            await AuthRepo(session).delete_session_by_id(
                session_id=row.activated_session_id,
                user_id=row.operator_user_id,
            )
        await _audit(
            session,
            request=request,
            ctx=ctx,
            row=row,
            action=audit_actions.PRIVILEGED_ACCESS_REVOKE,
        )
        result = _descriptor(row)
    clear_session_cookie(response, audience="support")
    return result


@router.get("/current")
async def current_privileged_access(
    ctx: AuthContext = Depends(current_user),
) -> dict:
    return {
        "enabled": config.privileged_access_enabled,
        "eligible_operator": await _current_operator_eligibility(ctx),
        "active": bool(ctx.privileged_access_request_id),
        "request_id": ctx.privileged_access_request_id or None,
        "organization_id": (
            ctx.active_organization_id
            if ctx.privileged_access_request_id
            else None
        ),
        "resource_type": ctx.privileged_resource_type or None,
        "resource_id": ctx.privileged_resource_id or None,
        "actions": sorted(ctx.privileged_actions),
        "expires_at": ctx.privileged_expires_at,
    }


@router.get("/status")
async def privileged_access_cookie_status(
    request: Request,
    response: Response,
) -> dict:
    """Probe only the ambient support cookie and clear it when inactive.

    This endpoint deliberately does not fall back to the parent Web Session.
    A remotely revoked support capability must disappear without converting
    the same business request into broader ordinary-user authority.
    """
    credential = cookie_credential(request)
    if credential is None or credential.audience != "support":
        return {"active": False}
    async with session_scope() as session:
        session_row = (
            await session.execute(
                select(Session).where(
                    Session.token_hash == hash_token(credential.raw_session),
                    Session.audience == "support",
                )
            )
        ).scalar_one_or_none()
        active = None
        if session_row is not None:
            from vibecanvas_api.auth.privileged_access import (
                resolve_active_privileged_access,
            )

            active = await resolve_active_privileged_access(
                session,
                session_row,
            )
    if active is None:
        clear_session_cookie(response, audience="support")
        return {"active": False}
    response.headers["Cache-Control"] = "no-store"
    return {
        "active": True,
        "request_id": active.request_id,
        "expires_at": active.expires_at,
    }
