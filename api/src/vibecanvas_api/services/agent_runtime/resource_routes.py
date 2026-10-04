"""Adapt an authenticated Agent context to the shared resource route policy.

The CLI is an internal caller, not a second HTTP authentication mechanism.
Callers must first resolve the live Agent identity and enter its tenant session;
route authorization and audit scopes then remain identical to the web API.
"""

from types import SimpleNamespace

from starlette.requests import Request

from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
    scope_authz_service,
)
from vibecanvas_api.services.agent_resources.authorization import agent_auth_context


def resource_route_params(context, session) -> dict:
    """Build route dependencies from trusted host state, never CLI arguments."""
    auth = agent_auth_context(context)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/internal/agent-cli/resource",
        "headers": [],
        "query_string": b"",
        "app": SimpleNamespace(state=SimpleNamespace(
            openfga_client=context.authorization_client,
        )),
        "state": {
            "request_id": f"flowork-cli:{context.turn_id}",
            "execution_source": "flowork_cli",
        },
    })
    service = authz_service_for_session(
        session=session,
        organization_id=str(context.tenant_id),
        openfga_client=context.authorization_client,
    )
    service = scope_authz_service(
        service, session=session, auth=auth,
    )
    return dict(request=request, ctx=auth, session=session, service=service)


async def admitted_resource_route_params(context, session, resource_type, resource_id):
    """Use the web API's exact-root admission for an authenticated CLI call."""
    from vibecanvas_api.auth.deps import _admit_shared_resource
    from vibecanvas_api.authorization.dependencies import get_authz_service
    params = resource_route_params(context, session)
    request = params["request"]
    request.scope["path_params"] = {"resource_type": resource_type, "resource_id": str(resource_id)}
    await _admit_shared_resource(request, params["ctx"], session)
    params["service"] = await get_authz_service(request, params["ctx"], session)
    return params


async def shared_resource_cards(context, session, resource_type):
    """Discover shares with the same per-resource checks as the web list."""
    from vibecanvas_api.routes.resource_access import list_shared_resources
    params = resource_route_params(context, session)
    cards = []
    offset = 0
    while True:
        page = await list_shared_resources(
            request=params["request"], auth=params["ctx"], session=session,
            resource_type=resource_type, limit=100, offset=offset,
        )
        cards.extend(page.items)
        if page.next_offset is None:
            return cards
        offset = page.next_offset


async def visible_resource_rows(context, resource_type, *, list_page, get_resource):
    """Combine authorized local and shared rows before CLI filtering/paging.

    Each foreign read gets its own scope so one share cannot change the tenant
    used for subsequent resources. Detail routes recheck current permission.
    """
    import uuid
    from fastapi import HTTPException
    from vibecanvas_api.storage.db import session_scope

    rows = {}
    async with session_scope(tenant_id=context.tenant_id, user_id=context.username) as session:
        params = resource_route_params(context, session)
        offset = 0
        while True:
            page = await list_page(params, offset)
            for row in page["items"]:
                rows[str(row["id"])] = row
            offset += len(page["items"])
            if not page["items"] or offset >= page["total"]:
                break
        cards = await shared_resource_cards(context, session, resource_type)
    for card in cards:
        if card.resource_id in rows:
            continue
        async with session_scope(tenant_id=context.tenant_id, user_id=context.username) as session:
            params = await admitted_resource_route_params(context, session, resource_type, card.resource_id)
            try:
                rows[card.resource_id] = await get_resource(uuid.UUID(card.resource_id), **params)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                # A share can be revoked or deleted between discovery and read.
    return list(rows.values())
