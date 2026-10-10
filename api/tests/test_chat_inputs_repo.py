"""Steering receipts and accepted history share one transaction, with no replay."""
import asyncio
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_inputs_repo import ChatInputConflict, ChatInputsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


async def fixture(client):
    _, me, chat = await seed(client)
    run = uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
    return me, chat, run


def session(me):
    return session_scope(tenant_id=me['tenant_id'], user_id=me['user_id'])


@pytest.mark.asyncio
async def test_receipt_is_encrypted_and_only_confirmed_input_becomes_history(client):
    me, chat, run = await fixture(client)
    input_id = str(uuid.uuid4())
    async with session(me) as db:
        repo = ChatInputsRepo(db, me['user_id'])
        receipt = await repo.create(chat, run, input_id, 'Additional private instruction')
        assert receipt['status'] == 'submitting'
        row = (await db.execute(text('SELECT * FROM chat_user_inputs WHERE input_id=:id'), {'id': input_id})).mappings().one()
        assert 'Additional private instruction' not in str(dict(row))
        assert await db.scalar(text('SELECT count(*) FROM chat_messages WHERE message_id=:id'), {'id': input_id}) == 0
    async with session(me) as db:
        repo = ChatInputsRepo(db, me['user_id'])
        assert (await repo.get(chat, input_id))['text'] == 'Additional private instruction'
        assert (await repo.resolve(chat, run, input_id, 'unknown', 'response_lost'))['status'] == 'unknown'
        assert await db.scalar(text('SELECT count(*) FROM chat_messages WHERE message_id=:id'), {'id': input_id}) == 0
    async with session(me) as db:
        repo = ChatInputsRepo(db, me['user_id'])
        accepted = await repo.resolve(chat, run, input_id, 'accepted', None)
        assert accepted['status'] == 'accepted'
        assert accepted['error_code'] is None
        assert (await repo.resolve(chat, run, input_id, 'unknown', 'late_disconnect'))['status'] == 'accepted'
        await repo.resolve(chat, run, input_id, 'accepted', None)
        assert await db.scalar(text('SELECT count(*) FROM chat_messages WHERE message_id=:id'), {'id': input_id}) == 1
        rows, _, _ = await ChatRepo(db, me['user_id']).list_message_page(chat, limit=100)
        assert any(row['message_id'] == input_id and 'Additional private instruction' in str(row) for row in rows)
        assert await db.scalar(text('SELECT status FROM agent_runs WHERE run_id=:id'), {'id': run}) == 'running'
    async with session(me) as db:
        with pytest.raises(ChatInputConflict, match='input_id_already_used'):
            await ChatInputsRepo(db, me['user_id']).create(chat, run, input_id, 'Do not dispatch again')


@pytest.mark.asyncio
async def test_receipt_and_canonical_message_roll_back_together(client):
    me, chat, run = await fixture(client)
    input_id = str(uuid.uuid4())
    async with session(me) as db:
        await ChatInputsRepo(db, me['user_id']).create(chat, run, input_id, 'Atomic input')
    async with session(me) as db:
        await ChatInputsRepo(db, me['user_id']).resolve(chat, run, input_id, 'accepted', None)
        await db.rollback()
    async with session(me) as db:
        repo = ChatInputsRepo(db, me['user_id'])
        assert (await repo.get(chat, input_id))['status'] == 'submitting'
        assert await db.scalar(text('SELECT count(*) FROM chat_messages WHERE message_id=:id'), {'id': input_id}) == 0
        await repo.resolve(chat, run, input_id, 'rejected', 'native_rejected')
        assert (await repo.resolve(chat, run, input_id, 'accepted', None))['status'] == 'rejected'
        assert await db.scalar(text('SELECT count(*) FROM chat_messages WHERE message_id=:id'), {'id': input_id}) == 0


@pytest.mark.asyncio
async def test_simultaneous_submissions_have_only_one_pending_receipt(client):
    me, chat, run = await fixture(client)

    async def submit():
        async with session(me) as db:
            try:
                return (await ChatInputsRepo(db, me['user_id']).create(chat, run, str(uuid.uuid4()), 'Direction'))['status']
            except ChatInputConflict as exc:
                return str(exc)

    assert sorted(await asyncio.gather(submit(), submit())) == ['steer_already_submitting', 'submitting']


@pytest.mark.asyncio
async def test_terminal_run_and_private_owner_boundaries(client):
    me, chat, run = await fixture(client)
    input_id = str(uuid.uuid4())
    async with session(me) as db:
        await ChatInputsRepo(db, me['user_id']).create(chat, run, input_id, 'Private direction')
        other = ChatInputsRepo(db, str(uuid.uuid4()))
        assert await other.get(chat, input_id) is None
        with pytest.raises(LookupError):
            await other.resolve(chat, run, input_id, 'accepted', None)
        with pytest.raises(LookupError):
            await other.create(chat, run, str(uuid.uuid4()), 'No ownership')
        await db.execute(text("UPDATE agent_runs SET status='cancelled' WHERE run_id=:id"), {'id': run})
    async with session(me) as db:
        repo = ChatInputsRepo(db, me['user_id'])
        receipt = await repo.get(chat, input_id)
        assert receipt['status'] == 'unknown'
        assert receipt['error_code'] == 'turn_ended_before_confirmation'
        with pytest.raises(ChatInputConflict, match='turn_not_running'):
            await repo.create(chat, run, str(uuid.uuid4()), 'Cannot target cancelled turn')
        await db.execute(text('UPDATE chats SET deleted_at=now() WHERE chat_id=:id'), {'id': chat})
        assert await repo.get(chat, input_id) is None


