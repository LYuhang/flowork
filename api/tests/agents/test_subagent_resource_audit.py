import json
from uuid import uuid4
import pytest
from vibecanvas_api.agents.tools.subagent.audit import ResourceAudit
from vibecanvas_api.services.exec_events import to_exec_update
from vibecanvas_api.services.node_results import persist_node_frame_payload


@pytest.mark.asyncio
async def test_evidence_reaches_durable_node_payload_without_command_or_output_secrets():
    skill = str(uuid4())
    path = f'/skills/{skill}/' + 'a' * 64 + '/scripts/subtotal.py'
    audit = ResourceAudit({'skills': [{'id': skill, 'name': 'audit', 'revision_hash': 'a' * 64}], 'mcp_servers': []})
    await audit.record({'role': 'ai', 'text': 'private reasoning', 'tool_calls': [{
        'id': 'call1', 'name': 'bash', 'args': {'command': f'API_KEY=secret python {path} secret-input'}}]})
    await audit.record({'role': 'tool', 'tool_call_id': 'call1', 'status': 'success',
                        'text': json.dumps({'exit_code': 0, 'status': 'success', 'stdout': 'secret-output'})})
    _, frame = to_exec_update({'node_id': 'node1', 'status': 'success', 'output': {'total': 63},
                              'resource_audit': audit.payload}, 'execution')
    payload = persist_node_frame_payload(frame)
    assert payload['output'] == {'total': 63}
    assert payload['resource_audit']['tools'] == [
        {'name': 'bash', 'status': 'success', 'skill_paths': [path], 'exit_code': 0}]
    assert 'secret' not in json.dumps(payload)
    assert 'reasoning' not in json.dumps(payload)


@pytest.mark.asyncio
async def test_failed_tools_and_trace_limit_remain_explicit():
    audit = ResourceAudit({})
    for i in range(300):
        await audit.record({'tool_calls': [{'id': str(i), 'name': 'bash', 'args': {'command': 'false'}}]})
    await audit.record({'role': 'tool', 'tool_call_id': '0', 'status': 'error', 'text': 'sensitive failure'})
    assert audit.payload['truncated'] is True
    assert len(audit.payload['tools']) == 256
    assert audit.payload['tools'][0]['status'] == 'error'
    assert 'sensitive' not in json.dumps(audit.payload)
    _, frame = to_exec_update({'node_id': 'node1', 'status': 'error', 'error_message': 'failed',
                              'resource_audit': audit.payload}, 'execution')
    assert persist_node_frame_payload(frame)['resource_audit'] == audit.payload
