"""Serialized encrypted draft mutations, consumed atomically with a user Turn.

Text uses compare-and-swap; append/remove operate only on their own items.
Receipts survive consumption so delayed retries never resurrect sent content.
"""
from __future__ import annotations

import json
from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import select
from vibecanvas_api.schemas.chat_drafts import DraftOut
from vibecanvas_api.security.content_encryption import content_encryption_service, content_lookup_digest
from vibecanvas_api.storage.models import Chat
from vibecanvas_api.storage.models_chat_drafts import ChatContextDraft, ChatContextDraftOperation


def attachment_key(item: dict) -> str:
    if item.get('schema_version') == 1:
        return str(item['id'])
    return json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def semantic_key(item: dict) -> str:
    if item.get('schema_version') != 1:
        return attachment_key(item)
    # Labels/instance IDs do not make the same source/selection a new excerpt.
    value = {key: value for key, value in item.items() if key not in {'id', 'label'}}
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def apply_draft_operation(state: dict, operation: dict) -> dict:
    next_state = deepcopy(state)
    kind = operation['kind']
    if kind == 'append':
        known = {semantic_key(item) for item in next_state['attachments']}
        by_id = {attachment_key(item): item for item in next_state['attachments']}
        for item in operation['attachments']:
            key = attachment_key(item)
            if key in by_id and semantic_key(by_id[key]) != semantic_key(item):
                raise HTTPException(409, 'draft_attachment_id_conflict')
            if semantic_key(item) not in known:
                next_state['attachments'].append(item)
                known.add(semantic_key(item))
                by_id[key] = item
        if len(next_state['attachments']) > 32:
            raise HTTPException(422, 'too_many_context_attachments')
        text_size = sum(len(json.dumps(item, ensure_ascii=False)) for item in next_state['attachments'])
        if text_size > 131072:
            raise HTTPException(413, 'draft_context_too_large')
    elif kind == 'remove':
        keys = set(operation['attachment_keys'])
        next_state['attachments'] = [item for item in next_state['attachments'] if attachment_key(item) not in keys]
    elif kind == 'text':
        if state['text'] != operation['previous_text'] and state['text'] != operation['text']:
            raise HTTPException(409, 'draft_text_changed_in_another_window')
        next_state['text'] = operation['text']
    elif kind == 'consume':
        # Late appends remain in the next generation. Concurrent edits survive.
        keys = set(operation['attachment_keys'])
        next_state['attachments'] = [item for item in next_state['attachments'] if attachment_key(item) not in keys]
        if state['text'] == operation['text']:
            next_state['text'] = ''
        next_state['generation'] += 1
    else:
        raise ValueError('unknown draft operation')
    next_state['version'] += 1
    return next_state


class ChatDraftRepo:
    def __init__(self, session, user_id):
        self.session, self.user_id = session, user_id

    async def _chat(self, chat_id, *, lock=False):
        query = select(Chat).where(Chat.chat_id == chat_id, Chat.creator_user_id == self.user_id,
                                   Chat.deleted_at.is_(None))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        chat = (await self.session.execute(query)).scalar_one_or_none()
        if chat is None:
            raise HTTPException(404, 'chat_not_found')
        return chat

    async def _read(self, chat_id):
        row = await self.session.get(ChatContextDraft, chat_id, populate_existing=True)
        if row is None:
            return DraftOut(chat_id=chat_id).model_dump(), None
        value = await content_encryption_service().decrypt_json(self.session,
            key_id=row.content_key_id, tenant_id=row.tenant_id, resource_type='chat',
            resource_id=chat_id, purpose='chat_draft', record_id=chat_id,
            ciphertext=row.content_ciphertext, nonce=row.content_nonce)
        return dict(chat_id=chat_id, version=row.version, generation=row.generation, **value), row

    async def get(self, chat_id):
        await self._chat(chat_id)
        state, _ = await self._read(chat_id)
        return state

    async def mutate(self, chat_id, operation: dict, *, internal=False):
        chat = await self._chat(chat_id, lock=True)
        state, row = await self._read(chat_id)
        operation_id = ('send:' if internal else 'edit:') + operation['operation_id']
        digest = content_lookup_digest(tenant_id=chat.tenant_id, namespace='chat_draft_operation',
            value=json.dumps(operation, sort_keys=True, ensure_ascii=False))
        receipt = await self.session.get(ChatContextDraftOperation, (chat_id, operation_id))
        if receipt is not None:
            if receipt.request_digest != digest:
                raise HTTPException(409, 'draft_operation_id_conflict')
            return state
        updated = apply_draft_operation(state, operation)
        encrypted = await content_encryption_service().encrypt_json(self.session,
            tenant_id=chat.tenant_id, resource_type='chat', resource_id=chat_id,
            purpose='chat_draft', record_id=chat_id,
            value={'text': updated['text'], 'attachments': updated['attachments']})
        if row is None:
            row = ChatContextDraft(chat_id=chat_id, tenant_id=chat.tenant_id)
            self.session.add(row)
        row.version, row.generation = updated['version'], updated['generation']
        row.content_ciphertext, row.content_nonce, row.content_key_id = encrypted.ciphertext, encrypted.nonce, encrypted.key_id
        self.session.add(ChatContextDraftOperation(chat_id=chat_id, tenant_id=chat.tenant_id,
            operation_id=operation_id, request_digest=digest, applied_version=updated['version']))
        await self.session.flush()
        return updated
