"""Branch API integration: owned history, independent Chat, no Agent execution."""
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.routes.chats import _chat_carrier_scope_id
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


async def completed_source(client):
    headers, me, chat = await seed(client)
    run = uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text('UPDATE chats SET scope_id=:scope WHERE chat_id=:chat'),
                         {'scope': _chat_carrier_scope_id(me['user_id']), 'chat': chat})
        await AgentRunsRepo(db).create(run_id=run, tenant_id=me['tenant_id'], chat_id=chat,
            creator_user_id=me['user_id'], client_request_id=run, input_snapshot={})
        await ChatRepo(db, me['user_id']).persist_message(chat, {'message_id': 'selected',
            'role': 'assistant', 'turn_id': run, 'content': {'text': 'Selected answer'}})
        await db.execute(text("UPDATE agent_runs SET status='completed' WHERE run_id=:run"), {'run': run})
        await ChatRepo(db, me['user_id']).persist_message(chat, {'message_id': 'later',
            'role': 'user', 'turn_id': 'later', 'content': {'text': 'Later source message'}})
    return headers, me, chat, run


@pytest.mark.asyncio
async def test_branch_preview_and_create_keep_boundary_and_do_not_start_agent(client):
    headers, me, source, run = await completed_source(client)
    endpoint = f'/api/v1/chats/{source}/branches'
    preview = await client.get(endpoint + '/preview', params={'turn_id': run}, headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()['message_count'] == 5
    assert preview.json()['unavailable_files'] == ['file']
    assert preview.json()['starts_agent'] is False
    target = str(uuid.uuid4())
    body = {'chat_id': target, 'turn_id': run, 'name': 'My branch'}
    response = await client.post(endpoint, json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()['project_id'] == preview.json()['project_id']
    assert response.json()['file_policy'] == 'shared_current_project_files'
    history = await client.get(f'/api/v1/chat-scopes/{_chat_carrier_scope_id(me["user_id"])}/chats/{target}/messages', headers=headers)
    assert history.status_code == 200, history.text
    assert 'Selected answer' in history.text
    assert 'Later source message' not in history.text
    assert (await client.post(endpoint, json=body, headers=headers)).status_code == 409
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        assert await db.scalar(text('SELECT count(*) FROM agent_runs WHERE chat_id=:chat'), {'chat': target}) == 0
        assert (await ChatRepo(db, me['user_id']).list_message_page(source, limit=20))[1] == 6
        assert (await ChatRepo(db, me['user_id']).list_message_page(target, limit=20))[1] == 5


@pytest.mark.asyncio
async def test_branch_rejects_live_turn_and_other_owner(client):
    headers, me, source, run = await completed_source(client)
    endpoint = f'/api/v1/chats/{source}/branches'
    body = {'chat_id': str(uuid.uuid4()), 'turn_id': run, 'name': 'Denied'}
    other, _, _ = await seed(client)
    assert (await client.post(endpoint, json=body, headers=other)).status_code in {403, 404}
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        await db.execute(text("UPDATE agent_runs SET status='running' WHERE run_id=:run"), {'run': run})
    response = await client.post(endpoint, json=body, headers=headers)
    assert response.status_code == 409, response.text
    assert 'branch_turn_not_finished' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('after_commit', [False, True])
async def test_branch_authorization_outage_reports_unavailable_without_repeating_creation(
    client, monkeypatch, after_commit,
):
    from vibecanvas_api.authorization.openfga_client import OpenFgaUnavailableError
    from vibecanvas_api.routes import chat_branches, chats

    headers, me, source, run = await completed_source(client)
    target = str(uuid.uuid4())
    calls = 0

    async def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise OpenFgaUnavailableError()

    if after_commit:
        monkeypatch.setattr(chats, 'apply_committed_structural_mutations', unavailable)
    else:
        monkeypatch.setattr(chat_branches, 'authorize_resource', unavailable)
    response = await client.post(f'/api/v1/chats/{source}/branches', headers=headers,
        json={'chat_id': target, 'turn_id': run, 'name': 'Outage branch'})
    assert response.status_code == 503, response.text
    assert response.json()['detail']['code'] == 'authorization_unavailable'
    assert calls == 1
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as db:
        assert await db.scalar(text('SELECT count(*) FROM chats WHERE chat_id=:chat'),
                               {'chat': target}) == int(after_commit)
        assert await db.scalar(text('SELECT count(*) FROM agent_runs WHERE chat_id=:chat'),
                               {'chat': target}) == 0
