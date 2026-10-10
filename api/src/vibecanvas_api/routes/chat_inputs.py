"""Owner-only state for explicit input into an existing Agent turn."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthContext, current_user, tenant_db
from ..authorization.dependencies import get_authz_service
from ..authorization.service import AuthzService
from ..storage.agent_runs_repo import AgentRunsRepo
from ..storage.chat_inputs_repo import ChatInputsRepo
from .chat_engagement import owned_chat

router = APIRouter(prefix='/api/v1/chats', tags=['chat-inputs'])


@router.get('/{chat_id}/runs/{run_id}/input-controls')
async def get_chat_input_controls(
    chat_id: str, run_id: str, request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    await owned_chat(request, auth, service, session, chat_id)
    repo = AgentRunsRepo(session)
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
    return {'chat_id': chat_id, 'run_id': run_id, 'run_status': run.status,
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
