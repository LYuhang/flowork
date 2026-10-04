"""Task/Deployment merge owner inventories before source filtering and paging."""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from tests.test_workflow_authorization_integration import _register_exact, _headers, _browser_sessions
from tests.test_task_authorization_integration import _RelationshipStore as TaskStore
from tests.test_deployment_authorization_integration import _RelationshipStore as DeploymentStore
from vibecanvas_api.app import build_app
from vibecanvas_api.config import config


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['task', 'deployment'])
async def test_three_owner_source_filter_pagination_and_revocation(pg_engine, monkeypatch, kind):
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    app = build_app()
    app.state.openfga_client = TaskStore() if kind == 'task' else DeploymentStore()
    collection = 'tasks' if kind == 'task' else 'deployments'
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        accounts = [await _register_exact(client, kind + suffix) for suffix in ('_reader', '_a', '_b')]
        reader_headers, reader, email = accounts[0]
        sources = []
        for headers, _, _ in accounts:
            created = await client.post('/api/v1/workflows', json={'name':'Inventory source'}, headers=_headers(headers))
            assert created.status_code == 201, created.text
            sources.append(created.json()['wf_id'])
        expected = []
        for index in range(7):
            owner_index = index % 3 if index < 6 else 1
            headers = accounts[owner_index][0]
            name = f'Inventory sample {index}'
            if kind == 'task':
                response = await client.post('/api/v1/tasks/scheduled-runs', headers=_headers(headers), json={
                    'name':name,'workflow_id':sources[owner_index],'schedule_type':'interval',
                    'interval_seconds':86400,'enabled':False})
            else:
                response = await client.post('/api/v1/deployments',headers=_headers(headers),json={
                    'name':name,'wf_id':sources[owner_index],'slug':'inventory-'+uuid.uuid4().hex[:12],
                    'trigger_type':'api','version_pin':'specific','pinned_major':1,'pinned_sub':0,'enabled':False})
            assert response.status_code == 201, response.text
            item = response.json()['task'] if kind == 'task' else response.json()
            identifier = item['id']
            # Deliberately interleave owners with deterministic distinct times.
            time_column = 'submitted_at' if kind == 'task' else 'created_at'
            async with pg_engine.begin() as conn:
                await conn.execute(text(f'UPDATE {collection} SET {time_column}=:time WHERE id=:id'),
                    {'time':datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=index),'id':uuid.UUID(identifier)})
            if index == 6:
                continue  # Foreign private resource must not enter any page/count.
            expected.insert(0, (identifier, owner_index))
            if owner_index:
                target = await client.post(f'/api/v1/resource-access/{kind}/{identifier}/resolve-target',
                    headers=_headers(headers),json={'target_type':'user','identifier':email})
                assert target.status_code == 200, target.text
                grant = await client.post(f'/api/v1/{collection}/{identifier}/access',
                    headers=_headers(headers, **{'Idempotency-Key':uuid.uuid4().hex}),
                    json={'relation':'viewer','resolution_token':target.json()['target']['resolution_token']})
                assert grant.status_code == 201, grant.text
        for source in ['all','created','shared']:
            filtered = [(identifier, owner) for identifier, owner in expected
                        if source == 'all' or (owner == 0) == (source == 'created')]
            for limit in [1,2,4]:
                seen = []
                for offset in range(0, len(filtered)+1, limit):
                    r = await client.get(f'/api/v1/{collection}', headers=_headers(reader_headers),
                        params={'source':source,'limit':limit,'offset':offset})
                    assert r.status_code == 200, r.text
                    page = r.json()
                    assert page['total'] == len(filtered)
                    assert [x['id'] for x in page['items']] == [x[0] for x in filtered[offset:offset+limit]]
                    for item in page['items']:
                        owner = next(owner for identifier, owner in expected if identifier == item['id'])
                        assert item['created_by_me'] is (owner == 0)
                    seen.extend(x['id'] for x in page['items'])
                assert seen == [x[0] for x in filtered]
            if kind == 'task':
                summary = await client.get('/api/v1/tasks/summary', headers=_headers(reader_headers),params={'source':source})
                assert summary.status_code == 200, summary.text
                assert summary.json()['paused'] == len(filtered)
            else:
                assert page['summary']['disabled'] == len(filtered)
        identifier, owner_index = next(pair for pair in expected if pair[1])
        revoked = await client.request('DELETE', f'/api/v1/{collection}/{identifier}/access',
            headers=_headers(accounts[owner_index][0], **{'Idempotency-Key':uuid.uuid4().hex}),
            json={'relation':'viewer','subject_type':'user','subject_id':reader['user_id']})
        assert revoked.status_code == 200, revoked.text
        remaining = await client.get(f'/api/v1/{collection}?limit=100',headers=_headers(reader_headers))
        assert remaining.json()['total'] == 5
        assert [x['id'] for x in remaining.json()['items']] == [x[0] for x in expected if x[0] != identifier]
