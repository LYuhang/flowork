import time
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from vibecanvas_api.app import build_app
from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.local_approval_capability import (
    mint_local_approval_capability, workflow_digest,
)
from vibecanvas_api.services.agent_runtime.model_capability import authorization_model_generation
from tests.test_workflow_authorization_integration import (
    _browser_sessions, _register, _headers, _RelationshipStore,
)


@pytest.mark.asyncio
async def test_local_approval_real_database_and_browser_decision(pg_engine):
    app = build_app()
    app.state.openfga_client = _RelationshipStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        headers, owner = await _register(client, 'local_approval_owner')
        made = await client.post('/api/v1/workflows', headers=_headers(headers), json={'name': 'Local approval test'})
        assert made.status_code == 201, made.text
        workflow_id = made.json()['wf_id']
        graph = {'node_1': {'node_id': 'node_1', 'node_type': 'HumanApprovalNode',
            'node_config': {'instruction': 'Confirm', 'timeout_seconds': 60}}}
        token = mint_local_approval_capability(organization_id=owner['active_organization_id'],
            user_id=owner['user_id'], workflow_id=workflow_id, run_id=str(uuid4()), sandbox_id='test-sandbox',
            workflow_digest=workflow_digest(graph), workflow_version='v1.sv0',
            authorization_generation=authorization_model_generation(model_id=config.openfga_authorization_model_id),
            secret=config.signing_secret, ttl_s=120)
        runtime_headers = {'Authorization': 'Bearer ' + token}
        approval_id = uuid4().hex
        base = '/api/internal/local-workflow-approvals/v1'
        body = {'index': 0, 'workflow': graph, 'inputs': {'sample': 1}, 'events': [{
            'type': 'approval_requested', 'seq': 1, 'approval_id': approval_id,
            'node_id': 'node_1', 'deadline': time.time() + 60, 'inputs': {}}]}
        admitted = await client.post(base+'/events', headers=runtime_headers, json=body)
        assert admitted.status_code == 200, admitted.text
        execution_id = admitted.json()['execution_id']
        detail_url = '/api/v1/workflow-executions/' + execution_id
        detail = await client.get(detail_url, headers=_headers(headers))
        assert detail.status_code == 200, detail.text
        assert detail.json()['status'] == 'waiting_approval'
        assert detail.json()['approvals'][0]['can_decide'] is True
        resolution = {'type': 'approval_resolved', 'seq': 2, 'approval_id': approval_id,
            'node_id': 'node_1', 'reason': 'approved', 'approved': True, 'decided_at': time.time()}
        # The runtime cannot manufacture a human decision.
        forged = await client.post(base+'/events', headers=runtime_headers, json={**body, 'events': [resolution]})
        assert forged.status_code == 409, forged.text
        approved = await client.post(detail_url+'/approvals/'+approval_id,
            headers=_headers(headers), json={'approved': True})
        assert approved.status_code == 202, approved.text
        pending = await client.post(base+'/decisions', headers=runtime_headers, json={'index': 0})
        assert pending.status_code == 200, pending.text
        assert pending.json()['decisions'][0]['requested_decision'] is True
        completed = await client.post(base+'/events', headers=runtime_headers, json={**body, 'events': [resolution,
            {'type': 'result', 'seq': 3, 'status': 'succeeded', 'final_outputs': {'__end__': {'approved': True}},
             'error_dict': {}, 'execution_time': 1}]})
        assert completed.status_code == 200, completed.text
        final = await client.get(detail_url, headers=_headers(headers))
        assert final.status_code == 200, final.text
        assert final.json()['status'] == 'succeeded'
        assert final.json()['approvals'][0]['status'] == 'approved'
