"""Encrypted title search: real PostgreSQL paging and private inventory scope."""
import uuid

import pytest
from sqlalchemy import text

from tests.test_chat_engagement import seed
from vibecanvas_api.authorization.openfga_client import OpenFgaTuple
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['projects', 'chats'])
async def test_search_scans_beyond_first_page_and_matches_literal_titles(client, openfga_allow_all, kind):
    headers, me, _ = await seed(client)
    prefix = 'search-' + uuid.uuid4().hex
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        projects = ChatProjectRepo(session, me['user_id'])
        chats = ChatRepo(session, me['user_id'])
        project = await projects.create(name='Search parent')
        for index in range(101):
            title = '图像 [A%_] STRASSE' if index == 100 else 'Unrelated title'
            identifier = f'{prefix}-{index:03d}'
            if kind == 'projects':
                await projects.create(name=title, project_id=identifier)
            else:
                await chats.register_session(f'__chat_{me["user_id"]}', project_id=project['project_id'],
                                             name=title, chat_id=identifier, surface='chat')
                await chats.persist_message(identifier, {'message_id': 'first', 'role': 'user',
                                                         'content': {'text': 'Message body is not a title'}})
                openfga_allow_all.tuples.add(OpenFgaTuple(f'user:{me["user_id"]}', 'creator', f'chat:{identifier}'))
    endpoint = f'/api/v1/search/{kind}'
    response = await client.post(endpoint, headers=headers, json={'query': ' [a%_] straße '})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['scanned'] == 100
    assert page['items'] == []
    assert page['next_cursor']
    second = await client.post(endpoint, headers=headers, json={
        'query': '[a%_] straße', 'after_id': page['next_cursor'],
    })
    assert second.status_code == 200, second.text
    assert [item['id'] for item in second.json()['items']] == [f'{prefix}-100']
    assert second.json()['next_cursor'] is None
    assert second.headers['cache-control'] == 'no-store'
    other, _, _ = await seed(client)
    denied = await client.post(endpoint, headers=other, json={'query': '[A%_]'})
    assert denied.status_code == 200
    assert denied.json()['items'] == []


@pytest.mark.asyncio
async def test_search_validation_and_unstarted_chat_exclusion(client, openfga_allow_all):
    headers, me, _ = await seed(client)
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        project = await ChatProjectRepo(session, me['user_id']).create(name='Draft workspace')
        identifier = str(uuid.uuid4())
        await ChatRepo(session, me['user_id']).register_session(f'__chat_{me["user_id"]}',
            project_id=project['project_id'], name='Unstarted secret', chat_id=identifier, surface='chat')
        openfga_allow_all.tuples.add(OpenFgaTuple(f'user:{me["user_id"]}', 'creator', f'chat:{identifier}'))
    result = await client.post('/api/v1/search/chats', headers=headers, json={'query': 'Unstarted'})
    assert result.status_code == 200, result.text
    assert result.json()['items'] == []
    # Once started it can be found, but deleting its project hides both entries.
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        await ChatRepo(session, me['user_id']).persist_message(identifier, {
            'message_id': 'first', 'role': 'user', 'content': {'text': 'Hello'},
        })
    started = await client.post('/api/v1/search/chats', headers=headers, json={'query': 'Unstarted'})
    assert [item['id'] for item in started.json()['items']] == [identifier]
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        await session.execute(text('UPDATE chat_projects SET deleted_at=now() WHERE project_id=:id'), {'id': project['project_id']})
    for kind, query in (('chats', 'Unstarted'), ('projects', 'Draft workspace')):
        deleted = await client.post(f'/api/v1/search/{kind}', headers=headers, json={'query': query})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()['items'] == []
    for kind in ('projects', 'chats'):
        for query in ('', '   ', 'x' * 201):
            response = await client.post(f'/api/v1/search/{kind}', headers=headers, json={'query': query})
            assert response.status_code == 422
