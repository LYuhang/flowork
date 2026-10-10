"""Owner-only state for explicit input into an existing Agent turn."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthContext, current_user, tenant_db
from ..authorization.dependencies import get_authz_service
from ..authorization.service import AuthzService
from ..authorization.types import Action, ConsistencyPreference
from ..storage.agent_runs_repo import AgentRunsRepo
from ..storage.chat_inputs_repo import ChatInputConflict, ChatInputsRepo
from .chat_engagement import owned_chat
from .chats import _authorize_chat

router = APIRouter(prefix='/api/v1/chats', tags=['chat-inputs'])


@router.get('/{chat_id}/input-controls')
@router.get('/{chat_id}/runs/{run_id}/input-controls')
async def get_chat_input_controls(
    chat_id: str, request: Request, run_id: str | None = None,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    repo = AgentRunsRepo(session)
    if run_id is None:
        run_id = await session.scalar(text("""
            SELECT run_id FROM agent_runs WHERE chat_id=:chat AND creator_user_id=:owner
                ORDER BY created_at DESC, run_id DESC LIMIT 1
        """), {'chat': chat_id, 'owner': str(auth.user_id)})
        if run_id is None:
            return {'chat_id': chat_id, 'run_id': None, 'run_status': None, 'latest_input': None,
                    'supported_controls': [], 'available_controls': [], 'unavailable_reason': 'turn_not_running'}
    run = await repo.get_for_chat(chat_id, run_id, creator_user_id=auth.user_id)
    if run is None:
        raise HTTPException(404, 'run_not_found')
    supported = await repo.supported_controls(run_id)
    reason = None
    if run.status != 'running' or run.cancel_requested_at is not None:
        reason = 'turn_not_running'
    elif 'steer' not in supported:
        reason = 'steer_not_supported'
    elif await session.scalar(text("SELECT EXISTS(SELECT 1 FROM hitl_requests WHERE run_id=:run AND status='pending')"), {'run': run_id}):
        reason = 'turn_waiting_for_input'
    elif await session.scalar(text("SELECT EXISTS(SELECT 1 FROM chat_user_inputs WHERE run_id=:run AND status='submitting')"), {'run': run_id}):
        reason = 'steer_already_submitting'
    latest_id = await session.scalar(text('''
        SELECT input_id FROM chat_user_inputs WHERE chat_id=:chat AND run_id=:run
            AND creator_user_id=:owner ORDER BY created_at DESC, input_id DESC LIMIT 1
    '''), {'chat': chat_id, 'run': run_id, 'owner': str(auth.user_id)})
    latest = await ChatInputsRepo(session, str(auth.user_id)).get(chat_id, latest_id) if latest_id else None
    return {'chat_id': chat_id, 'run_id': run_id, 'run_status': run.status, 'latest_input': latest,
            'supported_controls': supported, 'available_controls': supported if reason is None else [],
            'unavailable_reason': reason}


@router.get('/{chat_id}/inputs/{input_id}')
async def get_chat_input_receipt(
    chat_id: str, input_id: str, request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    receipt = await ChatInputsRepo(session, str(auth.user_id)).get(chat_id, input_id)
    if receipt is None:
        raise HTTPException(404, 'input_not_found')
    return receipt


class SteerInputBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=32768)

    @field_validator('text')
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('input text must not be blank')
        return value


@router.post('/{chat_id}/inputs', status_code=202)
async def submit_chat_input(
    chat_id: str, body: SteerInputBody, request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat(request=request, auth=auth, service=service, chat_id=chat_id,
                          action=Action.EXECUTE, consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    await owned_chat(request, auth, service, session, chat_id)
    runs = AgentRunsRepo(session)
    run = await runs.get_for_chat(chat_id, body.run_id, creator_user_id=auth.user_id)
    if run is None:
        raise HTTPException(404, 'run_not_found')
    if 'steer' not in await runs.supported_controls(body.run_id):
        raise HTTPException(409, 'steer_not_supported')
    try:
        receipt = await ChatInputsRepo(session, str(auth.user_id)).create(chat_id, body.run_id, body.input_id, body.text)
    except ChatInputConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    # PostgreSQL delivers this wake-up only after the input transaction commits.
    # Its payload is an ID, never user content; the owner process reads the row.
    await session.execute(text("SELECT pg_notify('flowork_agent_command', :run)"), {'run': body.run_id})
    await session.commit()
    return receipt
