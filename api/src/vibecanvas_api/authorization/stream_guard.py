"""Fresh authorization leases for long-lived SSE/WebSocket consumers."""

from __future__ import annotations

import structlog
from starlette.requests import Request

from vibecanvas_api.auth.deps import AuthContext, _admit_shared_resource
from vibecanvas_api.auth.live_identity import (
    LiveIdentityError,
    resolve_live_authorization_identity,
)
from vibecanvas_api.storage.db import session_scope

from .dependencies import authz_service_for_session, scope_authz_service, context_for_auth
from .parent_resolvers import collaboration_root_exists
from .types import (
    Action,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
)

logger = structlog.get_logger(__name__)


def _deny_lease(reason: str, *, auth: AuthContext) -> bool:
    logger.debug(
        "authorization_stream_lease_denied",
        reason=reason,
        session_id=auth.session_id,
        session_audience=auth.session_audience,
    )
    return False


async def authorization_lease_is_valid(
    *,
    auth: AuthContext,
    openfga_client,
    resource: ResourceRef,
    action: Action,
) -> bool:
    """Revalidate Session generation, membership, and resource authorization.

    A request-scoped ``AuthContext`` is only a snapshot. Long-lived streams
    must stop after logout, organization switch, membership suspension, or
    resource revocation instead of trusting that snapshot indefinitely.
    """
    try:
        async with session_scope() as identity_session:
            try:
                lease_auth = await resolve_live_authorization_identity(
                    identity_session,
                    session_id=auth.session_id,
                    user_id=auth.user_id,
                    organization_id=auth.active_organization_id,
                    session_generation=auth.session_generation,
                    membership_id=auth.membership_id,
                )
            except LiveIdentityError as exc:
                return _deny_lease(exc.reason, auth=auth)

        async with session_scope(
            tenant_id=auth.active_organization_id,
            user_id=lease_auth.user_id,
        ) as resource_session:
            request = Request({
                "type": "http", "method": "GET", "path": "/internal/stream-lease",
                "headers": [], "query_string": b"",
                "path_params": {"resource_type": resource.type.value, "resource_id": resource.id},
            })
            await _admit_shared_resource(request, lease_auth, resource_session)
            if not await collaboration_root_exists(resource_session, resource):
                return _deny_lease("resource_deleted", auth=auth)
            service = authz_service_for_session(
                session=resource_session,
                organization_id=(getattr(request.state, "admitted_resource_organization_id", None)
                                 or lease_auth.active_organization_id),
                openfga_client=openfga_client,
            )
            service = scope_authz_service(
                service,
                session=resource_session,
                auth=lease_auth,
                audit_uses=False,
            )
            decision = await service.check(
                PrincipalRef(PrincipalType.USER, lease_auth.user_id),
                action,
                resource,
                context_for_auth(lease_auth, request, consistency=ConsistencyPreference.HIGHER_CONSISTENCY),
            )
            if not decision.allowed:
                logger.debug(
                    "authorization_stream_lease_denied",
                    reason=decision.reason_code or "authorization_denied",
                    session_id=auth.session_id,
                    session_audience=auth.session_audience,
                    resource_type=resource.type.value,
                    resource_id=resource.id,
                    action=action.value,
                )
            return decision.allowed
    except Exception:
        # Long-lived consumers must not retain access when identity storage or
        # the authorization control plane is unavailable.
        logger.exception(
            "authorization_stream_lease_check_failed",
            resource_type=resource.type.value,
            resource_id=resource.id,
            action=action.value,
        )
        return False


__all__ = ["authorization_lease_is_valid"]
