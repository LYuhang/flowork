"""Explicit branches of finished chat turns, without starting Agent execution."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthContext, current_user, tenant_db
from ..authorization.dependencies import authorize_resource, get_authz_service
from ..authorization.service import AuthzService
from ..authorization.types import Action, ResourceRef, ResourceType
from ..services.chat_branch_files import unavailable_branch_files
from ..services.chat_history_projection import hitl_history_projection, interactive_artifact_out
from ..storage.chat_repo import ChatRepo
from ..storage.hitl_repo import HitlRepo
from ..storage.workflow_repo import WorkflowRepo
from .chat_engagement import owned_chat
from .chats import _authorize_chat_carrier, _commit_new_chat_projection

router = APIRouter(prefix='/api/v1/chats', tags=['chat-branches'])


class BranchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_id: UUID
    turn_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=200)


class BranchOut(BaseModel):
    chat_id: str
    source_chat_id: str
    through_turn_id: str
    project_id: str
    scope_id: str
    message_count: int
    file_policy: str = 'shared_current_project_files'
    starts_agent: bool = False
    unavailable_files: list[str] = Field(default_factory=list)


async def branch_source(chat_id, turn_id, request, auth, session, service):
    source = await owned_chat(request, auth, service, session, chat_id, export=True)
    repo = ChatRepo(session, auth.user_id)
    await repo.materialize_session_metadata(source)
    await _authorize_chat_carrier(
        request=request, auth=auth, service=service,
        workflow_repo=WorkflowRepo(session, auth.user_id), scope_id=source.scope_id,
        action=Action.UPDATE if source.meta.get('workflow_context') else Action.USE,
    )
    await authorize_resource(
        request=request, auth=auth, service=service,
        resource=ResourceRef(ResourceType.ORGANIZATION, auth.active_organization_id,
                             auth.active_organization_id), action=Action.CREATE,
    )
    try:
        _, total, _ = await repo.list_message_page(chat_id, through_turn_id=turn_id, limit=1)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    unavailable = await unavailable_branch_files(
        repo=repo, source=source, turn_id=turn_id, request=request,
        auth=auth, session=session, service=service,
    )
    return source, repo, total, unavailable


@router.get('/{chat_id}/branches/preview', response_model=BranchOut)
async def preview_branch(
    chat_id: str, turn_id: str, request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    source, _, total, unavailable = await branch_source(chat_id, turn_id, request, auth, session, service)
    return BranchOut(chat_id='', source_chat_id=chat_id, through_turn_id=turn_id,
                     project_id=source.project_id, scope_id=source.scope_id, message_count=total,
                     unavailable_files=unavailable)


@router.post('/{chat_id}/branches', response_model=BranchOut, status_code=201)
async def create_branch(
    chat_id: str, body: BranchBody, request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
):
    source, repo, total, unavailable = await branch_source(chat_id, body.turn_id, request, auth, session, service)
    definitions, projections = {}, {}
    for artifact, hitl in await HitlRepo(session).list_artifact_refs_for_chat(chat_id):
        definitions[artifact.artifact_id] = interactive_artifact_out(
            artifact, hitl_status=hitl.status if hitl else None,
        )['artifact']
        projected = hitl_history_projection(artifact, hitl)
        if projected:
            projections[projected[0]] = projected[1].artifact
    target = str(body.chat_id)
    try:
        await repo.create_branch_snapshot(
            chat_id, through_turn_id=body.turn_id, chat_id=target, name=body.name,
            tool_projections=projections, artifact_definitions=definitions,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _commit_new_chat_projection(
        request=request, session=session, auth=auth, chat_id=target,
        operation_id=f'{target}:create',
    )
    return BranchOut(chat_id=target, source_chat_id=chat_id, through_turn_id=body.turn_id,
                     project_id=source.project_id, scope_id=source.scope_id, message_count=total,
                     unavailable_files=unavailable)
