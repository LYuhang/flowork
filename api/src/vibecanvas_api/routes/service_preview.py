"""Map a conversation's loopback URL onto the independent sandbox gateway."""

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.auth.session_security import validate_browser_origin
from vibecanvas_api.services.chat_workspace import project_workspace_scope_id
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.storage.models import Chat, ChatProject

from .sandbox_forwarding import issue_forwarding_target, local_service_url

router = APIRouter(prefix="/api/v1/previews/service", tags=["previews"])


class OpenService(BaseModel):
    chat_id: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=8192)


async def owned_service(body: OpenService, auth: AuthContext, db):
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
    return sandbox, parsed, port, project_id, scope


class StopService(OpenService):
    runtime_id: str
    expected: list[dict]


@router.post("/stop")
async def stop(body: StopService, auth: AuthContext = Depends(current_user), db=Depends(tenant_db)):
    sandbox, _, port, _, _ = await owned_service(body, auth, db)
    try:
        await sandbox.service_inventory(action="stop", port=port, expected=body.expected,
                                        runtime_id=body.runtime_id)
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(409, str(exc) or "service_stop_timeout") from exc
    return {"status": "stopped", "port": port}


@router.post("/resolve")
async def resolve(body: OpenService, request: Request, auth: AuthContext = Depends(current_user), db=Depends(tenant_db)):
    # Bind the preview to the approved browser-facing origin, not the internal
    # reverse-proxy Host. Non-browser clients retain the configured public URL.
    browser_origin = None
    origin_header = request.headers.get("origin", "")
    if origin_header.startswith(("http://", "https://")):
        validate_browser_origin(request)
        origin = urlsplit(origin_header)
        browser_origin = f"{origin.scheme.lower()}://{origin.netloc.lower()}"
    sandbox, parsed, port, project_id, scope = await owned_service(body, auth, db)
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
    result = issue_forwarding_target(
        tenant=str(auth.tenant_id),
        user=str(auth.user_id),
        project=project_id,
        scope=scope,
        runtime_id=identity["runtime_id"],
        port=port,
        scheme=parsed.scheme,
        loopback_host=parsed.hostname,
        path=path,
        browser_origin=browser_origin,
    )
    try:
        inventory = await sandbox.service_inventory()
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(503, str(exc) or "service_inventory_timeout") from exc
    result["service"] = {
        "runtime_id": identity["runtime_id"],
        "expected": [item for item in inventory["services"] if item["port"] == port],
    }
    return result
