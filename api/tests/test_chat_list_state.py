"""Pin preferences leave history, execution and workspace ownership intact."""
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_attention import attention_fixture
from tests.test_chat_engagement import seed
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
async def test_pin_and_legacy_archive_visibility(client):
    headers, me, chat, scope, _ = await attention_fixture(client)
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        project = await db.scalar(text('SELECT project_id FROM chats WHERE chat_id=:id'), {'id': chat})
        await ChatRepo(db, me['user_id']).persist_message(chat, {
            'message_id': 'archived-history', 'role': 'user', 'content': {'text': 'Keep this message'},
        })
    newer = str(uuid.uuid4())
    response = await client.put(f'/api/v1/chat-scopes/{scope}/chats/{newer}', headers=headers, json={'project_id': project})
    assert response.status_code in (200, 201), response.text
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await ChatRepo(db, me['user_id']).persist_message(newer, {
            'message_id': 'recent', 'role': 'user', 'content': {'text': 'Newer chat'},
        })
        before = (await db.execute(text('SELECT project_id, runtime_state_ref, browser_session_id, deleted_at, last_message_at FROM chats WHERE chat_id=:id'), {'id': chat})).mappings().one()
        run_before = (await db.execute(text('SELECT run_id,status FROM agent_runs WHERE chat_id=:id'), {'id': chat})).mappings().all()
    listing = f'/api/v1/chat-scopes/{scope}/chats'
    patch = f'/api/v1/chats/{chat}/list-state'
    async def listed(state='all'):
        result = await client.get(listing, params={'archive_state': state, 'limit': 1}, headers=headers)
        assert result.status_code == 200, result.text
        return result.json()
    assert (await listed())['items'][0]['chat_id'] == newer
    pinned = await client.patch(patch, headers=headers, json={'pinned': True})
    assert pinned.status_code == 200, pinned.text
    pin_time = pinned.json()['pinned_at']
    assert pin_time and pinned.json()['archived_at'] is None
    assert (await listed())['items'][0]['chat_id'] == chat
    # Repeating the same preference does not bump its position or timestamp.
    assert (await client.patch(patch, headers=headers, json={'pinned': True})).json()['pinned_at'] == pin_time
    # Previously archived conversations are ordinary history now.
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text('UPDATE chats SET archived_at=now() WHERE chat_id=:id'), {'id': chat})
    assert (await listed('active'))['items'][0]['chat_id'] == chat
    assert (await listed())['total'] == 2
    history = await client.get(f'{listing}/{chat}/messages', headers=headers)
    assert history.status_code == 200 and 'Keep this message' in history.text
    pending = await client.get(f'/api/v1/chat-scopes/{scope}/attention', headers=headers)
    assert pending.status_code == 200 and len(pending.json()['items']) == 3
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        after = (await db.execute(text('SELECT project_id, runtime_state_ref, browser_session_id, deleted_at, last_message_at FROM chats WHERE chat_id=:id'), {'id': chat})).mappings().one()
        assert dict(after) == dict(before)
        assert (await db.execute(text('SELECT run_id,status FROM agent_runs WHERE chat_id=:id'), {'id': chat})).mappings().all() == run_before
        repo = ChatRepo(db, me['user_id'])
        assert any(x['chat_id'] == chat for x in await repo.list_sessions(scope))
        matches = await repo.search_titles('New chat', '', (chat, newer))
        assert chat in [x['id'] for x in matches['items']]
    assert (await client.patch(patch, headers=headers, json={'pinned': False})).json()['pinned_at'] is None
    assert (await listed('active'))['items'][0]['chat_id'] == newer


@pytest.mark.asyncio
async def test_list_state_validation_and_private_ownership(client):
    headers, me, chat, scope, _ = await attention_fixture(client)
    path = f'/api/v1/chats/{chat}/list-state'
    for body in ({}, {'pinned': None}, {'stop_run': True}, {'archived': True}, {'archived': False}):
        assert (await client.patch(path, json=body, headers=headers)).status_code == 422
    other_headers, _, _ = await seed(client)
    assert (await client.patch(path, json={'pinned': True}, headers=other_headers)).status_code in (403, 404)
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        assert await ChatRepo(db, str(uuid.uuid4())).set_list_state(chat, pinned=True) is None
        assert await db.scalar(text('SELECT archived_at FROM chats WHERE chat_id=:id'), {'id': chat}) is None
        await db.execute(text('UPDATE chats SET deleted_at=now() WHERE chat_id=:id'), {'id': chat})
    assert (await client.patch(path, json={'pinned': True}, headers=headers)).status_code == 404
