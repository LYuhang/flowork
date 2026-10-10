"""Real PostgreSQL coverage for the private, paged attention inventory."""
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo


async def attention_fixture(client, *, expired_first=False):
    headers, me, _ = await seed(client)
    scope = '__chat_' + me['user_id'].replace('-', '')[:24]
    project = await client.post('/api/v1/projects', headers=headers, json={'name': 'Attention fixture'})
    assert project.status_code in (200, 201), project.text
    chat = str(uuid.uuid4())
    created = await client.put(f'/api/v1/chat-scopes/{scope}/chats/{chat}', headers=headers, json={'project_id': project.json()['project_id']})
    assert created.status_code in (200, 201), created.text
    prefix = uuid.uuid4().hex
    ids = [f'{prefix}-{i}' for i in range(3)]
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        await AgentRunsRepo(session).create(run_id=prefix, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=prefix, input_snapshot={})
        repo = HitlRepo(session)
        for i, kind in enumerate(('pre_tool_approval', 'post_tool_review', 'elicitation')):
            await repo.create_request(hitl_request_id=ids[i], tenant_id=me['tenant_id'], chat_id=chat,
                run_id=prefix, artifact_id=None, hitl_type=kind, title=f'Action {i}',
                prompt_text='Private prompt', ui_payload_json={'secret': 'private UI payload'},
                agent_payload_json={'secret': 'private agent payload'}, mark_run_waiting=False,
                runtime_correlation_json=({'source': 'flowork_cli', 'runtime_method': 'workflow.delete',
                    'runtime_request_id': 'expired-call'} if expired_first and i == 2 else {}))
    return headers, me, chat, scope, list(reversed(ids))


@pytest.mark.asyncio
async def test_attention_pages_survive_resolution_and_hide_private_payloads(client):
    headers, me, chat, scope, ids = await attention_fixture(client)
    endpoint = f'/api/v1/chat-scopes/{scope}/attention'
    first = await client.get(endpoint, params={'limit': 1}, headers=headers)
    assert first.status_code == 200, first.text
    assert [x['hitl_request_id'] for x in first.json()['items']] == ids[:1]
    assert first.json()['next_cursor'] == ids[0]
    assert set(first.json()['items'][0]) == {'hitl_request_id', 'chat_id', 'run_id', 'artifact_id', 'hitl_type', 'title', 'created_at'}
    assert 'Private prompt' not in first.text and 'payload' not in first.text
    # Another window resolves the cursor row before this view asks for the next page.
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        await HitlRepo(session).resolve(hitl_request_id=ids[0], decision='approve', decision_payload={})
    remaining = await client.get(endpoint, params={'limit': 2, 'after_id': ids[0]}, headers=headers)
    assert remaining.status_code == 200, remaining.text
    assert [x['hitl_request_id'] for x in remaining.json()['items']] == ids[1:]
    assert remaining.json()['next_cursor'] is None
    assert (await client.get(endpoint, params={'after_id': 'missing'}, headers=headers)).status_code == 400
    assert (await client.get(endpoint, params={'limit': 51}, headers=headers)).status_code == 422
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        rows, cursor = await HitlRepo(session).list_pending_page(scope_id=scope, user_id=uuid.uuid4(),
            authorized_chat_ids=[chat], limit=30)
        assert rows == [] and cursor is None
        await session.execute(text('UPDATE chats SET deleted_at=now() WHERE chat_id=:chat'), {'chat': chat})
    assert (await client.get(endpoint, headers=headers)).json()['items'] == []


