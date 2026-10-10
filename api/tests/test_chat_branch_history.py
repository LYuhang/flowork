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


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_messages", [0, 201])
async def test_branch_snapshot_copies_only_boundary_with_new_encryption_and_original_times(client, extra_messages):
    _, me, source = await seed(client)
    run_id, target = uuid.uuid4().hex, uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        repo = ChatRepo(db, me['user_id'])
        await AgentRunsRepo(db).create(run_id=run_id, tenant_id=me['tenant_id'], chat_id=source,
            creator_user_id=me['user_id'], client_request_id=run_id, input_snapshot={})
        for index in range(extra_messages):
            await repo.persist_message(source, {'message_id': f'older-{index}', 'role': 'assistant',
                'turn_id': 'older', 'content': {'text': f'Older message {index}'}})
        await repo.persist_message(source, {'message_id': 'cutoff', 'role': 'assistant',
            'turn_id': run_id, 'content': {'text': 'Branch ends here'}})
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run_id})
        await db.execute(text("UPDATE chats SET runtime_state_ref='source-thread', browser_session_id='source-control', runtime_model_id='test-model' WHERE chat_id=:chat"), {'chat': source})
        original, _, _ = await repo.list_message_page(source, limit=300)
        await repo.persist_message(source, {'message_id': 'later', 'role': 'user',
            'turn_id': 'later', 'content': {'text': 'Excluded'}})
        await AgentRunsRepo(db).record_native_boundary(run_id, chat_id=source,
            creator_user_id=me['user_id'], runtime_type='codex',
            state_ref='source-private-thread', native_turn_id='selected-native-turn')
        await repo.create_branch_snapshot(source, through_turn_id=run_id, chat_id=target,
            name='Branch', tool_projections={})
        assert await repo.get_branch_native_boundary(target) == {
            'state_ref': 'source-private-thread', 'native_turn_id': 'selected-native-turn',
        }
        assert await ChatRepo(db, str(uuid.uuid4())).get_branch_native_boundary(target) is None
        copied, total, _ = await repo.list_message_page(target, limit=300)
        assert total == len(original)
        for before, after in zip(original, copied, strict=True):
            assert after['message_id'] == before['message_id']
            assert after['content'] == before['content']
            assert after['ts'] == before['ts']
            assert after['meta']['branch_source']['chat_id'] == source
        chats = (await db.execute(text('SELECT chat_id,project_id,runtime_state_ref,browser_session_id,runtime_model_id FROM chats WHERE chat_id IN (:source,:target)'), {'source': source, 'target': target})).mappings().all()
        by_id = {row['chat_id']: row for row in chats}
        assert by_id[source]['project_id'] == by_id[target]['project_id']
        assert by_id[target]['runtime_state_ref'] is None
        assert by_id[target]['browser_session_id'] is None
        assert by_id[target]['runtime_model_id'] == 'test-model'
        ciphertexts = (await db.execute(text("SELECT content_ciphertext FROM chat_messages WHERE chat_id IN (:source,:target) AND message_id='question'"), {'source': source, 'target': target})).scalars().all()
        assert len(set(ciphertexts)) == 2
        assert (await repo.list_message_page(source, limit=20))[1] == total + 1
        with pytest.raises(ValueError, match='branch_target_already_exists'):
            await repo.create_branch_snapshot(source, through_turn_id=run_id, chat_id=target,
                name='Duplicate', tool_projections={})
        assert (await repo.list_message_page(target, limit=20))[1] == total
        await db.execute(text("UPDATE chats SET runtime_state_ref='own-thread' WHERE chat_id=:chat"), {'chat': target})
        db.expire_all()
        assert await repo.get_branch_native_boundary(target) is None


@pytest.mark.asyncio
async def test_branch_snapshot_rolls_back_partial_copy_and_rejects_wrong_owner(client, monkeypatch):
    _, me, source = await seed(client)
    run_id, target = uuid.uuid4().hex, uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        repo = ChatRepo(db, me['user_id'])
        await AgentRunsRepo(db).create(run_id=run_id, tenant_id=me['tenant_id'], chat_id=source,
            creator_user_id=me['user_id'], client_request_id=run_id, input_snapshot={})
        await repo.persist_message(source, {'message_id': 'cutoff', 'role': 'assistant',
            'turn_id': run_id, 'content': {'text': 'Boundary'}})
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run_id})
        with pytest.raises(LookupError, match='branch_source_not_found'):
            await ChatRepo(db, str(uuid.uuid4())).create_branch_snapshot(source,
                through_turn_id=run_id, chat_id=target, name='Denied', tool_projections={})
        original_persist = repo.persist_message
        calls = 0
        async def fail_during_copy(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('copy failed')
            await original_persist(*args, **kwargs)
        monkeypatch.setattr(repo, 'persist_message', fail_during_copy)
        with pytest.raises(RuntimeError, match='copy failed'):
            await repo.create_branch_snapshot(source, through_turn_id=run_id, chat_id=target,
                name='Incomplete', tool_projections={})
        assert await db.scalar(text('SELECT count(*) FROM chats WHERE chat_id=:chat'), {'chat': target}) == 0
        assert await db.scalar(text('SELECT count(*) FROM chat_messages WHERE chat_id=:chat'), {'chat': target}) == 0
        assert (await repo.list_message_page(source, limit=20))[1] == 5