@pytest.mark.asyncio
async def test_pending_human_request_prevents_steering(client):
    from vibecanvas_api.storage.hitl_repo import HitlRepo

    me, chat, run = await fixture(client)
    async with session(me) as db:
        await HitlRepo(db).create_request(
            hitl_request_id=uuid.uuid4().hex, tenant_id=me['tenant_id'], chat_id=chat,
            run_id=run, artifact_id=None, hitl_type='pre_tool_approval', title='Confirm',
            prompt_text='Confirm first', ui_payload_json={}, agent_payload_json={},
            mark_run_waiting=False, runtime_correlation_json={},
        )
        with pytest.raises(ChatInputConflict, match='turn_waiting_for_input'):
            await ChatInputsRepo(db, me['user_id']).create(chat, run, 'blocked-input', 'Skip approval')
        assert await db.scalar(text('SELECT count(*) FROM chat_user_inputs WHERE chat_id=:id'), {'id': chat}) == 0


@pytest.mark.asyncio
async def test_input_rows_are_hidden_by_tenant_rls(client):
    me, chat, run = await fixture(client)
    async with session(me) as db:
        await ChatInputsRepo(db, me['user_id']).create(chat, run, 'opaque-input-id', 'Tenant private')
    _, outsider, _ = await seed(client)
    async with session(outsider) as db:
        assert await db.scalar(text('SELECT count(*) FROM chat_user_inputs WHERE chat_id=:id'), {'id': chat}) == 0
        assert await ChatInputsRepo(db, me['user_id']).get(chat, 'opaque-input-id') is None


@pytest.mark.asyncio
async def test_live_input_keeps_assistant_position_and_complete_text_after_reload(client):
    from vibecanvas_api.services.agent_run_writer import AgentRunWriter

    me, chat, run = await fixture(client)
    input_id = str(uuid.uuid4())
    async with session(me) as db:
        await ChatInputsRepo(db, me['user_id']).create(chat, run, input_id, 'New direction')
    writer = AgentRunWriter(run_id=run, tenant_id=me['tenant_id'], chat_id=chat, user_id=me['user_id'])
    await writer.emit(1, 'CHAT_EVENT', {'type': 'message_start', 'message_id': 'assistant-live', 'content': 'Before'})
    await writer.emit(2, 'USER_INPUT_STATUS', {'input_id': input_id, 'status': 'accepted', 'error': None})
    async with session(me) as db:
        rows, _, _ = await ChatRepo(db, me['user_id']).list_message_page(chat, limit=100)
        assert [r['message_id'] for r in rows] == ['question', 'answer', 'tool', 'hidden', 'assistant-live', input_id]
        rows = rows[-2:]
        original_id = rows[0]['id']
        assert rows[0]['content']['text'] == 'Before'
        assert rows[0]['meta']['status'] == 'streaming'
    await writer.emit(3, 'CHAT_EVENT', {'type': 'message_delta', 'message_id': 'assistant-live', 'delta': ' and after'})
    await writer.emit(4, 'CHAT_EVENT', {'type': 'message_end', 'message_id': 'assistant-live'})
    await writer.emit(5, 'USER_INPUT_STATUS', {'input_id': input_id, 'status': 'unknown', 'error': 'late_disconnect'})
    await writer.close()
    async with session(me) as db:
        rows, _, _ = await ChatRepo(db, me['user_id']).list_message_page(chat, limit=100)
        assert [r['message_id'] for r in rows] == ['question', 'answer', 'tool', 'hidden', 'assistant-live', input_id]
        rows = rows[-2:]
        assert rows[0]['id'] == original_id
        assert rows[0]['content']['text'] == 'Before and after'
        assert rows[0]['meta']['status'] == 'completed'
        assert (await ChatInputsRepo(db, me['user_id']).get(chat, input_id))['status'] == 'accepted'
        events = await AgentRunsRepo(db).list_events(run, after_seq=0)
        assert events[-1].payload['status'] == 'accepted'
        assert events[-1].payload['text'] == 'New direction'
        assert events[-1].payload['error'] is None


@pytest.mark.asyncio
async def test_interrupted_snapshot_retains_text_and_input_order(client):
    from vibecanvas_api.services.chat_transcript_writer import ChatTranscriptWriter

    me, chat, run = await fixture(client)
    input_id = str(uuid.uuid4())
    async with session(me) as db:
        await ChatInputsRepo(db, me['user_id']).create(chat, run, input_id, 'Keep this')
    writer = ChatTranscriptWriter(tenant_id=me['tenant_id'], user_id=me['user_id'], chat_id=chat, turn_id=run)
    await writer.consume('CHAT_EVENT', {'type': 'message_delta', 'message_id': 'partial', 'delta': 'First'})
    ack = {'input_id': input_id, 'status': 'accepted', 'error': None}
    await writer.consume('USER_INPUT_STATUS', ack)
    assert ack['text'] == 'Keep this'
    await writer.consume('CHAT_EVENT', {'type': 'message_delta', 'message_id': 'partial', 'delta': ' second'})
    await writer.close()
    async with session(me) as db:
        rows, _, _ = await ChatRepo(db, me['user_id']).list_message_page(chat, limit=100)
        assert [r['message_id'] for r in rows] == ['question', 'answer', 'tool', 'hidden', 'partial', input_id]
        rows = rows[-2:]
        assert rows[0]['content']['text'] == 'First second'
        assert rows[0]['meta']['status'] == 'interrupted'
