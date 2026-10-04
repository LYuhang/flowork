"""Cross-personal Knowledge sharing with HTTP, real RLS and package storage."""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from vibecanvas_api.app import build_app
from vibecanvas_api.config import config
from tests.test_workflow_authorization_integration import _browser_sessions, _register_exact, _headers
from tests.test_knowledge_base_authorization_integration import _RelationshipStore, _ObjectStore


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['viewer', 'editor', 'manager'])
async def test_cross_personal_knowledge_publish_preserves_owner_and_actor(pg_engine, monkeypatch, role):
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    objects = _ObjectStore()
    monkeypatch.setattr('vibecanvas_api.services.knowledge_packages.get_object_store', lambda: objects)
    monkeypatch.setattr('vibecanvas_api.routes.kb.get_object_store', lambda: objects)
    indexing = []
    async def enqueue(**kwargs):
        indexing.append(kwargs)
    monkeypatch.setattr('vibecanvas_api.routes.kb.enqueue_package_indexing', enqueue)
    app = build_app()
    app.state.openfga_client = _RelationshipStore()
    monkeypatch.setattr('vibecanvas_api.background_tasks.kb_indexer.openfga_client_from_config', lambda: app.state.openfga_client)
    from vibecanvas_api.background_tasks.kb_indexer import _require_captured_user_update
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.authorization.service import AuthorizationDeniedError
    async def check_index_permission(file_id):
        async with session_scope(tenant_id=owner['active_organization_id']) as session:
            await _require_captured_user_update(session, tenant_id=owner['active_organization_id'],
                file_id=uuid.UUID(file_id), user_id=recipient['user_id'])

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        owner_headers, owner, _ = await _register_exact(client, 'kb_share_owner')
        recipient_headers, recipient, email = await _register_exact(client, 'kb_share_recipient')
        created = await client.post('/api/v1/kb', json={'name': 'Shared knowledge'}, headers=owner_headers)
        assert created.status_code == 201, created.text
        kid = created.json()['id']
        path = f'/api/v1/kb/{kid}'
        assert (await client.get(path, headers=recipient_headers)).status_code == 404
        target = await client.post(f'/api/v1/resource-access/knowledge_base/{kid}/resolve-target', json={'target_type': 'user', 'identifier': email}, headers=owner_headers)
        assert target.status_code == 200, target.text
        resolved = target.json()['target']
        assert set(resolved['allowed_relations']) == {'viewer', 'editor', 'manager'}
        granted = await client.post(path + '/access', json={'relation': role, 'resolution_token': resolved['resolution_token']}, headers=_headers(owner_headers, **{'Idempotency-Key': 'grant-kb'}))
        assert granted.status_code == 201, granted.text
        for headers, own in [(owner_headers, True), (recipient_headers, False)]:
            listed = await client.get('/api/v1/kb', headers=headers)
            assert listed.status_code == 200, listed.text
            matches = [item for item in listed.json() if item['id'] == kid]
            assert len(matches) == 1
            assert matches[0]['created_by_me'] is own
            assert matches[0]['package_version'] == created.json()['package_version']
            assert 'file_count' in matches[0]
        detail = await client.get(path, headers=recipient_headers)
        assert detail.status_code == 200, detail.text
        assert 'use' in detail.json()['access']['capabilities']
        from types import SimpleNamespace
        from vibecanvas_api.services.agent_runtime.cli_knowledge import read
        cli_context = SimpleNamespace(tenant_id=recipient['active_organization_id'], username=recipient['user_id'],
            turn_id='knowledge-share-test', authorization_client=app.state.openfga_client,
            authorization_membership_role='owner', authorization_membership_status='active')
        discovered = await read(cli_context, 'knowledge.list', {'offset': 0, 'limit': 20})
        assert [item['knowledge_id'] for item in discovered['knowledge']] == [kid]
        downloaded = await read(cli_context, 'knowledge.download', {'knowledge_id': kid})
        assert downloaded['status'] == 'succeeded'
        assert any(item['path'] == 'README.md' for item in downloaded['files'])

        draft = await client.get(path + '/draft', headers=recipient_headers)
        assert draft.status_code == 200, draft.text
        edited = await client.patch(path, json={'name': 'Recipient edited', 'expected_hash': draft.json()['content_hash']}, headers=recipient_headers)
        if role == 'viewer':
            assert edited.status_code == 404, edited.text
        else:
            assert edited.status_code == 200, edited.text
            indexing.clear()
            published = await client.post(path + '/versions', json={'expected_hash': edited.json()['content_hash']}, headers=recipient_headers)
            assert published.status_code == 200, published.text
            assert len(indexing) == 1
            assert indexing[0]['tenant_id'] == owner['active_organization_id']
            assert indexing[0]['user_id'] == recipient['user_id']
            updated = await client.get(path, headers=owner_headers)
            assert updated.json()['name'] == 'Recipient edited'
            files = await client.get(path + '/files', headers=recipient_headers)
            assert files.status_code == 200, files.text
            file_id = files.json()[0]['id']
            await check_index_permission(file_id)
        revoked = await client.request('DELETE', path + '/access', json={'relation': role, 'subject_type': 'user', 'subject_id': recipient['user_id']}, headers=_headers(owner_headers, **{'Idempotency-Key': 'revoke-kb'}))
        assert revoked.status_code == 200, revoked.text
        assert all(item['id'] != kid for item in (await client.get('/api/v1/kb', headers=recipient_headers)).json())
        assert (await client.get(path + '/draft', headers=recipient_headers)).status_code == 404
        assert (await client.get(path + '/versions', headers=recipient_headers)).status_code == 404

        if role != 'viewer':
            with pytest.raises(AuthorizationDeniedError):
                await check_index_permission(file_id)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as denied:
            await read(cli_context, 'knowledge.download', {'knowledge_id': kid})
        assert denied.value.status_code == 404
        discovered = await read(cli_context, 'knowledge.list', {'offset': 0, 'limit': 20})
        assert discovered['knowledge'] == []
        if role == 'manager':
            target = await client.post(f'/api/v1/resource-access/knowledge_base/{kid}/resolve-target', json={'target_type': 'user', 'identifier': email}, headers=owner_headers)
            assert target.status_code == 200, target.text
            granted = await client.post(path + '/access', json={'relation': role, 'resolution_token': target.json()['target']['resolution_token']}, headers=_headers(owner_headers, **{'Idempotency-Key': 'regrant-manager'}))
            assert granted.status_code == 201, granted.text
            deleted = await client.delete(path, headers=recipient_headers)
            assert deleted.status_code == 204, deleted.text
            assert (await client.get(path, headers=owner_headers)).status_code == 404
            from vibecanvas_api.authorization.openfga_client import OpenFgaTuple
            assert OpenFgaTuple(f"organization:{owner['active_organization_id']}", 'organization', f'knowledge_base:{kid}') not in app.state.openfga_client.tuples
