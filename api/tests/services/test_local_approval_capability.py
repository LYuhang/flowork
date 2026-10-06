from dataclasses import asdict
from uuid import uuid4

import pytest

from vibecanvas_api.services.agent_runtime.local_approval_capability import (
    mint_local_approval_capability, verify_local_approval_capability, workflow_digest,
)
from vibecanvas_api.services.agent_runtime.workflow_model_capability import verify_runtime_workflow_model_capability
from vibecanvas_api.services.local_approval_records import validate_submission


def claims():
    return dict(organization_id=str(uuid4()), user_id=str(uuid4()), workflow_id='wf',
        run_id=str(uuid4()), sandbox_id='test-sandbox', workflow_digest=workflow_digest({'node': {}}),
        workflow_version='v1.sv2', authorization_generation='test')


def test_approval_capability_is_signed_expiring_and_audience_separated():
    value = claims()
    token = mint_local_approval_capability(**value, secret='test', ttl_s=60, now=100)
    cap = verify_local_approval_capability(token, secret='test', now=101)
    assert cap and cap.principal_id == value['user_id']
    assert cap.execution_id == value['workflow_id']
    assert cap.sample_id(0) == cap.sample_id(0) != cap.sample_id(1)
    assert verify_local_approval_capability(token, secret='other', now=101) is None
    assert verify_local_approval_capability(token, secret='test', now=160) is None
    assert verify_runtime_workflow_model_capability(token, secret='test', now=101) is None
    assert 'principal_type' not in asdict(cap)


@pytest.mark.parametrize('change', [{'organization_id': 'bad'}, {'run_id': 'bad'},
    {'workflow_digest': 'fake'}, {'workflow_version': 'latest'}, {'workflow_id': ''},
    {'audience': 'runtime-workflow-model'}])
def test_invalid_approval_claims_rejected(change):
    with pytest.raises(ValueError):
        mint_local_approval_capability(**{**claims(), **change}, secret='test', ttl_s=60, now=100)


@pytest.mark.parametrize('index', [-1, True, '0'])
def test_sample_identifiers_reject_invalid_indices(index):
    cap = verify_local_approval_capability(
        mint_local_approval_capability(**claims(), secret='test', ttl_s=60, now=100), secret='test', now=101)
    with pytest.raises(ValueError):
        cap.sample_id(index)


def test_approval_evidence_cannot_substitute_graph_or_operation():
    cap = verify_local_approval_capability(
        mint_local_approval_capability(**claims(), secret='test', ttl_s=60, now=100), secret='test', now=101)
    body = {'index': 0, 'workflow': {'node': {}}, 'inputs': {},
            'events': [{'type': 'approval_requested', 'seq': 1}]}
    assert validate_submission(cap, body) == cap.sample_id(0)
    for changed in ({**body, 'workflow': {'node': {'changed': True}}},
                    {**body, 'events': [{'type': 'approve', 'seq': 1}]},
                    {**body, 'events': [{'type': 'result', 'seq': True}]}):
        with pytest.raises(ValueError):
            validate_submission(cap, changed)


@pytest.mark.asyncio
@pytest.mark.parametrize('stored_status,stored_decision,reported_decision,allowed', [
    ('pending', None, True, False),
    ('decision_requested', False, True, False),
    ('decision_requested', True, True, True),
    ('decision_requested', False, False, True),
])
async def test_sandbox_cannot_forge_human_decision(monkeypatch, stored_status, stored_decision, reported_decision, allowed):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.services import local_approval_records as records
    from vibecanvas_api.storage.workflow_history_repo import HistoryConflict
    cap = verify_local_approval_capability(
        mint_local_approval_capability(**claims(), secret='test', ttl_s=60, now=100), secret='test', now=101)
    repo = SimpleNamespace(get=AsyncMock(return_value={'initiator_user_id': cap.user_id,
        'wf_id': cap.workflow_id, 'generation': cap.run_id, 'last_seq': 1}), persist_events=AsyncMock())
    row = {'status': stored_status, 'requested_decision': stored_decision, 'deadline': datetime.now(timezone.utc)}
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        mappings=lambda: SimpleNamespace(one_or_none=lambda: row))))
    monkeypatch.setattr(records, 'WorkflowHistoryRepo', lambda session: repo)
    body = {'index': 0, 'workflow': {'node': {}}, 'inputs': {}, 'events': [{
        'type': 'approval_resolved', 'seq': 2, 'approval_id': uuid4().hex,
        'reason': 'approved' if reported_decision else 'rejected', 'approved': reported_decision}]}
    if allowed:
        assert (await records.append_evidence(session=session, capability=cap, body=body))['last_seq'] == 2
        repo.persist_events.assert_awaited_once()
    else:
        with pytest.raises(HistoryConflict, match='human_decision_required'):
            await records.append_evidence(session=session, capability=cap, body=body)
        repo.persist_events.assert_not_awaited()
