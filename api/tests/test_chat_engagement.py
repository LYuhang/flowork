"""Real PostgreSQL/FORCE RLS coverage for feedback and public capabilities."""
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.config import PublicUrlsConfig, config
from vibecanvas_api.routes.chat_engagement import public_messages
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.db import session_scope


async def seed(client):
    response = await client.post('/api/v1/auth/register', json={
        'email': f'engagement-{uuid.uuid4().hex}@example.com',
        'username': 'Share Tester', 'password': 'pw12345678',
    })
    assert response.status_code in (200, 201), response.text
    headers = {'Authorization': f'Bearer {response.json()["session_token"]}'}
    me = (await client.get('/api/v1/auth/me', headers=headers)).json()
    chat_id = str(uuid.uuid4())
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        repo = ChatRepo(session, me['user_id'])
        project = await ChatProjectRepo(session, me['user_id']).create(name='Engagement')
        await repo.register_session(f'__chat_{me["user_id"]}', project_id=project['project_id'], name='Private title', chat_id=chat_id, surface='chat')
        for message_id, role, content in [
            ('question', 'user', {'text': 'Visible question', 'attachments': [{'path': '/private/file'}]}),
            ('answer', 'assistant', {'text': 'Visible answer', 'tool_calls': [{'secret': 'tool secret'}]}),
            ('tool', 'tool', {'text': 'tool secret'}),
            ('hidden', 'user', {'text': 'hidden secret', 'visibility': 'hidden'}),
        ]:
            await repo.persist_message(chat_id, {'message_id': message_id, 'role': role,
                'content': content, 'turn_id': 'turn-one'})
    return headers, me, chat_id


@pytest.mark.asyncio
async def test_feedback_is_idempotent_reversible_and_owner_only(client):
    headers, me, chat = await seed(client)
    endpoint = f'/api/v1/chats/{chat}/messages/answer/feedback'
    for rating in ('up', 'up', 'down', None):
        response = await client.put(endpoint, headers=headers, json={'rating': rating})
        assert response.status_code == 200, response.text
        current = await client.get(f'/api/v1/chats/{chat}/feedback', headers=headers)
        assert current.json()['ratings']['answer'] == rating
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        events = (await session.execute(text('SELECT rating,turn_id FROM chat_feedback_events WHERE chat_id=:chat ORDER BY id'), {'chat': chat})).all()
        assert events == [('up', 'turn-one'), ('down', 'turn-one'), (None, 'turn-one')]
    assert (await client.put(endpoint.replace('answer', 'question'), headers=headers, json={'rating': 'up'})).status_code == 404
    assert (await client.put(endpoint, headers=headers, json={'rating': 'invalid'})).status_code == 422
    other, _, _ = await seed(client)
    assert (await client.put(endpoint, headers=other, json={'rating': 'up'})).status_code == 404
    assert (await client.get(f'/api/v1/chats/{chat}/feedback', headers=other)).status_code == 404


