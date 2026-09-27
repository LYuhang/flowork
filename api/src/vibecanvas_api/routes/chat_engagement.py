"""Owner feedback and explicit public, read-only transcript snapshots."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthContext, current_user, tenant_db
from ..auth.ratelimit import (
    LoginRateLimitExceeded, LoginRateLimitUnavailable, consume_rate_limited_action,
)
from ..authorization.dependencies import get_authz_service
from ..authorization.service import AuthzService
from ..authorization.types import Action, ConsistencyPreference
from ..config import config
from ..security.content_encryption import content_encryption_service
from ..storage.chat_repo import ChatRepo
from ..storage.db import get_db
from ..storage.models import Chat, ChatMessage, User
from .chats import _authorize_chat

router = APIRouter(prefix="/api/v1", tags=["chat-engagement"])


class FeedbackBody(BaseModel):
    rating: Literal["up", "down"] | None


class ShareBody(BaseModel):
    message_id: str | None = Field(default=None, min_length=1, max_length=256)


async def owned_chat(request, auth, service, session, chat_id, *, export=False):
    await session.execute(text("SELECT set_config('app.user_id',:user,true)"),
                          {"user": str(auth.user_id)})
    await _authorize_chat(
        request=request, auth=auth, service=service, chat_id=chat_id,
        action=Action.EXPORT if export else Action.VIEW,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    row = (await session.execute(select(Chat).where(
        Chat.chat_id == chat_id, Chat.creator_user_id == auth.user_id,
        Chat.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "chat_not_found")
    return row


@router.get("/chats/{chat_id}/feedback")
async def get_feedback(
    chat_id: str, request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    rows = (await session.execute(text("""
        SELECT message_id, rating FROM chat_message_feedback
        WHERE chat_id=:chat AND user_id=:user
    """), {"chat": chat_id, "user": uuid.UUID(auth.user_id)})).mappings()
    return {"ratings": {row["message_id"]: row["rating"] for row in rows}}


@router.put("/chats/{chat_id}/messages/{message_id}/feedback")
async def put_feedback(
    chat_id: str, message_id: str, body: FeedbackBody, request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    chat = await owned_chat(request, auth, service, session, chat_id)
    message = (await session.execute(select(ChatMessage).where(
        ChatMessage.chat_id == chat_id, ChatMessage.message_id == message_id,
        ChatMessage.role == "assistant",
    ))).scalar_one_or_none()
    if message is None:
        raise HTTPException(404, "assistant_message_not_found")
    values = {"tenant": chat.tenant_id, "user": uuid.UUID(auth.user_id),
              "chat": chat_id, "message": message_id,
              "turn": message.turn_id, "rating": body.rating}
    # Only changed states produce an event; retrying the same PUT is idempotent.
    await session.execute(text("""
        WITH changed AS (
            INSERT INTO chat_message_feedback
                (tenant_id,user_id,chat_id,message_id,turn_id,rating)
            VALUES (:tenant,:user,:chat,:message,:turn,:rating)
            ON CONFLICT (user_id,chat_id,message_id) DO UPDATE
            SET rating=EXCLUDED.rating, updated_at=now()
            WHERE chat_message_feedback.rating IS DISTINCT FROM EXCLUDED.rating
            RETURNING *
        )
        INSERT INTO chat_feedback_events
            (tenant_id,user_id,chat_id,message_id,turn_id,rating)
        SELECT tenant_id,user_id,chat_id,message_id,turn_id,rating FROM changed
    """), values)
    return {"message_id": message_id, "rating": body.rating}


def public_messages(rows: list[dict], message_id: str | None) -> list[dict]:
    result = []
    for row in rows:
        if message_id and row["message_id"] != message_id:
            continue
        content = row["content"]
        if row["role"] not in {"user", "assistant"} or not isinstance(content, dict):
            continue
        if content.get("visibility") == "hidden" or content.get("message_type") == "control":
            continue
        if message_id and row["role"] != "assistant":
            continue
        value = str(content.get("text") or "")
        value = re.sub(r"<think_never_used_[^>]*>.*?(?:</think_never_used_[^>]*>|$)", "", value, flags=re.S)
        if value.strip():
            result.append({"id": row["message_id"], "role": row["role"], "content": value})
    return result


async def decrypt_share(session, row):
    return await content_encryption_service().decrypt_json(
        session, tenant_id=row["tenant_id"], resource_type="chat",
        resource_id=row["chat_id"], purpose="public_share", record_id=str(row["id"]),
        key_id=row["snapshot_key_id"], ciphertext=row["snapshot_ciphertext"],
        nonce=row["snapshot_nonce"],
    )


async def share_out(session, row):
    snapshot = await decrypt_share(session, row)
    path = f"/share/{snapshot['token']}"
    return {"id": str(row["id"]), "message_id": row["message_id"],
            "created_at": row["created_at"], "expires_at": row["expires_at"],
            "url": config.public_urls.absolute(path) if config.public_urls.public_url else None,
            "path": path, "message_count": len(snapshot["messages"])}


@router.get("/chats/{chat_id}/shares")
async def list_shares(
    chat_id: str, request: Request, auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db), service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    rows = (await session.execute(text("""
        SELECT * FROM chat_public_shares WHERE chat_id=:chat AND user_id=:user
        AND revoked_at IS NULL ORDER BY created_at DESC
    """), {"chat": chat_id, "user": uuid.UUID(auth.user_id)})).mappings().all()
    return {"items": [await share_out(session, row) for row in rows]}


@router.post("/chats/{chat_id}/shares")
async def create_share(
    chat_id: str, body: ShareBody, request: Request,
    auth: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    chat = await owned_chat(request, auth, service, session, chat_id, export=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                          {"key": f"public-share:{auth.user_id}:{chat_id}:{body.message_id or ''}"})
    existing = (await session.execute(text("""
        SELECT * FROM chat_public_shares WHERE chat_id=:chat AND user_id=:user
        AND message_id IS NOT DISTINCT FROM CAST(:message AS text) AND revoked_at IS NULL
    """), {"chat": chat_id, "user": uuid.UUID(auth.user_id), "message": body.message_id})).mappings().first()
    if existing:
        return await share_out(session, existing)
    repo = ChatRepo(session, auth.user_id)
    if body.message_id:
        message = (await session.execute(select(ChatMessage).where(
            ChatMessage.chat_id == chat_id, ChatMessage.message_id == body.message_id,
            ChatMessage.role == "assistant",
        ))).scalar_one_or_none()
        if message is None:
            raise HTTPException(404, "assistant_message_not_found")
        envelope = await content_encryption_service().decrypt_json(
            session, tenant_id=message.tenant_id, resource_type="chat", resource_id=chat_id,
            purpose="chat_message", record_id=message.message_id,
            key_id=message.content_key_id, ciphertext=message.content_ciphertext,
            nonce=message.content_nonce,
        )
        rows = [{"message_id": message.message_id, "role": message.role, "content": envelope["content"]}]
        total = 1
    else:
        rows, total, _ = await repo.list_message_page(chat_id, limit=10001)
    if total > 10000:
        raise HTTPException(413, "share_too_large")
    messages = public_messages(rows, body.message_id)
    if not messages:
        raise HTTPException(409, "no_completed_messages_to_share")
    await repo.materialize_session_metadata(chat)
    share_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    snapshot = {"token": token, "title": "Shared response" if body.message_id else chat.name,
                "messages": messages, "kind": "message" if body.message_id else "conversation"}
    if len(json.dumps(snapshot).encode()) > 4 * 1024 * 1024:
        raise HTTPException(413, "share_too_large")
    encrypted = await content_encryption_service().encrypt_json(
        session, tenant_id=chat.tenant_id, resource_type="chat", resource_id=chat_id,
        purpose="public_share", record_id=str(share_id), value=snapshot,
    )
    row = (await session.execute(text("""
        INSERT INTO chat_public_shares
        (id,tenant_id,user_id,chat_id,message_id,token_hash,snapshot_ciphertext,snapshot_nonce,snapshot_key_id)
        VALUES (:id,:tenant,:user,:chat,:message,:hash,:ciphertext,:nonce,:key)
        RETURNING *
    """), {"id": share_id, "tenant": chat.tenant_id, "user": uuid.UUID(auth.user_id),
           "chat": chat_id, "message": body.message_id,
           "hash": hashlib.sha256(token.encode()).hexdigest(),
           "ciphertext": encrypted.ciphertext, "nonce": encrypted.nonce, "key": encrypted.key_id})).mappings().one()
    return await share_out(session, row)


@router.delete("/chats/{chat_id}/shares/{share_id}", status_code=204)
async def revoke_share(
    chat_id: str, share_id: uuid.UUID, request: Request,
    auth: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    await session.execute(text("""
        UPDATE chat_public_shares SET revoked_at=COALESCE(revoked_at,now())
        WHERE id=:id AND chat_id=:chat AND user_id=:user
    """), {"id": share_id, "chat": chat_id, "user": uuid.UUID(auth.user_id)})
    return Response(status_code=204)


@router.get("/public/chat-shares/{token}")
async def read_public_share(token: str, request: Request, response: Response, session: AsyncSession = Depends(get_db)):
    headers = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow, noarchive",
               "Referrer-Policy": "no-referrer"}
    response.headers.update(headers)
    try:
        await consume_rate_limited_action(
            f"public-chat-share:{request.client.host if request.client else 'unknown'}",
            max_attempts=120, window_seconds=60,
        )
    except LoginRateLimitExceeded as exc:
        raise HTTPException(429, "share_rate_limited", headers={**headers, "Retry-After": "60"}) from exc
    except LoginRateLimitUnavailable as exc:
        raise HTTPException(503, "share_temporarily_unavailable", headers=headers) from exc
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(404, "share_unavailable", headers=headers)
    digest = hashlib.sha256(token.encode()).hexdigest()
    await session.execute(text("SELECT set_config('app.share_token_hash',:hash,true)"), {"hash": digest})
    row = (await session.execute(text("""
        SELECT * FROM chat_public_shares WHERE token_hash=:hash AND revoked_at IS NULL
        AND (expires_at IS NULL OR expires_at > now())
    """), {"hash": digest})).mappings().first()
    if not row:
        raise HTTPException(404, "share_unavailable", headers=headers)
    await session.execute(text("SELECT set_config('app.tenant_id',:tenant,true)"), {"tenant": str(row["tenant_id"])})
    alive = (await session.execute(select(Chat.chat_id).join(User, User.user_id == Chat.creator_user_id).where(
        Chat.chat_id == row["chat_id"], Chat.deleted_at.is_(None), User.status == "active",
    ))).scalar_one_or_none()
    if not alive:
        raise HTTPException(404, "share_unavailable", headers=headers)
    snapshot = await decrypt_share(session, row)
    return {"title": snapshot["title"], "kind": snapshot["kind"],
            "messages": snapshot["messages"], "created_at": row["created_at"],
            "expires_at": row["expires_at"]}
