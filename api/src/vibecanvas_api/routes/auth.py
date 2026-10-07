"""Auth endpoints — register / login / logout / me / password reset.
All operate on RLS-free auth tables; no tenant context needed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError

from vibecanvas_api.auth.deps import (
    AuthContext,
    current_user,
    require_recent_step_up,
)
from vibecanvas_api.auth.email_sender import get_email_sender
from vibecanvas_api.auth.password import hash_password, verify_password
from vibecanvas_api.auth.privileged_access import platform_role_for_user
from vibecanvas_api.auth.ratelimit import (
    LoginRateLimitExceeded,
    LoginRateLimitUnavailable,
    begin_login_attempt,
)
from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.auth.session_security import (
    clear_session_cookies,
    cookie_credential,
    set_session_cookies,
    validate_browser_origin,
)
from vibecanvas_api.auth.tokens import hash_token, new_token
from vibecanvas_api.audit import actions
from vibecanvas_api.audit.context import extract_request_audit_context
from vibecanvas_api.audit.service import record_auth_audit
from vibecanvas_api.authorization.dependencies import (
    mutation_coordinator_for_request,
)
from vibecanvas_api.authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    organization_membership_edges,
)
from vibecanvas_api.config import config as app_config
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.sync_session import short_admin_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_SESSION_TTL = timedelta(days=30)
_RESET_TTL = timedelta(minutes=30)
_EXTENSION_EXCHANGE_TTL = timedelta(seconds=60)
_TEST_LOGIN = "test"
_TEST_PASSWORD = "test"
_TEST_EMAIL = "test@test.local"


class RegisterIn(BaseModel):
    email: EmailStr
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=1024)


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class DeleteAccountIn(BaseModel):
    email: EmailStr


class CancelDeleteAccountIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class ExtensionExchangeIn(BaseModel):
    code: str = Field(min_length=32, max_length=512)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _login_email(raw: str) -> str:
    value = raw.strip()
    if app_config.enable_test_user and value.lower() == _TEST_LOGIN:
        return _TEST_EMAIL
    return value


def _is_test_login(raw_email: str, password: str) -> bool:
    return (
        app_config.enable_test_user
        and raw_email.strip().lower() == _TEST_LOGIN
        and password == _TEST_PASSWORD
    )


async def _consume_unauthenticated_rate_limit(key: str) -> None:
    """Consume one rate-limit slot regardless of endpoint outcome.

    Registration and reset requests are resource-consuming even when they
    succeed, so unlike login they must not erase their own provisional slot.
    """
    try:
        attempt = await begin_login_attempt(key)
    except LoginRateLimitExceeded:
        raise HTTPException(429, "Too many attempts; try again later") from None
    except LoginRateLimitUnavailable:
        raise HTTPException(503, "Authentication security service is temporarily unavailable") from None
    await attempt.failure()


def _session_audience(request: Request) -> str:
    requested = request.headers.get(
        "X-VibeCanvas-Session-Audience",
        "web",
    ).strip().lower()
    if requested == "extension" and app_config.extension_scoped_token_enabled:
        return "extension"
    return "web"


def _auth_response(
    *,
    response: Response,
    raw_session: str,
    raw_csrf: str,
    session_row,
    user,
) -> dict:
    if app_config.web_session_cookie_enabled:
        set_session_cookies(
            response,
            audience=str(session_row.audience),
            raw_session=raw_session,
            raw_csrf=raw_csrf,
            max_age=int(_SESSION_TTL.total_seconds()),
        )
    payload = {
        "session": {
            "session_id": str(session_row.session_id),
            "active_organization_id": str(
                session_row.active_organization_id
            ),
            "generation": int(session_row.generation),
            "authentication_strength": session_row.authentication_strength,
            "step_up_expires_at": session_row.step_up_expires_at,
            "audience": session_row.audience,
        },
        "user": {
            "user_id": str(user.user_id),
            "email": user.email,
            "display_name": user.display_name,
        },
    }
    # Legacy bearer is retained only while the secure-cookie feature is off.
    # In cookie mode browser JavaScript never receives the Session credential.
    if not app_config.web_session_cookie_enabled:
        payload["session_token"] = raw_session
    return payload


async def _create_authenticated_session(
    repo: AuthRepo,
    *,
    user,
    audience: str,
    strength: str,
    step_up_ttl: timedelta | None = None,
):
    raw, hashed = new_token()
    raw_csrf, csrf_hash = new_token()
    session_row = await repo.create_session(
        hashed,
        user.user_id,
        user.tenant_id,
        _now() + _SESSION_TTL,
        audience=audience,
        csrf_token_hash=csrf_hash,
        authentication_strength=strength,
    )
    if step_up_ttl is not None:
        session_row.step_up_expires_at = _now() + step_up_ttl
    return raw, raw_csrf, session_row


async def _ensure_test_user(repo: AuthRepo):
    identity = await repo.find_identity("password", _TEST_EMAIL)
    if identity is None:
        return (
            await repo.register(
                _TEST_EMAIL,
                hash_password(_TEST_PASSWORD),
                display_name="test",
            ),
            True,
        )
    user = await repo.get_user(identity.user_id)
    if identity.secret is None or not verify_password(_TEST_PASSWORD, identity.secret):
        await repo.update_password(identity.user_id, hash_password(_TEST_PASSWORD))
    if user is not None and user.status != "active":
        await repo.set_user_status(user.user_id, "active")
        user.status = "active"
    return user, False


@router.post("/register", status_code=201)
async def register(
    body: RegisterIn,
    request: Request,
    response: Response,
):
    if app_config.web_session_cookie_enabled:
        validate_browser_origin(request)
    actx = extract_request_audit_context(request)
    await _consume_unauthenticated_rate_limit(
        f"register:{actx.ip_address or 'unknown'}"
    )
    mutation_ids = ()
    try:
        async with session_scope() as s:
            repo = AuthRepo(s)
            if await repo.find_identity("password", body.email):
                raise HTTPException(409, "This email address is already registered")
            user = await repo.register(
                body.email,
                hash_password(body.password),
                display_name=body.username.strip(),
            )
            raw, hashed = new_token()
            raw_csrf, csrf_hash = new_token()
            session_row = await repo.create_session(
                hashed,
                user.user_id,
                user.tenant_id,
                _now() + _SESSION_TTL,
                audience=_session_audience(request),
                csrf_token_hash=csrf_hash,
            )
            coordinator = mutation_coordinator_for_request(
                request,
                str(user.tenant_id),
            )
            mutation_ids = await enqueue_structural_delta(
                session=s,
                coordinator=coordinator,
                actor_type="user",
                actor_id=str(user.user_id),
                before=frozenset(),
                after=organization_membership_edges(
                    organization_id=str(user.tenant_id),
                    user_id=str(user.user_id),
                    role="owner",
                    status="active",
                ),
                operation_id=str(session_row.session_id),
                source="personal-organization-register",
            )
    except IntegrityError:
        # Concurrent duplicate registration (e.g. a double-clicked
        # submit): the find_identity check above lost the race. The
        # The keyed auth-identity lookup UNIQUE constraint is the source of
        # truth — surface the same clean 409, not a 500.
        raise HTTPException(409, "This email address is already registered")
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    # success path only (the session_scope commit succeeded). NEVER body/password.
    await record_auth_audit(
        action=actions.AUTH_REGISTER, actor_user_id=user.user_id,
        actor_email=user.email, tenant_id=user.tenant_id, outcome="success",
        audit_ctx=actx, meta={})
    return _auth_response(
        response=response,
        raw_session=raw,
        raw_csrf=raw_csrf,
        session_row=session_row,
        user=user,
    )


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response):
    if app_config.web_session_cookie_enabled:
        validate_browser_origin(request)
    actx = extract_request_audit_context(request)
    client_host = request.client.host if request.client else "unknown"
    email = _login_email(body.email)
    key = f"{client_host}:{email}"
    mutation_ids = ()
    coordinator = None
    try:
        rate_limit_attempt = await begin_login_attempt(key)
    except LoginRateLimitExceeded:
        raise HTTPException(429, "Too many attempts; try again later")
    except LoginRateLimitUnavailable:
        raise HTTPException(503, "Authentication security service is temporarily unavailable")
    async with session_scope() as s:
        repo = AuthRepo(s)
        if _is_test_login(body.email, body.password):
            try:
                test_user, created = await _ensure_test_user(repo)
                if created:
                    coordinator = mutation_coordinator_for_request(
                        request,
                        str(test_user.tenant_id),
                    )
                    mutation_ids = await enqueue_structural_delta(
                        session=s,
                        coordinator=coordinator,
                        actor_type="system",
                        actor_id="development-test-user",
                        before=frozenset(),
                        after=organization_membership_edges(
                            organization_id=str(test_user.tenant_id),
                            user_id=str(test_user.user_id),
                            role="owner",
                            status="active",
                        ),
                        operation_id=uuid.uuid4().hex,
                        source="development-test-user-create",
                    )
            except IntegrityError:
                await s.rollback()
                repo = AuthRepo(s)
        identity = await repo.find_identity("password", email)
        ok = identity is not None and identity.secret is not None and \
            verify_password(body.password, identity.secret)
        if not ok:
            await rate_limit_attempt.failure()
            # Resolve tenant only when the user exists (known-user bad-password
            # carries a tenant; unknown email → actor/tenant stay NULL).
            uid = identity.user_id if identity else None
            tid = None
            if identity:
                u = await repo.get_user(identity.user_id)
                tid = u.tenant_id if u else None
            await record_auth_audit(
                action=actions.AUTH_LOGIN_FAILURE, actor_user_id=uid,
                actor_email=email, tenant_id=tid, outcome="failure",
                audit_ctx=actx, meta={})   # NEVER body/password
            raise HTTPException(401, "Invalid email or password")  # generic
        try:
            await rate_limit_attempt.success()
        except LoginRateLimitUnavailable:
            raise HTTPException(503, "Authentication security service is temporarily unavailable")
        user = await repo.get_user(identity.user_id)
        if user and user.status == "pending_deletion":
            raise HTTPException(
                status.HTTP_423_LOCKED,
                "Account deletion is pending; revoke the deletion request first",
            )
        raw, raw_csrf, session_row = await _create_authenticated_session(
            repo,
            user=user,
            audience=_session_audience(request),
            strength="password",
        )
    if coordinator is not None:
        await apply_committed_structural_mutations(
            coordinator,
            mutation_ids,
        )
    # Fired AFTER the session_scope commit succeeded. NEVER body/password/raw.
    await record_auth_audit(
        action=actions.AUTH_LOGIN_SUCCESS, actor_user_id=user.user_id,
        actor_email=user.email, tenant_id=user.tenant_id, outcome="success",
        audit_ctx=actx, meta={})
    return _auth_response(
        response=response,
        raw_session=raw,
        raw_csrf=raw_csrf,
        session_row=session_row,
        user=user,
    )


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    actx = extract_request_audit_context(request)
    uid = tid = email = None
    credential = (
        cookie_credential(request)
        if app_config.web_session_cookie_enabled
        else None
    )
    auth = request.headers.get("Authorization", "")
    raw = credential.raw_session if credential else (
        auth[7:] if auth.startswith("Bearer ") else ""
    )
    if raw:
        token_hash = hash_token(raw)
        async with session_scope() as s:
            repo = AuthRepo(s)
            # Resolve the actor while the session row still exists; an
            # already-invalid token leaves actor/tenant NULL (still a valid row).
            sess = await repo.resolve_session(token_hash)
            if sess:
                if credential is not None:
                    from vibecanvas_api.auth.session_security import (
                        validate_cookie_request,
                    )
                    validate_cookie_request(
                        request,
                        credential=credential,
                        stored_csrf_hash=sess.csrf_token_hash,
                    )
                uid, tid = sess.user_id, sess.tenant_id
                u = await repo.get_user(sess.user_id)
                email = u.email if u else None
                await repo.delete_session_by_id(
                    session_id=sess.session_id,
                    user_id=sess.user_id,
                )
            else:
                await repo.delete_session(token_hash)
    if app_config.web_session_cookie_enabled:
        clear_session_cookies(response)
    await record_auth_audit(
        action=actions.AUTH_LOGOUT, actor_user_id=uid, actor_email=email,
        tenant_id=tid, outcome="success", audit_ctx=actx, meta={})


@router.get("/sessions")
async def list_sessions(
    request: Request,
    ctx: AuthContext = Depends(current_user),
) -> dict:
    async with session_scope() as session:
        rows = await AuthRepo(session).list_user_sessions(
            uuid.UUID(ctx.user_id)
        )
    result = {
        "items": [
            {
                "session_id": str(item.session_id),
                "audience": item.audience,
                "active_organization_id": str(item.active_organization_id),
                "authentication_strength": item.authentication_strength,
                "generation": int(item.generation),
                "created_at": item.created_at,
                "last_seen_at": item.last_seen_at,
                "expires_at": item.expires_at,
                "current": str(item.session_id) == ctx.session_id,
            }
            for item in rows
        ]
    }
    await record_auth_audit(
        action=actions.AUTH_SESSION_LIST,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        tenant_id=ctx.tenant_id,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"session_count": len(rows)},
    )
    return result


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID,
    response: Response,
    request: Request,
    ctx: AuthContext = Depends(current_user),
):
    async with session_scope() as session:
        deleted = await AuthRepo(session).delete_session_by_id(
            session_id=session_id,
            user_id=uuid.UUID(ctx.user_id),
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="session_not_found")
    if str(session_id) == ctx.session_id:
        clear_session_cookies(response)
    await record_auth_audit(
        action=actions.AUTH_SESSION_REVOKE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        tenant_id=ctx.tenant_id,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"revoked_session_id": str(session_id)},
    )


@router.post("/sessions/current/rotate")
async def rotate_current_session(
    response: Response,
    request: Request,
    ctx: AuthContext = Depends(current_user),
) -> dict:
    if not app_config.web_session_cookie_enabled:
        raise HTTPException(status_code=404, detail="cookie_session_disabled")
    raw, token_hash = new_token()
    raw_csrf, csrf_hash = new_token()
    async with session_scope() as session:
        rotated = await AuthRepo(session).rotate_session_token(
            session_id=uuid.UUID(ctx.session_id),
            user_id=uuid.UUID(ctx.user_id),
            token_hash=token_hash,
            csrf_token_hash=csrf_hash,
        )
    if rotated is None:  # pragma: no cover - authenticated row disappeared
        raise HTTPException(401, "missing or invalid session")
    set_session_cookies(
        response,
        audience=rotated.audience,
        raw_session=raw,
        raw_csrf=raw_csrf,
        max_age=max(1, int((rotated.expires_at - _now()).total_seconds())),
    )
    await record_auth_audit(
        action=actions.AUTH_SESSION_ROTATE,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        tenant_id=ctx.tenant_id,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={
            "session_id": str(rotated.session_id),
            "generation": int(rotated.generation),
        },
    )
    return {
        "session_id": str(rotated.session_id),
        "generation": int(rotated.generation),
    }


@router.post("/extension/exchange-code")
async def create_extension_exchange_code(
    ctx: AuthContext = Depends(current_user),
) -> dict:
    if (
        not app_config.web_session_cookie_enabled
        or not app_config.extension_scoped_token_enabled
    ):
        raise HTTPException(status_code=404, detail="extension_exchange_disabled")
    if ctx.session_audience != "web":
        raise HTTPException(status_code=403, detail="primary_web_session_required")
    raw, code_hash = new_token()
    async with session_scope() as session:
        await AuthRepo(session).create_session_exchange_code(
            code_hash=code_hash,
            parent_session_id=uuid.UUID(ctx.session_id),
            user_id=uuid.UUID(ctx.user_id),
            tenant_id=uuid.UUID(ctx.active_organization_id),
            expires_at=_now() + _EXTENSION_EXCHANGE_TTL,
        )
    return {"code": raw, "expires_in": int(_EXTENSION_EXCHANGE_TTL.total_seconds())}


@router.post("/extension/exchange")
async def exchange_extension_session(
    body: ExtensionExchangeIn,
    request: Request,
    response: Response,
) -> dict:
    if (
        not app_config.web_session_cookie_enabled
        or not app_config.extension_scoped_token_enabled
    ):
        raise HTTPException(status_code=404, detail="extension_exchange_disabled")
    validate_browser_origin(request)
    raw_session, session_hash = new_token()
    raw_csrf, csrf_hash = new_token()
    async with session_scope() as session:
        consumed = await AuthRepo(session).consume_session_exchange_code(
            hash_token(body.code)
        )
        if consumed is None:
            raise HTTPException(400, "extension_exchange_invalid_or_expired")
        exchange, parent = consumed
        membership = await AuthRepo(session).get_membership(
            user_id=parent.user_id,
            organization_id=parent.active_organization_id,
        )
        if membership is None or membership.status != "active":
            raise HTTPException(403, "session organization membership is not active")
        expires_at = min(parent.expires_at, _now() + _SESSION_TTL)
        credential = cookie_credential(request)
        existing = (
            await AuthRepo(session).resolve_session(hash_token(credential.raw_session))
            if credential is not None and credential.audience == "extension"
            else None
        )
        if (
            existing is not None
            and existing.audience == "extension"
            and existing.parent_session_id == parent.session_id
            and existing.user_id == parent.user_id
            and existing.active_organization_id == parent.active_organization_id
            and existing.authentication_strength == parent.authentication_strength
            and existing.expires_at <= expires_at
            and credential.raw_csrf
            and hash_token(credential.raw_csrf) == existing.csrf_token_hash
        ):
            # All side panels in this browser share the partitioned cookie.
            # Consuming a fresh code must not rotate an already valid session
            # and disconnect another panel's in-flight browser commands.
            return {"ok": True}
        derived = await AuthRepo(session).create_session(
            session_hash,
            parent.user_id,
            parent.active_organization_id,
            expires_at,
            audience=str(exchange["audience"]),
            parent_session_id=parent.session_id,
            csrf_token_hash=csrf_hash,
            active_organization_id=parent.active_organization_id,
            authentication_strength=parent.authentication_strength,
        )
    set_session_cookies(
        response,
        audience="extension",
        raw_session=raw_session,
        raw_csrf=raw_csrf,
        max_age=max(1, int((derived.expires_at - _now()).total_seconds())),
    )
    return {"ok": True}


@router.get("/me")
async def me(ctx: AuthContext = Depends(current_user)):
    async with short_admin_session() as session:
        platform_management_role = await platform_role_for_user(session, ctx.user_id)
    return {
        "user_id": ctx.user_id,
        # Keep tenant_id during the physical-column migration; new clients use
        # the explicit organization field.
        "tenant_id": ctx.tenant_id,
        "active_organization_id": ctx.active_organization_id,
        "membership": {
            "membership_id": ctx.membership_id,
            "role": ctx.membership_role,
            "status": ctx.membership_status,
        },
        "session": {
            "session_id": ctx.session_id,
            "generation": ctx.session_generation,
            "authentication_strength": ctx.authentication_strength,
            "step_up_expires_at": (
                ctx.step_up_expires_at.isoformat()
                if ctx.step_up_expires_at is not None
                else None
            ),
            "audience": ctx.session_audience,
        },
        "privileged_access": {
            "active": bool(ctx.privileged_access_request_id),
            "request_id": ctx.privileged_access_request_id or None,
            "resource_type": ctx.privileged_resource_type or None,
            "resource_id": ctx.privileged_resource_id or None,
            "actions": sorted(ctx.privileged_actions),
            "expires_at": (
                ctx.privileged_expires_at.isoformat()
                if ctx.privileged_expires_at is not None
                else None
            ),
        },
        "email": ctx.email,
        "display_name": ctx.display_name,
        # This is the caller's own reviewed control-plane role. Projecting it
        # here lets the shell hide platform navigation without probing a
        # deliberately concealed endpoint and producing a 404 on every page.
        "platform_management_role": platform_management_role,
    }


@router.post("/delete-account", status_code=204)
async def delete_account(
    body: DeleteAccountIn,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    _step_up: AuthContext = Depends(require_recent_step_up),
):
    if body.email.lower() != ctx.email.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Enter the current account email to confirm deletion",
        )
    async with session_scope(tenant_id=ctx.tenant_id) as s:
        repo = AuthRepo(s)
        user = await repo.get_user(uuid.UUID(ctx.user_id))
        if user is None:  # authenticated row disappeared between requests
            raise HTTPException(401, "missing or invalid session")
        blocking = await repo.blocking_owned_organizations(
            user_id=user.user_id,
            personal_tenant_id=user.tenant_id,
        )
        if blocking:
            names = ", ".join(name for _tenant_id, name in blocking[:5])
            suffix = "" if len(blocking) <= 5 else f" and {len(blocking) - 5} more"
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Transfer ownership or delete these organizations before "
                f"deleting the account: {names}{suffix}",
            )
        deletion_mode = app_config.account_deletion_mode
        purge_after = _now()
        if deletion_mode == "delayed":
            purge_after += timedelta(
                days=app_config.account_deletion_retention_days
            )
        await repo.request_account_deletion(
            user_id=ctx.user_id,
            tenant_id=user.tenant_id,
            email=ctx.email,
            purge_after=purge_after,
            deletion_mode=deletion_mode,
        )
    await record_auth_audit(
        action=actions.AUTH_ACCOUNT_DELETE_REQUEST,
        actor_user_id=ctx.user_id,
        actor_email=ctx.email,
        tenant_id=ctx.tenant_id,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={"deletion_mode": deletion_mode},
    )


@router.post("/cancel-delete-account")
async def cancel_delete_account(body: CancelDeleteAccountIn, request: Request):
    user_id = tenant_id = None
    actor_email = str(body.email)
    async with session_scope() as s:
        repo = AuthRepo(s)
        identity = await repo.find_identity("password", body.email)
        ok = identity is not None and identity.secret is not None and \
            verify_password(body.password, identity.secret)
        if not ok:
            raise HTTPException(401, "Invalid email or password")
        user = await repo.get_user(identity.user_id)
        if user is None or user.status != "pending_deletion":
            raise HTTPException(400, "The account has no pending deletion request")
        user_id = user.user_id
        tenant_id = user.tenant_id
        actor_email = user.email
        cancelled = await repo.cancel_account_deletion(user.user_id)
        if not cancelled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This deletion request is immediate or has already started "
                "and cannot be cancelled",
            )
    await record_auth_audit(
        action=actions.AUTH_ACCOUNT_DELETE_CANCEL,
        actor_user_id=user_id,
        actor_email=actor_email,
        tenant_id=tenant_id,
        outcome="success",
        audit_ctx=extract_request_audit_context(request),
        meta={},
    )
    return {"ok": True}


class ResetReqIn(BaseModel):
    email: EmailStr


@router.post("/password-reset/request", status_code=200)
async def password_reset_request(body: ResetReqIn, request: Request):
    actx = extract_request_audit_context(request)
    await _consume_unauthenticated_rate_limit(
        f"password-reset:{actx.ip_address or 'unknown'}"
    )
    uid = tid = None
    async with session_scope() as s:
        repo = AuthRepo(s)
        identity = await repo.find_identity("password", body.email)
        if identity:
            uid = identity.user_id
            u = await repo.get_user(identity.user_id)
            tid = u.tenant_id if u else None
            raw, hashed = new_token()
            await repo.create_reset_token(hashed, identity.user_id,
                                          _now() + _RESET_TTL)
            get_email_sender().send(
                body.email,
                "Reset password",
                f"Reset token (valid for 30 minutes): {raw}",
            )
    # always recorded (request attempt); unknown email → actor/tenant NULL.
    # NEVER the reset token in meta.
    await record_auth_audit(
        action=actions.AUTH_PASSWORD_RESET_REQUEST, actor_user_id=uid,
        actor_email=body.email, tenant_id=tid, outcome="success",
        audit_ctx=actx, meta={})
    return {"ok": True}     # always 200 — no email enumeration


class ResetConfirmIn(BaseModel):
    reset_token: str = Field(min_length=32, max_length=512)
    new_password: str = Field(min_length=8, max_length=1024)


@router.post("/password-reset/confirm", status_code=200)
async def password_reset_confirm(body: ResetConfirmIn, request: Request):
    actx = extract_request_audit_context(request)
    async with session_scope() as s:
        repo = AuthRepo(s)
        t = await repo.consume_reset_token(hash_token(body.reset_token))
        if t is None:
            raise HTTPException(400, "Reset token is invalid or expired")
        await repo.update_password(t.user_id, hash_password(body.new_password))
        await repo.delete_user_sessions(t.user_id)   # force re-login
        u = await repo.get_user(t.user_id)
        uid = t.user_id
        email = u.email if u else None
        tid = u.tenant_id if u else None
    # success path only (commit succeeded). NEVER the token / new password.
    await record_auth_audit(
        action=actions.AUTH_PASSWORD_RESET_COMPLETE, actor_user_id=uid,
        actor_email=email, tenant_id=tid, outcome="success",
        audit_ctx=actx, meta={})
    return {"ok": True}
