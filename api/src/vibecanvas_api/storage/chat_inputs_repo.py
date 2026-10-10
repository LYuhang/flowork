"""Durable receipts for explicit steering; this is not an execution queue."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .chat_repo import ChatRepo
from vibecanvas_api.security.content_encryption import content_encryption_service


class ChatInputConflict(ValueError):
    pass


class ChatInputsRepo:
    def __init__(self, session: AsyncSession, user_id: str):
        self.session = session
        self.user_id = user_id

    async def _lock_chat(self, chat_id: str):
        # Match message submission's Chat-first lock order.
        row = (await self.session.execute(text('''
            SELECT tenant_id FROM chats WHERE chat_id=:chat AND creator_user_id=:owner
                AND deleted_at IS NULL FOR UPDATE
        '''), {'chat': chat_id, 'owner': self.user_id})).mappings().first()
        if row is None:
            raise LookupError('chat_not_found')
        return row['tenant_id']

    async def create(self, chat_id: str, run_id: str, input_id: str, value: str) -> dict:
        tenant_id = await self._lock_chat(chat_id)
        run = (await self.session.execute(text('''
            SELECT status, cancel_requested_at FROM agent_runs
            WHERE run_id=:run AND chat_id=:chat AND creator_user_id=:owner FOR UPDATE
        '''), {'run': run_id, 'chat': chat_id, 'owner': self.user_id})).mappings().first()
        if run is None:
            raise LookupError('run_not_found')
        if run['status'] != 'running' or run['cancel_requested_at'] is not None:
            raise ChatInputConflict('turn_not_running')
        if await self.session.scalar(text('''
            SELECT EXISTS(SELECT 1 FROM hitl_requests WHERE run_id=:run AND status='pending')
        '''), {'run': run_id}):
            raise ChatInputConflict('turn_waiting_for_input')
        if await self.session.scalar(text('''
            SELECT EXISTS(SELECT 1 FROM chat_user_inputs WHERE run_id=:run AND status='submitting')
        '''), {'run': run_id}):
            raise ChatInputConflict('steer_already_submitting')
        encrypted = await content_encryption_service().encrypt_json(
            self.session, tenant_id=tenant_id, resource_type='chat', resource_id=chat_id,
            purpose='chat_user_input', record_id=input_id, value={'text': value},
        )
        row = (await self.session.execute(text('''
            INSERT INTO chat_user_inputs
                (input_id, tenant_id, creator_user_id, chat_id, run_id,
                 content_ciphertext, content_nonce, content_key_id)
            VALUES (:id, :tenant, :owner, :chat, :run, :ciphertext, :nonce, :key)
            ON CONFLICT (input_id) DO NOTHING RETURNING *
        '''), {'id': input_id, 'tenant': tenant_id, 'owner': self.user_id,
               'chat': chat_id, 'run': run_id, 'ciphertext': encrypted.ciphertext,
               'nonce': encrypted.nonce, 'key': encrypted.key_id})).mappings().first()
        if row is None:
            raise ChatInputConflict('input_id_already_used')
        return await self._materialize(row)

    async def _materialize(self, row) -> dict:
        content = await content_encryption_service().decrypt_json(
            self.session, key_id=row['content_key_id'], tenant_id=row['tenant_id'],
            resource_type='chat', resource_id=row['chat_id'], purpose='chat_user_input',
            record_id=str(row['input_id']), ciphertext=row['content_ciphertext'], nonce=row['content_nonce'],
        )
        return {**{key: row[key] for key in ('chat_id', 'run_id', 'status', 'error_code', 'created_at', 'updated_at')},
                'input_id': str(row['input_id']), 'text': content['text']}

    async def get(self, chat_id: str, input_id: str) -> dict | None:
        row = (await self.session.execute(text('''
            SELECT i.*, r.status AS run_status FROM chat_user_inputs i
            JOIN agent_runs r ON r.run_id=i.run_id JOIN chats c ON c.chat_id=i.chat_id
            WHERE i.chat_id=:chat AND i.input_id=:id AND i.creator_user_id=:owner
                AND c.deleted_at IS NULL
        '''), {'chat': chat_id, 'id': input_id, 'owner': self.user_id})).mappings().first()
        if row is None:
            return None
        result = await self._materialize(row)
        if result['status'] == 'submitting' and row['run_status'] in {'completed', 'failed', 'cancelled'}:
            result.update(status='unknown', error_code='turn_ended_before_confirmation')
        return result

    async def resolve(self, chat_id: str, run_id: str, input_id: str, status: str, error_code: str | None) -> dict | None:
        if status not in {'accepted', 'rejected', 'unknown'}:
            raise ValueError('invalid_steer_status')
        await self._lock_chat(chat_id)
        row = (await self.session.execute(text('''
            SELECT * FROM chat_user_inputs WHERE input_id=:id AND chat_id=:chat
                AND run_id=:run AND creator_user_id=:owner FOR UPDATE
        '''), {'id': input_id, 'chat': chat_id, 'run': run_id, 'owner': self.user_id})).mappings().first()
        if row is None:
            return None
        stored = await self._materialize(row)
        # Late delivery confirmation can settle an unknown result, but may
        # never overwrite an accepted or explicitly rejected receipt.
        if stored['status'] in {'accepted', 'rejected'} or stored['status'] == status:
            return stored
        if status == 'accepted':
            await ChatRepo(self.session, self.user_id).persist_message(chat_id, {
                'message_id': input_id, 'turn_id': run_id, 'role': 'user',
                'content': {'schema_version': 2, 'message_type': 'text', 'visibility': 'visible',
                            'text': stored['text'], 'attachments': [], 'tool_calls': []},
                'meta': {'status': 'completed', 'input_kind': 'steer'},
            })
        await self.session.execute(text('''
            UPDATE chat_user_inputs SET status=:status, error_code=:error, updated_at=now()
            WHERE input_id=:id
        '''), {'id': input_id, 'status': status, 'error': error_code})
        return await self.get(chat_id, input_id)
