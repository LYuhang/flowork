"""Chats + SSE messages + resume + cancel.

The HTTP layer owns product persistence and streaming. Agent SDK execution is
selected only by ``AgentRuntimeOrchestrator`` and runs in the Chat sandbox.

URL layout: chat-scope id lives in the URL path so chat_id need not be
globally unique. resume / cancel are keyed by turn_id alone (turn_id is
unique across the whole runtime), with chat_id retained as a sub-path for REST
ergonomics only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
import hashlib
import json
import mimetypes
import os
import uuid
from datetime import datetime, timedelta, timezone
from time import perf_counter
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from ..auth.deps import (
    AuthContext,
    current_user,
    require_recent_step_up,
    tenant_db,
)
from ..authorization.dependencies import (
    authorize_resource,
    context_for_auth,
    get_authz_service,
    mutation_coordinator_for_request,
    principal_for_auth,
)
from ..authorization.projection import (
    apply_committed_structural_mutations,
    enqueue_structural_delta,
    resource_root_edges,
)
from ..authorization.service import (
    AuthzService,
    batch_resource_decisions,
)
from ..authorization.stream_guard import authorization_lease_is_valid
from ..authorization.types import (
    Action,
    AuthorizedResource,
    ConsistencyPreference,
    ResourceRef,
    ResourceType,
)
from ..config import config as app_config
from ..security.upload_scanner import require_clean_upload
from ..schemas.access import access_from_decision, decision_allows_content
from ..schemas.chat import (
    ActiveAgentRun, Attachment,
    BackgroundJobCancelBody, BackgroundJobOut,
    BackgroundResultsControl,
    BrowserBindingOut, ChatInventoryItem, ChatListItem, ChatRenameBody,
    ChatRuntimeBindingOut, ChatStateOut, HistoryMessage,
    HitlContinueControl, HitlDecisionBody, HitlRequestOut,
    InteractiveArtifactResultFileBody,
    InteractiveArtifactStateBody, MessagePostBody, AgentSettings,
)
from ..schemas.pagination import Page, PageRequest
from ..services.user_mount_workspace import mount_scope_id as _mount_scope_id
from ..storage.chat_repo import ChatRepo
from ..storage.db import session_scope
from ..storage.background_jobs_repo import (
    BackgroundJobsRepo,
    project_background_job,
)
from ..storage.background_delivery_repo import BackgroundDeliveryRepo
from ..storage.models_background_jobs import ACTIVE_BACKGROUND_JOB_STATUSES
from ..storage.vfs_store import VfsRepo, _validate_artifact_path
from ..storage.workflow_repo import WorkflowRepo
from ..streaming.sse import format_event
from ..streaming.turn_runtime import (
    TURN_BUFFERS, TURN_TASKS, new_turn_id, register_turn,
    run_turn,
)
# NOTE: `run_turn` is already imported above — it fences both the normal agent
# turn and the `/browser` handoff producer with the frozen started/done envelope.
from ..services.llm_credentials_inject import merge_agent_settings_override
from ..services.object_store import get_object_store
from ..services.sandbox.manager import get_sandbox_manager
from ..services.vfs_volume import get_chat_runtime_volume_provider
from ..services.agent_runtime.capabilities import (
    codex_account_model_id,
    codex_capabilities,
    codex_credential_id,
    codex_managed_model,
    codex_openrouter_model,
    langchain_capabilities,
    langchain_credential_id,
    langchain_openrouter_model,
    runtime_model_connection_id,
    validate_model_effort,
)
from ..services.agent_runtime.model_capability import (
    authorization_model_generation,
    mint_runtime_model_capability,
    model_config_revision,
)
from ..services.agent_runtime.orchestrator import (
    AgentRuntimeOrchestrator,
    private_runtime_root,
)
from ..services.agent_runtime.mcp_host_resolution import (
    McpSelectionError,
    platform_mcp_names_for_modes,
    resolve_custom_mcp_authority,
    resolve_platform_mcp_authority,
)
from ..services.agent_runtime.instructions import command_instructions_for_modes
from ..services.agent_runtime.history_recovery import (
    build_durable_history_snapshot,
)
from ..services.runtime_skills import runtime_skill_descriptors
from ..services.agent_runtime.protocol import (
    RuntimeOpenRequest,
    RuntimeTurnRequest,
    RuntimeType,
)
from ..services.agent_runtime.registry import AVAILABLE_RUNTIME_TYPES
from ..services.chat_workspace import (
    chat_workspace_scope_id as _chat_workspace_scope_id,
)
from ..storage.repo_llm_credentials import LlmCredentialsRepo
from ..services.preview_resource_policy import (
    html_vfs_read_rules,
    rules_for_root,
)
from ..services.vfs_signing import issue_vfs_resource_capability
from .deps import (
    get_agent_runs_repo,
    get_agent_runtime_repo,
    get_chat_repo,
    get_hitl_repo,
    get_workflow_repo,
)
from langchain_core.messages.utils import count_tokens_approximately
from ..agents.middleware.compaction_forms import (
    parse_envelope, output_content_type, output_path,
)

router = APIRouter(prefix="/api/v1", tags=["chats"])
logger = structlog.get_logger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

AVAILABLE_COMMANDS_BY_SURFACE: dict[str, set[str]] = {
    "chat": {"task", "deployment", "knowledge", "workflow", "diagram", "document"},
    "browser": {"task", "deployment", "knowledge", "workflow", "browser", "diagram", "document"},
}

def _available_commands(surface: str, runtime_type: str | None = None) -> set[str]:
    del runtime_type
    return set(AVAILABLE_COMMANDS_BY_SURFACE.get(surface, set()))


def _chat_carrier_scope_id(user_id: str) -> str:
    return f"__chat_{user_id.replace('-', '')[:24]}"


def _browser_carrier_scope_id(user_id: str) -> str:
    return f"__browser_{user_id.replace('-', '')[:21]}"


def _surface_carrier_scope_id(user_id: str, surface: str) -> str:
    return (
        _browser_carrier_scope_id(user_id)
        if surface == "browser"
        else _chat_carrier_scope_id(user_id)
    )


def _is_internal_carrier_scope(scope_id: str, user_id: str) -> bool:
    return scope_id in {
        _chat_carrier_scope_id(user_id),
        _browser_carrier_scope_id(user_id),
    }


def _chat_resource(auth: AuthContext, chat_id: str) -> ResourceRef:
    return ResourceRef(
        ResourceType.CHAT,
        chat_id,
        auth.active_organization_id,
    )


async def _authorize_chat(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    chat_id: str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    decision = await service.check(
        principal_for_auth(auth),
        action,
        _chat_resource(auth, chat_id),
        context_for_auth(auth, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="chat_not_found")
    return AuthorizedResource(
        resource=_chat_resource(auth, chat_id),
        decision=decision,
    )


async def _authorize_chat_child(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    resource_type: ResourceType,
    resource_id: str,
    action: Action,
    not_found_detail: str,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> AuthorizedResource:
    resource = ResourceRef(
        resource_type,
        resource_id,
        auth.active_organization_id,
    )
    decision = await service.check(
        principal_for_auth(auth),
        action,
        resource,
        context_for_auth(auth, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return AuthorizedResource(resource=resource, decision=decision)


async def _authorize_chat_carrier(
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    workflow_repo: WorkflowRepo,
    scope_id: str,
    action: Action,
) -> None:
    """Authorize the product resource carrying a Chat collection."""
    if scope_id.startswith("__"):
        if not _is_internal_carrier_scope(scope_id, auth.user_id):
            raise HTTPException(status_code=404, detail="chat_scope_not_found")
        return
    await authorize_resource(
        request=request,
        auth=auth,
        service=service,
        resource=ResourceRef(
            ResourceType.WORKFLOW,
            scope_id,
            auth.active_organization_id,
        ),
        action=action,
    )
    if not await workflow_repo.get_meta(scope_id):
        raise HTTPException(status_code=404, detail="chat_scope_not_found")


async def _rebind_request_organization(
    session: AsyncSession,
    auth: AuthContext,
) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :organization_id, true)"),
        {"organization_id": auth.active_organization_id},
    )


async def _commit_new_chat_projection(
    *,
    request: Request,
    session: AsyncSession,
    auth: AuthContext,
    chat_id: str,
    operation_id: str,
) -> None:
    coordinator = mutation_coordinator_for_request(
        request,
        auth.active_organization_id,
    )
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=frozenset(),
        after=resource_root_edges(
            organization_id=auth.active_organization_id,
            object_type="chat",
            object_id=chat_id,
            owner_relation="creator",
            owner_type="user",
            owner_id=auth.user_id,
        ),
        operation_id=operation_id,
        source="chat-create",
    )
    await session.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    await _rebind_request_organization(session, auth)


@router.get("/chats/bootstrap", dependencies=[Depends(current_user)])
async def bootstrap_general_chat(
    surface: str = Query(default="chat"),
    runtime_repo=Depends(get_agent_runtime_repo),
    auth: AuthContext = Depends(current_user),
) -> dict:
    """Return the internal carrier scope for the general Chat surface."""
    agent_surface = "browser" if surface == "browser" else "chat"
    scope_id = _surface_carrier_scope_id(auth.user_id, agent_surface)
    runtime_type = (await runtime_repo.get_preferences())["default_runtime_type"]
    return {
        "carrier_scope_id": scope_id,
        "surface": agent_surface,
        "available_commands": sorted(
            _available_commands(agent_surface, runtime_type)
        ),
        "debug_view_enabled": bool(app_config.agent_debug_view_enabled),
    }


@router.get("/chats/workspace", dependencies=[Depends(current_user)])
async def get_chat_workspace(
    request: Request,
    chat_id: str = Query(...),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Return the persistent VFS/sandbox workspace scope for one chat."""
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.VIEW,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    scope_id = _chat_workspace_scope_id(chat_id)
    return {
        "workspace_scope_id": scope_id,
        "mount_scope_id": _mount_scope_id(auth.user_id),
        "chat_id": chat_id,
        "current_workflow_id": await chat_repo.get_current_workflow_id(chat_id),
    }


def _safe_attachment_name(filename: str | None) -> str:
    """Return a display-safe basename while preserving a useful extension."""
    name = os.path.basename(filename or "attachment").strip()
    name = "".join("_" if ord(ch) < 0x20 or ch == "\x7f" else ch for ch in name)
    if not name or name in {".", ".."}:
        name = "attachment"
    stem, suffix = os.path.splitext(name)
    # Keep VFS paths and UI payloads bounded without losing the file type.
    return f"{stem[:140]}{suffix[:20]}"


@router.post(
    "/chat-scopes/{scope_id}/chats/{chat_id}/attachments",
    response_model=Attachment,
    dependencies=[Depends(current_user)],
)
async def upload_chat_attachment(
    scope_id: str,
    chat_id: str,
    request: Request,
    file: UploadFile = File(...),
    attachment_type: str = Query(default="file"),
    wf_repo: WorkflowRepo = Depends(get_workflow_repo),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> Attachment:
    """Persist one composer attachment in this chat's durable VFS workspace.

    The client only supplies ``chat_id`` and the visible carrier scope.  The
    private VFS workspace id is derived server-side so main-app and extension
    clients stay decoupled from storage layout details.
    """
    if attachment_type not in {"file", "image", "video"}:
        raise HTTPException(status_code=400, detail="invalid_attachment_type")
    await _authorize_chat_carrier(
        request=request,
        auth=auth,
        service=service,
        workflow_repo=wf_repo,
        scope_id=scope_id,
        action=Action.USE,
    )

    name = _safe_attachment_name(file.filename)
    content_type = (file.content_type or "application/octet-stream").split(";", 1)[0]
    if content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(name)[0] or content_type
    if attachment_type == "image" and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="attachment_not_image")
    if attachment_type == "video" and not content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="attachment_not_video")

    data = await file.read()
    if len(data) > app_config.storage.vfs_upload_max_bytes:
        raise HTTPException(status_code=413, detail="file_too_large")
    await require_clean_upload(data)

    sessions = await chat_repo.list_sessions(scope_id)
    is_first = not any(item["chat_id"] == chat_id for item in sessions)
    if is_first:
        await authorize_resource(
            request=request,
            auth=auth,
            service=service,
            resource=ResourceRef(
                ResourceType.ORGANIZATION,
                auth.active_organization_id,
                auth.active_organization_id,
            ),
            action=Action.CREATE,
        )
        surface = "browser" if scope_id == _browser_carrier_scope_id(auth.user_id) else "chat"
        try:
            await chat_repo.register_session(
                scope_id,
                chat_id,
                chat_context="New chat",
                surface=surface,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404,
                detail="chat_not_found",
            ) from exc
        await _commit_new_chat_projection(
            request=request,
            session=session,
            auth=auth,
            chat_id=chat_id,
            operation_id=f"{chat_id}:attachment:{uuid.uuid4().hex}",
        )
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )

    path = _validate_artifact_path(
        f"/data/attachments/{uuid.uuid4().hex[:12]}_{name}"
    )
    workspace_scope = _chat_workspace_scope_id(chat_id)
    repo = VfsRepo(session, object_store=get_object_store())
    await repo.upsert_artifact_bytes(
        wf_id=workspace_scope,
        tenant=auth.tenant_id,
        path=path,
        data=data,
        content_type=content_type,
    )
    # Durability precedes the optional live-sandbox projection. A worker loss
    # after this point can only miss the mirror; the next sandbox materialize
    # still reconstructs the file from VFS.
    await chat_repo.commit()
    # If the resident sandbox is already warm, make the upload visible to the
    # next tool call immediately; a cold sandbox will materialize it from VFS.
    try:
        await get_sandbox_manager().mirror_vfs_write(
            auth.tenant_id, workspace_scope, path, data,
        )
    except Exception:  # pragma: no cover - durable VFS remains authoritative
        logger.warning("chat_attachment_live_mirror_failed", chat_id=chat_id, path=path)
    return Attachment(
        type=attachment_type,
        name=name,
        path=path,
        content_type=content_type,
        size_bytes=len(data),
    )


