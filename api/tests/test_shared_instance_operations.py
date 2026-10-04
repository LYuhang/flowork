"""Shared instance mutations retain the original resource ownership."""
import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from vibecanvas_api.app import build_app
from vibecanvas_api.config import config
from tests.test_workflow_authorization_integration import _browser_sessions, _register, _headers
from tests.test_task_authorization_integration import _RelationshipStore as TaskStore
from tests.test_deployment_authorization_integration import _RelationshipStore as DeploymentStore


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['task', 'deployment'])
async def test_cross_personal_manager_operations_keep_resource_tenant(pg_engine, monkeypatch, kind):
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    app = build_app()
    app.state.openfga_client = TaskStore() if kind == 'task' else DeploymentStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        owner_headers, owner = await _register(client, 'instance_owner')
        recipient_headers, recipient = await _register(client, 'instance_recipient')
        made = await client.post('/api/v1/workflows', json={'name': 'Instance source'}, headers=_headers(owner_headers))
        assert made.status_code == 201, made.text
        workflow = made.json()['wf_id']
        if kind == 'task':
            made = await client.post('/api/v1/tasks/scheduled-runs', headers=_headers(owner_headers), json={
                'name': 'Shared schedule', 'workflow_id': workflow, 'schedule_type': 'interval', 'interval_seconds': 3600})
            assert made.status_code == 201, made.text
            identifier = made.json()['task']['id']
            base = '/api/v1/tasks/' + identifier
        else:
            made = await client.post('/api/v1/deployments', headers=_headers(owner_headers), json={
                'name': 'Shared deployment', 'wf_id': workflow, 'slug': 'shared-' + uuid.uuid4().hex[:10],
                'trigger_type': 'api', 'version_pin': 'specific', 'pinned_major': 1, 'pinned_sub': 0})
            assert made.status_code == 201, made.text
            identifier = made.json()['id']
            base = '/api/v1/deployments/' + identifier
        resolved = await client.post(f'/api/v1/resource-access/{kind}/{identifier}/resolve-target',
            headers=_headers(owner_headers), json={'target_type': 'user', 'identifier': recipient['email']})
        assert resolved.status_code == 200, resolved.text
        granted = await client.post(base + '/access', headers=_headers(owner_headers, **{'Idempotency-Key': uuid.uuid4().hex}),
            json={'relation': 'manager', 'resolution_token': resolved.json()['target']['resolution_token']})
        assert granted.status_code == 201, granted.text
        assert (await client.get(base, headers=_headers(recipient_headers))).status_code == 200
        if kind == 'task':
            schedule_url = '/api/v1/tasks/scheduled-runs/' + identifier
            assert (await client.post(schedule_url + '/pause', headers=_headers(owner_headers))).status_code == 200
            async def change_role(role, present):
                if present:
                    target = await client.post(f'/api/v1/resource-access/task/{identifier}/resolve-target',
                        headers=_headers(owner_headers), json={'target_type':'user','identifier':recipient['email']})
                    response = await client.post(base+'/access', headers=_headers(owner_headers, **{'Idempotency-Key':uuid.uuid4().hex}),
                        json={'relation':role,'resolution_token':target.json()['target']['resolution_token']})
                    assert response.status_code == 201, response.text
                else:
                    response = await client.request('DELETE',base+'/access',headers=_headers(owner_headers, **{'Idempotency-Key':uuid.uuid4().hex}),
                        json={'relation':role,'subject_type':'user','subject_id':recipient['user_id']})
                    assert response.status_code == 200, response.text
            await change_role('editor', True)
            await change_role('manager', False)
            assert (await client.post(schedule_url+'/resume',headers=_headers(recipient_headers))).status_code == 404
            rejected = await client.patch(schedule_url,headers=_headers(recipient_headers),json={'enabled':True})
            assert rejected.status_code == 404, rejected.text
            edited = await client.patch(schedule_url,headers=_headers(recipient_headers),json={'name':'Paused edited schedule','enabled':False})
            assert edited.status_code == 200 and edited.json()['schedule']['enabled'] is False
            await change_role('manager', True)
            await change_role('editor', False)
            enabled = await client.patch(schedule_url,headers=_headers(recipient_headers),json={'enabled':True})
            assert enabled.status_code == 200 and enabled.json()['schedule']['enabled'] is True
            running = await client.post('/api/v1/tasks/scheduled-runs/' + identifier + '/run-now', headers=_headers(recipient_headers))
            assert running.status_code == 202, running.text
            execution_id = running.json()['execution']['id']
            async with pg_engine.begin() as connection:
                row = (await connection.execute(text('SELECT tenant_id FROM scheduled_run_executions WHERE id=:id'),
                    {'id': uuid.UUID(execution_id)})).one()
                assert str(row.tenant_id) == owner['active_organization_id']
                # No worker is launched in this isolated API test. Mark the QA
                # execution terminal to exercise the normal deletion guard.
                await connection.execute(text("UPDATE scheduled_run_executions SET status='cancelled',finished_at=now() WHERE id=:id"),
                                         {'id': uuid.UUID(execution_id)})
            delete_path = '/api/v1/tasks/scheduled-runs/' + identifier
        else:
            updated = await client.patch(base, headers=_headers(recipient_headers), json={'name': 'Recipient edit'})
            assert updated.status_code == 200, updated.text
            assert (await client.get(base, headers=_headers(owner_headers))).json()['name'] == 'Recipient edit'
            selection = {'version_pin':'specific','pinned_major':1,'pinned_sub':0}
            denied = await client.patch(base,headers=_headers(recipient_headers),json=selection)
            assert denied.status_code == 404, denied.text
            source_target = await client.post(f'/api/v1/resource-access/workflow/{workflow}/resolve-target',
                headers=_headers(owner_headers),json={'target_type':'user','identifier':recipient['email']})
            assert source_target.status_code == 200, source_target.text
            source_grant = await client.post(f'/api/v1/workflows/{workflow}/access',
                headers=_headers(owner_headers, **{'Idempotency-Key':uuid.uuid4().hex}),
                json={'relation':'manager','resolution_token':source_target.json()['target']['resolution_token']})
            assert source_grant.status_code == 201, source_grant.text
            repinned = await client.patch(base,headers=_headers(recipient_headers),json=selection)
            assert repinned.status_code == 200, repinned.text
            source_revoke = await client.request('DELETE',f'/api/v1/workflows/{workflow}/access',
                headers=_headers(owner_headers, **{'Idempotency-Key':uuid.uuid4().hex}),
                json={'relation':'manager','subject_type':'user','subject_id':recipient['user_id']})
            assert source_revoke.status_code == 200, source_revoke.text
            assert (await client.patch(base,headers=_headers(recipient_headers),json=selection)).status_code == 404
            delete_path = base
        from vibecanvas_api.auth.deps import AuthContext
        from vibecanvas_api.authorization.stream_guard import authorization_lease_is_valid
        from vibecanvas_api.authorization.types import ResourceRef, ResourceType, Action
        from vibecanvas_api.authorization.openfga_client import OpenFgaTuple
        async with pg_engine.connect() as connection:
            row = (await connection.execute(text("""SELECT s.session_id::text, s.generation,
                s.authentication_strength, m.membership_id::text, m.org_role, m.status
                FROM sessions s JOIN org_memberships m ON m.user_id=s.user_id AND m.tenant_id=s.active_organization_id
                WHERE s.user_id=:user"""), {'user': uuid.UUID(recipient['user_id'])})).one()
        auth = AuthContext(user_id=recipient['user_id'], tenant_id=recipient['active_organization_id'],
            active_organization_id=recipient['active_organization_id'], email=recipient['email'],
            session_id=row.session_id, session_generation=row.generation, membership_id=row.membership_id,
            membership_role=row.org_role, membership_status=row.status, authentication_strength=row.authentication_strength)
        async def lease():
            return await authorization_lease_is_valid(auth=auth, openfga_client=app.state.openfga_client,
                resource=ResourceRef(ResourceType(kind), identifier, recipient['active_organization_id']),
                action=Action.INSPECT_RUNS)
        assert await lease()
        edge = OpenFgaTuple('user:' + recipient['user_id'], 'manager', kind + ':' + identifier)
        app.state.openfga_client.tuples.remove(edge)
        assert not app.state.openfga_client._allowed(edge.user, "can_inspect_runs", edge.object), app.state.openfga_client.tuples
        assert not await lease()  # A stale discovery locator must not extend the lease.
        app.state.openfga_client.tuples.add(edge)
        assert await lease()
        deleted = await client.delete(delete_path, headers=_headers(recipient_headers))
        assert deleted.status_code in {200, 204}, deleted.text
        assert (await client.get(base, headers=_headers(owner_headers))).status_code == 404
        assert not await lease()