@pytest.mark.asyncio
async def test_attention_expires_dead_cli_requests_without_losing_page_position(client):
    headers, me, _, scope, ids = await attention_fixture(client, expired_first=True)
    endpoint = f'/api/v1/chat-scopes/{scope}/attention'
    first = await client.get(endpoint, params={'limit': 1}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json() == {'items': [], 'next_cursor': ids[0]}
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        assert (await session.execute(text('SELECT status FROM hitl_requests WHERE hitl_request_id=:id'), {'id': ids[0]})).scalar_one() == 'cancelled'
    next_page = await client.get(endpoint, params={'after_id': ids[0]}, headers=headers)
    assert [x['hitl_request_id'] for x in next_page.json()['items']] == ids[1:]


@pytest.mark.asyncio
async def test_attention_requires_content_access_and_rejects_other_carriers(client, monkeypatch, openfga_allow_all):
    headers, _, _, scope, _ = await attention_fixture(client)
    endpoint = f'/api/v1/chat-scopes/{scope}/attention'
    other, _, _ = await seed(client)
    assert (await client.get(endpoint, headers=other)).status_code == 404
    async def no_access(*, object_type, relation, **kwargs):
        assert object_type == "chat" and relation == "can_view"
        return []
    async def must_not_decrypt(*args):
        raise AssertionError('Unauthorized request was decrypted')
    monkeypatch.setattr(openfga_allow_all, 'list_objects', no_access)
    monkeypatch.setattr(HitlRepo, '_materialize_request', must_not_decrypt)
    result = await client.get(endpoint, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json() == {'items': [], 'next_cursor': None}


@pytest.mark.asyncio
async def test_attention_notifications_are_committed_and_owner_scoped(client):
    import asyncio
    from vibecanvas_api.services.state_notifications import state_changes

    _, me, chat, _, ids = await attention_fixture(client)
    key = f"{me['tenant_id']}:{me['user_id']}"
    async with state_changes('flowork_chat_attention', key) as changed:
        await asyncio.wait_for(changed.wait(), 5)
        async with state_changes('flowork_chat_attention', f"{me['tenant_id']}:{uuid.uuid4()}") as other:
            changed.clear()
            async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
                await HitlRepo(session).resolve(hitl_request_id=ids[0], decision='approve', decision_payload={})
                assert not changed.is_set()
                await session.rollback()
            await asyncio.sleep(.1)
            assert not changed.is_set()
            async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
                await HitlRepo(session).resolve(hitl_request_id=ids[0], decision='approve', decision_payload={})
            await asyncio.wait_for(changed.wait(), 5)
            assert not other.is_set()
            changed.clear()
            async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
                await session.execute(text('UPDATE chats SET deleted_at=now() WHERE chat_id=:chat'), {'chat': chat})
            await asyncio.wait_for(changed.wait(), 5)
            assert not other.is_set()


@pytest.mark.asyncio
async def test_attention_stream_authorizes_before_subscribing(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from vibecanvas_api.routes import chats
    from vibecanvas_api.services import state_notifications

    authorize = AsyncMock()
    monkeypatch.setattr(chats, '_authorize_chat_carrier', authorize)
    guard = lambda: None
    monkeypatch.setattr(chats, '_chat_stream_guard', lambda **kwargs: guard)
    seen = []
    async def invalidations(channel, owner, authorized):
        seen.append((channel, owner, authorized))
        yield True
        yield False
    monkeypatch.setattr(state_notifications, 'invalidations', invalidations)
    repo = SimpleNamespace(_s=SimpleNamespace(commit=AsyncMock()))
    auth = SimpleNamespace(active_organization_id='org', user_id='user')
    response = await chats.stream_chat_attention('__chat_user', SimpleNamespace(), repo, auth, SimpleNamespace())
    assert [chunk async for chunk in response.body_iterator] == [b'event: changed\ndata: {}\n\n', b': heartbeat\n\n']
    assert seen == [('flowork_chat_attention', 'org:user', guard)]
    repo._s.commit.assert_awaited_once()
    authorize.assert_awaited_once()
    authorize.side_effect = HTTPException(403)
    with pytest.raises(HTTPException):
        await chats.stream_chat_attention('__chat_user', SimpleNamespace(), repo, auth, SimpleNamespace())
    assert len(seen) == 1