@router.get("/chats/sandbox", dependencies=[Depends(current_user)])
async def get_chat_sandbox_status(
    request: Request,
    chat_id: str | None = Query(default=None),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Resident Agent sandbox status for the general Chat surface.

    This is read-only and intentionally does not create a sandbox. The resident
    sandbox is still created lazily by tools that require it.
    """
    if chat_id:
        await _authorize_chat(
            request=request,
            auth=auth,
            service=service,
            chat_id=chat_id,
            action=Action.INSPECT_RUNS,
        )
    scope_id = (
        _chat_workspace_scope_id(chat_id)
        if chat_id else _chat_carrier_scope_id(auth.user_id)
    )
    status = await get_sandbox_manager().status(auth.tenant_id, scope_id)
    return {"scope_id": scope_id, **status}


@router.get("/chats/sandboxes", dependencies=[Depends(current_user)])
async def get_chat_sandbox_statuses(
    request: Request,
    chat_id: list[str] = Query(default=[]),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Batch resident sandbox statuses for the Chat history sidebar.

    Read-only and non-creating: a history list should show resource placement
    without warming missing workspaces or sandboxes.
    """
    items = []
    seen: set[str] = set()
    authorized_ids = set(await service.list_authorized_ids(
        principal_for_auth(auth),
        Action.INSPECT_RUNS,
        ResourceType.CHAT,
        context_for_auth(auth, request),
    ))
    for cid in chat_id[:200]:
        if not cid or cid in seen or cid not in authorized_ids:
            continue
        seen.add(cid)
        scope_id = _chat_workspace_scope_id(cid)
        status = await get_sandbox_manager().status(auth.tenant_id, scope_id)
        items.append({"chat_id": cid, "scope_id": scope_id, **status})
    return {"items": items}


@router.patch(
    "/chat-scopes/{scope_id}/chats/{chat_id}",
    response_model=ChatListItem,
    dependencies=[Depends(current_user)],
)
async def rename_chat_session(
    scope_id: str,
    chat_id: str,
    body: ChatRenameBody,
    request: Request,
    chat_repo: ChatRepo = Depends(get_chat_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> ChatListItem:
    """Rename one Chat without changing its Runtime or transcript."""
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    updated = await chat_repo.rename_session(scope_id, chat_id, body.name)
    if updated is None:
        raise HTTPException(status_code=404, detail="chat_not_found")
    await chat_repo.commit()
    return ChatListItem(**updated)


@router.delete("/chat-scopes/{scope_id}/chats/{chat_id}", dependencies=[Depends(current_user)])
async def delete_chat_session(
    scope_id: str,
    chat_id: str,
    request: Request,
    surface: str = Query(default="chat"),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    agent_runs_repo=Depends(get_agent_runs_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Delete one chat and its chat-local workspace data.

    The chat row is soft-deleted, persisted message rows are removed, the live
    sandbox is closed, and the chat workspace VFS prefixes `/data`, `/memory`,
    and `/logs` are removed. Creators use their normal carrier; organization
    admins may use the content-free inventory scope after an explicit DELETE
    decision and high-risk step-up.
    """
    agent_surface = "browser" if surface == "browser" else "chat"
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.DELETE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    selected = await chat_repo.get_authorized_inventory(chat_id)
    if (
        selected is None
        or selected["scope_id"] != scope_id
        or selected["surface"] != agent_surface
    ):
        raise HTTPException(status_code=404, detail="chat_not_found")
    creator_user_id = selected["creator_user_id"]
    if creator_user_id != auth.user_id:
        await require_recent_step_up(auth)
    if selected.get("browser_control_status") in {
        "attaching", "attached", "lost",
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "browser_session_active",
                "message": (
                    "End browser control before deleting this chat. "
                    "This prevents leaving controlled tabs detached from their persisted chat."
                ),
            },
        )

    active_run = await agent_runs_repo.get_active_for_chat_user(
        chat_id,
        creator_user_id=creator_user_id,
    )
    if active_run is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "chat_turn_active",
                "message": (
                    "Stop the active Agent turn before deleting this chat. "
                    "This preserves the transcript and prevents revoking "
                    "Runtime capabilities while work is still running."
                ),
            },
        )

    workspace_scope_id = _chat_workspace_scope_id(chat_id)
    runtime_binding = (
        {
            "runtime_type": selected["runtime_type"],
            "runtime_session_id": selected["runtime_session_id"],
            "runtime_state_ref": selected["runtime_state_ref"],
            "runtime_version": selected["runtime_version"],
        }
        if selected.get("runtime_type")
        and selected.get("runtime_session_id")
        and selected.get("runtime_state_ref")
        else None
    )
    await get_sandbox_manager().close_session(auth.tenant_id, workspace_scope_id)
    vfs_deleted = await VfsRepo(
        session,
        object_store=get_object_store(),
    ).delete_scope_prefixes(
        wf_id=workspace_scope_id,
        prefixes=["/data", "/memory", "/logs", "/__runtime"],
    )
    runtime_state_deleted = False
    if runtime_binding is not None and runtime_binding.get("runtime_type"):
        runtime_type = RuntimeType(runtime_binding["runtime_type"])
        runtime_state_deleted = await AgentRuntimeOrchestrator().delete_state(
            RuntimeOpenRequest(
                tenant_id=auth.tenant_id,
                user_id=creator_user_id,
                chat_id=chat_id,
                runtime_type=runtime_type,
                runtime_session_id=runtime_binding["runtime_session_id"],
                runtime_root=private_runtime_root(runtime_type, chat_id),
                state_ref=runtime_binding["runtime_state_ref"],
                runtime_version=runtime_binding["runtime_version"],
            )
        )
    # The Runtime Volume belongs to the Chat, not to one adapter checkpoint.
    # A Chat may have created the volume before its first Runtime state_ref, and
    # LangChain Chats can also have an empty volume from sandbox preparation.
    # Delete it unconditionally after the sandbox owner has been closed.
    runtime_volume_deleted = await asyncio.to_thread(
        get_chat_runtime_volume_provider().delete,
        tenant_id=auth.tenant_id,
        user_id=creator_user_id,
        chat_scope_id=workspace_scope_id,
    )
    runtime_state_deleted = runtime_state_deleted or runtime_volume_deleted
    await chat_repo.drop_authorized_session(chat_id)
    coordinator = mutation_coordinator_for_request(
        request,
        auth.active_organization_id,
    )
    mutation_ids = await enqueue_structural_delta(
        session=session,
        coordinator=coordinator,
        actor_type="user",
        actor_id=auth.user_id,
        before=resource_root_edges(
            organization_id=auth.active_organization_id,
            object_type="chat",
            object_id=chat_id,
            owner_relation="creator",
            owner_type="user",
            owner_id=creator_user_id,
        ),
        after=frozenset(),
        operation_id=f"{chat_id}:delete:{uuid.uuid4().hex}",
        source="chat-delete",
    )
    await chat_repo.commit()
    await apply_committed_structural_mutations(coordinator, mutation_ids)
    logger.info(
        "chat_session_deleted",
        chat_id=chat_id,
        scope_id=scope_id,
        workspace_scope_id=workspace_scope_id,
        vfs_deleted=vfs_deleted,
        runtime_type=runtime_binding.get("runtime_type") if runtime_binding else None,
        runtime_state_deleted=runtime_state_deleted,
    )
    return {
        "chat_id": chat_id,
        "workspace_scope_id": workspace_scope_id,
        "vfs_deleted": vfs_deleted,
        "runtime_state_deleted": runtime_state_deleted,
    }


@router.post("/chats/sandbox", dependencies=[Depends(current_user)])
async def start_chat_sandbox(
    request: Request,
    chat_id: str = Query(...),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Explicitly warm the resident sandbox for one Chat workspace."""
    carrier_scope_id = _chat_carrier_scope_id(auth.user_id)
    sessions = await chat_repo.list_sessions(carrier_scope_id, surface="chat")
    if not any(item["chat_id"] == chat_id for item in sessions):
        await authorize_resource(
            request=request,
            auth=auth,
            service=service,
            resource=ResourceRef(
                ResourceType.ORGANIZATION,
                auth.active_organization_id,
                auth.active_organization_id,
            ),
            action=Action.CREATE,
        )
        try:
            await chat_repo.register_session(
                carrier_scope_id,
                chat_id,
                chat_context="New chat",
                surface="chat",
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404,
                detail="chat_not_found",
            ) from exc
        await _commit_new_chat_projection(
            request=request,
            session=session,
            auth=auth,
            chat_id=chat_id,
            operation_id=f"{chat_id}:sandbox:{uuid.uuid4().hex}",
        )
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.MOUNT,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    scope_id = _chat_workspace_scope_id(chat_id)
    current_workflow_id = await chat_repo.get_current_workflow_id(chat_id)
    session = await get_sandbox_manager().get_session(
        auth.tenant_id,
        scope_id,
        user_id=auth.user_id,
        expose_run=True,
    )
    await session.prewarm_fileops()
    status = await get_sandbox_manager().status(auth.tenant_id, scope_id)
    return {
        "scope_id": scope_id,
        "mount_scope_id": _mount_scope_id(auth.user_id),
        "current_workflow_id": current_workflow_id,
        **status,
    }


@router.delete("/chats/sandbox", dependencies=[Depends(current_user)])
async def close_chat_sandbox(
    request: Request,
    chat_id: str | None = Query(default=None),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Release a Chat sandbox only when no background result still needs it."""
    scope_id = (
        _chat_workspace_scope_id(chat_id)
        if chat_id else _chat_carrier_scope_id(auth.user_id)
    )
    if chat_id:
        await _authorize_chat(
            request=request,
            auth=auth,
            service=service,
            chat_id=chat_id,
            action=Action.CANCEL,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
        )
        repo = BackgroundDeliveryRepo(session)
        holds = await repo.list_sandbox_holds_for_user(
            chat_id=chat_id,
            creator_user_id=auth.user_id,
            limit=200,
        )
        if holds:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "sandbox_held_by_background_jobs",
                    "job_ids": [job.job_id for job in holds],
                },
            )
    status = await get_sandbox_manager().close_session(auth.tenant_id, scope_id)
    return {
        "scope_id": scope_id,
        "cancelled_background_job_ids": [],
        **status,
    }


def _debug_meta(m) -> dict:
    """Per-message debug meta for the ?debug history read — role, approx tokens,
    and (for tool outputs) content_type/path + whether the lifecycle middleware
    would degrade it (the `context_editing` stamp, if present)."""
    role = {"HumanMessage": "user", "AIMessage": "assistant",
            "ToolMessage": "tool", "SystemMessage": "system"}.get(
        type(m).__name__, "unknown")
    meta: dict = {"role": role, "approx_tokens": count_tokens_approximately([m])}
    artifact = getattr(m, "artifact", None)
    if isinstance(artifact, dict):
        ameta = artifact.get("meta") if isinstance(artifact.get("meta"), dict) else {}
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        art = artifact.get("artifact") if isinstance(artifact.get("artifact"), dict) else {}
        target = art.get("target") if isinstance(art.get("target"), dict) else {}
        if isinstance(ameta.get("content_type"), str):
            meta["content_type"] = ameta["content_type"]
        path = payload.get("ref") or target.get("path")
        if isinstance(path, str) and path:
            meta["path"] = path
    content = getattr(m, "content", "")
    if isinstance(content, str):
        env = parse_envelope(content)
        if env:
            meta.setdefault("content_type", output_content_type(env))
            meta.setdefault("path", output_path(env))
    ce = (getattr(m, "response_metadata", {}) or {}).get("context_editing", {})
    if ce:
        meta["frozen"] = bool(ce.get("cleared"))
        if ce.get("form"):
            meta["aged_form"] = ce["form"]
    return meta


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("Last-Event-ID", "0")
    try:
        return max(0, int(raw or "0"))
    except ValueError:
        return 0


def _chat_stream_guard(
    *,
    request: Request,
    auth: AuthContext,
    resource_type: ResourceType,
    resource_id: str,
    action: Action,
) -> Callable[[], Awaitable[bool]]:
    openfga_client = getattr(request.app.state, "openfga_client", None)
    resource = ResourceRef(
        resource_type,
        resource_id,
        auth.active_organization_id,
    )

    async def guard() -> bool:
        return await authorization_lease_is_valid(
            auth=auth,
            openfga_client=openfga_client,
            resource=resource,
            action=action,
        )

    return guard


async def _sse_from_turn(
    turn_id: str,
    *,
    after_seq: int = 0,
    authorization_guard: Callable[[], Awaitable[bool]] | None = None,
):
    """Subscribe to the per-turn buffer and yield SSE-encoded bytes.

    This inlines what the deleted stream bridge used to provide: tap the
    buffer's replay+live subscription and translate ``(event_name,
    payload)`` tuples to wire-format SSE chunks. ``run_turn`` is still
    the producer that fences the stream with ``started`` / ``done`` /
    ``error`` — the route is just the consumer.
    """
    buf = TURN_BUFFERS.get(turn_id)
    if buf is None:
        return
    next_authorization_check = 0.0
    async for seq, event in buf.subscribe_with_ids(15.0, after_seq=after_seq):
        now = asyncio.get_running_loop().time()
        if (
            authorization_guard is not None
            and now >= next_authorization_check
        ):
            if not await authorization_guard():
                return
            next_authorization_check = now + 5.0
        event_name, payload = event
        yield format_event(event_name, payload, event_id=seq)


def _session_to_list_item(
    session: dict,
    scope_id: str,
    decision=None,
) -> ChatListItem:
    can_view_content = bool(
        decision is not None and decision_allows_content(decision)
    )
    return ChatListItem(
        chat_id=session["chat_id"],
        scope_id=scope_id,
        surface=session.get("surface", "chat"),
        chat_context=(
            session.get("chat_context", "") if can_view_content else ""
        ),
        created_at=str(session.get("created_at", "")),
        browser_control_status=session.get("browser_control_status", "inactive"),
        runtime_type=session.get("runtime_type") if can_view_content else None,
        access=access_from_decision(decision) if decision else None,
    )


@router.get(
    "/chats/{chat_id}/runtime",
    response_model=ChatRuntimeBindingOut,
    dependencies=[Depends(current_user)],
)
async def get_chat_runtime_binding(
    chat_id: str,
    request: Request,
    runtime_repo=Depends(get_agent_runtime_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> ChatRuntimeBindingOut:
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.VIEW,
    )
    binding = await runtime_repo.get_chat_binding(chat_id)
    if binding is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    return ChatRuntimeBindingOut(**binding)


@router.get(
    "/chats/inventory",
    response_model=Page[ChatInventoryItem],
    dependencies=[Depends(current_user)],
)
async def list_chat_inventory(
    request: Request,
    page: PageRequest = Depends(PageRequest.as_query),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> Page[ChatInventoryItem]:
    """List content-free Chat inventory across the active organization."""
    principal = principal_for_auth(auth)
    context = context_for_auth(auth, request)
    authorized_ids = await service.list_authorized_ids(
        principal,
        Action.VIEW_METADATA,
        ResourceType.CHAT,
        context,
    )
    rows = await chat_repo.list_authorized_inventory(authorized_ids)
    total = len(rows)
    rows = rows[page.offset:page.offset + page.limit]
    resources = [_chat_resource(auth, row["chat_id"]) for row in rows]
    decisions = await batch_resource_decisions(
        service,
        principal=principal,
        resources=resources,
        context=context,
    )
    return Page[ChatInventoryItem](
        items=[
            ChatInventoryItem(
                chat_id=row["chat_id"],
                scope_id=row["scope_id"],
                surface=row["surface"],
                runtime_type=row["runtime_type"],
                browser_control_status=row["browser_control_status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                last_message_at=row["last_message_at"],
                access=access_from_decision(decisions[resource]),
            )
            for row, resource in zip(rows, resources, strict=True)
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/chat-scopes/{scope_id}/chats", response_model=Page[ChatListItem],
            dependencies=[Depends(current_user)])
async def list_chats(
    scope_id: str,
    request: Request,
    page: PageRequest = Depends(PageRequest.as_query),
    surface: str | None = Query(default=None),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    workflow_repo: WorkflowRepo = Depends(get_workflow_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat_carrier(
        request=request,
        auth=auth,
        service=service,
        workflow_repo=workflow_repo,
        scope_id=scope_id,
        action=Action.VIEW,
    )
    chat_surface = surface if surface in {"chat", "browser"} else None
    expired = await chat_repo.expire_stale_browser_lost_sessions(
        scope_id=scope_id,
        surface=chat_surface,
        grace_seconds=app_config.browser_lost_grace_seconds,
    )
    if expired:
        await chat_repo.commit()
    context = context_for_auth(auth, request)
    authorized_ids = await service.list_authorized_ids(
        principal_for_auth(auth),
        Action.VIEW_METADATA,
        ResourceType.CHAT,
        context,
    )
    chats = await chat_repo.list_authorized_sessions(
        scope_id,
        authorized_ids,
        surface=chat_surface,
    )
    sliced = chats[page.offset:page.offset + page.limit]
    resources = [
        _chat_resource(auth, item["chat_id"]) for item in sliced
    ]
    decisions = await batch_resource_decisions(
        service,
        principal=principal_for_auth(auth),
        resources=resources,
        context=context,
    )
    return Page[ChatListItem](
        items=[
            _session_to_list_item(item, scope_id, decisions[resource])
            for item, resource in zip(sliced, resources, strict=True)
        ],
        total=len(chats), limit=page.limit, offset=page.offset,
    )


@router.get(
    "/chats/{chat_id}/browser-binding",
    response_model=BrowserBindingOut,
    dependencies=[Depends(current_user)],
)
async def get_browser_binding(
    chat_id: str,
    request: Request,
    chat_repo: ChatRepo = Depends(get_chat_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.BROWSER_BINDING,
        resource_id=chat_id,
        action=Action.VIEW,
        not_found_detail="browser_binding_not_found",
    )
    binding = await chat_repo.expire_stale_browser_lost_session(
        chat_id,
        grace_seconds=app_config.browser_lost_grace_seconds,
    )
    if binding is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    await chat_repo.commit()
    return BrowserBindingOut(**binding)


def _hitl_out(row, *, decision_applied: bool | None = None) -> HitlRequestOut:
    ui_payload = row.ui_payload_json or {}
    projection_event = ui_payload.get("projection_event") if isinstance(ui_payload, dict) else None
    return HitlRequestOut(
        hitl_request_id=row.hitl_request_id,
        chat_id=row.chat_id,
        run_id=row.run_id,
        artifact_id=row.artifact_id,
        hitl_type=row.hitl_type,
        status=row.status,
        title=row.title,
        prompt_text=row.prompt_text,
        ui_payload_json=ui_payload,
        ui_projection_event_json=projection_event if isinstance(projection_event, dict) else {},
        decision_payload_json=row.decision_payload_json or {},
        interaction_result_json=row.interaction_result_json or {},
        is_interacted=bool(row.is_interacted),
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        decision_applied=decision_applied,
    )


def _interactive_artifact_out(row, *, hitl_status: str | None = None) -> dict:
    definition = dict(row.definition_json or {})
    widget_state = dict(row.widget_state_json or {})
    interaction_result = dict(row.interaction_result_json or {})
    definition["artifact_id"] = row.artifact_id
    definition.setdefault("kind", "interactive_artifact")
    definition.setdefault("title", row.title)
    definition.setdefault("component_type", row.component_type)
    definition.setdefault("completion_mode", row.completion_mode)
    definition["widget_state"] = widget_state
    definition["hitl_request_id"] = row.hitl_request_id
    definition["interaction_state"] = {
        "is_interacted": bool(row.is_interacted),
        "status": hitl_status or (
            "submitted"
            if row.is_interacted
            else ("pending" if row.hitl_request_id else "none")
        ),
        "result": interaction_result,
    }
    return {
        "artifact_id": row.artifact_id,
        "chat_id": row.chat_id,
        "run_id": row.run_id,
        "hitl_request_id": row.hitl_request_id,
        "artifact": definition,
        "artifact_ref": row.artifact_ref,
        "content_hash": row.content_hash,
        "is_interacted": bool(row.is_interacted),
        "interaction_result_json": interaction_result,
    }


def _hitl_history_projection(artifact_row, hitl_row) -> tuple[str, HistoryMessage] | None:
    """Build the durable tool-result projection for one HITL request.

    Runtime streams carry the initial projection so the card appears
    immediately.  History reads must reconstruct the same projection from the
    authoritative HITL/artifact rows; otherwise a refresh loses the card or
    revives a completed interaction as pending.
    """
    if hitl_row is None:
        return None
    ui_payload = (
        hitl_row.ui_payload_json
        if isinstance(hitl_row.ui_payload_json, dict)
        else {}
    )
    projection = ui_payload.get("projection_event")
    if not isinstance(projection, dict):
        return None
    tool_call_id = projection.get("tool_call_id")
    projected_artifact = projection.get("artifact")
    if not tool_call_id or not isinstance(projected_artifact, dict):
        return None

    envelope = deepcopy(projected_artifact)
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        envelope["payload"] = payload
    meta = envelope.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        envelope["meta"] = meta

    status = str(hitl_row.status or "pending")
    pending = status == "pending"
    is_pre_tool_approval = hitl_row.hitl_type == "pre_tool_approval"
    artifact_projection = _interactive_artifact_out(
        artifact_row,
        hitl_status=status,
    )
    payload["artifact"] = artifact_projection["artifact"]
    payload["hitl_request_id"] = hitl_row.hitl_request_id
    payload["hitl_type"] = hitl_row.hitl_type
    meta["hitl_type"] = hitl_row.hitl_type
    if is_pre_tool_approval:
        payload["pending_approval"] = pending
        meta["pending_approval"] = pending
    else:
        # ``pending_approval`` means permission to execute a tool, not the
        # independent post-tool Continue gate. Reusing it for both makes an
        # HTML review restore as an "Approve Unknown tool" authorization card.
        payload.pop("pending_approval", None)
        meta.pop("pending_approval", None)

    created_at = getattr(artifact_row, "created_at", None)
    ts = created_at.timestamp() if created_at is not None else None
    return str(tool_call_id), HistoryMessage(
        id=f"hitl:{hitl_row.hitl_request_id}:projection",
        role="tool",
        content=str(envelope.get("content") or ""),
        ts=ts,
        tool_call_id=str(tool_call_id),
        artifact=envelope,
    )


def _merge_hitl_history_projections(
    messages: list[HistoryMessage],
    projections: list[tuple[str, HistoryMessage]],
) -> list[HistoryMessage]:
    """Merge durable HITL cards into the ordinary persisted transcript."""
    stored_tool_call_ids = {
        message.tool_call_id
        for message in messages
        if message.role == "tool" and message.tool_call_id
    }
    by_tool_call: dict[str, list[HistoryMessage]] = {}
    for tool_call_id, projection in projections:
        by_tool_call.setdefault(tool_call_id, []).append(projection)

    projected_history: list[HistoryMessage] = []
    for original in messages:
        message = original
        if message.role == "tool" and message.tool_call_id:
            matching = by_tool_call.pop(message.tool_call_id, [])
            if matching:
                # A completed call already has its ordinary tool-result row.
                # Overlay the durable HITL projection onto that row instead of
                # emitting a second result with the same tool_call_id (the
                # frontend reducer correctly treats the latter as a replacement).
                message = message.model_copy(update={"artifact": matching[-1].artifact})
        projected_history.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if not isinstance(call, dict):
                continue
            tool_call_id = call.get("id") or call.get("tool_call_id")
            if tool_call_id and str(tool_call_id) not in stored_tool_call_ids:
                projected_history.extend(
                    by_tool_call.pop(str(tool_call_id), [])
                )
    return projected_history


@router.get(
    "/chats/{chat_id}/hitl-requests",
    response_model=list[HitlRequestOut],
    dependencies=[Depends(current_user)],
)
async def list_chat_hitl_requests(
    chat_id: str,
    request: Request,
    status: str = Query(default="pending"),
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.VIEW,
    )
    if status != "pending":
        raise HTTPException(status_code=400, detail="only pending status is supported in V1")
    rows = await hitl_repo.list_pending_for_chat_user(chat_id, auth.user_id)
    return [_hitl_out(row) for row in rows]


@router.get(
    "/hitl-requests/{hitl_request_id}",
    response_model=HitlRequestOut,
    dependencies=[Depends(current_user)],
)
async def get_hitl_request(
    hitl_request_id: str,
    request: Request,
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.HITL_REQUEST,
        resource_id=hitl_request_id,
        action=Action.VIEW,
        not_found_detail="hitl_request_not_found",
    )
    row = await hitl_repo.get_request_for_user(hitl_request_id, auth.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"hitl request {hitl_request_id} not found")
    return _hitl_out(row)


@router.get(
    "/interactive-artifacts/{artifact_id}",
    dependencies=[Depends(current_user)],
)
async def get_interactive_artifact(
    artifact_id: str,
    request: Request,
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.INTERACTIVE_ARTIFACT,
        resource_id=artifact_id,
        action=Action.VIEW,
        not_found_detail="interactive_artifact_not_found",
    )
    row = await hitl_repo.get_artifact_for_user(artifact_id, auth.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"interactive artifact {artifact_id} not found")
    hitl = await hitl_repo.get_request_by_artifact(artifact_id)
    return _interactive_artifact_out(row, hitl_status=getattr(hitl, "status", None))


@router.post(
    "/interactive-artifacts/{artifact_id}/resource-session",
    dependencies=[Depends(current_user)],
)
async def create_interactive_resource_session(
    artifact_id: str,
    request: Request,
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Mint an ephemeral read-only resource gateway from durable Artifact state.

    The client intentionally sends only ``artifact_id``. User/chat/storage scope
    are resolved here so neither Agent HTML nor the frontend projection owns
    backend storage identifiers.
    """
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.INTERACTIVE_ARTIFACT,
        resource_id=artifact_id,
        action=Action.VIEW,
        not_found_detail="interactive_artifact_not_found",
    )
    row = await hitl_repo.get_artifact_for_user(artifact_id, auth.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"interactive artifact {artifact_id} not found")
    cfg = app_config.agent.compaction_v2
    ttl = max(30, int(cfg.interactive_artifact_resource_ttl_s))
    workspace_scope = _chat_workspace_scope_id(row.chat_id)
    definition = dict(row.definition_json or {})
    props = definition.get("props")
    props = props if isinstance(props, dict) else {}
    rules: set[str] = set()
    interaction_result = row.interaction_result_json
    interaction_result = (
        interaction_result if isinstance(interaction_result, dict) else {}
    )
    saved_files = interaction_result.get("saved_files")
    if isinstance(saved_files, list):
        for saved_file in saved_files:
            saved_path = saved_file.get("path") if isinstance(saved_file, dict) else None
            if (
                isinstance(saved_path, str)
                and saved_path.startswith("/data/")
                and "\x00" not in saved_path
                and "\\" not in saved_path
                and not any(segment in {".", ".."} for segment in saved_path.split("/"))
            ):
                rules.add(saved_path)
    if row.component_type == "html_preview":
        html = props.get("html") or props.get("srcdoc")
        if isinstance(html, str):
            rules.update(html_vfs_read_rules(html))
    elif row.component_type == "file_preview":
        path = props.get("path")
        if (
            isinstance(path, str)
            and path.startswith(("/data/", "/mount/"))
            and "\x00" not in path
            and "\\" not in path
            and not any(segment in {".", ".."} for segment in path.split("/"))
        ):
            rules.add(path)
            mime = str(props.get("mime") or props.get("content_type") or "").lower()
            if mime in {"text/html", "application/xhtml+xml"} or path.lower().endswith(
                (".html", ".htm")
            ):
                source_scope = (
                    _mount_scope_id(auth.user_id)
                    if path.startswith("/mount/")
                    else workspace_scope
                )
                source = await VfsRepo(
                    hitl_repo.session,
                    object_store=get_object_store(),
                ).read_bytes(wf_id=source_scope, path=path)
                if source is not None and len(source) <= 2 * 1024 * 1024:
                    rules.update(html_vfs_read_rules(source.decode("utf-8", "replace")))

    sorted_rules = tuple(sorted(rules))
    workspace_rules = rules_for_root(sorted_rules, "data")
    mount_rules = rules_for_root(sorted_rules, "mount")
    # A non-authorizing sentinel preserves an ordinary base URL for HTML with
    # no local dependencies without granting the rest of the Chat root.
    capability = issue_vfs_resource_capability(
        tenant_id=auth.tenant_id,
        audience="interactive-artifact",
        allowed_paths=workspace_rules or ("/data/__no_resource__",),
        wf_id=workspace_scope,
        expires_in_s=ttl,
    )
    root_url = (
        f"/api/v1/vfs/resources/interactive-artifact/"
        f"{quote(capability, safe='-_')}/"
    )
    # Files in the user mount scope retain their Agent-visible ``/mount/...``
    # VFS path.  The frontend strips the matched virtual prefix before joining
    # it to ``root_url``, so keep the storage prefix in the root itself:
    #
    #   /mount/data/items.jsonl
    #     -> <mount capability>/mount/data/items.jsonl
    #     -> VFS lookup /mount/data/items.jsonl
    #
    # Without this segment the gateway incorrectly looks up ``/data/...`` in
    # the mount scope and reports 404 even though the file exists.
    mounts = [{"path_prefix": "/", "root_url": root_url}]
    if mount_rules:
        mount_capability = issue_vfs_resource_capability(
            tenant_id=auth.tenant_id,
            audience="interactive-artifact",
            allowed_paths=mount_rules,
            wf_id=_mount_scope_id(auth.user_id),
            expires_in_s=ttl,
        )
        mount_root_url = (
            f"/api/v1/vfs/resources/interactive-artifact/"
            f"{quote(mount_capability, safe='-_')}/mount/"
        )
        mounts.insert(0, {"path_prefix": "/mount/", "root_url": mount_root_url})
    return {
        "artifact_id": artifact_id,
        # Ordered virtual-path mounts are the complete frontend/backend
        # contract. The renderer does not know concrete VFS root names; a new
        # workspace path becomes renderable without changing frontend code.
        # Longest-prefix matching routes user-level /mount to its own scope,
        # while the workspace root handles every other safe VFS path.
        "resource_mounts": mounts,
        # Relative paths are an implementation convenience only; the Agent is
        # encouraged to use the normal absolute paths it already knows.
        "base_url": root_url + "data/",
        "expires_in": ttl,
        "draft_debounce_ms": max(100, int(cfg.interactive_artifact_draft_debounce_ms)),
    }


@router.put(
    "/interactive-artifacts/{artifact_id}/state",
    dependencies=[Depends(current_user)],
)
async def save_interactive_artifact_state(
    artifact_id: str,
    body: InteractiveArtifactStateBody,
    request: Request,
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Persist a debounced standard-form draft without resolving HITL."""
    cfg = app_config.agent.compaction_v2
    serialized = json.dumps(body.state, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > int(cfg.interactive_artifact_state_max_chars):
        raise HTTPException(status_code=413, detail="interactive_state_too_large")
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.INTERACTIVE_ARTIFACT,
        resource_id=artifact_id,
        action=Action.UPDATE,
        not_found_detail="interactive_artifact_not_found",
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    row, updated = await hitl_repo.update_artifact_state_for_user(
        artifact_id=artifact_id,
        user_id=auth.user_id,
        state=body.state,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"interactive artifact {artifact_id} not found")
    await hitl_repo.commit()
    return {
        "artifact_id": artifact_id,
        "status": "saved" if updated else "frozen",
        "widget_state": dict(row.widget_state_json or {}),
        "is_interacted": bool(row.is_interacted),
    }


@router.put(
    "/interactive-artifacts/{artifact_id}/result-file",
    dependencies=[Depends(current_user)],
)
async def save_interactive_artifact_result_file(
    artifact_id: str,
    body: InteractiveArtifactResultFileBody,
    request: Request,
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Save a form result in the Artifact's Chat VFS before HITL resolution."""
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.INTERACTIVE_ARTIFACT,
        resource_id=artifact_id,
        action=Action.UPDATE,
        not_found_detail="interactive_artifact_not_found",
    )
    initial = await hitl_repo.get_artifact_for_user(artifact_id, auth.user_id)
    if initial is None:
        raise HTTPException(status_code=404, detail=f"interactive artifact {artifact_id} not found")
    # Serialize Save with the atomic Continue transaction. Whichever operation
    # obtains the Chat lock first wins: a completed Save is visible to the new
    # Human Turn, while a post-Continue Save sees the frozen artifact.
    await hitl_repo.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"agent-run:{initial.chat_id}"},
    )
    row = await hitl_repo.lock_artifact_for_user(artifact_id, auth.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"interactive artifact {artifact_id} not found")
    if row.is_interacted:
        raise HTTPException(status_code=409, detail="interactive_artifact_frozen")
    cfg = app_config.agent.compaction_v2
    requested = body.path or f"{cfg.interactive_artifact_result_dir}/{artifact_id}/result.json"
    try:
        path = _validate_artifact_path(requested)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_result_path") from exc
    if not path.startswith("/data/"):
        raise HTTPException(status_code=400, detail="result_path_must_be_under_data")
    data = body.content.encode("utf-8")
    if len(data) > app_config.storage.vfs_upload_max_bytes:
        raise HTTPException(status_code=413, detail="interactive_result_too_large")
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.INTERACTIVE_ARTIFACT,
        resource_id=artifact_id,
        action=Action.UPDATE,
        not_found_detail="interactive_artifact_not_found",
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    workspace_scope = _chat_workspace_scope_id(row.chat_id)
    repo = VfsRepo(hitl_repo.session, object_store=get_object_store())
    replaced = await repo.upsert_artifact_bytes(
        wf_id=workspace_scope,
        tenant=auth.tenant_id,
        path=path,
        data=data,
        content_type=body.content_type,
    )
    result_file = {
        "result_path": path,
        "path": path,
        "content_type": body.content_type,
        "size_bytes": len(data),
        "hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
        # V1 uses the immutable content hash as its file-result revision.
        "revision": f"sha256:{hashlib.sha256(data).hexdigest()}",
    }
    await hitl_repo.record_artifact_result_file(
        artifact=row,
        result_file=result_file,
    )
    await hitl_repo.commit()
    # A warm resident sandbox otherwise keeps its materialized pre-Save view
    # of /data. Project the durable write into that live workspace so the new
    # Continue Turn can read exactly what the user just saved.
    try:
        await get_sandbox_manager().mirror_vfs_write(
            auth.tenant_id,
            workspace_scope,
            path,
            data,
        )
    except Exception:  # pragma: no cover - durable VFS remains authoritative
        logger.warning(
            "interactive_result_live_mirror_failed",
            chat_id=row.chat_id,
            artifact_id=artifact_id,
            path=path,
        )
    return {
        "artifact_id": artifact_id,
        **result_file,
        "replaced": replaced,
    }


@router.post(
    "/hitl-requests/{hitl_request_id}/decision",
    response_model=HitlRequestOut,
    dependencies=[Depends(current_user)],
)
async def decide_hitl_request(
    hitl_request_id: str,
    body: HitlDecisionBody,
    request: Request,
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.HITL_REQUEST,
        resource_id=hitl_request_id,
        action=Action.RESUME,
        not_found_detail="hitl_request_not_found",
    )
    before = await hitl_repo.get_request_for_user(hitl_request_id, auth.user_id)
    if before is None:
        raise HTTPException(status_code=404, detail=f"hitl request {hitl_request_id} not found")
    effective_decision = body.decision
    if before.hitl_type == "pre_tool_approval":
        if body.decision in {"submit", "submitted"}:
            effective_decision = "approve"
        elif body.decision in {"cancel", "cancelled"}:
            effective_decision = "deny"
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.HITL_REQUEST,
        resource_id=hitl_request_id,
        action=Action.RESUME,
        not_found_detail="hitl_request_not_found",
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    row, decision_applied = await hitl_repo.resolve(
        hitl_request_id=hitl_request_id,
        decision=effective_decision,
        decision_payload=body.decision_payload,
        interaction_result=body.interaction_result,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"hitl request {hitl_request_id} not found")
    await hitl_repo.commit()
    # The request only commits the user's decision. The owning outer Runtime
    # loop observes this row and resumes the suspended SDK request. This keeps
    # HTTP/frontends independent from SDK-native control identifiers and works
    # even when the decision reaches a different API worker.
    return _hitl_out(row, decision_applied=decision_applied)


@router.get("/chat-scopes/{scope_id}/chats/{chat_id}/messages",
            response_model=Page[HistoryMessage],
            dependencies=[Depends(current_user)])
async def get_chat_history(
    scope_id: str, chat_id: str,
    request: Request,
    page: PageRequest = Depends(PageRequest.as_query),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    debug: bool = Query(default=False),
    tail: bool = Query(default=False),
    before_turn_id: str | None = Query(default=None),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.VIEW,
    )
    def _sanitize_visible_content(content: str) -> str:
        text = content or ""
        while True:
            start = text.find("<think_never_used_")
            if start < 0:
                break
            end = text.find("</think_never_used_", start)
            if end < 0:
                break
            close = text.find(">", end)
            if close < 0:
                break
            text = text[:start] + text[close + 1:]
        lines = [
            line
            for line in text.splitlines()
            if "<think_never_used_" not in line and "</think_never_used_" not in line
        ]
        return "\n".join(lines)

    started_at = perf_counter()
    selected = await chat_repo.get_authorized_inventory(chat_id)
    if selected is None or selected["scope_id"] != scope_id:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    visible, stored_total, stored_offset = await chat_repo.list_message_page(
        chat_id,
        limit=page.limit,
        offset=page.offset,
        tail=tail,
        before_turn_id=before_turn_id,
    )
    hitl_projections: list[tuple[str, HistoryMessage]] = []
    for artifact_row, hitl_row in await hitl_repo.list_artifact_refs_for_chat(chat_id):
        projected = _hitl_history_projection(artifact_row, hitl_row)
        if projected is not None:
            hitl_projections.append(projected)

    stored_history: list[HistoryMessage] = []
    for item in visible:
        stored = item.get("content") if isinstance(item.get("content"), dict) else {}
        if (
            stored.get("message_type") == "control"
            or stored.get("visibility") == "hidden"
        ):
            # Control turns remain durable product facts and Runtime input, but
            # never become ordinary user bubbles in the frontend transcript.
            continue
        content = str(stored.get("text") or "")
        if item.get("role") == "assistant":
            content = _sanitize_visible_content(content)
        message = HistoryMessage(
            id=str(item.get("message_id") or item.get("id") or ""),
            role=item.get("role", "assistant"),
            content=content,
            attachments=(
                stored.get("attachments")
                if isinstance(stored.get("attachments"), list)
                else []
            ),
            ts=item.get("ts"),
            tool_calls=(
                stored.get("tool_calls")
                if isinstance(stored.get("tool_calls"), list)
                else None
            ),
            tool_call_id=(
                str(stored.get("tool_call_id"))
                if stored.get("tool_call_id")
                else None
            ),
            artifact=(
                stored.get("artifact")
                if isinstance(stored.get("artifact"), dict)
                else None
            ),
            invocation=(
                stored.get("invocation")
                if isinstance(stored.get("invocation"), dict)
                else None
            ),
            activity=(
                stored.get("activity")
                if isinstance(stored.get("activity"), dict)
                else None
            ),
            meta=(
                {
                    **(
                        item.get("meta")
                        if isinstance(item.get("meta"), dict)
                        else {}
                    ),
                    # Debug consumers need a durable boundary for projecting
                    # messages emitted after a model-input snapshot. Keep this
                    # out of the ordinary transcript response so it remains an
                    # observability detail rather than frontend message state.
                    "turn_id": item.get("turn_id"),
                }
                if debug
                else None
            ),
        )
        stored_history.append(message)

    projected_history = _merge_hitl_history_projections(
        stored_history,
        hitl_projections,
    )
    # HITL projections normally replace an existing tool result. A still-open
    # interaction can add one synthetic row to this window; account for that
    # row without forcing the endpoint to decrypt the entire transcript first.
    total = stored_total + max(0, len(projected_history) - len(stored_history))
    offset = stored_offset
    msgs = projected_history
    elapsed_ms = int((perf_counter() - started_at) * 1000)
    if elapsed_ms >= 250:
        logger.info(
            "chat_history_page_loaded",
            chat_id=chat_id,
            scope_id=scope_id,
            visible_count=total,
            offset=offset,
            limit=page.limit,
            returned=len(msgs),
            tail=tail,
            elapsed_ms=elapsed_ms,
        )
    return Page[HistoryMessage](
        items=msgs, total=total, limit=page.limit, offset=offset,
    )


@router.get("/chat-scopes/{scope_id}/chats/{chat_id}/state",
            response_model=ChatStateOut,
            dependencies=[Depends(current_user)])
async def get_chat_state(
    scope_id: str,
    chat_id: str,
    request: Request,
    chat_repo: ChatRepo = Depends(get_chat_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.VIEW,
    )
    sessions = await chat_repo.list_sessions(scope_id)
    if not any(item["chat_id"] == chat_id for item in sessions):
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    raw = await chat_repo.get_todo_items(chat_id)
    items = []
    for item in raw:
        if not isinstance(item, dict) or item.get("status") not in {"pending", "in_progress", "done"}:
            continue
        try:
            item_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue
        text = item.get("text")
        items.append({
            "id": item_id,
            "text": text if isinstance(text, str) else str(text or ""),
            "status": item["status"],
        })
    selection = await chat_repo.get_mcp_selection(chat_id)
    background_repo = BackgroundJobsRepo(session)
    await background_repo.reconcile_stale_for_chat(chat_id=chat_id)
    background_jobs = await background_repo.list_for_user(
        chat_id=chat_id,
        creator_user_id=auth.user_id,
        statuses=ACTIVE_BACKGROUND_JOB_STATUSES,
    )
    return ChatStateOut(
        todo_items=items,
        background_jobs=[
            project_background_job(job) for job in background_jobs
        ],
        active_modes=sorted(await chat_repo.get_active_modes(chat_id)),
        mcp_server_ids=(selection or {}).get("mcp_server_ids", []),
        mcp_config_revision=(selection or {}).get("mcp_config_revision", 0),
    )


@router.delete(
    "/chat-scopes/{scope_id}/chats/{chat_id}/commands/{command}",
    dependencies=[Depends(current_user)],
)
async def deactivate_chat_command(
    scope_id: str,
    chat_id: str,
    command: str,
    request: Request,
    chat_repo: ChatRepo = Depends(get_chat_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> dict:
    """Deactivate one sticky command for subsequent Runtime turns."""
    from ..agents.commands import COMMAND_MODES

    if command not in COMMAND_MODES:
        raise HTTPException(status_code=404, detail="command_not_found")
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.EXECUTE,
    )
    sessions = await chat_repo.list_sessions(scope_id)
    if not any(item["chat_id"] == chat_id for item in sessions):
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    active_modes = await chat_repo.deactivate_active_mode(
        chat_id,
        command,
        actor_user_id=auth.user_id,
    )
    if active_modes is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    return {"active_modes": sorted(active_modes)}


@router.get(
    "/chat-scopes/{scope_id}/chats/{chat_id}/background-jobs",
    response_model=list[BackgroundJobOut],
    dependencies=[Depends(current_user)],
)
async def list_chat_background_jobs(
    scope_id: str,
    chat_id: str,
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.VIEW,
    )
    sessions = await chat_repo.list_sessions(scope_id)
    if not any(item["chat_id"] == chat_id for item in sessions):
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    statuses = None
    if status and status != "all":
        allowed = {
            "queued",
            "running",
            "cancelling",
            "completed",
            "failed",
            "cancelled",
        }
        requested = tuple(
            item.strip() for item in status.split(",") if item.strip()
        )
        if not requested or any(item not in allowed for item in requested):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_background_job_status"},
            )
        statuses = requested
    background_repo = BackgroundJobsRepo(session)
    await background_repo.reconcile_stale_for_chat(chat_id=chat_id)
    rows = await background_repo.list_for_user(
        chat_id=chat_id,
        creator_user_id=auth.user_id,
        statuses=statuses,
        limit=limit,
    )
    return [BackgroundJobOut.model_validate(project_background_job(row)) for row in rows]


@router.get(
    "/chat-scopes/{scope_id}/chats/{chat_id}/background-jobs/events",
    dependencies=[Depends(current_user)],
)
async def stream_chat_background_job_events(
    scope_id: str,
    chat_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Replay then follow the Chat-wide durable background event sequence."""

    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.VIEW,
    )
    sessions = await chat_repo.list_sessions(scope_id)
    if not any(item["chat_id"] == chat_id for item in sessions):
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    header_cursor = request.headers.get("last-event-id", "")
    try:
        cursor = max(after, int(header_cursor or 0))
    except ValueError:
        cursor = after
    await chat_repo.commit()
    authorization_guard = _chat_stream_guard(
        request=request,
        auth=auth,
        resource_type=ResourceType.CHAT,
        resource_id=chat_id,
        action=Action.VIEW,
    )

    async def event_stream():
        nonlocal cursor
        idle_ticks = 0
        next_authorization_check = 0.0
        while not await request.is_disconnected():
            now = asyncio.get_running_loop().time()
            if now >= next_authorization_check:
                if not await authorization_guard():
                    logger.info(
                        "background_job_sse_authorization_lease_closed",
                        chat_id=chat_id,
                    )
                    return
                next_authorization_check = now + 5.0
            async with session_scope(tenant_id=auth.tenant_id) as event_session:
                events = await BackgroundJobsRepo(
                    event_session
                ).list_chat_events_for_user(
                    chat_id=chat_id,
                    creator_user_id=auth.user_id,
                    after_event_id=cursor,
                )
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = int(event.event_id)
                    yield format_event(
                        "background_job",
                        {
                            "event_id": cursor,
                            "job_id": event.job_id,
                            "seq": int(event.seq),
                            "event_type": event.event_type,
                            "payload": dict(event.payload or {}),
                            "created_at": (
                                event.created_at.isoformat()
                                if event.created_at
                                else None
                            ),
                        },
                        event_id=cursor,
                    )
                continue
            idle_ticks += 1
            if idle_ticks >= 15:
                idle_ticks = 0
                yield b": heartbeat\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/chat-scopes/{scope_id}/chats/{chat_id}/background-jobs/{job_id}",
    response_model=BackgroundJobOut,
    dependencies=[Depends(current_user)],
)
async def get_chat_background_job(
    scope_id: str,
    chat_id: str,
    job_id: str,
    request: Request,
    chat_repo: ChatRepo = Depends(get_chat_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.BACKGROUND_JOB,
        resource_id=job_id,
        action=Action.VIEW,
        not_found_detail="background_job_not_found",
    )
    sessions = await chat_repo.list_sessions(scope_id)
    if not any(item["chat_id"] == chat_id for item in sessions):
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    background_repo = BackgroundJobsRepo(session)
    await background_repo.reconcile_stale_for_chat(chat_id=chat_id)
    row = await background_repo.get_for_user(
        chat_id=chat_id,
        job_id=job_id,
        creator_user_id=auth.user_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="background job not found")
    return BackgroundJobOut.model_validate(project_background_job(row))


@router.post(
    "/chat-scopes/{scope_id}/chats/{chat_id}/background-jobs/{job_id}/cancel",
    response_model=BackgroundJobOut,
    dependencies=[Depends(current_user)],
)
async def cancel_chat_background_job(
    scope_id: str,
    chat_id: str,
    job_id: str,
    body: BackgroundJobCancelBody,
    request: Request,
    chat_repo: ChatRepo = Depends(get_chat_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.BACKGROUND_JOB,
        resource_id=job_id,
        action=Action.CANCEL,
        not_found_detail="background_job_not_found",
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    sessions = await chat_repo.list_sessions(scope_id)
    if not any(item["chat_id"] == chat_id for item in sessions):
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    row = await BackgroundJobsRepo(session).request_cancel(
        chat_id=chat_id,
        job_id=job_id,
        creator_user_id=auth.user_id,
        reason=body.reason or "user_requested",
    )
    if row is None:
        raise HTTPException(status_code=404, detail="background job not found")
    return BackgroundJobOut.model_validate(project_background_job(row))


@router.post("/chat-scopes/{scope_id}/chats/{chat_id}/messages",
             dependencies=[Depends(current_user)])
async def post_message(
    scope_id: str, chat_id: str, body: MessagePostBody,
    http_request: Request,
    wf_repo: WorkflowRepo = Depends(get_workflow_repo),
    chat_repo: ChatRepo = Depends(get_chat_repo),
    hitl_repo=Depends(get_hitl_repo),
    runtime_repo=Depends(get_agent_runtime_repo),
    agent_runs_repo=Depends(get_agent_runs_repo),
    session: AsyncSession = Depends(tenant_db),
    auth: AuthContext = Depends(current_user),
    authz_service: AuthzService = Depends(get_authz_service),
) -> StreamingResponse:
    """Send a user message; respond with SSE stream of agent events.

    Auto-creates the chat session if it doesn't exist (legacy parity).
    The API owns durable Chat/Run/event state. SDK execution is delegated only
    through ``AgentRuntimeOrchestrator`` to the Chat-bound sandbox Runtime.

    The request-scoped DB session is committed before the detached producer is
    started. The producer uses the private runtime protocol and short durable
    writer sessions; it never retains this request session.
    """
    request_started = perf_counter()
    from ..agents.commands import COMMAND_MODES, parse_command

    await _authorize_chat_carrier(
        request=http_request,
        auth=auth,
        service=authz_service,
        workflow_repo=wf_repo,
        scope_id=scope_id,
        action=Action.USE,
    )
    if body.control is not None:
        await _authorize_chat(
            request=http_request,
            auth=auth,
            service=authz_service,
            chat_id=chat_id,
            action=Action.EXECUTE,
        )
    control_projection: dict | None = None
    control_request = None
    if isinstance(body.control, HitlContinueControl):
        request = await hitl_repo.get_request_for_user(
            body.control.hitl_request_id,
            auth.user_id,
        )
        if request is None:
            raise HTTPException(status_code=404, detail="hitl_request_not_found")
        if request.chat_id != chat_id:
            raise HTTPException(status_code=409, detail="hitl_request_chat_mismatch")
        if request.artifact_id != body.control.artifact_id:
            raise HTTPException(status_code=409, detail="hitl_artifact_mismatch")
        if request.hitl_type == "pre_tool_approval":
            raise HTTPException(
                status_code=409,
                detail="pre_tool_approval_cannot_start_control_turn",
            )
        if request.status not in {"pending", "submitted", "approved"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "hitl_continue_not_ready",
                    "status": request.status,
                },
            )
        # The durable decision and the unique follow-up Turn are claimed in the
        # same transaction below. Do not resolve the request here: a frontend
        # crash between a standalone decision request and Turn creation would
        # otherwise leave a frozen card with no resumable Turn.
        control_request = request
        stripped = ""
        cmd = None
    elif isinstance(body.control, BackgroundResultsControl):
        # Result bytes are resolved from durable rows after the chat's Turn
        # reservation lock is acquired. The control packet only identifies the
        # batch; it cannot inject model-facing text.
        stripped = ""
        cmd = None
    else:
        # `/command` parsing (Design §6 — a tool cannot do this; it lives at the
        # routes layer). Resolve a leading slash command, strip it from
        # the content, and reconcile the persisted active_modes for this chat.
        cmd, stripped = parse_command(body.content)
    agent_surface = body.agent_surface or "chat"
    command_runtime_binding = await runtime_repo.get_chat_binding(chat_id)
    command_runtime_type = (
        command_runtime_binding.get("runtime_type")
        if command_runtime_binding is not None
        else None
    ) or (await runtime_repo.get_preferences())["default_runtime_type"]
    available_commands = _available_commands(
        agent_surface,
        command_runtime_type,
    )

    if cmd is not None and cmd not in available_commands:
        turn_id = new_turn_id()
        buf, stop = register_turn(turn_id)
        notice_payload = {
            "level": "info",
            "code": "command_not_available",
            "message": f"/{cmd} is not available on the {agent_surface} surface.",
            "turn_disposition": "cancel",
        }

        async def notice_producer(stop_ev: asyncio.Event):
            yield ("NOTICE", notice_payload)

        TURN_TASKS[turn_id] = asyncio.create_task(
            run_turn(turn_id, buf, stop, notice_producer)
        )
        return StreamingResponse(
            _sse_from_turn(turn_id), media_type="text/event-stream",
            headers={**SSE_HEADERS, "X-Turn-Id": turn_id},
        )

    # `/browser` is SIDE-PANEL-ONLY: browser control runs in the extension's side
    # panel, not the main app. A main-app `/browser` is REFUSED with a single
    # NOTICE frame (no agent turn, no mode change) telling the user to use the
    # side panel — the frontend shows it as a toast and clears the optimistic
    # turn (no lingering "thinking" dots). This replaces the old cross-app
    # handoff/relay (dropped — conversation sync was too complex).
    _cmd_cfg = COMMAND_MODES.get(cmd) if cmd is not None else None
    if _cmd_cfg is not None and _cmd_cfg.sidepanel_only and body.surface != "sidepanel":
        turn_id = new_turn_id()
        buf, stop = register_turn(turn_id)
        notice_payload = {
            "level": "info",
            "code": "browser_sidepanel_only",
            "message": (
                "Browser mode is available only in the Flowork extension side panel. "
                "Open the side panel, start or resume a conversation there, and use /browser."
            ),
            "turn_disposition": "cancel",
        }

        async def notice_producer(stop_ev: asyncio.Event):
            # One NOTICE frame, then return — run_turn fences it with the frozen
            # started/done envelope. No agent-turn frames are emitted.
            yield ("NOTICE", notice_payload)

        TURN_TASKS[turn_id] = asyncio.create_task(
            run_turn(turn_id, buf, stop, notice_producer)
        )
        return StreamingResponse(
            _sse_from_turn(turn_id), media_type="text/event-stream",
            headers={**SSE_HEADERS, "X-Turn-Id": turn_id},
        )

    sessions = await chat_repo.list_sessions(scope_id)
    is_first = not any(s["chat_id"] == chat_id for s in sessions)
    if body.control is not None and is_first:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    if is_first:
        await authorize_resource(
            request=http_request,
            auth=auth,
            service=authz_service,
            resource=ResourceRef(
                ResourceType.ORGANIZATION,
                auth.active_organization_id,
                auth.active_organization_id,
            ),
            action=Action.CREATE,
        )
        # Synchronous, in-handler, BEFORE the streaming task is created:
        # the per-request DI session safely owns this write. The SSE producer
        # below does NOT touch chat_repo / its session. (Committed just before
        # the turn streams — see the `chat_repo.commit()` below.)
        try:
            await chat_repo.register_session(
                scope_id,
                chat_id,
                chat_context=stripped[:80],
                surface=agent_surface,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404,
                detail="chat_not_found",
            ) from exc
        await _commit_new_chat_projection(
            request=http_request,
            session=session,
            auth=auth,
            chat_id=chat_id,
            operation_id=(
                f"{chat_id}:{body.client_request_id or uuid.uuid4().hex}"
            ),
        )
    elif body.control is None:
        await _authorize_chat(
            request=http_request,
            auth=auth,
            service=authz_service,
            chat_id=chat_id,
            action=Action.EXECUTE,
        )
        await chat_repo.update_session_name_if_default(chat_id, stripped[:80])

    # Serialize the complete Turn reservation transaction before mutating sticky
    # command/MCP Chat state. Without this outer fence, two workers retrying the
    # same client_request_id could both change the Chat selection before the
    # later AgentRun idempotency check noticed that only one Turn should exist.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"agent-run:{chat_id}"},
    )
    effective_client_request_id = (
        f"hitl_continue:{body.control.hitl_request_id}"
        if isinstance(body.control, HitlContinueControl)
        else f"background_results:{body.control.batch_id}"
        if isinstance(body.control, BackgroundResultsControl)
        else body.client_request_id
    )
    if effective_client_request_id:
        existing_request = await agent_runs_repo.get_by_client_request(
            chat_id,
            effective_client_request_id,
            creator_user_id=auth.user_id,
        )
        if existing_request is not None:
            await chat_repo.commit()
            from ..services.agent_run_stream import agent_run_event_stream
            return StreamingResponse(
                agent_run_event_stream(
                    run_id=existing_request.run_id,
                    after_seq=0,
                    tenant_id=auth.tenant_id,
                    authorization_guard=_chat_stream_guard(
                        request=http_request,
                        auth=auth,
                        resource_type=ResourceType.AGENT_RUN,
                        resource_id=existing_request.run_id,
                        action=Action.VIEW,
                    ),
                ),
                media_type="text/event-stream",
                headers={**SSE_HEADERS, "X-Turn-Id": existing_request.run_id},
            )
    active_request = await agent_runs_repo.get_active_for_chat_user(
        chat_id,
        creator_user_id=auth.user_id,
    )
    if active_request is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "chat_run_active", "run_id": active_request.run_id},
        )

    if isinstance(body.control, HitlContinueControl):
        if control_request is None:  # pragma: no cover - guarded above
            raise HTTPException(status_code=404, detail="hitl_request_not_found")
        artifact = await hitl_repo.get_artifact(body.control.artifact_id)
        if artifact is None or artifact.chat_id != chat_id:
            raise HTTPException(status_code=409, detail="hitl_artifact_mismatch")
        if control_request.status == "pending":
            definition = (
                artifact.definition_json
                if isinstance(artifact.definition_json, dict)
                else {}
            )
            interaction_schema = (
                definition.get("interaction_schema")
                if isinstance(definition.get("interaction_schema"), dict)
                else {}
            )
            continue_only = bool(
                definition.get("require_human_confirm")
                or interaction_schema.get("interaction_type") == "continue"
            )
            if not continue_only:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "hitl_continue_requires_submitted_input",
                        "status": control_request.status,
                    },
                )
            decision_payload = {
                "artifact_id": artifact.artifact_id,
                "decision": "continue",
                "widget_state": (
                    artifact.widget_state_json
                    if isinstance(artifact.widget_state_json, dict)
                    else {}
                ),
            }
            interaction_result = (
                artifact.interaction_result_json
                if isinstance(artifact.interaction_result_json, dict)
                and artifact.interaction_result_json
                else decision_payload
            )
            resolved, _ = await hitl_repo.resolve(
                hitl_request_id=control_request.hitl_request_id,
                decision="submit",
                decision_payload=decision_payload,
                interaction_result=interaction_result,
            )
            if resolved is None:  # pragma: no cover - row was validated above
                raise HTTPException(status_code=404, detail="hitl_request_not_found")
            control_request = resolved
        result = (
            control_request.interaction_result_json
            if isinstance(control_request.interaction_result_json, dict)
            else {}
        )
        control_projection = {
            **body.control.model_dump(mode="json"),
            "status": control_request.status,
            "title": control_request.title,
            "interaction_result": result,
        }
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        stripped = (
            "<system-reminder>\n"
            f'<human-control type="hitl_continue" '
            f'hitl_request_id="{control_request.hitl_request_id}" '
            f'artifact_id="{control_request.artifact_id or ""}">\n'
            "The user selected Continue after reviewing the durable interactive "
            "artifact. Continue the task in this new Human Turn using the saved "
            "interaction result below.\n"
            f"<interaction-result>{result_json}</interaction-result>\n"
            "</human-control>\n"
            "</system-reminder>"
        )
    elif isinstance(body.control, BackgroundResultsControl):
        expected_batch_id = "bg_" + hashlib.sha256(
            "\n".join(sorted(body.control.job_ids)).encode()
        ).hexdigest()[:24]
        if body.control.batch_id != expected_batch_id:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_background_result_batch"},
            )
        jobs = await BackgroundDeliveryRepo(session).claim_batch(
            chat_id=chat_id,
            creator_user_id=auth.user_id,
            job_ids=body.control.job_ids,
            delivery_batch_id=body.control.batch_id,
        )
        if not jobs:
            raise HTTPException(
                status_code=409,
                detail={"code": "background_result_batch_not_available"},
            )
        job_payloads = [project_background_job(job) for job in jobs]
        control_projection = {
            **body.control.model_dump(mode="json"),
            "jobs": job_payloads,
            "status": "delivered",
        }
        result_json = json.dumps(
            {"jobs": job_payloads},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        stripped = (
            "<system-reminder>\n"
            f'<human-control type="background_results" '
            f'batch_id="{body.control.batch_id}">\n'
            "The following durable background tasks have finished. Correlate "
            "each result with its job_id and continue the user's work. A failed "
            "or cancelled task is information; only create a separate new "
            "background task when further work is actually needed.\n"
            f"<background-results>{result_json}</background-results>\n"
            "</human-control>\n"
            "</system-reminder>"
        )

    runtime_binding = await runtime_repo.bind_chat(
        chat_id,
        user_timezone=body.timezone,
    )
    if runtime_binding is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    if runtime_binding["runtime_type"] not in AVAILABLE_RUNTIME_TYPES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "runtime_adapter_unavailable",
                "runtime_type": runtime_binding["runtime_type"],
            },
        )
    # Runtime capabilities are Turn-scoped, so allocate the durable Run id
    # before constructing any custom MCP or model descriptor.
    turn_id = new_turn_id()
    if control_projection is not None:
        existing_selection = await chat_repo.get_mcp_selection(chat_id)
        if existing_selection is None:
            raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
        selected_mcp_values = list(existing_selection["mcp_server_ids"])
    else:
        selected_mcp_values = list(body.mcp_server_ids)
    try:
        selected_mcp_ids = [uuid.UUID(value) for value in selected_mcp_values]
    except ValueError as exc:  # schema validation normally catches this
        raise HTTPException(status_code=422, detail="invalid MCP server id") from exc
    try:
        selected_custom_mcp_authority = await resolve_custom_mcp_authority(
            auth.tenant_id,
            user_id=auth.user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            runtime_session_id=runtime_binding["runtime_session_id"],
            session_id=auth.session_id,
            session_generation=auth.session_generation,
            membership_id=auth.membership_id,
            server_ids=selected_mcp_values,
        )
    except McpSelectionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "mcp_selection_unavailable", "message": str(exc)},
        ) from exc
    mcp_selection = (
        {
            "ok": True,
            **existing_selection,
        }
        if control_projection is not None
        else await chat_repo.set_mcp_selection(
            chat_id,
            mcp_server_ids=selected_mcp_ids,
            expected_revision=body.chat_config_revision,
        )
    )
    if not mcp_selection.get("ok"):
        if mcp_selection.get("error_code") == "mcp_config_revision_conflict":
            raise HTTPException(status_code=409, detail=mcp_selection)
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")

    # Load the chat's persisted active_modes, then apply this turn's command.
    active_modes = await chat_repo.get_active_modes(chat_id)
    # `/command` mode system: commands are sticky built-in capabilities. The
    # active set gates tools; the user message carries command_activation meta so
    # CommandContextEdit can inject protocol text at the latest command position.
    activated_this_turn: set[str] = set()
    if cmd is not None:
        mode_cfg = COMMAND_MODES.get(cmd)
        if mode_cfg is not None and mode_cfg.kind == "additive":
            # Sticky: accumulate + persist so it survives turns/reopen. (A
            # side-panel `/browser` reaches here; a main-app `/browser` was
            # already refused with a NOTICE above and never gets this far.)
            # Every explicit slash command is an activation, even when the
            # capability was already sticky-active. This moves prompt anchoring
            # to the latest command and gives every Runtime identical semantics.
            activated_this_turn = {cmd}
            active_modes = active_modes | {cmd}
            await chat_repo.set_active_modes(chat_id, active_modes)

    chat_workspace_scope_id = _chat_workspace_scope_id(chat_id)
    current_workflow_id = await chat_repo.get_current_workflow_id(chat_id)
    effective_current_workflow_id = current_workflow_id
    # ``wf_id`` passed to AgentContext is the chat workspace owner for
    # /data, /memory, and /logs. ``current_workflow_id`` separately mounts the
    # selected Workflow is resolved by Platform MCP and does not alter mounts.
    agent_wf_id = chat_workspace_scope_id
    # NOTE: `/browser` is no longer a handoff. A side-panel `/browser` activates
    # browser mode (additive, above) and runs a normal agent turn below; a
    # main-app `/browser` was already refused with a NOTICE near the top. The old
    # MODE_CONTROL scoped-token handoff producer was removed with the cross-app
    # relay.

    thread_id = ChatRepo.checkpointer_thread_id(auth.user_id, scope_id, chat_id)
    user_message = {
        "role": "user",
        # Slash commands are platform control syntax, not Agent dialogue. The
        # Runtime receives only the command-stripped body; the Runtime-neutral
        # product transcript below persists the exact original text for display
        # and audit.
        "content": stripped,
    }
    if cmd is not None:
        cfg = COMMAND_MODES.get(cmd)
        user_message["additional_kwargs"] = {
            "command_activation": {
                "name": cmd,
                "trigger": getattr(cfg, "trigger", f"/{cmd}") if cfg else f"/{cmd}",
            }
        }
    attachments = [a.model_dump(exclude_none=True) for a in body.attachments]
    if attachments:
        user_message.setdefault("additional_kwargs", {})["attachments"] = attachments
    if control_projection is not None:
        user_message.setdefault("additional_kwargs", {})["control"] = (
            control_projection
        )
    # Resolve and validate the Chat-bound model/effort against the SAME runtime
    # catalog rendered by the composer. A stale or cross-runtime choice is a
    # protocol error, never a silent fallback to a different model.
    requested_settings = body.agent_settings
    stored_settings_payload = runtime_binding.get("runtime_agent_settings")
    stored_settings = (
        AgentSettings.model_validate(stored_settings_payload)
        if stored_settings_payload is not None
        else None
    )
    # A request may explicitly change the model or reasoning effort between
    # Turns. If it omits settings, Resume keeps the Chat's last accepted
    # selection instead of re-evaluating the account-wide defaults.
    settings = stored_settings or requested_settings
    if requested_settings is not None:
        settings = requested_settings
    runtime_type = RuntimeType(runtime_binding["runtime_type"])
    requested_model_id = settings.model_id if settings is not None else None
    # A missing picker value means "keep this Chat's model" after the first
    # Turn.  Re-evaluating the global default here can silently move a resumed
    # Codex thread from ChatGPT-account auth to an API-compatible provider.
    selected_model_id = (
        requested_model_id or runtime_binding.get("runtime_model_id")
    )
    selected_effort = settings.reasoning_effort if settings is not None else None
    credential_rows = await LlmCredentialsRepo(session).list_for_user(
        auth.user_id
    )
    if runtime_type == RuntimeType.CODEX:
        runtime_preferences = await runtime_repo.get_preferences()
        selected_managed_profile = runtime_preferences.get(
            "codex_managed_profile_id"
        )
        if selected_managed_profile not in {
            str(profile["id"]) for profile in app_config.codex_managed_apis
        }:
            selected_managed_profile = None
        runtime_capabilities = await codex_capabilities(
            credential_rows,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            selected_managed_profile_id=selected_managed_profile,
            auth_methods=app_config.codex_runtime_auth_methods,
        )
        if not runtime_capabilities.runtime_available:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": runtime_capabilities.error_code
                    or "codex_cli_unavailable",
                    "runtime_type": runtime_type.value,
                },
            )
        if runtime_capabilities.error_code:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": runtime_capabilities.error_code,
                    "runtime_type": runtime_type.value,
                },
            )
        if settings is not None and any(
            value is not None
            for value in (settings.temperature, settings.max_tokens, settings.timeout)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "runtime_setting_not_supported",
                    "runtime_type": runtime_type.value,
                    "fields": ["temperature", "max_tokens", "timeout"],
                },
            )
        try:
            selected_runtime_model = validate_model_effort(
                runtime_capabilities,
                model_id=selected_model_id,
                reasoning_effort=selected_effort,
            )
            effective_codex_model_id = (
                selected_model_id or runtime_capabilities.default_model_id
            )
            account_model_id = codex_account_model_id(effective_codex_model_id)
            managed_model = codex_managed_model(effective_codex_model_id)
            credential_id = (
                None
                if account_model_id is not None or managed_model is not None
                else codex_credential_id(effective_codex_model_id)
            )
            selected_openrouter_model = codex_openrouter_model(
                effective_codex_model_id
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": str(exc), "runtime_type": runtime_type.value},
            ) from exc
    else:
        runtime_capabilities = langchain_capabilities(credential_rows)
        try:
            selected_runtime_model = validate_model_effort(
                runtime_capabilities,
                model_id=selected_model_id,
                reasoning_effort=selected_effort,
            )
            # Resolve the credential from the effective catalog selection, not
            # from the possibly-empty browser field.  On a new Chat the
            # catalog may select the user's first real saved API.  Treating the
            # original ``None`` as a platform credential would silently change
            # the authentication source and is therefore forbidden.
            effective_langchain_model_id = (
                selected_model_id or runtime_capabilities.default_model_id
            )
            credential_id = langchain_credential_id(
                effective_langchain_model_id
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": str(exc), "runtime_type": runtime_type.value},
            ) from exc
        account_model_id = None
        managed_model = None
        selected_openrouter_model = langchain_openrouter_model(
            effective_langchain_model_id
        )
    effective_runtime_model_id = (
        selected_model_id or runtime_capabilities.default_model_id
    )
    if effective_runtime_model_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_not_available_for_runtime",
                "runtime_type": runtime_type.value,
            },
        )
    runtime_connection_id = runtime_model_connection_id(
        runtime_type,
        effective_runtime_model_id,
    )
    bound_connection_id = runtime_binding.get("runtime_connection_id")
    if (
        isinstance(bound_connection_id, str)
        and bound_connection_id
        and bound_connection_id != runtime_connection_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "runtime_connection_locked",
                "runtime_type": runtime_type.value,
            },
        )
    credential_row = (
        await LlmCredentialsRepo(session).get_for_user(
            credential_id,
            auth.user_id,
        )
        if credential_id is not None else None
    )
    if credential_id is not None and credential_row is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_not_available_for_runtime",
                "runtime_type": runtime_type.value,
            },
        )
    if selected_openrouter_model is not None and credential_row is not None:
        if credential_row.get("connection_kind") != "openrouter_oauth":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "model_not_available_for_runtime",
                    "runtime_type": runtime_type.value,
                },
            )
        credential_row = {
            **credential_row,
            "model_name": selected_openrouter_model,
        }

    if runtime_type == RuntimeType.LANGCHAIN:
        if settings is not None and any(
            value is not None
            for value in (
                settings.model_id,
                settings.temperature,
                settings.max_tokens,
                settings.timeout,
                settings.reasoning_effort,
            )
        ):
            agent_cfg = merge_agent_settings_override(
                app_config.agent.to_agent_cfg(),
                credential_row=credential_row,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
                timeout=settings.timeout,
            )
        else:
            agent_cfg = app_config.agent
        if selected_effort is not None:
            # LangChain/OpenAI maps this stable field to Responses reasoning.
            if not isinstance(agent_cfg, dict):
                agent_cfg = agent_cfg.to_agent_cfg()
            else:
                agent_cfg = dict(agent_cfg)
            agent_cfg["reasoning"] = {"effort": selected_effort}
        runtime_model = (
            agent_cfg.to_agent_cfg()
            if hasattr(agent_cfg, "to_agent_cfg")
            else dict(agent_cfg)
        )

    elif account_model_id is not None:
        # Account-backed Codex uses its official model transport. The account
        # cache is mounted independently from Chat thread state and is never
        # reused as a provider API key.
        runtime_model = {
            "id": account_model_id,
            "connection_type": "chatgpt_account",
        }
    else:
        # Codex receives only the SDK model name plus the same host-brokered
        # transport used by LangChain. No Codex account token or provider key
        # enters the Chat sandbox.
        runtime_model = {}

    # API-backed modes receive only a short-lived host-broker capability. The
    # ChatGPT account mode above deliberately bypasses this API-key transport.
    if account_model_id is not None:
        model_provider = "chatgpt"
        model_name = account_model_id
    elif managed_model is not None:
        managed_profile_id, model_name = managed_model
        model_provider = "openai"
        credential_revision = model_config_revision(
            provider=model_provider,
            model=model_name,
            updated_at=f"managed:{managed_profile_id}",
        )
    elif credential_row is None:
        if runtime_type == RuntimeType.CODEX:
            # Codex API mode must always resolve to an explicitly configured
            # managed profile or a user-owned saved credential. Falling
            # through to ``config.agent`` here would silently recreate the
            # removed platform-default API path. A real personal credential
            # continues through the shared broker branch below.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "model_not_available_for_runtime",
                    "runtime_type": runtime_type.value,
                },
            )
        configured_model = str(app_config.agent.model or "")
        configured_provider, separator, configured_name = (
            configured_model.partition(":")
        )
        model_provider = (
            configured_provider if separator else ""
        ).strip().lower().replace("-", "_")
        model_name = (
            configured_name if separator else configured_model
        ).strip()
        credential_revision = model_config_revision(
            provider=model_provider,
            model=model_name,
            updated_at="platform-process-config",
        )
    else:
        model_provider = str(
            credential_row.get("provider") or ""
        ).strip().lower().replace("-", "_")
        model_name = str(credential_row.get("model_name") or "").strip()
        credential_revision = model_config_revision(
            provider=model_provider,
            model=model_name,
            updated_at=credential_row.get("updated_at"),
        )
    if account_model_id is None:
        model_capability = mint_runtime_model_capability(
            organization_id=auth.active_organization_id,
            user_id=auth.user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            runtime_session_id=runtime_binding["runtime_session_id"],
            session_id=auth.session_id,
            session_generation=auth.session_generation,
            membership_id=auth.membership_id,
            credential_id=(
                str(credential_row["id"]) if credential_row is not None else None
            ),
            managed_profile_id=(
                managed_model[0] if managed_model is not None else None
            ),
            provider=model_provider,
            model=model_name,
            config_revision=credential_revision,
            authorization_generation=authorization_model_generation(
                model_id=app_config.openfga_authorization_model_id,
            ),
            resources=[
                f"chat:{chat_id}",
                *(
                    [f"llm_credential:{credential_row['id']}"]
                    if credential_row is not None
                    else []
                ),
            ],
            actions=[
                "chat:execute",
                "model:invoke",
                *(
                    ["llm_credential:use"]
                    if credential_row is not None
                    else []
                ),
            ],
            secret=app_config.signing_secret,
            ttl_s=app_config.mcp.runtime_model_capability_ttl_s,
        )
        runtime_model = {
            key: value
            for key, value in runtime_model.items()
            if key not in {"api_key", "base_url", "proxy"}
        }
        runtime_model.update(
            {
                "id": model_name,
                "base_url": (
                    f"{app_config.mcp.platform_internal_base_url}"
                    "/api/internal/runtime-model/v1"
                ),
                "api_key": model_capability,
                # The sandbox converts this non-secret provider metadata into
                # Codex's official model_catalog_json. Dynamic provider ids
                # must not fall back to guessed context or reasoning limits.
                "label": selected_runtime_model.label,
                "description": selected_runtime_model.description,
                "context_length": selected_runtime_model.context_length,
                "input_modalities": selected_runtime_model.input_modalities,
                "supports_tools": selected_runtime_model.supports_tools,
                "supports_web_search": selected_runtime_model.supports_web_search,
                "api_source": selected_runtime_model.api_source,
                "provider": selected_runtime_model.provider,
                "supported_reasoning_efforts": [
                    option.model_dump()
                    for option in selected_runtime_model.supported_reasoning_efforts
                ],
                "default_reasoning_effort": (
                    selected_runtime_model.default_reasoning_effort
                ),
            }
        )

    runtime_root = private_runtime_root(runtime_type, chat_id)
    effective_active_modes = (
        (active_modes | {"browser"}) if body.mode == "browser" else active_modes
    )
    effective_activated_this_turn = (
        activated_this_turn | (effective_active_modes - active_modes)
    )
    runtime_instructions = command_instructions_for_modes(
        effective_active_modes,
        activated_this_turn=effective_activated_this_turn,
        active_diagram=await chat_repo.get_active_diagram(chat_id),
    )
    active_platform_mcps = platform_mcp_names_for_modes(
        effective_active_modes,
        runtime_type=runtime_type.value,
    )
    host_mcp_authority = [
        *selected_custom_mcp_authority,
        *resolve_platform_mcp_authority(
            active_platform_mcps,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            workspace_scope_id=agent_wf_id,
            runtime_session_id=runtime_binding["runtime_session_id"],
            session_id=auth.session_id,
            session_generation=auth.session_generation,
            membership_id=auth.membership_id,
            approval_mode=body.approval_mode,
        ),
    ]
    runtime_skills = await runtime_skill_descriptors(
        session=session,
        service=authz_service,
        principal=principal_for_auth(auth),
        context=context_for_auth(auth, http_request),
    )
    todo_state = await chat_repo.get_todo_state(chat_id)
    interactive_artifact_refs = (
        await hitl_repo.project_artifact_refs_for_chat(chat_id)
    )
    from ..services.agent_runtime.context_manifest import (
        build_context_manifest,
        context_v2_rollout_mode,
    )
    context_rollout_mode = context_v2_rollout_mode(
        app_config.agent.compaction_v2,
        tenant_id=auth.tenant_id,
        workspace_scope_id=agent_wf_id,
    )
    if runtime_type == RuntimeType.LANGCHAIN:
        runtime_model.setdefault("compaction_v2", {})
        runtime_model["compaction_v2"].update({
            "v2_enabled": context_rollout_mode == "active",
            "effective_mode": context_rollout_mode,
        })
    context_manifest = build_context_manifest(
        runtime_type=runtime_type.value,
        rollout_mode=context_rollout_mode,
        max_tokens=(
            app_config.agent.model_context_tokens
            or app_config.agent.compaction_v2.window_tokens
        ),
        message=user_message,
        instructions=runtime_instructions,
        mcp_servers=host_mcp_authority,
        skills=runtime_skills,
        todo_items=todo_state["items"],
        artifact_refs=interactive_artifact_refs,
        workspace_scope_id=agent_wf_id,
        active_modes=effective_active_modes,
    )
    durable_history = None
    if runtime_type == RuntimeType.CODEX:
        history_rows, history_total, _history_offset = (
            await chat_repo.list_message_page(
                chat_id,
                limit=512,
                tail=True,
            )
        )
        durable_history = build_durable_history_snapshot(
            history_rows,
            source_total=history_total,
        )
    open_request = RuntimeOpenRequest(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        chat_id=chat_id,
        runtime_type=runtime_type,
        runtime_session_id=runtime_binding["runtime_session_id"],
        runtime_root=runtime_root,
        state_ref=runtime_binding["runtime_state_ref"],
        runtime_version=runtime_binding["runtime_version"],
    )
    turn_request = RuntimeTurnRequest(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        chat_id=chat_id,
        turn_id=turn_id,
        runtime_type=runtime_type,
        runtime_session_id=runtime_binding["runtime_session_id"],
        runtime_root=runtime_root,
        runtime_state_ref=runtime_binding["runtime_state_ref"],
        conversation_clock=(
            {
                "timezone": runtime_binding["runtime_timezone"],
                "started_at": runtime_binding["runtime_started_at"],
            }
            if runtime_type == RuntimeType.LANGCHAIN
            and runtime_binding.get("runtime_timezone")
            and runtime_binding.get("runtime_started_at") is not None
            else None
        ),
        durable_history=(
            durable_history.model_dump(mode="json")
            if durable_history is not None
            else {}
        ),
        message=user_message,
        attachments=attachments,
        model=runtime_model,
        reasoning_effort=(
            settings.reasoning_effort if settings is not None else None
        ),
        approval_mode=body.approval_mode,
        surface=body.surface,
        active_platform_mcps=active_platform_mcps,
        mcp_config_revision=int(mcp_selection["mcp_config_revision"]),
        mcp_host_servers=host_mcp_authority,
        skills=runtime_skills,
        todo_items=todo_state["items"],
        todo_revision=todo_state["revision"],
        interactive_artifact_refs=interactive_artifact_refs,
        instructions=runtime_instructions,
        command_context={
            "thread_id": thread_id,
            "is_first": is_first,
            "chat_context": stripped[:80],
            "workspace_scope_id": agent_wf_id,
            "current_workflow_id": effective_current_workflow_id,
            "agent_surface": agent_surface,
            "available_commands": sorted(available_commands),
            "active_modes": sorted(effective_active_modes),
            "activated_this_turn": sorted(effective_activated_this_turn),
        },
        context_manifest=context_manifest,
    )

    bootstrap_sidepanel_browser = (
        body.surface == "sidepanel" and "browser" in effective_active_modes
    )

    async def producer(stop_ev: asyncio.Event):
        browser_lease = None
        if bootstrap_sidepanel_browser:
            # Sending from the side panel is the explicit user action that
            # authorizes the visible page for this Chat. Reserve the durable
            # lease before official Playwright MCP connects; the CDP endpoint
            # confirms it only after the extension initializes successfully.
            from ..browser.session_control import (
                reserve_sidepanel_browser_session,
            )

            browser_lease = await reserve_sidepanel_browser_session(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                chat_id=chat_id,
            )
        try:
            orchestrator = AgentRuntimeOrchestrator()
            async for product_event in orchestrator.stream_turn(
                open_request=open_request,
                turn_request=turn_request,
                workspace_scope_id=agent_wf_id,
                current_workflow_id=effective_current_workflow_id,
                stop_event=stop_ev,
            ):
                yield product_event
        finally:
            if browser_lease is not None:
                from ..browser.session_control import (
                    release_unconfirmed_browser_session,
                )

                await release_unconfirmed_browser_session(
                    browser_lease,
                    reason="playwright_mcp_startup_incomplete",
                )

    # Authorization may have changed while model/MCP/runtime inputs were being
    # assembled. Recheck at the durable Agent Run mutation boundary so revoke
    # wins before a new Turn becomes visible or executable.
    await _authorize_chat(
        request=http_request,
        auth=auth,
        service=authz_service,
        chat_id=chat_id,
        action=Action.EXECUTE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    from ..storage.agent_runs_repo import AgentRunActiveError
    try:
        reserved_run, run_created = await agent_runs_repo.create_exclusive(
            run_id=turn_id,
            tenant_id=auth.tenant_id,
            chat_id=chat_id,
            creator_user_id=auth.user_id,
            client_request_id=effective_client_request_id or turn_id,
            input_message_id=f"{chat_id}:user:{turn_id}",
            input_snapshot={
                "content": body.content,
                "message_type": (
                    "control" if control_projection is not None else "text"
                ),
                "visibility": (
                    "hidden" if control_projection is not None else "visible"
                ),
                "control": control_projection,
                "attachments": attachments,
                "mode": body.mode,
                "surface": body.surface,
                "agent_surface": body.agent_surface,
                "approval_mode": body.approval_mode,
                "runtime_type": runtime_binding["runtime_type"],
                "runtime_session_id": runtime_binding["runtime_session_id"],
                "runtime_version": runtime_binding["runtime_version"],
                "runtime_connection_id": runtime_connection_id,
                "model_id": effective_runtime_model_id,
                "provider_model_id": selected_runtime_model.provider_model_id,
                "model_provider": selected_runtime_model.provider,
                "api_source": selected_runtime_model.api_source,
                "api_protocol": selected_runtime_model.api_protocol,
                "reasoning_effort": (
                    settings.reasoning_effort if settings is not None else None
                ),
                "command": cmd,
                "runtime_instructions": [
                    {
                        "instruction_id": item.instruction_id,
                        "name": item.name,
                        "version": item.version,
                        "activated_this_turn": item.activated_this_turn,
                    }
                    for item in runtime_instructions
                ],
                "skill_snapshot": [
                    {
                        "skill_id": item.skill_id,
                        "revision_hash": item.revision_hash,
                    }
                    for item in runtime_skills
                ],
                "mcp_snapshot": {
                    "server_ids": mcp_selection["mcp_server_ids"],
                    "chat_config_revision": mcp_selection["mcp_config_revision"],
                    "set_hash": hashlib.sha256(
                        json.dumps(
                            mcp_selection["mcp_server_ids"],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "server_config_revisions": {
                        item.server_id: item.config_revision
                        for item in host_mcp_authority
                        if item.server_id is not None
                    },
                },
            },
        )
    except AgentRunActiveError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "chat_run_active", "run_id": exc.run_id},
        ) from exc

    if not run_created:
        # Idempotent POST replay: no second HumanMessage and no second Agent
        # execution. Rebuild this request from the durable event log. This path
        # is protected by the same DB lock as creation, so concurrent retries
        # cannot fall through and start a duplicate producer.
        await chat_repo.commit()
        from ..services.agent_run_stream import agent_run_event_stream
        return StreamingResponse(
            agent_run_event_stream(
                run_id=reserved_run.run_id,
                after_seq=0,
                tenant_id=auth.tenant_id,
                authorization_guard=_chat_stream_guard(
                    request=http_request,
                    auth=auth,
                    resource_type=ResourceType.AGENT_RUN,
                    resource_id=reserved_run.run_id,
                    action=Action.VIEW,
                ),
            ),
            media_type="text/event-stream",
            headers={
                **SSE_HEADERS,
                "X-Turn-Id": reserved_run.run_id,
            },
        )

    # Only an accepted, newly reserved Turn may advance the Chat's Resume
    # selection. An active-run rejection or an idempotent POST replay must not
    # overwrite the model/source chosen by the already-authoritative Turn.
    persisted_runtime_binding = await runtime_repo.set_runtime_model_selection(
        chat_id,
        runtime_type=runtime_type.value,
        model_id=effective_runtime_model_id,
        connection_id=runtime_connection_id,
        agent_settings={
            "model_id": effective_runtime_model_id,
            "temperature": settings.temperature if settings is not None else None,
            "max_tokens": settings.max_tokens if settings is not None else None,
            "timeout": settings.timeout if settings is not None else None,
            "reasoning_effort": (
                settings.reasoning_effort if settings is not None else None
            ),
        },
    )
    if persisted_runtime_binding is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    runtime_binding = persisted_runtime_binding

    # Product transcript is authoritative and Runtime-neutral. Persist the
    # completed user message exactly once, in the same transaction that made
    # its Agent Run durable, before any runtime output can be published.
    if isinstance(body.control, BackgroundResultsControl):
        delivered_jobs = (
            control_projection.get("jobs")
            if isinstance(control_projection, dict)
            and isinstance(control_projection.get("jobs"), list)
            else []
        )
        labels = [
            str(job.get("job_id"))
            for job in delivered_jobs
            if isinstance(job, dict) and job.get("job_id")
        ]
        status_summary = {
            status: sum(
                1
                for job in delivered_jobs
                if isinstance(job, dict) and job.get("status") == status
            )
            for status in ("completed", "failed", "cancelled")
        }
        single_title = (
            str(delivered_jobs[0].get("title") or labels[0])
            if len(delivered_jobs) == 1
            and isinstance(delivered_jobs[0], dict)
            and labels
            else ""
        )
        notice_text = (
            f"{single_title} finished · {labels[0]}"
            if single_title
            else (
                f"{len(labels)} background jobs have results"
                f" · {status_summary['completed']} completed"
                f" · {status_summary['failed']} failed"
                f" · {status_summary['cancelled']} cancelled"
            )
        )
        await chat_repo.persist_message(
            chat_id,
            {
                "message_id": f"{chat_id}:notice:{reserved_run.run_id}",
                "turn_id": reserved_run.run_id,
                "role": "system",
                "content": {
                    "schema_version": 2,
                    "message_type": "activity",
                    "visibility": "visible",
                    "text": notice_text,
                    "attachments": [],
                    "tool_calls": [],
                    "activity": {
                        "type": "background_jobs_delivered",
                        "delivery_batch_id": body.control.batch_id,
                        "job_ids": labels,
                        "summary": status_summary,
                    },
                },
                "meta": {"surface": "background_delivery"},
            },
        )
    await chat_repo.persist_message(
        chat_id,
        {
            "message_id": reserved_run.input_message_id,
            "turn_id": reserved_run.run_id,
            "role": "user",
            "content": {
                "schema_version": 2,
                "message_type": (
                    "control" if control_projection is not None else "text"
                ),
                "visibility": (
                    "hidden" if control_projection is not None else "visible"
                ),
                "text": body.content,
                "attachments": attachments,
                "tool_calls": [],
                "control": control_projection,
            },
            "meta": {
                "command": cmd,
                "surface": body.surface,
            },
        },
    )

    # Make the Chat row, metadata, and Agent Run durable NOW, before streaming.
    # Dependency teardown happens only after the whole SSE response is sent.
    # This also releases any chat-row lock taken by sticky mode metadata before
    # build tools open their own short sessions during the Turn.
    await chat_repo.commit()
    logger.info(
        "agent_turn_timing",
        phase="request_prepare",
        elapsed_ms=int((perf_counter() - request_started) * 1000),
        runtime_type=runtime_type.value,
        chat_id=chat_id,
        turn_id=turn_id,
        first_turn=is_first,
        custom_mcp_count=len(selected_custom_mcp_authority),
        platform_mcp_count=len(active_platform_mcps),
        skill_count=len(runtime_skills),
    )

    from ..services.agent_run_writer import AgentRunWriter
    buf, stop = register_turn(turn_id)
    durable_writer = AgentRunWriter(
        run_id=turn_id,
        tenant_id=auth.tenant_id,
        chat_id=chat_id,
        user_id=auth.user_id,
    )

    TURN_TASKS[turn_id] = asyncio.create_task(
        run_turn(turn_id, buf, stop, producer, durable_writer=durable_writer)
    )
    # Mark this chat busy so the bg-task watcher defers callback turns until the
    # current turn finishes (cleared in run_turn's finally).
    from vibecanvas_api.streaming.turn_runtime import mark_chat_active
    mark_chat_active(chat_id, turn_id)

    return StreamingResponse(
        _sse_from_turn(
            turn_id,
            authorization_guard=_chat_stream_guard(
                request=http_request,
                auth=auth,
                resource_type=ResourceType.AGENT_RUN,
                resource_id=turn_id,
                action=Action.VIEW,
            ),
        ),
        media_type="text/event-stream",
        headers={
            **SSE_HEADERS,
            "X-Turn-Id": turn_id,
        },
    )


@router.get("/chats/{chat_id}/turns/{turn_id}/resume",
            dependencies=[Depends(current_user)])
async def resume_turn(
    chat_id: str,
    turn_id: str,
    request: Request,
    agent_runs_repo=Depends(get_agent_runs_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> StreamingResponse:
    """Re-attach through the durable event log, independent of API worker."""
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.AGENT_RUN,
        resource_id=turn_id,
        action=Action.RESUME,
        not_found_detail="agent_run_not_found",
    )
    if await agent_runs_repo.get_for_chat(
        chat_id,
        turn_id,
        creator_user_id=auth.user_id,
    ) is None:
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not found")
    await agent_runs_repo.commit()  # release the request transaction before SSE
    after_seq = _last_event_id(request)
    # Same-worker recovery can subscribe to the already-durable in-memory
    # replay buffer immediately. A different worker has no such buffer and
    # falls back to the PostgreSQL event log below.
    if TURN_BUFFERS.get(turn_id) is not None:
        return StreamingResponse(
            _sse_from_turn(
                turn_id,
                after_seq=after_seq,
                authorization_guard=_chat_stream_guard(
                    request=request,
                    auth=auth,
                    resource_type=ResourceType.AGENT_RUN,
                    resource_id=turn_id,
                    action=Action.RESUME,
                ),
            ),
            media_type="text/event-stream",
            headers={**SSE_HEADERS, "X-Replay-Source": "memory"},
        )
    from ..services.agent_run_stream import agent_run_event_stream
    return StreamingResponse(
        agent_run_event_stream(
            run_id=turn_id,
            after_seq=after_seq,
            tenant_id=auth.tenant_id,
            authorization_guard=_chat_stream_guard(
                request=request,
                auth=auth,
                resource_type=ResourceType.AGENT_RUN,
                resource_id=turn_id,
                action=Action.RESUME,
            ),
        ),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Replay-Source": "database"},
    )


@router.post("/chats/{chat_id}/turns/{turn_id}/cancel",
             dependencies=[Depends(current_user)], status_code=202)
async def cancel_turn(
    chat_id: str,
    turn_id: str,
    request: Request,
    agent_runs_repo=Depends(get_agent_runs_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    from vibecanvas_api.streaming.turn_runtime import request_cancel_for_chat
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.AGENT_RUN,
        resource_id=turn_id,
        action=Action.CANCEL,
        not_found_detail="agent_run_not_found",
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    if not await agent_runs_repo.request_cancel(
        chat_id,
        turn_id,
        creator_user_id=auth.user_id,
    ):
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not running")
    # Commit before signalling the local task: a task on this or another worker
    # observes the durable cancel flag rather than depending on process memory.
    await agent_runs_repo.commit()
    request_cancel_for_chat(chat_id, turn_id)  # same-worker fast path
    return {"status": "cancel-requested"}


@router.post(
    "/chats/{chat_id}/active-turn/cancel",
    dependencies=[Depends(current_user)],
    status_code=202,
)
async def cancel_active_turn(
    chat_id: str,
    request: Request,
    agent_runs_repo=Depends(get_agent_runs_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
):
    """Cancel exactly the currently-active Turn owned by this Chat.

    Frontends express the user intent (Stop this Chat) without caching or
    joining Runtime ids. The database control plane resolves the active Run;
    the explicit Run endpoint above remains useful to Runtime peers that
    already possess a correlation id.
    """
    from vibecanvas_api.streaming.turn_runtime import request_cancel_for_chat

    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.CANCEL,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    active = await agent_runs_repo.get_active_for_chat_user(
        chat_id,
        creator_user_id=auth.user_id,
    )
    if active is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} has no active turn")
    if not await agent_runs_repo.request_cancel(
        chat_id,
        active.run_id,
        creator_user_id=auth.user_id,
    ):
        raise HTTPException(status_code=409, detail="active turn changed; retry Stop")
    await agent_runs_repo.commit()
    request_cancel_for_chat(chat_id, active.run_id)
    return {
        "status": "cancel-requested",
        "chat_id": chat_id,
        "run_id": active.run_id,
    }


@router.get("/chats/{chat_id}/turns/{turn_id}/stream",
            dependencies=[Depends(current_user)])
async def stream_turn(
    chat_id: str,
    turn_id: str,
    request: Request,
    agent_runs_repo=Depends(get_agent_runs_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> StreamingResponse:
    """Replay and tail a Turn from PostgreSQL, across refreshes and workers."""
    await _authorize_chat_child(
        request=request,
        auth=auth,
        service=service,
        resource_type=ResourceType.AGENT_RUN,
        resource_id=turn_id,
        action=Action.VIEW,
        not_found_detail="agent_run_not_found",
    )
    if await agent_runs_repo.get_for_chat(
        chat_id,
        turn_id,
        creator_user_id=auth.user_id,
    ) is None:
        raise HTTPException(status_code=404, detail=f"turn {turn_id} not found")
    await agent_runs_repo.commit()  # stream opens independent short sessions
    after_seq = _last_event_id(request)
    if TURN_BUFFERS.get(turn_id) is not None:
        return StreamingResponse(
            _sse_from_turn(
                turn_id,
                after_seq=after_seq,
                authorization_guard=_chat_stream_guard(
                    request=request,
                    auth=auth,
                    resource_type=ResourceType.AGENT_RUN,
                    resource_id=turn_id,
                    action=Action.VIEW,
                ),
            ),
            media_type="text/event-stream",
            headers={
                **SSE_HEADERS,
                "X-Turn-Id": turn_id,
                "X-Replay-Source": "memory",
            },
        )
    from ..services.agent_run_stream import agent_run_event_stream
    return StreamingResponse(
        agent_run_event_stream(
            run_id=turn_id,
            after_seq=after_seq,
            tenant_id=auth.tenant_id,
            authorization_guard=_chat_stream_guard(
                request=request,
                auth=auth,
                resource_type=ResourceType.AGENT_RUN,
                resource_id=turn_id,
                action=Action.VIEW,
            ),
        ),
        media_type="text/event-stream",
        headers={
            **SSE_HEADERS,
            "X-Turn-Id": turn_id,
            "X-Replay-Source": "database",
        },
    )


@router.get(
    "/chat-scopes/{scope_id}/active-runs",
    response_model=list[ActiveAgentRun],
    dependencies=[Depends(current_user)],
)
async def list_active_agent_runs(
    scope_id: str,
    request: Request,
    agent_runs_repo=Depends(get_agent_runs_repo),
    hitl_repo=Depends(get_hitl_repo),
    workflow_repo: WorkflowRepo = Depends(get_workflow_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> list[ActiveAgentRun]:
    # The MVP has no automatic Worker lease transfer. Heartbeats let a reader
    # close a crashed process's orphan Run instead of restoring an endless
    # "streaming" / "waiting approval" UI.
    # Capture the active set first. If stale reconciliation closes one of these
    # rows below, return it once so the frontend can replay its persisted partial
    # UI plus terminal worker_lost event. The next discovery no longer returns it.
    await _authorize_chat_carrier(
        request=request,
        auth=auth,
        service=service,
        workflow_repo=workflow_repo,
        scope_id=scope_id,
        action=Action.VIEW,
    )
    authorized_chat_ids = set(await service.list_authorized_ids(
        principal_for_auth(auth),
        Action.INSPECT_RUNS,
        ResourceType.CHAT,
        context_for_auth(auth, request),
    ))
    runs = await agent_runs_repo.list_active_for_scope(
        scope_id,
        creator_user_id=auth.user_id,
    )
    runs = [run for run in runs if run.chat_id in authorized_chat_ids]
    await agent_runs_repo.mark_stale_for_scope(
        scope_id=scope_id,
        stale_before=datetime.now(timezone.utc) - timedelta(seconds=45),
        tenant_id=auth.tenant_id,
        creator_user_id=auth.user_id,
    )
    pending_by_run: dict[str, list[HitlRequestOut]] = {}
    for run in runs:
        pending_by_run[run.run_id] = [
            _hitl_out(row)
            for row in await hitl_repo.list_pending_for_run(run.run_id)
        ]
    return [
        ActiveAgentRun(
            run_id=run.run_id,
            chat_id=run.chat_id,
            status=run.status,
            last_event_id=run.last_event_id,
            created_at=run.created_at,
            input_message_id=run.input_message_id,
            input_message=(
                None
                if (run.input_snapshot or {}).get("message_type") == "control"
                else HistoryMessage(
                    id=run.input_message_id,
                    role="user",
                    content=str((run.input_snapshot or {}).get("content") or ""),
                    attachments=(
                        (run.input_snapshot or {}).get("attachments")
                        if isinstance(
                            (run.input_snapshot or {}).get("attachments"),
                            list,
                        )
                        else []
                    ),
                    ts=run.created_at.timestamp(),
                )
            ),
            pending_hitl=pending_by_run.get(run.run_id, []),
        )
        for run in runs
    ]


@router.get(
    "/chats/{chat_id}/turns/by-client-request/{client_request_id}",
    response_model=ActiveAgentRun,
    dependencies=[Depends(current_user)],
)
async def get_agent_run_by_client_request(
    chat_id: str,
    client_request_id: str,
    request: Request,
    agent_runs_repo=Depends(get_agent_runs_repo),
    hitl_repo=Depends(get_hitl_repo),
    auth: AuthContext = Depends(current_user),
    service: AuthzService = Depends(get_authz_service),
) -> ActiveAgentRun:
    """Find a durable Agent Run after the POST stream disconnected early.

    The frontend generates `client_request_id` before sending a message. If the
    POST response drops before `X-Turn-Id` is observed, this endpoint lets the
    client recover the already-created run and switch to the database-backed
    GET stream instead of retrying the POST and duplicating the turn.
    """
    await _authorize_chat(
        request=request,
        auth=auth,
        service=service,
        chat_id=chat_id,
        action=Action.INSPECT_RUNS,
    )
    run = await agent_runs_repo.get_by_client_request(
        chat_id,
        client_request_id,
        creator_user_id=auth.user_id,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="agent_run_not_found")
    pending_hitl = [
        _hitl_out(row)
        for row in await hitl_repo.list_pending_for_run(run.run_id)
    ]
    return ActiveAgentRun(
        run_id=run.run_id,
        chat_id=run.chat_id,
        status=run.status,
        last_event_id=run.last_event_id,
        created_at=run.created_at,
        input_message_id=run.input_message_id,
        pending_hitl=pending_hitl,
    )
