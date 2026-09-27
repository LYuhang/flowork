"""Deployment-only, idempotent cleanup of encrypted legacy Chat pointers.

Run after schema/content migrations and before admitting new application work.
Never rewrite plaintext metadata or delete Chat/browser/runtime state.
"""
from sqlalchemy import select

from vibecanvas_api.security.content_encryption import content_encryption_service
from vibecanvas_api.storage.chat_repo import ChatRepo, without_workflow_state
from vibecanvas_api.storage.models import Chat
from vibecanvas_api.services.tenant_db import session_scope_admin


async def clear_retired_workflow_state() -> int:
    cursor = ""
    changed = 0
    while True:
        async with session_scope_admin() as session:
            rows = (await session.execute(select(Chat).where(Chat.chat_id > cursor)
                .order_by(Chat.chat_id).limit(100).with_for_update())).scalars().all()
            if not rows:
                return changed
            for chat in rows:
                value = await content_encryption_service().decrypt_json(
                    session, key_id=chat.metadata_key_id, tenant_id=chat.tenant_id,
                    resource_type="organization_metadata", resource_id=str(chat.tenant_id),
                    purpose="chat_metadata", record_id=chat.chat_id,
                    ciphertext=chat.metadata_ciphertext, nonce=chat.metadata_nonce)
                if not isinstance(value, dict) or not isinstance(value.get("meta", {}), dict):
                    raise ValueError("Invalid encrypted Chat metadata during state cleanup")
                old = value.get("meta", {})
                clean = without_workflow_state(old)
                if old != clean:
                    await ChatRepo(session, str(chat.creator_user_id))._store_chat_private(
                        chat, name=str(value.get("name") or ""), meta=clean)
                    changed += 1
            cursor = rows[-1].chat_id
