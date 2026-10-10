import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_inputs_repo import ChatInputsRepo
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
async def test_controls_require_native_readiness_and_receipts_survive_reload(client):
    headers, me, chat = await seed(client)
    run = uuid.uuid4().hex
    endpoint = f'/api/v1/chats/{chat}/runs/{run}/input-controls'
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
    response = await client.get(endpoint, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['available_controls'] == []
    assert response.json()['unavailable_reason'] == 'steer_not_supported'
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await AgentRunsRepo(db).append_event(run_id=run, seq=1, event_type='RUNTIME_CAPABILITIES',
            payload={'supported_controls': ['steer']}, tenant_id=me['tenant_id'])
    assert (await client.get(endpoint, headers=headers)).json()['available_controls'] == ['steer']
    input_id = uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await ChatInputsRepo(db, me['user_id']).create(chat, run, input_id, 'Private extra input')
    pending = (await client.get(endpoint, headers=headers)).json()
    assert pending['unavailable_reason'] == 'steer_already_submitting'
    assert pending['latest_input']['input_id'] == input_id
    assert pending['latest_input']['text'] == 'Private extra input'
    receipt_url = f'/api/v1/chats/{chat}/inputs/{input_id}'
    response = await client.get(receipt_url, headers=headers)
    assert response.status_code == 200
    assert response.json()['text'] == 'Private extra input'
    assert response.json()['status'] == 'submitting'
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
    assert (await client.get(endpoint, headers=headers)).json()['available_controls'] == []
    assert (await client.get(receipt_url, headers=headers)).json()['status'] == 'unknown'
    restored = (await client.get(f'/api/v1/chats/{chat}/input-controls', headers=headers)).json()
    assert restored['run_id'] == run
    assert restored['latest_input']['status'] == 'unknown'
    assert restored['available_controls'] == []
    # A different account cannot use a known Chat/Run/input ID to recover text.
    other_headers, _, _ = await seed(client)
    assert (await client.get(endpoint, headers=other_headers)).status_code in {403, 404}
    assert (await client.get(receipt_url, headers=other_headers)).status_code in {403, 404}


@pytest.mark.asyncio
async def test_submit_notifies_original_runtime_and_persists_acknowledgment(client):
    import asyncio
    from tests.services.test_agent_runtime_orchestrator import _Manager
    from vibecanvas_api.services.agent_runtime.orchestrator import AgentRuntimeOrchestrator
    from vibecanvas_api.services.agent_runtime.protocol import RuntimeOpenRequest, RuntimeTurnRequest
    from vibecanvas_api.services.agent_run_writer import AgentRunWriter

    headers, me, chat = await seed(client)
    run = uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
    ready, received = asyncio.Event(), asyncio.Event()
    sent = []

    class Sandbox:
        async def run_agent_runtime_stream(self, request):
            common = {'chat_id': chat, 'turn_id': run, 'runtime_type': 'codex', 'runtime_session_id': 'runtime'}
            yield {**common, 'event_id': 'ready', 'seq': 1, 'type': 'runtime.started', 'payload': {'supported_controls': ['steer']}}
            await received.wait()
            yield {**common, 'event_id': 'ack', 'seq': 2, 'type': 'projection',
                   'payload': {'event_type': 'USER_INPUT_STATUS', 'payload': {'input_id': sent[0]['input_id'], 'status': 'accepted', 'error': None}}}
            yield {**common, 'event_id': 'done', 'seq': 3, 'type': 'runtime.completed'}

        async def send_agent_runtime_control(self, turn_id, response):
            assert turn_id == run
            sent.append(response)
            received.set()

    manager = _Manager()
    manager.session = Sandbox()
    params = dict(tenant_id=me['tenant_id'], user_id=me['user_id'], chat_id=chat,
                  runtime_type='codex', runtime_session_id='runtime', runtime_root='/runtime/.codex')
    writer = AgentRunWriter(run_id=run, tenant_id=me['tenant_id'], chat_id=chat, user_id=me['user_id'])

    async def execute():
        seq = 0
        async for kind, payload in AgentRuntimeOrchestrator(manager).stream_turn(
            open_request=RuntimeOpenRequest(**params),
            turn_request=RuntimeTurnRequest(**params, turn_id=run, message={'role': 'user', 'content': 'original'},
                                           model={'id': 'gpt-test', 'connection_type': 'chatgpt_account'}),
            workspace_scope_id='workspace', stop_event=asyncio.Event(),
        ):
            seq += 1
            await writer.emit(seq, kind, payload)
            if kind == 'RUNTIME_CAPABILITIES':
                ready.set()
        await writer.close()

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(ready.wait(), 10)
        body = {'input_id': uuid.uuid4().hex, 'run_id': run, 'text': 'Add this once'}
        response = await client.post(f'/api/v1/chats/{chat}/inputs', headers=headers, json=body)
        assert response.status_code == 202, response.text
        assert response.json()['status'] == 'submitting'
        await asyncio.wait_for(task, 10)
        assert len(sent) == 1
        assert sent[0]['text'] == body['text']
        receipt = await client.get(f"/api/v1/chats/{chat}/inputs/{body['input_id']}", headers=headers)
        assert receipt.json()['status'] == 'accepted'
        duplicate = await client.post(f'/api/v1/chats/{chat}/inputs', headers=headers, json=body)
        assert duplicate.status_code == 409
        async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
            assert await ChatInputsRepo(db, me['user_id']).claim_pending(chat, run) is None
        assert len(sent) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
