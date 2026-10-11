import uuid
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy import text

from tests.test_chat_inputs_repo import fixture, session
from vibecanvas_api.services.chat_queue_dispatch import dispatch_queued_input
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_queue_repo import ChatQueueRepo


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['accepted', 'lost_after_commit', 'denied', 'disconnected'])
async def test_queued_dispatch_runs_once_and_reconciles_durable_result(client, outcome):
    me, chat, run = await fixture(client)
    input_id = uuid.uuid4().hex
    async with session(me) as db:
        await ChatQueueRepo(db, me['user_id']).enqueue(chat, run, input_id, {'content': 'Next'})
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})

    async def submit(message, request_id, authority):
        assert message == {'content': 'Next'}
        assert request_id == input_id
        if outcome in {'accepted', 'lost_after_commit'}:
            async with session(me) as db:
                await AgentRunsRepo(db).create(run_id='queued-' + request_id, tenant_id=me['tenant_id'],
                    chat_id=chat, creator_user_id=me['user_id'], client_request_id=request_id, input_snapshot={})
        if outcome == 'denied':
            raise HTTPException(403, 'permission_revoked')
        if outcome in {'disconnected', 'lost_after_commit'}:
            raise ConnectionError('lost response')
    submitted = AsyncMock(side_effect=submit)
    arguments = dict(tenant_id=me['tenant_id'], user_id=me['user_id'], chat_id=chat,
                     finished_run_id=run, submit=submitted)
    await dispatch_queued_input(**arguments)
    await dispatch_queued_input(**arguments)
    submitted.assert_awaited_once()
    async with session(me) as db:
        result = await ChatQueueRepo(db, me['user_id']).get(chat, input_id)
        expected = 'started' if outcome in {'accepted', 'lost_after_commit'} else 'failed' if outcome == 'denied' else 'unknown'
        assert result['status'] == expected
        assert result['started_run_id'] == ('queued-' + input_id if expected == 'started' else None)
        assert (await ChatQueueRepo(db, me['user_id']).latest(chat))['status'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('event_type', ['HITL_REQUIRED', 'INTERACTION_REQUIRED'])
async def test_human_wait_event_permanently_blocks_previously_queued_input(client, event_type):
    from vibecanvas_api.services.agent_run_writer import AgentRunWriter
    me, chat, run = await fixture(client)
    input_id = uuid.uuid4().hex
    async with session(me) as db:
        await ChatQueueRepo(db, me['user_id']).enqueue(chat, run, input_id, {'content': 'Next'})
    writer = AgentRunWriter(run_id=run, tenant_id=me['tenant_id'], chat_id=chat, user_id=me['user_id'])
    await writer.emit(1, event_type, {'hitl_request_id': 'approval'})
    await writer.close()
    async with session(me) as db:
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
    submit = AsyncMock()
    await dispatch_queued_input(tenant_id=me['tenant_id'], user_id=me['user_id'], chat_id=chat,
                                finished_run_id=run, submit=submit)
    submit.assert_not_awaited()
    async with session(me) as db:
        receipt = await ChatQueueRepo(db, me['user_id']).get(chat, input_id)
        assert receipt['status'] == 'blocked'
        assert receipt['error_code'] == 'previous_turn_waiting_for_input'
