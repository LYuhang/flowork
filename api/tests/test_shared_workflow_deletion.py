"""A shared manager can delete the source without borrowing its owner's identity."""
import uuid
import pytest
from sqlalchemy import text
from httpx import AsyncClient, ASGITransport
from tests.test_workflow_authorization_integration import _RelationshipStore, _register_exact, _headers, _browser_sessions
from vibecanvas_api.app import build_app
from vibecanvas_api.config import config

@pytest.mark.asyncio
async def test_foreign_manager_deletes_workflow(pg_engine, monkeypatch):
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    app = build_app(); app.state.openfga_client = _RelationshipStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        a, owner, _ = await _register_exact(client, 'delete_owner')
        b, recipient, email = await _register_exact(client, 'delete_manager')
        r = await client.post('/api/v1/workflows', headers=a, json={'name':'Shared deletion'})
        assert r.status_code == 201, r.text
        wf = r.json()['wf_id']; base = '/api/v1/workflows/' + wf
        target = await client.post('/api/v1/resource-access/workflow/'+wf+'/resolve-target', headers=a,
            json={'target_type':'user','identifier':email})
        assert target.status_code == 200, target.text
        r = await client.post(base+'/access', headers=_headers(a, **{'Idempotency-Key':uuid.uuid4().hex}),
            json={'relation':'manager','resolution_token':target.json()['target']['resolution_token']})
        assert r.status_code == 201, r.text
        r = await client.delete(base, headers=b)
        assert r.status_code == 204, r.text
        assert (await client.get(base, headers=a)).status_code == 404
        assert (await client.get(base, headers=b)).status_code == 404

        async with pg_engine.connect() as connection:
            cleanup_owner = await connection.scalar(text("SELECT tenant_id FROM workflow_deletion_cleanup WHERE workflow_id=:id"), {"id":wf})
            assert str(cleanup_owner) == owner["tenant_id"]
