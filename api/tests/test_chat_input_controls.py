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
    assert (await client.get(endpoint, headers=headers)).json()['unavailable_reason'] == 'steer_already_submitting'
    receipt_url = f'/api/v1/chats/{chat}/inputs/{input_id}'
    response = await client.get(receipt_url, headers=headers)
    assert response.status_code == 200
    assert response.json()['text'] == 'Private extra input'
    assert response.json()['status'] == 'submitting'
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
    assert (await client.get(endpoint, headers=headers)).json()['available_controls'] == []
    assert (await client.get(receipt_url, headers=headers)).json()['status'] == 'unknown'
    # A different account cannot use a known Chat/Run/input ID to recover text.
    other_headers, _, _ = await seed(client)
    assert (await client.get(endpoint, headers=other_headers)).status_code in {403, 404}
    assert (await client.get(receipt_url, headers=other_headers)).status_code in {403, 404}
