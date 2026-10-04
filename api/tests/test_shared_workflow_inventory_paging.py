"""Mixed-owner inventory is sorted and paginated as one authorized set."""
import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_workflow_authorization_integration import (
    _RelationshipStore, _register_exact, _headers, _browser_sessions,
)
from vibecanvas_api.app import build_app
from vibecanvas_api.config import config


@pytest.mark.asyncio
async def test_interleaved_three_owner_inventory_pages_and_revoke(pg_engine, monkeypatch):
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    app = build_app()
    app.state.openfga_client = _RelationshipStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        accounts = [await _register_exact(client, name) for name in ('inventory_reader', 'inventory_a', 'inventory_b')]
        reader_headers, reader, reader_email = accounts[0]
        expected = []
        for index in range(6):
            owner_index = index % 3
            headers, _, _ = accounts[owner_index]
            created = await client.post('/api/v1/workflows', json={'name': f'Interleaved {index}'}, headers=_headers(headers))
            assert created.status_code == 201, created.text
            item = created.json()
            expected.append((item, owner_index))
            if owner_index:
                root = f'/api/v1/workflows/{item["wf_id"]}/access'
                target = await client.post(f'/api/v1/resource-access/workflow/{item["wf_id"]}/resolve-target',
                    json={'target_type': 'user', 'identifier': reader_email}, headers=_headers(headers))
                assert target.status_code == 200, target.text
                grant = await client.post(root, json={'relation': 'viewer',
                    'resolution_token': target.json()['target']['resolution_token']},
                    headers=_headers(headers, **{'Idempotency-Key': f'inventory-{index}'}))
                assert grant.status_code == 201, grant.text
        hidden = await client.post('/api/v1/workflows', json={'name': 'Not shared'}, headers=_headers(accounts[1][0]))
        assert hidden.status_code == 201
        ordered = sorted(expected, key=lambda pair: (-pair[0]['updated_at'], pair[0]['wf_id']))
        expected_ids = [item['wf_id'] for item, _ in ordered]
        for limit in (1, 2, 4):
            collected = []
            for offset in range(0, 7, limit):
                response = await client.get(f'/api/v1/workflows?limit={limit}&offset={offset}', headers=_headers(reader_headers))
                assert response.status_code == 200, response.text
                page = response.json()
                assert page['total'] == 6
                assert [item['wf_id'] for item in page['items']] == expected_ids[offset:offset + limit]
                collected.extend(item['wf_id'] for item in page['items'])
                for item in page['items']:
                    owner_index = next(owner for original, owner in expected if original['wf_id'] == item['wf_id'])
                    assert item['created_by_me'] is (owner_index == 0)
            assert collected == expected_ids
        foreign, owner_index = next(pair for pair in expected if pair[1])
        revoked = await client.request('DELETE', f'/api/v1/workflows/{foreign["wf_id"]}/access',
            json={'relation': 'viewer', 'subject_type': 'user', 'subject_id': reader['user_id']},
            headers=_headers(accounts[owner_index][0], **{'Idempotency-Key': 'inventory-revoke'}))
        assert revoked.status_code == 200, revoked.text
        response = await client.get('/api/v1/workflows?limit=100', headers=_headers(reader_headers))
        assert response.json()['total'] == 5
        assert [item['wf_id'] for item in response.json()['items']] == [identifier for identifier in expected_ids if identifier != foreign['wf_id']]
