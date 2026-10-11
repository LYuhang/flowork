"""Durable next-turn intent. Claiming grants one dispatch attempt, never a retry."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service


class ChatQueueConflict(ValueError):
    pass


class ChatQueueRepo:
    def __init__(self, session: AsyncSession, user_id: str):
        self.session = session
        self.user_id = user_id

    async def _lock(self, chat_id: str):
        tenant = await self.session.scalar(text('''SELECT tenant_id FROM chats
            WHERE chat_id=:chat AND creator_user_id=:owner AND deleted_at IS NULL FOR UPDATE'''),
            {'chat': chat_id, 'owner': self.user_id})
        if tenant is None:
            raise LookupError('chat_not_found')
        return tenant

    async def enqueue(self, chat_id: str, run_id: str, input_id: str, message: dict, *, authority: dict | None = None) -> dict:
        tenant = await self._lock(chat_id)
        run = (await self.session.execute(text('''SELECT status, cancel_requested_at FROM agent_runs
            WHERE run_id=:run AND chat_id=:chat AND creator_user_id=:owner FOR UPDATE'''),
            {'run': run_id, 'chat': chat_id, 'owner': self.user_id})).mappings().first()
        if run is None or run['status'] != 'running' or run['cancel_requested_at'] is not None:
            raise ChatQueueConflict('turn_not_running')
        if await self.session.scalar(text("SELECT EXISTS(SELECT 1 FROM hitl_requests WHERE run_id=:run AND status='pending')"), {'run': run_id}):
            raise ChatQueueConflict('turn_waiting_for_input')
        encrypted = await content_encryption_service().encrypt_json(
            self.session, tenant_id=tenant, resource_type='chat', resource_id=chat_id,
            purpose='chat_queued_input', record_id=input_id, value={'message': message, 'authority': authority},
        )
        row = (await self.session.execute(text('''INSERT INTO chat_queued_inputs
            (input_id, tenant_id, creator_user_id, chat_id, after_run_id,
             content_ciphertext, content_nonce, content_key_id)
            VALUES (:id, :tenant, :owner, :chat, :run, :ciphertext, :nonce, :key)
            ON CONFLICT DO NOTHING RETURNING *'''),
            {'id': input_id, 'tenant': tenant, 'owner': self.user_id, 'chat': chat_id, 'run': run_id,
             'ciphertext': encrypted.ciphertext, 'nonce': encrypted.nonce, 'key': encrypted.key_id})).mappings().first()
        if row is None:
            raise ChatQueueConflict('queued_input_already_exists')
        return await self._materialize(row)

    async def _materialize(self, row) -> dict:
        content = await content_encryption_service().decrypt_json(
            self.session, tenant_id=row['tenant_id'], resource_type='chat', resource_id=row['chat_id'],
            purpose='chat_queued_input', record_id=row['input_id'],
            ciphertext=row['content_ciphertext'], nonce=row['content_nonce'], key_id=row['content_key_id'],
        )
        keys = ('input_id', 'chat_id', 'after_run_id', 'status', 'error_code', 'started_run_id', 'created_at', 'updated_at')
        return {**{key: row[key] for key in keys}, 'message': content['message'], 'authority': content.get('authority')}

    async def get(self, chat_id: str, input_id: str) -> dict | None:
        row = (await self.session.execute(text('''SELECT q.* FROM chat_queued_inputs q
            JOIN chats c ON c.chat_id=q.chat_id WHERE q.chat_id=:chat AND q.input_id=:id
            AND q.creator_user_id=:owner AND c.deleted_at IS NULL'''),
            {'chat': chat_id, 'id': input_id, 'owner': self.user_id})).mappings().first()
        return await self._materialize(row) if row else None

    async def latest(self, chat_id: str) -> dict | None:
        # A process may exit between accepting a turn and linking its queue
        # receipt. Read durable execution state; never replay work on a GET.
        row = (await self.session.execute(text('''SELECT q.*, previous.status AS previous_status,
            accepted.run_id AS accepted_run_id FROM chat_queued_inputs q
            JOIN chats c ON c.chat_id=q.chat_id
            JOIN agent_runs previous ON previous.run_id=q.after_run_id
            LEFT JOIN agent_runs accepted ON accepted.chat_id=q.chat_id
                AND accepted.creator_user_id=q.creator_user_id AND accepted.client_request_id=q.input_id
            WHERE q.chat_id=:chat AND q.creator_user_id=:owner AND c.deleted_at IS NULL
            ORDER BY q.created_at DESC, q.input_id DESC LIMIT 1'''),
            {'chat': chat_id, 'owner': self.user_id})).mappings().first()
        if row is None:
            return None
        result = await self._materialize(row)
        if result['status'] in {'claimed', 'unknown'} and row['accepted_run_id']:
            result.update(status='started', started_run_id=row['accepted_run_id'], error_code=None)
        elif result['status'] == 'queued':
            if row['previous_status'] in {'failed', 'cancelled', 'cancel_requested'}:
                result.update(status='blocked', error_code='previous_turn_not_successful')
            elif row['previous_status'] == 'completed':
                result['error_code'] = 'previous_turn_completed_pending_dispatch'
        return result

    async def cancel(self, chat_id: str, input_id: str) -> dict:
        await self._lock(chat_id)
        row = (await self.session.execute(text('''UPDATE chat_queued_inputs
            SET status='cancelled', updated_at=now() WHERE chat_id=:chat AND input_id=:id
            AND creator_user_id=:owner AND status IN ('queued', 'blocked') RETURNING *'''),
            {'chat': chat_id, 'id': input_id, 'owner': self.user_id})).mappings().first()
        if row is None:
            raise ChatQueueConflict('queued_input_not_cancellable')
        return await self._materialize(row)

    async def claim(self, chat_id: str, run_id: str) -> dict | None:
        """Call after finalization; commit the claim before sending anything.

        A claimed record remains claimed across restart. No worker scans it for
        redispatch. The eventual run is linked separately after acceptance.
        """
        await self._lock(chat_id)
        run = (await self.session.execute(text('''SELECT status, cancel_requested_at FROM agent_runs
            WHERE run_id=:run AND chat_id=:chat AND creator_user_id=:owner FOR UPDATE'''),
            {'run': run_id, 'chat': chat_id, 'owner': self.user_id})).mappings().first()
        if run is None:
            raise LookupError('run_not_found')
        pending_approval = await self.session.scalar(text("SELECT EXISTS(SELECT 1 FROM hitl_requests WHERE run_id=:run AND status='pending')"), {'run': run_id})
        if run['status'] == 'running' and run['cancel_requested_at'] is None and not pending_approval:
            return None
        newer_run = await self.session.scalar(text('''SELECT EXISTS(SELECT 1 FROM agent_runs
            WHERE chat_id=:chat AND run_id<>:run AND status='running')'''), {'chat': chat_id, 'run': run_id})
        eligible = run['status'] == 'completed' and run['cancel_requested_at'] is None and not pending_approval and not newer_run
        row = (await self.session.execute(text('''UPDATE chat_queued_inputs
            SET status=:status, error_code=:error, updated_at=now()
            WHERE chat_id=:chat AND after_run_id=:run AND creator_user_id=:owner AND status='queued'
            RETURNING *'''), {'chat': chat_id, 'run': run_id, 'owner': self.user_id,
            'status': 'claimed' if eligible else 'blocked',
            'error': None if eligible else 'chat_has_newer_run' if newer_run else 'previous_turn_not_successful'})).mappings().first()
        return await self._materialize(row) if row and eligible else None

    async def resolve_dispatch(self, chat_id: str, input_id: str, *,
                               error_code: str | None = None, rejected: bool = False) -> dict | None:
        """Reconcile an attempted submission against its durable execution ID."""
        from .agent_runs_repo import AgentRunsRepo

        await self._lock(chat_id)
        run = await AgentRunsRepo(self.session).get_by_client_request(
            chat_id, input_id, creator_user_id=self.user_id,
        )
        row = (await self.session.execute(text('''UPDATE chat_queued_inputs
            SET status=:status, started_run_id=:run, error_code=:error, updated_at=now()
            WHERE chat_id=:chat AND input_id=:id AND creator_user_id=:owner AND status='claimed'
            RETURNING *'''), {'chat': chat_id, 'id': input_id, 'owner': self.user_id,
            'status': 'started' if run else 'failed' if rejected else 'unknown',
            'run': run.run_id if run else None, 'error': None if run else error_code or 'submission_unconfirmed'})).mappings().first()
        return await self._materialize(row) if row else await self.get(chat_id, input_id)

    async def block_for_interaction(self, chat_id: str, run_id: str) -> None:
        await self._lock(chat_id)
        await self.session.execute(text("""UPDATE chat_queued_inputs SET status='blocked',
            error_code='previous_turn_waiting_for_input', updated_at=now()
            WHERE chat_id=:chat AND after_run_id=:run AND creator_user_id=:owner AND status='queued'"""),
            {'chat': chat_id, 'run': run_id, 'owner': self.user_id})
