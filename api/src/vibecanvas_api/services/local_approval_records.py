"""Persist approval evidence from local execution without owning its process."""
from datetime import datetime, timezone
import time
from uuid import UUID

from sqlalchemy import text

from vibecanvas_api.services.agent_runtime.local_approval_capability import workflow_digest
from vibecanvas_api.services.workflow_approvers import resolve_workflow_approvers
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo, HistoryConflict


def validate_submission(capability, body):
    if not isinstance(body, dict) or set(body) != {'index', 'workflow', 'inputs', 'events'}:
        raise ValueError('invalid approval evidence envelope')
    execution_id = capability.sample_id(body['index'])
    if not isinstance(body['workflow'], dict) or workflow_digest(body['workflow']) != capability.workflow_digest:
        raise ValueError('approval workflow snapshot mismatch')
    if not isinstance(body['inputs'], dict):
        raise ValueError('invalid approval execution inputs')
    if not isinstance(body['events'], list) or not 1 <= len(body['events']) <= 1000:
        raise ValueError('invalid approval evidence batch')
    for event in body['events']:
        if not isinstance(event, dict) or event.get('type') not in {'node_event', 'approval_requested', 'approval_resolved', 'result'}:
            raise ValueError('invalid approval evidence event')
        if type(event.get('seq')) is not int or event['seq'] < 1:
            raise ValueError('invalid approval evidence sequence')
    return execution_id


async def append_evidence(*, session, capability, body):
    execution_id = validate_submission(capability, body)
    repo = WorkflowHistoryRepo(session)
    run = await repo.get(execution_id, lock=True)
    if run is None:
        if not any(isinstance(node, dict) and node.get('node_type') == 'HumanApprovalNode' for node in body['workflow'].values()):
            raise HistoryConflict('approval_request_required')
        approvers = await resolve_workflow_approvers(session, tenant_id=capability.organization_id,
            workflow=body['workflow'], initiator_user_id=capability.user_id, require_explicit=False)
        await repo.create(execution_id=execution_id, tenant_id=capability.organization_id,
            wf_id=capability.workflow_id, source_type='workflow', source_id=capability.workflow_id,
            initiator_user_id=capability.user_id, workflow=body['workflow'], inputs=body['inputs'],
            approvers=approvers, input_index=body['index'], workflow_version=capability.workflow_version)
        await repo.bind_runtime(execution_id, capability.run_id, process={
            **capability.runtime_process,
            'kind': 'local_cli', 'sandbox_id': capability.sandbox_id, 'run_id': capability.run_id})
        run = await repo.get(execution_id, lock=True)
    if (str(run['initiator_user_id']) != capability.user_id or run['wf_id'] != capability.workflow_id
            or run['generation'] != capability.run_id):
        raise HistoryConflict('approval_execution_scope_mismatch')
    frames = []
    for event in body['events']:
        if event['seq'] <= run['last_seq']:
            continue
        frame = {**event, 'invocation_id': execution_id, 'generation': capability.run_id}
        if event['type'] == 'approval_requested':
            node = body['workflow'].get(event.get('node_id'), {})
            if node.get('node_type') != 'HumanApprovalNode':
                raise ValueError('approval node mismatch')
            seconds = node['node_config']['timeout_seconds']
            deadline = event.get('deadline')
            if type(deadline) not in {int, float} or not time.time() - 5 <= deadline <= time.time() + seconds + 5:
                raise ValueError('invalid approval deadline')
            if not isinstance(event.get('approval_id'), str):
                raise ValueError('invalid approval identifier')
            UUID(event['approval_id'])
            frame['instruction'] = node['node_config']['instruction']
            frame['approver_email'] = node['node_config'].get('approver_email', '')
        elif event['type'] == 'approval_resolved':
            # A sandbox capability cannot approve itself. Only the existing
            # authenticated browser decision endpoint writes requested_decision.
            row = (await session.execute(text('''SELECT status, requested_decision, deadline
                FROM workflow_execution_approvals WHERE execution_id=:execution AND id=:approval'''),
                {'execution': UUID(execution_id), 'approval': event.get('approval_id')})).mappings().one_or_none()
            if row is None:
                raise HistoryConflict('approval_not_found')
            reason = event.get('reason')
            if reason in {'approved', 'rejected'}:
                if (row['status'] != 'decision_requested' or type(event.get('approved')) is not bool
                        or row['requested_decision'] != event['approved']):
                    raise HistoryConflict('human_decision_required')
            elif reason != 'timeout' or event.get('approved') is not None or row['deadline'] > datetime.now(timezone.utc):
                raise HistoryConflict('invalid_approval_resolution')
            frame['decided_at'] = time.time()
        frames.append(frame)
    if frames:
        await repo.persist_events(execution_id, capability.run_id, frames)
    return {'execution_id': execution_id, 'last_seq': frames[-1]['seq'] if frames else run['last_seq']}


async def decisions(*, session, capability, index):
    execution_id = capability.sample_id(index)
    repo = WorkflowHistoryRepo(session)
    run = await repo.get(execution_id)
    if run is None or str(run['initiator_user_id']) != capability.user_id or run['generation'] != capability.run_id:
        raise KeyError(execution_id)
    return {'execution_id': execution_id, 'status': run['status'],
            'cancel_requested': run['cancel_requested_at'] is not None,
            'decisions': await repo.pending_commands(execution_id)}
