"""FastAPI dependencies for centralized resource authorization."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

from sqlalchemy import text

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db

from .service import AuthzService
from .types import (
    Action,
    AuthorizedResource,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)


def mutation_coordinator_for_request(
    request: Request,
    organization_id: str,
):
    """Build the durable mutation seam without exposing OpenFGA to routes."""
    from .mutations import AuthzMutationCoordinator

    return AuthzMutationCoordinator(
        client=getattr(request.app.state, "openfga_client", None),
        organization_id=organization_id,
    )


def principal_for_auth(auth: AuthContext) -> PrincipalRef:
    return PrincipalRef(PrincipalType.USER, auth.user_id)


def context_for_auth(
    auth: AuthContext,
    request: Request,
    *,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthzRequestContext:
    """Build the single HTTP-to-authorization context projection.

    Routes, dependencies, and Platform MCP adapters must not each invent a
    subtly different subset of Session state.  In particular, generation and
    membership lifecycle are security inputs rather than optional metadata.
    """
    return AuthzRequestContext(
        active_organization_id=auth.active_organization_id,
        admitted_resource_organization_id=str(
            getattr(
                getattr(request, "state", None),
                "admitted_resource_organization_id",
                "",
            )
            or ""
        ),
        admitted_resource_type=str(
            getattr(
                getattr(request, "state", None),
                "admitted_resource_type",
                "",
            )
            or ""
        ),
        admitted_resource_id=str(
            getattr(
                getattr(request, "state", None),
                "admitted_resource_id",
                "",
            )
            or ""
        ),
        request_id=str(
            getattr(getattr(request, "state", None), "request_id", "") or ""
        ),
        session_id=auth.session_id,
        session_generation=auth.session_generation,
        membership_id=auth.membership_id,
        membership_role=auth.membership_role,
        membership_status=auth.membership_status,
        authentication_strength=auth.authentication_strength,
        consistency=consistency,
    )


async def authorize_resource(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    resource: ResourceRef,
    action: Action,
) -> AuthorizedResource:
    """Check one resource and preserve the non-enumerating HTTP contract."""
    decision = await service.check(
        principal_for_auth(auth),
        action,
        resource,
        context_for_auth(auth, request),
    )
    if not decision.allowed:
        # Do not reveal whether the resource exists to an unauthorized caller.
        # Structured permission errors are reserved for resources whose
        # metadata the caller may already discover.
        raise HTTPException(status_code=404, detail="resource_not_found")
    return AuthorizedResource(resource=resource, decision=decision)


async def get_authz_service(
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
) -> AuthzService:
    service = authz_service_for_session(
        session=session,
        organization_id=(
            getattr(request.state, "admitted_resource_organization_id", None)
            or auth.active_organization_id
        ),
        openfga_client=getattr(request.app.state, "openfga_client", None),
    )
    return scope_authz_service(
        service,
        session=session,
        auth=auth,
        request=request,
    )


def scope_authz_service(
    service: AuthzService,
    *,
    session: AsyncSession,
    auth: AuthContext,
    request: Request | None = None,
    audit_uses: bool = True,
) -> AuthzService:
    """Apply the active privileged ceiling without changing normal users."""
    if not auth.privileged_access_request_id:
        return service
    from .privileged import PrivilegedAuthzService

    return PrivilegedAuthzService(
        service,
        session=session,
        auth=auth,
        request=request,
        audit_uses=audit_uses,
    )


def authz_service_for_session(
    *,
    session: AsyncSession,
    organization_id: str,
    openfga_client,
) -> AuthzService:
    """Build the single OpenFGA backend used by HTTP, workers and MCP."""
    from .mutations import AuthzMutationCoordinator
    from .openfga import OpenFgaAuthzService
    from .openfga_client import OpenFgaUnavailableError

    if openfga_client is None:
        raise OpenFgaUnavailableError()
    openfga = OpenFgaAuthzService(
        session,
        openfga_client,
        mutation_coordinator=AuthzMutationCoordinator(
            client=openfga_client,
            organization_id=organization_id,
        ),
    )
    return openfga


def require_resource(
    resource_type: ResourceType,
    action: Action,
    *,
    id_param: str,
) -> Callable:
    """Build a dependency that reads a path/query id and requires access."""

    async def dependency(
        request: Request,
        auth: AuthContext = Depends(current_user),
        service: AuthzService = Depends(get_authz_service),
    ) -> AuthorizedResource:
        resource_id = (
            request.path_params.get(id_param)
            or request.query_params.get(id_param)
            or ""
        )
        if not resource_id:
            # All current callers declare the resource id as a required
            # path/query parameter. Preserve FastAPI's client-error semantics
            # even though dependencies may execute before endpoint validation.
            raise HTTPException(
                status_code=422,
                detail={"code": "missing_resource_id", "field": id_param},
            )
        resource = ResourceRef(
            type=resource_type,
            id=str(resource_id),
            organization_id=auth.active_organization_id,
        )
        return await authorize_resource(
            request=request,
            auth=auth,
            service=service,
            resource=resource,
            action=action,
        )

    return dependency


@asynccontextmanager
async def authorized_resource_scope(*, request, auth, session, resource_type,
                                    resource_id, action,
                                    consistency=ConsistencyPreference.HIGHER_CONSISTENCY):
    """Temporarily admit one dependency without changing the actor or owner.

    The copied request prevents admission leaking into the outer operation.
    A savepoint restores RLS on failure; on success restore it explicitly.
    Callers must not commit inside this scope.
    """
    from vibecanvas_api.auth.deps import _admit_shared_resource

    scoped_request = Request({**request.scope,
        "path_params": {"resource_type": resource_type.value, "resource_id": str(resource_id)},
        "state": {key: value for key, value in request.scope.get("state", {}).items()
                  if not key.startswith("admitted_resource_")},
    })
    async with session.begin_nested():
        previous = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one() or ""
        await _admit_shared_resource(scoped_request, auth, session)
        from .parent_resolvers import collaboration_root_exists
        resource = ResourceRef(resource_type, str(resource_id), auth.active_organization_id)
        if not await collaboration_root_exists(session, resource):
            raise HTTPException(status_code=404, detail="resource_not_found")
        scoped_service = await get_authz_service(scoped_request, auth, session)
        decision = await scoped_service.check(principal_for_auth(auth), action,
            resource, context_for_auth(auth, scoped_request, consistency=consistency))
        if not decision.allowed:
            raise HTTPException(status_code=404, detail="resource_not_found")
        yield scoped_request, scoped_service
        await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": previous})