@pytest.mark.asyncio
async def test_share_snapshot_public_read_revoke_and_prefix(client, monkeypatch):
    headers, me, chat = await seed(client)
    monkeypatch.setenv('VIBECANVAS_PUBLIC_URL', 'https://example.org/deployment/')
    monkeypatch.setattr(config, 'public_urls', PublicUrlsConfig({}))
    endpoint = f'/api/v1/chats/{chat}/shares'
    preview = await client.post(endpoint + '/preview', headers=headers, json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()['existing'] is False
    assert len(preview.json()['messages']) == 2
    assert (await client.get(endpoint, headers=headers)).json()['items'] == []
    response = await client.post(endpoint, headers=headers, json={})
    assert response.status_code == 200, response.text
    share = response.json()
    assert share['url'] == 'https://example.org/deployment' + share['path']
    assert share['expires_at'] is None
    assert (await client.post(endpoint, headers=headers, json={})).json()['id'] == share['id']
    public_path = '/api/v1/public/chat-shares/' + share['path'].split('/')[-1]
    client.cookies.clear()
    public = await client.get(public_path)
    assert public.status_code == 200, public.text
    assert public.headers['cache-control'] == 'no-store'
    assert 'noindex' in public.headers['x-robots-tag']
    assert [m['content'] for m in public.json()['messages']] == ['Visible question', 'Visible answer']
    assert 'secret' not in public.text and '/private/file' not in public.text
    assert 'tenant_id' not in public.text and 'token' not in public.text
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        raw = (await session.execute(text('SELECT snapshot_ciphertext,token_hash FROM chat_public_shares WHERE chat_id=:chat'), {'chat': chat})).one()
        assert 'Visible answer' not in raw[0] and share['path'].split('/')[-1] not in raw[1]
        await ChatRepo(session, me['user_id']).persist_message(chat, {
            'message_id': 'later', 'role': 'assistant', 'content': {'text': 'Later private answer'}, 'turn_id': 'turn-two',
        })
    assert len((await client.get(public_path)).json()['messages']) == 2
    preview = await client.post(endpoint + '/preview', headers=headers, json={})
    assert preview.json()['existing'] is True
    assert preview.json()['messages'] == public.json()['messages']
    other, _, _ = await seed(client)
    assert (await client.get(endpoint, headers=other)).status_code == 404
    assert (await client.post(endpoint, headers=other, json={})).status_code == 404
    assert (await client.post(endpoint + '/preview', headers=other, json={})).status_code == 404
    assert (await client.delete(endpoint + '/' + share['id'], headers=other)).status_code == 404
    assert (await client.delete(endpoint + '/' + share['id'], headers=headers)).status_code == 204
    assert (await client.get(public_path)).status_code == 404
    new = (await client.post(endpoint, headers=headers, json={})).json()
    assert new['path'] != share['path'] and new['message_count'] == 3


@pytest.mark.asyncio
async def test_single_response_share_expiry_deletion_and_rls(client, app_engine):
    headers, me, chat = await seed(client)
    endpoint = f'/api/v1/chats/{chat}/shares'
    preview = await client.post(endpoint + '/preview', headers=headers, json={'message_id': 'answer'})
    assert preview.status_code == 200, preview.text
    assert preview.json()['messages'] == [{'id': 'answer', 'role': 'assistant', 'content': 'Visible answer'}]
    response = await client.post(endpoint, headers=headers, json={'message_id': 'answer'})
    assert response.status_code == 200, response.text
    share = response.json()
    public_path = '/api/v1/public/chat-shares/' + share['path'].split('/')[-1]
    public = (await client.get(public_path)).json()
    assert public['kind'] == 'message' and public['title'] != 'Private title'
    assert public['messages'] == [{'id': 'answer', 'role': 'assistant', 'content': 'Visible answer'}]
    async with app_engine.connect() as connection:
        assert not (await connection.execute(text('SELECT id FROM chat_public_shares'))).all()
    assert (await client.post(endpoint, headers=headers, json={'message_id': 'question'})).status_code == 404
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        await session.execute(text("UPDATE chat_public_shares SET expires_at=now()-interval '1 second' WHERE chat_id=:chat"), {'chat': chat})
    assert (await client.get(public_path)).status_code == 404
    async with session_scope(tenant_id=me['tenant_id'], user_id=me['user_id']) as session:
        await session.execute(text('UPDATE chat_public_shares SET expires_at=NULL WHERE chat_id=:chat'), {'chat': chat})
        await session.execute(text('UPDATE chats SET deleted_at=now() WHERE chat_id=:chat'), {'chat': chat})
    assert (await client.get(public_path)).status_code == 404
    invalid = await client.get('/api/v1/public/chat-shares/not-a-token')
    assert invalid.status_code == 404 and invalid.headers['cache-control'] == 'no-store'


def test_public_messages_strip_internal_thinking_and_controls():
    rows = [{'message_id': 'a', 'role': 'assistant', 'content': {'text': 'Public<think_never_used_x>secret</think_never_used_x> answer'}},
            {'message_id': 'b', 'role': 'assistant', 'content': {'text': '<think_never_used_x>unfinished secret'}},
            {'message_id': 'c', 'role': 'user', 'content': {'text': 'control secret', 'message_type': 'control'}}]
    assert public_messages(rows, None) == [{'id': 'a', 'role': 'assistant', 'content': 'Public answer'}]


def test_public_messages_do_not_publish_attachment_context():
    """Public transcripts contain only visible text, never private references."""
    private = [
        {'type': 'quote', 'source': {'chat_id': 'private-chat'},
         'snapshot': {'text': 'private excerpt'}},
        {'type': 'file', 'resource': {'file_ref': {'path': '/data/private.pdf'}},
         'snapshot': {'text': 'private resolved metadata'}},
    ]
    rows = [{'message_id': 'user-message', 'role': 'user', 'attachments': private,
             'content': {'text': 'Please review the references', 'attachments': private}}]
    assert public_messages(rows, None) == [
        {'id': 'user-message', 'role': 'user', 'content': 'Please review the references'},
    ]
