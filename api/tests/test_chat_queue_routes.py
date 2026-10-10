import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
async def test_queue_api_persists_queryable_intent_without_starting_another_turn(client):
    headers, me, chat = await seed(client)
    run = uuid.uuid4().hex
    endpoint = f'/api/v1/chats/{chat}/queued-input'
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
    assert (await client.get(endpoint, headers=headers)).json() == {'input': None}
    body = {'input_id': uuid.uuid4().hex, 'after_run_id': run, 'message': {'content': 'Next instruction'}}
    response = await client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 201, response.text
    assert response.json()['status'] == 'queued'
    current = (await client.get(endpoint, headers=headers)).json()['input']
    assert current['input_id'] == body['input_id']
    assert current['message']['content'] == 'Next instruction'
    duplicate = await client.post(endpoint, headers=headers, json={**body, 'input_id': uuid.uuid4().hex})
    assert duplicate.status_code == 409
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        assert await db.scalar(text('SELECT count(*) FROM agent_runs WHERE chat_id=:chat'), {'chat': chat}) == 1
    response = await client.delete(endpoint + '/' + body['input_id'], headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'cancelled'
    assert (await client.get(endpoint, headers=headers)).json()['input']['status'] == 'cancelled'
    other_headers, _, _ = await seed(client)
    assert (await client.get(endpoint, headers=other_headers)).status_code == 404
    assert (await client.delete(endpoint + '/' + body['input_id'], headers=other_headers)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    {'content': '   '},
    {'content': 'Next', 'client_request_id': 'user-chosen-execution'},
])
async def test_queue_rejects_empty_input_and_execution_override(client, message):
    headers, _, chat = await seed(client)
    response = await client.post(f'/api/v1/chats/{chat}/queued-input', headers=headers,
        json={'input_id': uuid.uuid4().hex, 'after_run_id': 'run', 'message': message})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_queue_consumes_draft_once_and_preserves_the_next_identical_draft(client):
    from vibecanvas_api.storage.chat_draft_repo import ChatDraftRepo

    headers, me, chat = await seed(client)
    run, input_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
        await ChatDraftRepo(db, me['user_id']).mutate(chat, {
            'kind': 'text', 'operation_id': uuid.uuid4().hex, 'previous_text': '', 'text': 'Same instruction',
        })
    response = await client.post(f'/api/v1/chats/{chat}/queued-input', headers=headers,
        json={'input_id': input_id, 'after_run_id': run, 'message': {'content': 'Same instruction'}})
    assert response.status_code == 201, response.text
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        drafts = ChatDraftRepo(db, me['user_id'])
        consumed = await drafts.get(chat)
        assert consumed['text'] == ''
        await drafts.mutate(chat, {'kind': 'text', 'operation_id': uuid.uuid4().hex,
                                  'previous_text': '', 'text': 'Same instruction'})
        # Later ordinary submission of the accepted queue uses its input ID.
        # It must not consume another generation, even if the text is identical.
        await drafts.mutate(chat, {'kind': 'consume', 'operation_id': input_id,
                                  'text': 'Same instruction', 'attachment_keys': []}, internal=True)
        latest = await drafts.get(chat)
        assert latest['text'] == 'Same instruction'
        assert latest['generation'] == consumed['generation']
