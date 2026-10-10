"""Explicit next-turn intent; API requests never dispatch queued work."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthContext, current_user, tenant_db
from ..authorization.dependencies import get_authz_service
from ..authorization.service import AuthzService
from ..authorization.types import Action, ConsistencyPreference
from ..schemas.chat import MessagePostBody
from ..storage.chat_draft_repo import ChatDraftRepo, attachment_key
from ..storage.chat_queue_repo import ChatQueueConflict, ChatQueueRepo
from .chat_engagement import owned_chat
from .chats import _authorize_chat

router = APIRouter(prefix='/api/v1/chats', tags=['chat-queue'])


class QueueInputBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_id: str = Field(min_length=1, max_length=160)
    after_run_id: str = Field(min_length=1, max_length=256)
    message: MessagePostBody

    @model_validator(mode='after')
    def ordinary_message_only(self):
        if self.message.control is not None or self.message.client_request_id is not None:
            raise ValueError('queued inputs cannot contain control messages or execution IDs')
        if not self.message.content.strip() and not self.message.attachments:
            raise ValueError('queued input is empty')
        return self


class QueuedInputOut(BaseModel):
    input_id: str
    chat_id: str
    after_run_id: str
    status: Literal['queued', 'claimed', 'started', 'cancelled', 'blocked', 'failed', 'unknown']
    error_code: str | None
    started_run_id: str | None
    message: MessagePostBody
    created_at: datetime
    updated_at: datetime


class QueuedInputState(BaseModel):
    input: QueuedInputOut | None


@router.get('/{chat_id}/queued-input', response_model=QueuedInputState)
async def latest_queued_input(
    chat_id: str, request: Request,
    auth: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    return {'input': await ChatQueueRepo(session, str(auth.user_id)).latest(chat_id)}


@router.post('/{chat_id}/queued-input', status_code=201, response_model=QueuedInputOut)
async def queue_input(
    chat_id: str, body: QueueInputBody, request: Request,
    auth: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    await _authorize_chat(request=request, auth=auth, service=service, chat_id=chat_id,
                          action=Action.EXECUTE, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    try:
        result = await ChatQueueRepo(session, str(auth.user_id)).enqueue(
            chat_id, body.after_run_id, body.input_id, body.message.model_dump(mode='json', exclude_none=True),
            authority={'session_id': auth.session_id, 'user_id': auth.user_id,
                       'organization_id': auth.active_organization_id, 'session_generation': auth.session_generation,
                       'membership_id': auth.membership_id},
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ChatQueueConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    # Accepting the queue consumes this snapshot now, not when the next turn
    # starts. The ordinary submit path uses the same receipt ID and operation.
    await ChatDraftRepo(session, auth.user_id).mutate(chat_id, {
        'kind': 'consume', 'operation_id': body.input_id,
        'text': body.message.content,
        'attachment_keys': [attachment_key(a.model_dump(mode='json', exclude_none=True))
                            for a in body.message.attachments],
    }, internal=True)
    await session.commit()
    return result


@router.delete('/{chat_id}/queued-input/{input_id}', response_model=QueuedInputOut)
async def cancel_queued_input(
    chat_id: str, input_id: str, request: Request,
    auth: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    try:
        result = await ChatQueueRepo(session, str(auth.user_id)).cancel(chat_id, input_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ChatQueueConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return result
