import uuid
from sqlalchemy import text
import pytest
from fastapi import HTTPException
from vibecanvas_api.storage.chat_draft_repo import apply_draft_operation


TENANT = uuid.uuid4()
USER = uuid.uuid4()
PROJECT = 'context-draft-tests'


async def _seed_and_bind(session):
    from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
    await session.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'draft-tests') ON CONFLICT DO NOTHING"), {'t':TENANT})
    await session.execute(text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,'draft@test') ON CONFLICT DO NOTHING"), {'u':USER,'t':TENANT})
    await session.execute(text("SELECT set_config('app.tenant_id',:t,false),set_config('app.user_id',:u,false)"), {'t':str(TENANT),'u':str(USER)})
    repo = ChatProjectRepo(session,str(USER))
    if await repo.get(PROJECT) is None:
        await repo.create(project_id=PROJECT,name='Draft tests')


def attachment(id='a', text='excerpt'):
    return {'schema_version': 1, 'id': id, 'label': 'Quote', 'type': 'quote',
            'source': {'kind': 'message', 'chat_id': 'c', 'message_id': 'm'},
            'snapshot': {'text': text}}


def state():
    return {'chat_id': 'c', 'version': 0, 'generation': 0, 'text': 'my draft', 'attachments': []}


def test_appends_preserve_text_and_deduplicate_only_matching_excerpts():
    initial = state()
    updated = apply_draft_operation(initial, {'kind':'append', 'attachments': [attachment()]})
    updated = apply_draft_operation(updated, {'kind':'append', 'attachments': [attachment('b'), attachment('c','other')]})
    assert updated['text'] == 'my draft'
    assert [item['id'] for item in updated['attachments']] == ['a','c']
    assert initial == state()


def test_consume_does_not_remove_late_attachments_or_changed_text():
    initial = state()
    initial['attachments'] = [attachment(), attachment('late', 'late excerpt')]
    updated = apply_draft_operation(initial, {'kind':'consume', 'text':'previous text', 'attachment_keys':['a']})
    assert updated['text'] == 'my draft'
    assert updated['attachments'] == [attachment('late','late excerpt')]
    assert updated['generation'] == 1


def test_compare_text_ignores_concurrent_attachment_version():
    initial = apply_draft_operation(state(), {'kind':'append','attachments':[attachment()]})
    updated = apply_draft_operation(initial, {'kind':'text','previous_text':'my draft','text':'new text'})
    assert updated['attachments'] == initial['attachments']
    assert updated['text'] == 'new text'
    with pytest.raises(HTTPException) as error:
        apply_draft_operation(updated, {'kind':'text','previous_text':'my draft','text':'overwrite'})
    assert error.value.status_code == 409


def test_reusing_attachment_id_for_different_content_is_rejected():
    initial = state()
    with pytest.raises(HTTPException):
        apply_draft_operation(initial, {'kind':'append','attachments':[attachment(),attachment(text='changed')]})
    assert initial == state()


def test_remove_only_addresses_requested_item():
    initial = state()
    initial['attachments'] = [attachment(), attachment('b','other')]
    updated = apply_draft_operation(initial, {'kind':'remove','attachment_keys':['a']})
    assert updated['attachments'] == [attachment('b','other')]
    assert updated['text'] == 'my draft'


@pytest.mark.asyncio
async def test_durable_retry_after_send_does_not_resurrect_draft(pg_session):
    from vibecanvas_api.storage.chat_repo import ChatRepo
    from vibecanvas_api.storage.chat_draft_repo import ChatDraftRepo
    from vibecanvas_api.storage.models_chat_drafts import ChatContextDraft
    await _seed_and_bind(pg_session)
    chat = await ChatRepo(pg_session, str(USER)).register_session('__context_test', project_id=PROJECT)
    repo = ChatDraftRepo(pg_session, str(USER))
    append = {'kind':'append','operation_id':'preview-one','attachments':[attachment()]}
    first = await repo.mutate(chat, append)
    assert len(first['attachments']) == 1
    await repo.mutate(chat, {'kind':'text','operation_id':'text-one','previous_text':'','text':'question'})
    await repo.mutate(chat, {'kind':'consume','operation_id':'turn-one','text':'question','attachment_keys':['a']}, internal=True)
    retried = await repo.mutate(chat, append)
    assert retried['attachments'] == [] and retried['text'] == ''
    assert retried['generation'] == 1
    row = await pg_session.get(ChatContextDraft, chat)
    assert 'excerpt' not in row.content_ciphertext and 'question' not in row.content_ciphertext
    with pytest.raises(HTTPException) as error:
        await repo.mutate(chat, {**append, 'attachments':[attachment('different')]})
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_draft_owner_is_checked_even_inside_same_tenant(pg_session):
    from vibecanvas_api.storage.chat_repo import ChatRepo
    from vibecanvas_api.storage.chat_draft_repo import ChatDraftRepo
    import uuid
    await _seed_and_bind(pg_session)
    chat = await ChatRepo(pg_session, str(USER)).register_session('__context_owner_test', project_id=PROJECT)
    with pytest.raises(HTTPException) as error:
        await ChatDraftRepo(pg_session, str(uuid.uuid4())).get(chat)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_concurrent_windows_append_without_lost_items(pg_session, pg_engine):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from vibecanvas_api.storage.chat_repo import ChatRepo
    from vibecanvas_api.storage.chat_draft_repo import ChatDraftRepo
    await _seed_and_bind(pg_session)
    chat = await ChatRepo(pg_session, str(USER)).register_session('__context_concurrent', project_id=PROJECT)
    await pg_session.commit()
    sessions = async_sessionmaker(pg_engine, expire_on_commit=False)

    async def append(index):
        async with sessions() as session:
            await session.execute(text("SELECT set_config('app.tenant_id',:t,true),set_config('app.user_id',:u,true)"),
                                  {'t':str(TENANT),'u':str(USER)})
            result = await ChatDraftRepo(session, str(USER)).mutate(chat, {
                'kind':'append','operation_id':f'window-{index}',
                'attachments':[attachment(f'a-{index}',f'excerpt-{index}')],
            })
            await session.commit()
            return result
    # Duplicate deliveries from two windows must still apply exactly once.
    await asyncio.gather(*(append(i) for i in [0,1,2,3,0,1]))
    draft = await ChatDraftRepo(pg_session, str(USER)).get(chat)
    assert {item['id'] for item in draft['attachments']} == {'a-0','a-1','a-2','a-3'}
    assert draft['version'] == 4
