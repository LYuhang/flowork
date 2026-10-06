from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import pytest

from vibecanvas_api.services.agent_runtime.workflow_model_capability import (
    mint_runtime_workflow_model_capability, verify_runtime_workflow_model_capability,
)
from vibecanvas_api.services.agent_runtime.workflow_mcp_capability import (
    mint_workflow_mcp_capability, verify_workflow_mcp_capability,
)


def identity():
    user = str(uuid4())
    return dict(organization_id=str(uuid4()), user_id=user, workflow_id='wf-local',
        execution_id='wf-local', execution_resource_type='workflow',
        principal_type='user', principal_id=user, principal_generation=0,
        authorization_generation='test-generation')


@pytest.mark.parametrize('kind', ['model', 'mcp'])
@pytest.mark.parametrize('change', [{}, {'execution_id': 'another-workflow'},
    {'principal_type': 'service_account', 'principal_generation': 1}, {'principal_id': 'other'}])
def test_local_capabilities_bind_user_workflow_and_expiry(kind, change):
    claims = {**identity(), **change}
    if kind == 'model':
        mint = mint_runtime_workflow_model_capability
        verify = lambda token, now: verify_runtime_workflow_model_capability(token, secret='test', now=now)
        claims.update(credential_id=None, provider='openai', model='test', config_revision='1')
    else:
        mint = mint_workflow_mcp_capability
        server_id = str(uuid4())
        claims.update(server_id=server_id, tools_fingerprint='a'*64)
        verify = lambda token, now: verify_workflow_mcp_capability(token, secret='test', server_id=server_id, now=now)
    if change:
        with pytest.raises(ValueError):
            mint(**claims, secret='test', ttl_s=60, now=100)
    else:
        token = mint(**claims, secret='test', ttl_s=60, now=100)
        assert verify(token, 101).execution_resource_type == 'workflow'
        assert verify(token, 160) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [None, 'user', 'membership', 'workflow'])
async def test_local_shared_workflow_checks_live_access_in_owner_scope(monkeypatch, revoked):
    from vibecanvas_api.services import workflow_execution_authorization as auth
    from vibecanvas_api.storage import shared_resource_locator
    from vibecanvas_api.authorization import parent_resolvers
    from vibecanvas_api.authorization.types import Action, ResourceType
    claims = identity()
    cap = SimpleNamespace(**claims)
    owner = str(uuid4())
    membership = SimpleNamespace(membership_id=uuid4(), org_role='member', status='active')
    results = iter([None if revoked == 'user' else object(), None,
                    None if revoked == 'membership' else membership])
    class Session:
        async def execute(self, *args, **kwargs):
            value = next(results)
            return SimpleNamespace(scalar_one_or_none=lambda: value)
    session = Session()
    @asynccontextmanager
    async def scope(**kwargs):
        yield session
    @asynccontextmanager
    async def temporary(session, tenant):
        assert tenant == owner
        yield
    async def roots(user_id, **kwargs):
        assert user_id == cap.user_id
        assert kwargs['active_organization_id'] == cap.organization_id
        return [SimpleNamespace(owner_tenant_id=owner)]
    async def exists(session, resource):
        assert resource.organization_id == owner
        return True
    calls = []
    async def check(principal, action, resource, context):
        calls.append(resource)
        assert resource.type is ResourceType.WORKFLOW
        assert resource.organization_id == owner
        assert action is Action.EXECUTE
        assert context.active_organization_id == cap.organization_id
        assert context.admitted_resource_organization_id == owner
        return SimpleNamespace(allowed=revoked != 'workflow')
    async def resolve(**kwargs):
        assert kwargs['capability'].organization_id == cap.organization_id
        return 'authorized'
    monkeypatch.setattr(auth, 'session_scope', scope)
    monkeypatch.setattr(auth, 'temporary_tenant_scope', temporary)
    monkeypatch.setattr(auth, 'authorization_model_generation', lambda **kwargs: 'test-generation')
    monkeypatch.setattr(auth, 'authz_service_for_session', lambda **kwargs: SimpleNamespace(check=check))
    monkeypatch.setattr(shared_resource_locator, 'shared_resource_roots', roots)
    monkeypatch.setattr(parent_resolvers, 'collaboration_root_exists', exists)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(openfga_client=None)))
    if revoked:
        with pytest.raises(HTTPException) as failure:
            await auth.authorize_workflow_execution(request, cap, resolve=resolve)
        assert failure.value.status_code == 403
    else:
        assert await auth.authorize_workflow_execution(request, cap, resolve=resolve) == 'authorized'
        assert len(calls) == 1
