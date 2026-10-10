"""A branch reads a finished inclusive boundary, never the source's later turns."""
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
async def test_branch_boundary_excludes_later_source_messages_across_pages(client):
    _, me, chat = await seed(client)
    selected, later = uuid.uuid4().hex, uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        repo = ChatRepo(db, me['user_id'])
        await AgentRunsRepo(db).create(run_id=selected, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=selected, input_snapshot={})
        await repo.persist_message(chat, {'message_id': 'boundary', 'role': 'assistant',
            'turn_id': selected, 'content': {'text': 'Selected answer'}})
        await db.execute(text("UPDATE agent_runs SET status='completed', ended_at=now() WHERE run_id=:run"), {'run': selected})
        await repo.persist_message(chat, {'message_id': 'later', 'role': 'user',
            'turn_id': later, 'content': {'text': 'Must not appear in the branch'}})
        first, total, _ = await repo.list_message_page(chat, through_turn_id=selected, limit=3)
        second, second_total, offset = await repo.list_message_page(chat, through_turn_id=selected, limit=3, offset=3)
        assert total == second_total == 5
        assert offset == 3
        assert [row['message_id'] for row in first + second] == ['question', 'answer', 'tool', 'hidden', 'boundary']
        tail, _, _ = await repo.list_message_page(chat, through_turn_id=selected, limit=1, tail=True)
        assert tail[0]['content']['text'] == 'Selected answer'
        assert (await repo.list_message_page(chat, limit=20))[1] == 6
        with pytest.raises(LookupError, match='branch_turn_not_found'):
            await repo.list_message_page(chat, through_turn_id=later, limit=20)


@pytest.mark.asyncio
async def test_branch_boundary_rejects_live_or_missing_history_and_checks_owner(client):
    _, me, chat = await seed(client)
    run = uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        repo = ChatRepo(db, me['user_id'])
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
        with pytest.raises(ValueError, match='branch_turn_not_finished'):
            await repo.list_message_page(chat, through_turn_id=run, limit=20)
        await db.execute(text("UPDATE agent_runs SET status='cancelled', ended_at=now() WHERE run_id=:run"), {'run': run})
        with pytest.raises(LookupError, match='branch_turn_history_missing'):
            await repo.list_message_page(chat, through_turn_id=run, limit=20)
        with pytest.raises(ValueError, match='mutually_exclusive'):
            await repo.list_message_page(chat, through_turn_id=run, before_turn_id=run, limit=20)
        other = ChatRepo(db, str(uuid.uuid4()))
        assert await other.list_message_page(chat, through_turn_id=run, limit=20) == ([], 0, 0)


@pytest.mark.asyncio
async def test_native_branch_boundary_is_private_and_survives_later_run_updates(client):
    _, me, chat = await seed(client)
    run_id = uuid.uuid4().hex
    boundary = {'runtime_type': 'codex', 'state_ref': 'private-native-thread', 'native_turn_id': 'last-native-turn'}
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        repo = AgentRunsRepo(db)
        await repo.create(run_id=run_id, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run_id, input_snapshot={'content': 'original'})
        await repo.record_native_boundary(run_id, chat_id=chat, creator_user_id=me['user_id'], **boundary)
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        repo = AgentRunsRepo(db)
        run = await repo.get_for_chat(chat, run_id, creator_user_id=me['user_id'])
        assert run.native_boundary == boundary
        raw = (await db.execute(text('SELECT * FROM agent_runs WHERE run_id=:run'), {'run': run_id})).mappings().one()
        assert 'private-native-thread' not in str(dict(raw))
        await repo.record_runtime_input(run_id, chat_id=chat, creator_user_id=me['user_id'], event_id='audit', payload={'input': []})
        await repo.append_event(run_id=run_id, seq=1, event_type='error', payload={'code': 'cancelled', 'message': 'Stopped'}, tenant_id=me['tenant_id'])
        with pytest.raises(LookupError):
            await repo.record_native_boundary(run_id, chat_id=chat, creator_user_id=str(uuid.uuid4()), **boundary)
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        run = await AgentRunsRepo(db).get_for_chat(chat, run_id, creator_user_id=me['user_id'])
        assert run.native_boundary == boundary
        assert run.input_snapshot['content'] == 'original'
        assert run.error_message == 'Stopped'
