"""Map a conversation's loopback URL onto the independent sandbox gateway."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.storage.models import Chat, ChatProject

from .sandbox_forwarding import issue_forwarding_target, local_service_url, preview_domain

router = APIRouter(prefix="/api/v1/previews/service", tags=["previews"])


class OpenService(BaseModel):
    chat_id: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=8192)


@router.post("/resolve")
async def resolve(body: OpenService, auth: AuthContext = Depends(current_user), db=Depends(tenant_db)):
    if not preview_domain():
        raise HTTPException(503, "service_preview_domain_not_configured")
    try:
        parsed, port = local_service_url(body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    project_id = (
        await db.execute(
            select(Chat.project_id)
            .join(ChatProject, ChatProject.project_id == Chat.project_id)
            .where(
                Chat.chat_id == body.chat_id,
                Chat.creator_user_id == auth.user_id,
                Chat.deleted_at.is_(None),
                ChatProject.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(404, "chat_not_found")
    scope = project_workspace_scope_id(project_id)
    sandbox = await get_sandbox_manager().get_loaded_session(auth.tenant_id, scope)
    if sandbox is None:
        raise HTTPException(409, "service_preview_sandbox_stopped")
    try:
        identity = await sandbox.service_forwarding_identity()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    path = parsed.path or "/"
    if path.startswith("//"):
        raise HTTPException(422, "invalid_service_path")
    if parsed.query:
        path += "?" + parsed.query
    if parsed.fragment:
        path += "#" + parsed.fragment
    return issue_forwarding_target(
        tenant=str(auth.tenant_id),
        user=str(auth.user_id),
        project=project_id,
        scope=scope,
        runtime_id=identity["runtime_id"],
        port=port,
        scheme=parsed.scheme,
        loopback_host=parsed.hostname,
        path=path,
    )
