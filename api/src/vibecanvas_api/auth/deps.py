"""Auth dependencies for HttpOnly browser and explicit API Sessions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.auth.tokens import hash_token
from vibecanvas_api.auth.session_security import (
    CookieCredential,
    cookie_credential,
    validate_cookie_request,
)
from vibecanvas_api.config import config
from vibecanvas_api.observability import context as obs_context
from vibecanvas_api.storage.db import session_scope

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    tenant_id: str
    email: str
    display_name: str = ""
    # Semantic organization identity projected from the Session record.
    active_organization_id: str = ""
    membership_id: str = ""
    membership_role: str = ""
    membership_status: str = ""
    session_generation: int = 1
    authentication_strength: str = "password"
    step_up_expires_at: datetime | None = None
    # Stable internal Session UUID, never the raw bearer or its stored hash.
    session_id: str = ""
    session_audience: str = "web"
    privileged_access_request_id: str = ""
    privileged_resource_type: str = ""
    privileged_resource_id: str = ""
    privileged_actions: frozenset[str] = frozenset()
    privileged_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.active_organization_id:
            object.__setattr__(self, "active_organization_id", self.tenant_id)


async def resolve_authenticated_user(
    raw_token: str,
    session: AsyncSession,
    *,
    request: Request | None = None,
    cookie: CookieCredential | None = None,
) -> AuthContext:
    """Resolve a raw Session credential and enforce its browser binding."""
    repo = AuthRepo(session)
    s = await repo.resolve_session(hash_token(raw_token))
    if s is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid session",
            headers={"WWW-Authenticate": "Bearer"})
    if cookie is not None:
        if s.audience != cookie.audience:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="session audience mismatch",
            )
        if request is None:  # pragma: no cover - caller contract
            raise RuntimeError("cookie Session validation requires Request")
        validate_cookie_request(
            request,
            credential=cookie,
            stored_csrf_hash=s.csrf_token_hash,
        )
    await repo.touch_session(s.token_hash)
    user = await repo.get_user(s.user_id)
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid session",
            headers={"WWW-Authenticate": "Bearer"})
    privileged = None
    if s.audience == "support":
        from vibecanvas_api.auth.privileged_access import (
            resolve_active_privileged_access,
        )

        privileged = await resolve_active_privileged_access(session, s)
        if privileged is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="privileged session is inactive or expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        membership_id = f"privileged:{privileged.request_id}"
        membership_role = "privileged_support"
        membership_status = "active"
    else:
        membership = await repo.get_membership(
            user_id=s.user_id,
            organization_id=s.active_organization_id,
        )
        if membership is None or membership.status != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="session organization membership is not active",
                headers={"WWW-Authenticate": "Bearer"},
            )
        membership_id = str(membership.membership_id)
        membership_role = membership.org_role
        membership_status = membership.status
    # `user` is always present: sessions.user_id is an ON DELETE CASCADE
    # FK to users, so a resolved session cannot outlive its user. The
    # `else ""` is belt-and-suspenders for that can't-happen case.
    # Bind the resolved tenant into the request-path logging context so every
    # subsequent log line in this request carries the real tenant_id (the
    # request-id middleware only had tenant_id=None this early). Fire-and-forget
    # and never allowed to break auth.
    obs_context.bind_tenant_id(str(s.active_organization_id))
    context = AuthContext(
        user_id=str(s.user_id),
        tenant_id=str(s.active_organization_id),
        email=user.email,
        display_name=user.display_name,
        active_organization_id=str(s.active_organization_id),
        membership_id=membership_id,
        membership_role=membership_role,
        membership_status=membership_status,
        session_generation=int(s.generation),
        authentication_strength=s.authentication_strength,
        step_up_expires_at=s.step_up_expires_at,
        session_id=str(s.session_id),
        session_audience=str(s.audience),
        privileged_access_request_id=(
            privileged.request_id if privileged is not None else ""
        ),
        privileged_resource_type=(
            privileged.resource_type or "" if privileged is not None else ""
        ),
        privileged_resource_id=(
            privileged.resource_id or "" if privileged is not None else ""
        ),
        privileged_actions=(
            frozenset(action.value for action in privileged.actions)
            if privileged is not None
            else frozenset()
        ),
        privileged_expires_at=(
            privileged.expires_at if privileged is not None else None
        ),
    )
    # Register, but do not synchronously scan, the optional self-hosted mount.
    # Its watcher knows only server-validated identity/organization pairs and
    # therefore never infers authorization from directory names.
    from vibecanvas_api.services.user_mount_workspace import host_mount_bridge

    host_mount_bridge.register(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )
    return context


async def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    """Resolve the primary browser cookie or an explicit non-cookie Session.

    Production cookie mode deliberately rejects raw Web Session bearer tokens:
    Platform MCP and browser WebSocket capabilities have separate verifiers,
    while the extension receives a derived HttpOnly cookie via one-time code.
    """
    cookie = cookie_credential(request) if config.web_session_cookie_enabled else None
    if cookie is not None:
        raw_token = cookie.raw_session
    elif creds is not None and not config.web_session_cookie_enabled:
        raw_token = creds.credentials
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    async with session_scope() as auth_sess:   # no tenant — auth tables
        return await resolve_authenticated_user(
            raw_token,
            auth_sess,
            request=request,
            cookie=cookie,
        )


async def require_recent_step_up(
    ctx: AuthContext = Depends(current_user),
) -> AuthContext:
    """Require an unexpired phishing-resistant high-risk step-up boundary.

    The production security profile requires a phishing-resistant passkey for
    this gate; development may opt in explicitly.
    """
    if not config.high_risk_step_up_required:
        return ctx
    return await require_webauthn_step_up(ctx)


async def require_webauthn_step_up(
    ctx: AuthContext = Depends(current_user),
) -> AuthContext:
    """Always require a fresh WebAuthn web Session, independent of profile."""
    expires_at = ctx.step_up_expires_at
    if (
        ctx.session_audience != "web"
        or ctx.authentication_strength != "webauthn"
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "step_up_required", "method": "webauthn"},
        )
    return ctx


_SHAREABLE_PATH_PARAMETERS = (
    ("wf_id", "workflow"),
    ("task_id", "task"),
    ("dep_id", "deployment"),
    ("kb_id", "knowledge_base"),
    ("skill_id", "skill_installation"),
)


async def _admit_shared_resource(
    request: Request,
    ctx: AuthContext,
    session: AsyncSession,
) -> None:
    """Rebind RLS to one explicitly shared resource's owning tenant.

    The recipient projection is only a locator. Every resource route still
    performs its ordinary OpenFGA check, and the authorization context admits
    only the exact projected root. Ambiguous IDs fail closed.
    """
    resource_type = ""
    resource_id = ""
    execution_id = None
    for parameter, candidate_type in _SHAREABLE_PATH_PARAMETERS:
        candidate_id = request.path_params.get(parameter)
        if candidate_id:
            resource_type = candidate_type
            resource_id = str(candidate_id)
            break
    if not resource_id:
        # Generic share-target routes still admit exactly one known root.
        candidate_type = request.path_params.get("resource_type")
        if candidate_type in {kind for _, kind in _SHAREABLE_PATH_PARAMETERS}:
            resource_type = candidate_type
            resource_id = str(request.path_params.get("resource_id") or "")
    if not resource_id:
        # Execution routes have no parent ID in their path. Resolve only the
        # source root, never execution inputs, events or credentials. Admission
        # remains a locator; the route must authorize the source and visibility.
        path = request.scope.get("path", "").rstrip("/")
        if path == "/api/v1/workflow-executions":
            candidate_type = request.query_params.get("source_type")
            if candidate_type == "workflow":
                # Interactive runs belong to the caller, even when their
                # definition belongs to another organization. The history
                # route admits the definition only during its permission check.
                return
            if candidate_type in {"workflow", "task", "deployment"}:
                resource_type = candidate_type
                resource_id = request.query_params.get("source_id", "")
        elif path.startswith("/api/v1/workflow-executions/"):
            candidate_id = request.path_params.get("execution_id")
            try:
                execution_id = uuid.UUID(str(candidate_id))
            except (ValueError, TypeError):
                return
    if not resource_id and execution_id is None:
        return
    from vibecanvas_api.storage.shared_resource_locator import shared_resource_roots
    roots = await shared_resource_roots(ctx.user_id, active_organization_id=ctx.active_organization_id, resource_type=resource_type or None,
                                       resource_id=resource_id or None, execution_id=execution_id, limit=2)
    if len(roots) != 1:
        return
    root = roots[0]
    resource_type, resource_id = root.resource_type, root.resource_id
    owner_id = str(root.owner_tenant_id)
    if owner_id == ctx.active_organization_id:
        return
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": owner_id},
    )
    request.state.admitted_resource_organization_id = owner_id
    request.state.admitted_resource_type = resource_type
    request.state.admitted_resource_id = resource_id


async def tenant_db(
    request: Request,
    ctx: AuthContext = Depends(current_user),
):
    """Request-scoped, tenant-bound DB session for business routes.
    RLS sees `app.tenant_id` for the whole request transaction."""
    async with session_scope(
        tenant_id=ctx.active_organization_id,
        user_id=ctx.user_id,
    ) as s:
        await _admit_shared_resource(request, ctx, s)
        yield s
