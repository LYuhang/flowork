"""Company enrollment uses browser auth and the existing membership model."""
import pytest
from httpx import ASGITransport, AsyncClient
from vibecanvas_api.app import build_app
from vibecanvas_api.authorization import openfga_client
from tests.test_workflow_authorization_integration import (
    _RelationshipStore, _register_exact, _session_headers, _browser_sessions,
)


@pytest.mark.asyncio
async def test_company_add_registered_member_and_reject_unauthorized(pg_engine, monkeypatch):
    app = build_app()
    store = _RelationshipStore()
    app.state.openfga_client = store
    monkeypatch.setattr(openfga_client, 'openfga_client_from_config', lambda: store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        owner_headers, owner, _ = await _register_exact(client, 'company_owner')
        recipient_headers, recipient, email = await _register_exact(client, 'company_member')
        personal = owner['active_organization_id']
        denied = await client.post(f'/api/v1/organizations/{personal}/members', headers=owner_headers, json={'email': email})
        assert denied.status_code == 409, denied.text
        import uuid
        created = await client.post('/api/v1/organizations', headers=owner_headers, json={'name': 'Membership QA', 'slug': f'membership-{uuid.uuid4().hex[:12]}'})
        assert created.status_code == 201, created.text
        company = created.json()['organization_id']
        path = f'/api/v1/organizations/{company}/members'
        wrong_scope = await client.post(path, headers=owner_headers, json={'email': email})
        assert wrong_scope.status_code == 404, wrong_scope.text
        switched = await client.post('/api/v1/organizations/active', headers=owner_headers, json={'organization_id': company})
        assert switched.status_code == 200, switched.text
        owner_headers = _session_headers(switched, client)
        added = await client.post(path, headers=owner_headers, json={'email': email})
        assert added.status_code == 201, added.text
        assert added.json()['role'] == 'member'
        assert added.json()['user_id'] == recipient['user_id']
        duplicate = await client.post(path, headers=owner_headers, json={'email': email})
        assert duplicate.status_code == 409, duplicate.text
        unknown = await client.post(path, headers=owner_headers, json={'email': f'{uuid.uuid4()}@example.com'})
        assert unknown.status_code == 404, unknown.text
        switched = await client.post('/api/v1/organizations/active', headers=recipient_headers, json={'organization_id': company})
        assert switched.status_code == 200, switched.text
        recipient_headers = _session_headers(switched, client)
        forbidden = await client.post(path, headers=recipient_headers, json={'email': email})
        assert forbidden.status_code in (403, 404), forbidden.text
        promoted = await client.patch(f'{path}/{recipient["user_id"]}', headers=owner_headers, json={'role': 'admin', 'status': 'active'})
        assert promoted.status_code == 200, promoted.text
        for target, role in [(recipient['user_id'], 'owner'), (owner['user_id'], 'member')]:
            denied = await client.patch(f'{path}/{target}', headers=recipient_headers, json={'role': role, 'status': 'active'})
            assert denied.status_code == 403, denied.text
        last_owner = await client.patch(f'{path}/{owner["user_id"]}', headers=owner_headers, json={'role': 'member', 'status': 'active'})
        assert last_owner.status_code == 409, last_owner.text
        assert last_owner.json()['detail'] == 'organization_requires_active_owner'

        removed = await client.patch(f'{path}/{recipient["user_id"]}', headers=owner_headers, json={'role': 'member', 'status': 'revoked'})
        assert removed.status_code == 200, removed.text
        restored = await client.patch(f'{path}/{recipient["user_id"]}', headers=owner_headers, json={'role': 'member', 'status': 'active'})
        assert restored.status_code == 200, restored.text
        assert restored.json()['membership_id'] == added.json()['membership_id']
