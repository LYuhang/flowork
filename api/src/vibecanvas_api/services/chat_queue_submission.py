"""Submit explicit queued intent through the ordinary authorized message path."""
from fastapi import HTTPException, Request
from sqlalchemy import text

from vibecanvas_api.auth.live_identity import LiveIdentityError, resolve_live_authorization_identity
from vibecanvas_api.authorization.dependencies import authz_service_for_session
from vibecanvas_api.schemas.chat import MessagePostBody
from vibecanvas_api.storage.agent_runtime_repo import AgentRuntimeRepo
from vibecanvas_api.storage.agent_runs_repo import AgentRunsRepo
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from .chat_queue_dispatch import dispatch_queued_input


async def finish_chat_queue(*, tenant_id: str, user_id: str, chat_id: str,
                            finished_run_id: str, app) -> None:
    async def submit(message: dict, input_id: str, authority: dict | None) -> None:
        if not authority or authority.get('user_id') != user_id or authority.get('organization_id') != tenant_id:
            raise HTTPException(401, 'queued_identity_unavailable')
        async with session_scope() as identity_session:
            try:
                auth = await resolve_live_authorization_identity(identity_session, **authority)
            except LiveIdentityError as exc:
                raise HTTPException(401, exc.reason) from exc
        async with session_scope(tenant_id=tenant_id, user_id=user_id) as session:
            repo = ChatRepo(session, user_id)
            scope_id = await session.scalar(text('''SELECT scope_id FROM chats
                WHERE chat_id=:chat AND creator_user_id=:owner AND deleted_at IS NULL'''),
                {'chat': chat_id, 'owner': user_id})
            if scope_id is None:
                raise HTTPException(404, 'chat_not_found')
            body = MessagePostBody.model_validate({**message, 'client_request_id': input_id})
            request = Request({'type': 'http', 'method': 'INTERNAL', 'path': '/internal/chat-queue',
                               'headers': [], 'query_string': b'', 'app': app,
                               'state': {'request_id': f'chat-queue:{input_id}', 'queued_after_run_id': finished_run_id}})
            from vibecanvas_api.routes.chats import post_message
            await post_message(
                str(scope_id), chat_id, body, request,
                wf_repo=WorkflowRepo(session, user_id), chat_repo=repo,
                project_repo=ChatProjectRepo(session, user_id), hitl_repo=HitlRepo(session),
                runtime_repo=AgentRuntimeRepo(session, user_id), agent_runs_repo=AgentRunsRepo(session),
                session=session, auth=auth,
                authz_service=authz_service_for_session(session=session, organization_id=tenant_id,
                                                       openfga_client=app.state.openfga_client),
            )
    await dispatch_queued_input(tenant_id=tenant_id, user_id=user_id, chat_id=chat_id,
                                finished_run_id=finished_run_id, submit=submit)
