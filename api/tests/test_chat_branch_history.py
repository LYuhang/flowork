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
