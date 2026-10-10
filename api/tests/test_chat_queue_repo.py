"""Queued next turns are durable intent, not an automatic retry mechanism."""
import asyncio
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_inputs_repo import fixture, session
from vibecanvas_api.storage.chat_queue_repo import ChatQueueConflict, ChatQueueRepo


@pytest.mark.asyncio
async def test_queue_encrypts_full_input_and_only_claims_once_after_success(client):
    me, chat, run = await fixture(client)
    input_id = uuid.uuid4().hex
    message = {'content': 'Private next instruction', 'attachments': [{'path': '/data/input.txt'}]}
    async with session(me) as db:
        repo = ChatQueueRepo(db, me['user_id'])
        assert (await repo.enqueue(chat, run, input_id, message))['status'] == 'queued'
        row = (await db.execute(text('SELECT * FROM chat_queued_inputs WHERE input_id=:id'), {'id': input_id})).mappings().one()
        assert 'Private next instruction' not in str(dict(row))
        assert '/data/input.txt' not in str(dict(row))
        assert await repo.claim(chat, run) is None
    async with session(me) as db:
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
    async with session(me) as db:
        claimed = await ChatQueueRepo(db, me['user_id']).claim(chat, run)
        assert claimed['message'] == message
        assert claimed['status'] == 'claimed'
    # A fresh process/session cannot take the same dispatch again.
    async with session(me) as db:
        repo = ChatQueueRepo(db, me['user_id'])
        assert await repo.claim(chat, run) is None
        with pytest.raises(ChatQueueConflict, match='not_cancellable'):
            await repo.cancel(chat, input_id)
        assert await db.scalar(text('SELECT count(*) FROM agent_runs WHERE chat_id=:chat'), {'chat': chat}) == 1
        assert await db.scalar(text('SELECT count(*) FROM chat_messages WHERE message_id=:id'), {'id': input_id}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['failed', 'cancelled'])
async def test_failed_or_stopped_turn_blocks_queue_without_replay(client, status):
    me, chat, run = await fixture(client)
    input_id = uuid.uuid4().hex
    async with session(me) as db:
        await ChatQueueRepo(db, me['user_id']).enqueue(chat, run, input_id, {'content': 'Next'})
        await db.execute(text('UPDATE agent_runs SET status=:status WHERE run_id=:run'), {'run': run, 'status': status})
    async with session(me) as db:
        repo = ChatQueueRepo(db, me['user_id'])
        assert await repo.claim(chat, run) is None
        assert (await repo.get(chat, input_id))['status'] == 'blocked'
        assert (await repo.cancel(chat, input_id))['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_two_windows_can_only_queue_one_input(client):
    me, chat, run = await fixture(client)
    async def enqueue():
        try:
            async with session(me) as db:
                return await ChatQueueRepo(db, me['user_id']).enqueue(chat, run, uuid.uuid4().hex, {'content': 'Next'})
        except ChatQueueConflict:
            return None
    results = await asyncio.gather(enqueue(), enqueue())
    assert sum(result is not None for result in results) == 1
    queued = next(result for result in results if result)
    async with session(me) as db:
        assert await ChatQueueRepo(db, str(uuid.uuid4())).get(chat, queued['input_id']) is None
        await ChatQueueRepo(db, me['user_id']).cancel(chat, queued['input_id'])
        assert (await ChatQueueRepo(db, me['user_id']).enqueue(chat, run, uuid.uuid4().hex, {'content': 'Replacement'}))['status'] == 'queued'


@pytest.mark.asyncio
async def test_cancel_and_claim_race_has_only_one_winner(client):
    me, chat, run = await fixture(client)
    input_id = uuid.uuid4().hex
    async with session(me) as db:
        await ChatQueueRepo(db, me['user_id']).enqueue(chat, run, input_id, {'content': 'Next'})
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
    async def cancel():
        try:
            async with session(me) as db:
                return await ChatQueueRepo(db, me['user_id']).cancel(chat, input_id)
        except ChatQueueConflict:
            return None
    async def claim():
        async with session(me) as db:
            return await ChatQueueRepo(db, me['user_id']).claim(chat, run)
    results = await asyncio.gather(cancel(), claim())
    assert sum(result is not None for result in results) == 1
    async with session(me) as db:
        assert (await ChatQueueRepo(db, me['user_id']).get(chat, input_id))['status'] in {'claimed', 'cancelled'}
        assert await ChatQueueRepo(db, me['user_id']).claim(chat, run) is None


@pytest.mark.asyncio
async def test_queue_changes_notify_existing_chat_observers(client, pg_url):
    from contextlib import aclosing
    import psycopg

    me, chat, run = await fixture(client)
    input_id = uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(pg_url.replace('+asyncpg', ''), autocommit=True) as listener:
        await listener.execute('LISTEN flowork_chat_activity')
        for cancel in (False, True):
            async with aclosing(listener.notifies(timeout=3, stop_after=1)) as notifications:
                async with session(me) as db:
                    repo = ChatQueueRepo(db, me['user_id'])
                    if cancel:
                        await repo.cancel(chat, input_id)
                    else:
                        await repo.enqueue(chat, run, input_id, {'content': 'Next'})
                assert (await anext(notifications)).payload == chat


@pytest.mark.asyncio
async def test_queue_read_after_process_loss_does_not_dispatch_or_claim(client):
    from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo

    me, chat, run = await fixture(client)
    input_id = uuid.uuid4().hex
    async with session(me) as db:
        repo = ChatQueueRepo(db, me['user_id'])
        await repo.enqueue(chat, run, input_id, {'content': 'Next'})
        await db.execute(text("UPDATE agent_runs SET status='failed' WHERE run_id=:run"), {'run': run})
    async with session(me) as db:
        repo = ChatQueueRepo(db, me['user_id'])
        assert (await repo.latest(chat))['status'] == 'blocked'
        assert (await repo.get(chat, input_id))['status'] == 'queued'
        await repo.cancel(chat, input_id)
        await db.execute(text("UPDATE agent_runs SET status='running' WHERE run_id=:run"), {'run': run})
        input_id = uuid.uuid4().hex
        await repo.enqueue(chat, run, input_id, {'content': 'Next'})
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
        assert (await repo.latest(chat))['error_code'] == 'previous_turn_completed_pending_dispatch'
        await repo.claim(chat, run)
    async with session(me) as db:
        repo = ChatQueueRepo(db, me['user_id'])
        assert (await repo.latest(chat))['status'] == 'unknown'
        assert await repo.claim(chat, run) is None
        await AgentRunsRepo(db).create(run_id='accepted-' + input_id, tenant_id=me['tenant_id'],
            chat_id=chat, creator_user_id=me['user_id'], client_request_id=input_id, input_snapshot={})
    async with session(me) as db:
        result = await ChatQueueRepo(db, me['user_id']).latest(chat)
        assert result['status'] == 'started'
        assert result['started_run_id'] == 'accepted-' + input_id
        assert await db.scalar(text('SELECT count(*) FROM agent_runs WHERE chat_id=:chat'), {'chat': chat}) == 2
