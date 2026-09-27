from types import SimpleNamespace
from unittest.mock import Mock

from vibecanvas_api.services.agent_runtime import resource_routes


def test_cli_route_dependencies_preserve_identity_and_audit_source(monkeypatch):
    client, session, service, scoped = object(), object(), object(), object()
    context = SimpleNamespace(
        username="user", tenant_id="tenant", turn_id="turn", authorization_client=client,
        authorization_session_id="session", authorization_session_generation=7,
        authorization_membership_id="member", authorization_membership_role="owner",
        authorization_membership_status="active", authorization_authentication_strength="webauthn",
        authorization_session_audience="web", authorization_privileged_access_request_id="grant",
        authorization_privileged_resource_type="knowledge_base", authorization_privileged_resource_id="kb",
        authorization_privileged_actions=("view",), authorization_privileged_expires_at=None,
    )
    factory = Mock(return_value=service)
    scope = Mock(return_value=scoped)
    monkeypatch.setattr(resource_routes, "authz_service_for_session", factory)
    monkeypatch.setattr(resource_routes, "scope_authz_service", scope)
    params = resource_routes.resource_route_params(context, session)
    auth, request = params["ctx"], params["request"]
    assert params["session"] is session and params["service"] is scoped
    assert (auth.user_id, auth.tenant_id, auth.active_organization_id) == ("user", "tenant", "tenant")
    assert (auth.session_id, auth.session_generation) == ("session", 7)
    assert auth.membership_id == "member" and auth.membership_role == "owner"
    assert auth.authentication_strength == "webauthn"
    assert auth.privileged_access_request_id == "grant"
    assert auth.privileged_resource_id == "kb" and auth.privileged_actions == frozenset({"view"})
    factory.assert_called_once_with(session=session, organization_id="tenant", openfga_client=client)
    scope.assert_called_once_with(service, session=session, auth=auth)
    assert request.method == "POST"
    assert request.url.path == "/internal/agent-cli/resource"
    assert request.state.request_id == "flowork-cli:turn"
    assert request.state.execution_source == "flowork_cli"
    assert request.app.state.openfga_client is client
    assert not request.headers
