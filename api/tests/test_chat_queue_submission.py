from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.services.chat_queue_submission import finish_chat_queue
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_queue_repo import ChatQueueRepo
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_submission_revalidates_queued_identity_before_ordinary_message_path(client, monkeypatch, revoked):
    headers, me, chat = await seed(client)
    run, input_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
    response = await client.post(f'/api/v1/chats/{chat}/queued-input', headers=headers, json={
        'input_id': input_id, 'after_run_id': run, 'message': {'content': 'Queued next turn'},
    })
    assert response.status_code == 201, response.text
    assert 'authority' not in response.json()
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
        if revoked:
            await db.execute(text('DELETE FROM sessions WHERE user_id=:owner'), {'owner': me['user_id']})

    async def ordinary_message(scope, chat_id, body, request, **kwargs):
        assert chat_id == chat
        assert body.content == 'Queued next turn'
        assert body.client_request_id == input_id
        assert kwargs['auth'].user_id == me['user_id']
        await kwargs['agent_runs_repo'].create(run_id='next-' + input_id, tenant_id=me['tenant_id'],
            chat_id=chat, creator_user_id=me['user_id'], client_request_id=input_id, input_snapshot={})
        await kwargs['session'].commit()
    submit = AsyncMock(side_effect=ordinary_message)
    monkeypatch.setattr('vibecanvas_api.routes.chats.post_message', submit)
    args = dict(tenant_id=me['tenant_id'], user_id=me['user_id'], chat_id=chat,
                finished_run_id=run, app=client._transport.app)
    await finish_chat_queue(**args)
    await finish_chat_queue(**args)
    assert submit.await_count == (0 if revoked else 1)
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        receipt = await ChatQueueRepo(db, me['user_id']).get(chat, input_id)
        assert receipt['status'] == ('failed' if revoked else 'started')
        if revoked:
            assert receipt['error_code'] == 'session_revoked'


@pytest.mark.usefixtures('fake_codex_cli')
@pytest.mark.asyncio
@pytest.mark.parametrize('intervening_turn', [False, True])
async def test_queue_dispatch_uses_real_message_route_and_consumes_draft_once(client, intervening_turn):
    import asyncio
    from vibecanvas_api.routes.chats import TURN_TASKS
    from vibecanvas_api.storage.chat_draft_repo import ChatDraftRepo

    headers, me, chat = await seed(client)
    bootstrap = await client.get('/api/v1/chats/bootstrap?surface=chat', headers=headers)
    assert bootstrap.status_code == 200, bootstrap.text
    run, input_id = uuid.uuid4().hex, uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text('UPDATE chats SET scope_id=:scope WHERE chat_id=:chat'),
                         {'scope': bootstrap.json()['carrier_scope_id'], 'chat': chat})
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
    response = await client.post(f'/api/v1/chats/{chat}/queued-input', headers=headers, json={
        'input_id': input_id, 'after_run_id': run, 'message': {'content': 'Next instruction'},
    })
    assert response.status_code == 201, response.text
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
        await ChatDraftRepo(db, me['user_id']).mutate(chat, {'kind': 'text', 'operation_id': uuid.uuid4().hex,
            'previous_text': '', 'text': 'Next instruction'})
    if intervening_turn:
        async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
            later = await AgentRunsRepo(db).create(run_id=uuid.uuid4().hex, tenant_id=me['tenant_id'],
                chat_id=chat, creator_user_id=me['user_id'], client_request_id=uuid.uuid4().hex, input_snapshot={})
            later.status = 'completed'
    await finish_chat_queue(tenant_id=me['tenant_id'], user_id=me['user_id'], chat_id=chat,
                            finished_run_id=run, app=client._transport.app)
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        receipt = await ChatQueueRepo(db, me['user_id']).latest(chat)
        if intervening_turn:
            assert receipt['status'] == 'failed'
            assert receipt['error_code'] == 'chat_has_newer_run'
            assert await db.scalar(text('SELECT count(*) FROM agent_runs WHERE chat_id=:chat'), {'chat': chat}) == 2
            return
        assert receipt['status'] == 'started', receipt
        assert (await ChatDraftRepo(db, me['user_id']).get(chat))['text'] == 'Next instruction'
        next_run = receipt['started_run_id']
    task = TURN_TASKS.get(next_run)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
