"""Definition sharing cannot grant another user's Workflow run history."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException
from vibecanvas_api.routes import workflow_history as routes
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from tests.storage.test_workflow_history import owner
from tests.test_workflow_authorization_integration import _browser_sessions  # noqa: F401 - autouse fixture


@pytest.mark.asyncio
async def test_shared_workflow_history_filters_initiator_and_rejects_known_ids(pg_engine, monkeypatch):
    tenant, creator, _ = await owner()
    _, recipient, _ = await owner()
    workflow = 'history-boundary-' + uuid.uuid4().hex
    creator_run, recipient_run = str(uuid.uuid4()), str(uuid.uuid4())
    async with session_scope(tenant_id=tenant) as session:
        await WorkflowRepo(session, creator).create_workflow(wf_id=workflow, name='History privacy')
        repo = WorkflowHistoryRepo(session)
        for identifier, actor in ((creator_run, creator), (recipient_run, recipient)):
            await repo.create(execution_id=identifier, tenant_id=tenant, wf_id=workflow,
                source_type='workflow', source_id=workflow, initiator_user_id=actor,
                workflow={}, inputs={'private_input': actor}, approvers={}, workflow_version='v1.sv0')
    # Even a successful source-level inspect/cancel check must not grant the
    # recipient access to someone else's execution. Assignment is tested by
    # the existing approval suite; this test has no assigned approvals.
    monkeypatch.setattr(routes, '_authorize_source', AsyncMock())
    async with session_scope(tenant_id=tenant) as session:
        auth = SimpleNamespace(user_id=recipient, active_organization_id=tenant)
        request = SimpleNamespace()
        result = await routes.history(request=request, source_type='workflow', source_id=workflow,
            statuses=None, mine=False, before_time=None, before_id=None, limit=50,
            auth=auth, session=session, service=object())
        assert len(result['items']) == 1
        assert set(result['items'][0]) == {
            'id', 'created_at', 'status', 'input_index', 'pending_approvals',
        }
        repo = WorkflowHistoryRepo(session)
        own = await routes._authorize_detail(request, auth, object(), repo, recipient_run)
        assert str(own['id']) == recipient_run
        with pytest.raises(HTTPException) as denied:
            await routes._authorize_detail(request, auth, object(), repo, creator_run)
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as denied_cancel:
            await routes.cancel(request=request, execution_id=uuid.UUID(creator_run), auth=auth,
                                session=session, service=object())
        assert denied_cancel.value.status_code == 404
        auth.user_id = creator
        result = await routes.history(request=request, source_type='workflow', source_id=workflow,
            statuses=None, mine=False, before_time=None, before_id=None, limit=50,
            auth=auth, session=session, service=object())
        assert len(result['items']) == 2


@pytest.mark.asyncio
async def test_recipient_reads_own_cross_organization_history_through_http(pg_engine, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from vibecanvas_api.app import build_app
    from vibecanvas_api.config import config
    from tests.test_workflow_authorization_integration import _register, _headers, _RelationshipStore
    monkeypatch.setattr(config, 'resource_sharing_enabled', True)
    app = build_app()
    app.state.openfga_client = _RelationshipStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        owner_headers, creator = await _register(client, 'history_source')
        recipient_headers, recipient = await _register(client, 'history_caller')
        made = await client.post('/api/v1/workflows', headers=_headers(owner_headers), json={'name': 'Shared history source'})
        assert made.status_code == 201, made.text
        wf_id = made.json()['wf_id']
        resolved = await client.post(f'/api/v1/resource-access/workflow/{wf_id}/resolve-target',
            headers=_headers(owner_headers), json={'target_type': 'user', 'identifier': recipient['email']})
        assert resolved.status_code == 200, resolved.text
        granted = await client.post(f'/api/v1/workflows/{wf_id}/access',
            headers=_headers(owner_headers, **{'Idempotency-Key': uuid.uuid4().hex}),
            json={'relation': 'operator', 'resolution_token': resolved.json()['target']['resolution_token']})
        assert granted.status_code == 201, granted.text
        identifiers = []
        for actor in (creator, recipient):
            execution = str(uuid.uuid4())
            identifiers.append(execution)
            async with session_scope(tenant_id=actor['active_organization_id'], user_id=actor['user_id']) as db:
                await WorkflowHistoryRepo(db).create(execution_id=execution,
                    tenant_id=actor['active_organization_id'], wf_id=wf_id, source_type='workflow',
                    source_id=wf_id, initiator_user_id=actor['user_id'], workflow={}, inputs={'actor': actor['user_id']},
                    approvers={}, workflow_version='v1.sv0')
        listing = await client.get('/api/v1/workflow-executions', headers=_headers(recipient_headers),
            params={'source_type': 'workflow', 'source_id': wf_id})
        assert listing.status_code == 200, listing.text
        assert len(listing.json()['items']) == 1
        own = await client.get(f'/api/v1/workflow-executions/{identifiers[1]}', headers=_headers(recipient_headers))
        assert own.status_code == 200, own.text
        assert own.json()['inputs'] == {'actor': recipient['user_id']}
        foreign = await client.get(f'/api/v1/workflow-executions/{identifiers[0]}', headers=_headers(recipient_headers))
        assert foreign.status_code == 404, foreign.text
