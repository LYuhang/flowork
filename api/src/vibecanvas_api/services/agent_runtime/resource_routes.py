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
