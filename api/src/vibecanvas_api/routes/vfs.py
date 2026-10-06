"""VFS 2c — read-only HTTP surface over the agent's VFS (artifacts + scratch).

GET /api/v1/vfs          — list entries (metadata only, NO content)
GET /api/v1/vfs/content  — read one file (byte-bounded, no-touch)

RLS (tenant_db) isolates rows by tenant. Reads are no-touch so a human
browsing never perturbs the agent's LRU. The `stale` current-version string
comes from the SAME version_str formatter the agent's read_file uses.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from urllib.parse import urlencode
import base64
import binascii
import os

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.authorization.dependencies import (
    authorized_resource_scope,
    context_for_auth,
    get_authz_service,
    principal_for_auth,
)
from vibecanvas_api.authorization.service import AuthzService
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    ConsistencyPreference,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.security.upload_scanner import require_clean_upload
from vibecanvas_api.schemas.vfs import (
    VfsDeleteOut, VfsEntryOut, VfsListOut, VfsReadOut, VfsRenameIn, VfsRenameOut,
    VfsRunEntryOut, VfsRunListOut, VfsSignIn, VfsSignOut, VfsUploadOut,
    VfsWriteBytesIn, VfsWriteIn, VfsWriteOut,
)
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.workspace_file_access import owned_workspace_chats, workspace_path_visible
from vibecanvas_api.services.file_revision import vfs_row_revision
from vibecanvas_api.services.chat_workspace import (
    project_id_from_workspace_scope,
    is_agent_workspace_scope,
)
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.user_mount_workspace import (
    host_mount_bridge,
    mount_scope_id as _mount_scope_id,
)
from vibecanvas_api.services.vfs_signing import (
    sign_vfs_url,
    vfs_resource_access_allowed,
    verify_vfs_resource_capability,
    verify_vfs_sig,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import ChatProject, VfsArtifact, VfsRun, VfsScratch
from vibecanvas_api.storage.vfs_run_repo import VfsRunRepo, _validate as _validate_run_path
from vibecanvas_api.storage.vfs_store import VfsRepo, _validate_artifact_path
from vibecanvas_api.storage.workflow_repo import WorkflowRepo
from vibecanvas_api.utils.versioning import version_str

router = APIRouter(prefix="/api/v1/vfs", tags=["vfs"])
logger = structlog.get_logger(__name__)

# Browser file View inline-text cap. Keep this deliberately below the upload
# limit: the current table editor still parses/renders in-memory, so very large
# tabular files should degrade to a truncated read-only preview instead of
# risking a frozen tab.
VFS_HTTP_MAX_BYTES = 5 * 1024 * 1024

# Signed-URL raw-bytes media endpoint (UX-10e).
RAW_URL_TTL_S = 300
# Cap for the raw media endpoint — binary/image/document previews are also kept
# conservative until the View surface has streaming/range-based rendering.
VFS_RAW_MAX_BYTES = 5 * 1024 * 1024
_LIVE_SANDBOX_RECONCILE_TIMEOUT_S = 15.0
# Content-types we are willing to serve INLINE (browser renders in-place). Any
# other type is forced to application/octet-stream + attachment so this endpoint
# can NEVER be coerced into hosting text/html or javascript (an XSS / content-
# injection vector if the bytes were rendered inline by the browser).
_RAW_INLINE_PREFIXES = ("image/", "video/", "audio/")
_RAW_INLINE_EXACT = ("application/pdf",)

_INLINE_TEXT_EXT_CONTENT_TYPES = {
    ".csv": "table/csv",
    ".htm": "text/html",
    ".html": "text/html",
    ".json": "application/json",
    ".jsonl": "table/jsonl",
    ".md": "text/markdown",
    ".ndjson": "table/jsonl",
    ".py": "text/python",
    ".tsv": "table/tsv",
    ".txt": "text/plain",
}


def _safe_raw_content_type(ct: str) -> tuple[str, str]:
    """Decide the response Content-Type + Content-Disposition for the raw
    endpoint. Allowlisted media → served inline with its real type; everything
    else (esp. text/html, application/javascript) → octet-stream + attachment."""
    ct = (ct or "").split(";")[0].strip().lower()
    if ct.startswith(_RAW_INLINE_PREFIXES) or ct in _RAW_INLINE_EXACT:
        return ct, "inline"
    return "application/octet-stream", "attachment"


def _validate_raw_path(path: str) -> None:
    """Reject traversal before any lookup — mirrors the validators the write /
    run paths use. Artifact reads are keyed exact-match under RLS (no traversal
    risk), but we still bar ``..`` / NUL / non-absolute defensively."""
    if not path or not path.startswith("/") or "\x00" in path:
        raise HTTPException(status_code=400, detail="invalid_path")
    for seg in path.split("/"):
        if seg in (".", ".."):
            raise HTTPException(status_code=400, detail="invalid_path")


def _is_hidden_path(path: str) -> bool:
    """Hidden convention: any path segment starting with ``__`` is INTERNAL
    plumbing — e.g. ``/exec/__compaction__/`` (the context-compaction cache the
    s2a/s2b middleware writes). Such entries are not user-facing, so the Explorer
    listing skips them (and the otherwise-empty ``/exec/`` parent disappears)."""
    return any(
        seg.startswith("__") or seg.startswith(".")
        for seg in path.split("/")
        if seg
    )


def _inline_text_content_type(path: str, content_type: str) -> str | None:
    """Return the effective inline text content-type for HTTP preview/edit.

    Browser uploads often send text-ish extensions such as `.jsonl` as
    `application/octet-stream`; keep the stored type unchanged, but use the path
    extension as a read-time fallback so the Explorer can still preview/edit.
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if (
        ct.startswith("text/")
        or ct in ("application/json", "json", "text")
        or (ct.startswith("table/") and ct != "table/xlsx")
    ):
        return ct
    return _INLINE_TEXT_EXT_CONTENT_TYPES.get(os.path.splitext(path.lower())[1])


async def _current(session: AsyncSession, user_id: str, wf_id: str) -> str | None:
    if not wf_id or wf_id.startswith("__"):
        return None
    meta = await WorkflowRepo(session, user_id).get_meta(wf_id)
    return version_str(meta) if meta else None


async def _ensure_vfs_scope_access(
    *,
    request: Request,
    session: AsyncSession,
    auth: AuthContext,
    service: AuthzService,
    wf_id: str,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> None:
    """Authorize one durable VFS scope through its product resource root.

    Personal mount rows inherit the caller's per-organization Storage root;
    Workflow rows inherit the Workflow; canonical Chat workspace identifiers
    resolve to the Chat root.  The identifier itself never grants access.
    Unknown internal scopes fail closed.
    """
    if not wf_id:
        raise HTTPException(status_code=404, detail="vfs_scope_not_found")
    if _is_user_mount_scope(wf_id, auth.user_id):
        resource = ResourceRef(
            ResourceType.STORAGE_ROOT,
            auth.user_id,
            auth.active_organization_id,
        )
    elif (project_id := project_id_from_workspace_scope(wf_id)) is not None:
        # A Project workspace is private to its owner. Resolve the durable row
        # under tenant RLS before applying the live personal-storage policy;
        # the encoded identifier is not proof of ownership.
        owned_project_id = (await session.execute(
            select(ChatProject.project_id).where(
                ChatProject.project_id == project_id,
                ChatProject.creator_user_id == auth.user_id,
                ChatProject.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if owned_project_id is None:
            raise HTTPException(status_code=404, detail="vfs_scope_not_found")
        resource = ResourceRef(
            ResourceType.STORAGE_ROOT, auth.user_id, auth.active_organization_id,
        )
    elif wf_id.startswith("__"):
        raise HTTPException(status_code=404, detail="vfs_scope_not_found")
    else:
        resource = ResourceRef(
            ResourceType.WORKFLOW,
            wf_id,
            auth.active_organization_id,
        )

    decision = await service.check(
        principal_for_auth(auth),
        action,
        resource,
        context_for_auth(auth, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="vfs_scope_not_found")
    if resource.type is ResourceType.WORKFLOW:
        meta = await WorkflowRepo(session, auth.user_id).get_meta(wf_id)
        if not meta:
            raise HTTPException(status_code=404, detail="workflow_not_found")


async def _ensure_run_access(
    *,
    service: AuthzService,
    auth: AuthContext,
    run_id: str,
    action: Action = Action.VIEW,
) -> None:
    decision = await service.check(
        PrincipalRef(PrincipalType.USER, auth.user_id),
        action,
        ResourceRef(
            ResourceType.VFS_RUN,
            run_id,
            auth.active_organization_id,
        ),
        AuthzRequestContext(
            active_organization_id=auth.active_organization_id,
            session_id=auth.session_id,
            session_generation=auth.session_generation,
            membership_id=auth.membership_id,
            membership_role=auth.membership_role,
            membership_status=auth.membership_status,
            authentication_strength=auth.authentication_strength,
        ),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="vfs_run_not_found")


@asynccontextmanager
async def _authorized_run_scope(request, session, auth, service, run_id):
    try:
        await _ensure_run_access(service=service, auth=auth, run_id=run_id)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        # Shared workflow run folders have exactly the workflow ID. Private
        # execution IDs and Chat run IDs cannot acquire this root permission.
        async with authorized_resource_scope(request=request, auth=auth, session=session,
                resource_type=ResourceType.WORKFLOW, resource_id=run_id, action=Action.VIEW) as (scoped, scoped_service):
            decision = await scoped_service.check(principal_for_auth(auth), Action.VIEW,
                ResourceRef(ResourceType.VFS_RUN, run_id, auth.active_organization_id),
                context_for_auth(auth, scoped))
            if not decision.allowed:
                raise HTTPException(404, "vfs_run_not_found")
            yield getattr(scoped.state, "admitted_resource_organization_id", None) or auth.tenant_id
    else:
        yield auth.tenant_id


@router.get("", response_model=VfsListOut)
async def list_vfs(
    request: Request,
    wf_id: str = Query(default=""),
    prefix: str = Query(default="/"),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsListOut:
    # Debug now reads the owner-scoped database transcript. No special hidden
    # directory is exposed by the ordinary file Explorer.
    if config.workspace_storage_backend != "posix" and _is_user_mount_scope(wf_id, ctx.user_id):
        await host_mount_bridge.sync_user(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=wf_id,
        action=Action.VIEW,
    )
    # The file tree is also the user-controlled reconciliation surface.  This
    # call never allocates a sandbox: it only flushes an already-running Chat
    # workspace, so both the Refresh button and the existing 3-second polling
    # are Runtime/tool agnostic.
    if config.workspace_storage_backend == "posix":
        from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
        from vibecanvas_api.services.workspace_vfs import list_workspace_files
        private_chats = await owned_workspace_chats(session, wf_id, ctx.user_id)
        entries = await asyncio.to_thread(
            list_workspace_files, PosixWorkspaceStorage(config.workspace_storage_root),
            tenant_id=context_for_auth(ctx, request).admitted_resource_organization_id or ctx.tenant_id,
            scope_id=wf_id, user_id=ctx.user_id,
            prefix=prefix, owned_chats=private_chats,
        )
    else:
        await _reconcile_loaded_chat_workspace(
            tenant_id=ctx.tenant_id,
            scope_id=wf_id,
        )
        entries = await VfsRepo(session, object_store=get_object_store()).ls_meta(
            wf_id=wf_id or None, prefix=prefix)
    current = await _current(session, ctx.user_id, wf_id)
    writable_root = (
        "mount" if _is_user_mount_scope(wf_id, ctx.user_id)
        else "data" if is_agent_workspace_scope(wf_id)
        else None
    )
    writable_roots = {writable_root} if writable_root else set()
    private_chats = (await owned_workspace_chats(session, wf_id, ctx.user_id)
                     if any(not workspace_path_visible(e.path, set()) for e in entries) else set())
    out = [
        VfsEntryOut(
            path=e.path, kind=e.kind, content_type=e.content_type,
            abstract=e.abstract, size_bytes=e.size_bytes, wf_version=e.wf_version,
            last_access=e.last_access,
            stale=bool(e.kind == "artifact" and e.wf_version and current
                       and e.wf_version != current),
            capabilities=[
                "read", "download", "copy_path",
                *(["rename", "delete"] if e.path.strip("/").split("/", 1)[0] in writable_roots else []),
            ],
        )
        for e in entries
        if not _is_hidden_path(e.path) and workspace_path_visible(e.path, private_chats)
    ]
    return VfsListOut(
        entries=out,
        root_capabilities={
            root: ["upload", "create_folder", "rename", "delete"]
            for root in sorted(writable_roots)
        },
    )


@router.get("/runs/{run_id}", response_model=VfsRunListOut)
async def list_run_vfs(
    run_id: str,
    request: Request,
    prefix: str = Query("/"),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsRunListOut:
    async with _authorized_run_scope(request, session, ctx, authz, run_id) as tenant:
        if config.workspace_storage_backend == "posix":
            from vibecanvas_api.services.run_workspace_resolution import resolve_run_workspace
            from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
            from vibecanvas_api.services.file_format import content_type_for
            try:
                identity = await resolve_run_workspace(session, run_id)
            except (FileNotFoundError, ValueError) as error:
                raise HTTPException(status_code=404, detail="vfs_run_not_found") from error
            def list_entries():
                storage = PosixWorkspaceStorage(config.workspace_storage_root)
                entries = storage.entries(identity, visible=lambda relative:
                    not _is_hidden_path(relative) and
                    not (identity.kind == "workflow" and relative.split('/')[0] == 'chats'))
                return [VfsRunEntryOut(
                    path='/run/' + entry.path, content_type=content_type_for(entry.path),
                    size_bytes=entry.size_bytes, capabilities=['read', 'download', 'copy_path'],
                ) for entry in entries if not entry.is_directory and ('/run/' + entry.path).startswith(prefix)]
            return VfsRunListOut(entries=await asyncio.to_thread(list_entries))
        repo = VfsRunRepo(session, get_object_store(), tenant)
        rows = await repo.ls(run_id=run_id, prefix=prefix)
    return VfsRunListOut(entries=[
        VfsRunEntryOut(
            path=r.path,
            content_type=r.content_type,
            size_bytes=r.size_bytes,
            capabilities=["read", "download", "copy_path"],
        )
        for r in rows
    ])


@router.get("/content", response_model=VfsReadOut)
async def read_vfs(
    request: Request,
    path: str = Query(...),
    wf_id: str = Query(default=""),
    run_id: str | None = Query(default=None),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsReadOut:
    if run_id:
        async with _authorized_run_scope(request, session, ctx, authz, run_id) as tenant:
            if config.workspace_storage_backend == "posix":
                from vibecanvas_api.services.run_workspace_resolution import resolve_run_workspace, run_relative_path
                from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
                from vibecanvas_api.services.file_format import content_type_for
                content_type = content_type_for(path)
                effective_ct = _inline_text_content_type(path, content_type)
                try:
                    identity = await resolve_run_workspace(session, run_id)
                    relative = run_relative_path(identity, path)
                    def read_content():
                        storage = PosixWorkspaceStorage(config.workspace_storage_root)
                        with storage.open_read(identity, relative) as source:
                            size = os.fstat(source.fileno()).st_size
                            return source.read(VFS_HTTP_MAX_BYTES if effective_ct else 0), size
                    raw, size = await asyncio.to_thread(read_content)
                except (FileNotFoundError, NotADirectoryError, ValueError) as error:
                    raise HTTPException(status_code=404, detail="vfs_path_not_found") from error
                return VfsReadOut(
                    path=path, content_type=effective_ct or content_type,
                    content=raw.decode("utf-8", "replace") if effective_ct else None,
                    size_bytes=size, truncated=bool(effective_ct and size > VFS_HTTP_MAX_BYTES),
                    run_id=run_id, stale=False,
                )
            repo = VfsRunRepo(session, get_object_store(), tenant)
            entry = await repo.read(run_id=run_id, path=path)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"{path} not in run {run_id}")
            effective_ct = _inline_text_content_type(path, entry.content_type)
            if effective_ct:
                raw = await repo.read_bytes(run_id=run_id, path=path)
                truncated = len(raw) > VFS_HTTP_MAX_BYTES
                text = raw[:VFS_HTTP_MAX_BYTES].decode("utf-8", "replace")
                return VfsReadOut(
                    path=path, content_type=effective_ct, content=text,
                    size_bytes=entry.size_bytes, truncated=truncated,
                    run_id=run_id, stale=False)
            # binary → descriptor (no inline content)
            return VfsReadOut(
                path=path, content_type=entry.content_type, content=None,
                size_bytes=entry.size_bytes, truncated=False,
                run_id=run_id, stale=False)
    effective_wf_id = _mount_scope_id(ctx.user_id) if path.startswith("/mount/") else wf_id
    if config.workspace_storage_backend != "posix" and path.startswith("/mount/"):
        await host_mount_bridge.sync_user(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=effective_wf_id,
        action=Action.VIEW,
    )
    if not workspace_path_visible(path, set()) and not workspace_path_visible(
        path, await owned_workspace_chats(session, effective_wf_id, ctx.user_id),
    ):
        raise HTTPException(status_code=404, detail='vfs_path_not_found')
    if config.workspace_storage_backend == "posix":
        from vibecanvas_api.services.file_format import content_type_for
        from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
        from vibecanvas_api.services.workspace_vfs import read_workspace_file
        content_type = content_type_for(path)
        effective_ct = _inline_text_content_type(path, content_type)
        try:
            raw, size = await asyncio.to_thread(
                read_workspace_file, PosixWorkspaceStorage(config.workspace_storage_root),
                tenant_id=context_for_auth(ctx, request).admitted_resource_organization_id or ctx.tenant_id,
                scope_id=effective_wf_id, user_id=ctx.user_id, path=path,
                max_bytes=VFS_HTTP_MAX_BYTES if effective_ct else 0,
            )
        except (FileNotFoundError, NotADirectoryError, ValueError) as error:
            raise HTTPException(status_code=404, detail="vfs_path_not_found") from error
        return VfsReadOut(
            path=path, content_type=effective_ct or content_type,
            content=raw.decode("utf-8", "replace") if effective_ct else None,
            size_bytes=size, truncated=bool(effective_ct and size > VFS_HTTP_MAX_BYTES),
            stale=False,
        )
    entry = await VfsRepo(session, object_store=get_object_store()).read(
        wf_id=effective_wf_id or None, path=path, touch=False)
    if entry is None:
        raise HTTPException(status_code=404, detail="vfs_path_not_found")
    current = (
        await _current(session, ctx.user_id, effective_wf_id)
        if entry.kind == "artifact"
        else None
    )
    stale = bool(entry.kind == "artifact" and entry.wf_version and current
                 and entry.wf_version != current)
    effective_ct = _inline_text_content_type(entry.path, entry.content_type)
    if not effective_ct:
        # binary → descriptor (content lives in the object store, not inline)
        return VfsReadOut(
            path=entry.path, content_type=entry.content_type, content=None,
            size_bytes=entry.size_bytes, truncated=False,
            wf_version=entry.wf_version, stale=stale)
    raw_bytes = await VfsRepo(session, object_store=get_object_store()).read_bytes(
        wf_id=effective_wf_id or None, path=entry.path)
    raw = raw_bytes if raw_bytes is not None else entry.content.encode()
    truncated = len(raw) > VFS_HTTP_MAX_BYTES
    content = raw[:VFS_HTTP_MAX_BYTES].decode("utf-8", "replace")
    return VfsReadOut(
        path=entry.path, content_type=effective_ct, content=content,
        size_bytes=entry.size_bytes, truncated=truncated,
        wf_version=entry.wf_version, stale=stale)


@router.post("/sign", response_model=VfsSignOut)
async def sign_vfs(
    body: VfsSignIn,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsSignOut:
    """Mint a short-lived signed URL for ``/api/v1/vfs/raw`` so the frontend can
    render a VFS media file via ``<img src>`` / ``<video src>`` WITHOUT an
    Authorization header. The tenant baked into the signature comes from the
    AUTH context (never the client) — the raw endpoint scopes its read to it."""
    _validate_raw_path(body.path)
    effective_wf_id = (
        _mount_scope_id(ctx.user_id)
        if body.path.startswith("/mount/") and not body.run_id
        else (body.wf_id or "")
    )
    if body.run_id:
        async with _authorized_run_scope(request, session, ctx, authz, body.run_id):
            return VfsSignOut(url="/api/v1/vfs/run-content?" + urlencode({"run_id": body.run_id, "path": body.path}))
    else:
        await _ensure_vfs_scope_access(
            request=request,
            session=session,
            auth=ctx,
            service=authz,
            wf_id=effective_wf_id,
            action=Action.VIEW,
        )
    if not body.run_id and not workspace_path_visible(body.path, set()) and not workspace_path_visible(
        body.path, await owned_workspace_chats(session, effective_wf_id, ctx.user_id),
    ):
        raise HTTPException(status_code=404, detail='vfs_path_not_found')
    url = sign_vfs_url(
        tenant_id=context_for_auth(ctx, request).admitted_resource_organization_id or ctx.tenant_id,
        path=body.path,
        wf_id=effective_wf_id,
        run_id=body.run_id or "",
        expires_in_s=RAW_URL_TTL_S,
    )
    return VfsSignOut(url=url)


class _FileStreamingResponse(StreamingResponse):
    """Own open file handles through normal completion or client disconnect."""

    def __init__(self, *args, resources: ExitStack, **kwargs):
        super().__init__(*args, **kwargs)
        self.resources = resources

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.resources.close()


async def _serve_vfs_resource(
    *,
    tenant: str,
    wf_id: str,
    run_id: str,
    path: str,
    sandbox_cors: bool = False,
    max_bytes: int = VFS_RAW_MAX_BYTES,
    range_header: str | None = None,
) -> Response | StreamingResponse:
    resources = ExitStack()
    try:
        if config.workspace_storage_backend == "posix":
            from vibecanvas_api.services.file_format import content_type_for
            from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
            from vibecanvas_api.services.workspace_vfs import signed_workspace_scope
            storage = PosixWorkspaceStorage(config.workspace_storage_root)
            try:
                async with session_scope(tenant_id=tenant) as session:
                    if run_id:
                        from vibecanvas_api.services.run_workspace_resolution import resolve_run_workspace
                        identity = await resolve_run_workspace(session, run_id)
                        prefix = "/run/"
                    else:
                        identity, prefix = await signed_workspace_scope(session, tenant_id=tenant, scope_id=wf_id)
                if not path.startswith(prefix):
                    raise ValueError("Invalid workspace path")
                relative = path[len(prefix):]
                if run_id:
                    from vibecanvas_api.services.run_workspace_resolution import run_relative_path
                    relative = run_relative_path(identity, path)
                source = resources.enter_context(storage.open_read(identity, relative))
                info = os.fstat(source.fileno())
            except (FileNotFoundError, NotADirectoryError, ValueError) as error:
                raise HTTPException(status_code=404, detail="vfs_path_not_found") from error
            content_type = content_type_for(path)
            size_bytes = info.st_size
            revision = f"{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_ctime_ns:x}-{info.st_size:x}"
            def stream_bytes(start, end):
                try:
                    source.seek(start)
                    remaining = 0 if size_bytes == 0 else end - start + 1
                    while remaining > 0:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        yield chunk
                        remaining -= len(chunk)
                finally:
                    resources.close()
        else:
            store = get_object_store()
            async with session_scope(tenant_id=tenant) as s:
                if run_id:
                    _validate_run_path(path)
                    row = await s.get(VfsRun, (run_id, path))
                    if row is None:
                        raise HTTPException(status_code=404, detail="vfs_path_not_found")
                else:
                    # Chat workspace snapshots store every durable root (including
                    # /memory and /logs) in VfsArtifact. Only legacy Workflow scratch
                    # uses VfsScratch; choosing by path alone breaks signed Preview URLs.
                    model = (
                        VfsScratch
                        if path.startswith("/memory/")
                        and not is_agent_workspace_scope(wf_id)
                        else VfsArtifact
                    )
                    row = await s.get(model, (wf_id or None, path))
                    if row is None:
                        raise HTTPException(status_code=404, detail="vfs_path_not_found")
                content_type = row.content_type
                size_bytes = int(row.size_bytes)
                object_key = row.object_key
                revision = vfs_row_revision(row)

            if not object_key:
                raise HTTPException(status_code=404, detail="vfs_path_not_found")

            def stream_bytes(start, end):
                return store.iter_bytes(object_key, start=start, end=end)

        served_ct, disposition = _safe_raw_content_type(content_type)
        headers = {
            "Content-Type": served_ct,
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
            "Accept-Ranges": "bytes",
            "ETag": f'"{revision}"',
        }
        if sandbox_cors:
            # The sandbox has an opaque origin and carries no cookies. The opaque
            # URL is itself the short-lived read capability, so wildcard CORS is
            # safe and lets artifact JavaScript fetch JSON/text manifests.
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Expose-Headers"] = (
                "Accept-Ranges, Content-Length, Content-Range"
            )

        status_code = 200
        start = 0
        end = max(0, size_bytes - 1)
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                raise HTTPException(status_code=416, detail="invalid_byte_range")
            raw_start, separator, raw_end = range_header[6:].partition("-")
            try:
                if not separator:
                    raise ValueError
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else size_bytes - 1
                else:
                    suffix = int(raw_end)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, size_bytes - suffix)
                    end = size_bytes - 1
            except ValueError as exc:
                raise HTTPException(status_code=416, detail="invalid_byte_range") from exc
            if start < 0 or start >= size_bytes or end < start:
                raise HTTPException(
                    status_code=416,
                    detail="invalid_byte_range",
                    headers={"Content-Range": f"bytes */{size_bytes}"},
                )
            end = min(end, size_bytes - 1)
            headers["Content-Range"] = f"bytes {start}-{end}/{size_bytes}"
            status_code = 206
        content_length = 0 if size_bytes == 0 else end - start + 1
        headers["Content-Length"] = str(content_length)

        body = stream_bytes(start, end)
        return _FileStreamingResponse(
            body,
            resources=resources,
            headers=headers,
            status_code=status_code,
        )

    except BaseException:
        resources.close()
        raise


@router.get("/resources/{audience}/{capability}/{resource_path:path}")
async def gateway_vfs_resource(
    audience: str,
    capability: str,
    resource_path: str,
    request: Request,
) -> Response:
    scope = verify_vfs_resource_capability(capability)
    if scope is None:
        raise HTTPException(status_code=403, detail="invalid_resource_capability")
    path = "/" + resource_path.lstrip("/")
    _validate_raw_path(path)
    if not vfs_resource_access_allowed(scope, audience=audience, path=path):
        raise HTTPException(status_code=403, detail="resource_capability_scope_mismatch")
    # The opaque capability already binds this request to one read-only VFS
    # scope. Keep path safety generic so newly introduced workspace roots do
    # not require a frontend/backend allowlist release, while internal
    # plumbing (for example __compaction__) remains unreachable.
    if _is_hidden_path(path):
        raise HTTPException(status_code=403, detail="resource_path_not_allowed")
    return await _serve_vfs_resource(
        tenant=scope["tenant"],
        wf_id=scope["wf_id"],
        run_id=scope["run_id"],
        path=path,
        sandbox_cors=True,
        max_bytes=config.storage.vfs_upload_max_bytes,
        range_header=request.headers.get("range"),
    )


@router.get("/run-content")
async def raw_run_content(
    request: Request, run_id: str, path: str,
    ctx: AuthContext = Depends(current_user), session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
):
    _validate_run_path(path)
    async with _authorized_run_scope(request, session, ctx, authz, run_id) as tenant:
        response = await _serve_vfs_resource(tenant=tenant, wf_id="", run_id=run_id,
            path=path, range_header=request.headers.get("range"))
        response.headers["Cache-Control"] = "private, no-store"
        return response


@router.get("/raw")
async def raw_vfs(
    request: Request,
    path: str = Query(...),
    wf_id: str = Query(default=""),
    run_id: str = Query(default=""),
    exp: int = Query(...),
    sig: str = Query(...),
    tenant: str = Query(...),
) -> Response:
    """Serve raw VFS file BYTES authorized SOLELY by the signature (NO Bearer).

    The signed ``tenant`` is the read scope: we open a tenant-bound session for
    exactly that tenant (so RLS confines the lookup to its rows) and read the
    object bytes. Bad/forged/expired signature → 403. Allowlisted media types
    are served inline; anything else is forced to octet-stream + attachment, and
    every response carries ``X-Content-Type-Options: nosniff`` so this endpoint
    can never become an HTML/JS host (XSS) or a MIME-sniffing vector.
    """
    _validate_raw_path(path)
    signed_tenant = verify_vfs_sig(
        tenant=tenant, path=path, wf_id=wf_id or "", run_id=run_id or "",
        exp=exp, sig=sig)
    if signed_tenant is None:
        raise HTTPException(status_code=403, detail="invalid_signature")

    # Read scoped to the SIGNED tenant: a fresh tenant-bound session sets
    # app.tenant_id for RLS so the lookup can only see that tenant's rows.
    return await _serve_vfs_resource(
        tenant=signed_tenant,
        wf_id=wf_id,
        run_id=run_id,
        path=path,
        range_header=request.headers.get("range"),
    )


# User-writable upload folders (depth-0). Maps to the durable `VfsArtifact`
# table via `upsert_artifact_bytes` and is enforced by `_validate_artifact_path`.
_UPLOAD_FOLDERS = ("mount", "data")

_EDITABLE_CONTENT_TYPES = {
    "application/json",
    "json",
    "table/csv",
    "table/tsv",
    "table/jsonl",
    "text",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/python",
    "text/tab-separated-values",
}

_EDITABLE_EXT_CONTENT_TYPES = {
    ".csv": "table/csv",
    ".htm": "text/html",
    ".html": "text/html",
    ".json": "application/json",
    ".jsonl": "table/jsonl",
    ".md": "text/markdown",
    ".py": "text/python",
    ".tsv": "table/tsv",
    ".txt": "text/plain",
}


def _is_user_mount_scope(scope_id: str, user_id: str) -> bool:
    return scope_id == _mount_scope_id(user_id)


async def _reconcile_loaded_chat_workspace(
    *, tenant_id: str, scope_id: str,
) -> bool:
    """Persist one already-loaded Chat sandbox before listing its VFS.

    This is deliberately driven by the file-list read, not by Runtime tool
    names.  The Explorer's manual refresh and its open-panel polling therefore
    observe files written by any CLI, MCP, SDK, or future Runtime without
    cold-starting an idle sandbox.  Turn-end writeback remains the durability
    fallback when nobody has the Explorer open.
    """
    if not is_agent_workspace_scope(scope_id):
        return False
    try:
        manager = get_sandbox_manager()
        loaded = await manager.get_loaded_session(tenant_id, scope_id)
        if loaded is None:
            return False
        await asyncio.wait_for(
            loaded.writeback_vfs(),
            timeout=_LIVE_SANDBOX_RECONCILE_TIMEOUT_S,
        )
        return True
    except TimeoutError:
        logger.info(
            "vfs_live_sandbox_reconcile_timeout",
            wf_id=scope_id,
            timeout_s=_LIVE_SANDBOX_RECONCILE_TIMEOUT_S,
        )
    except Exception:  # pragma: no cover - fail-soft read-side freshness aid
        logger.warning(
            "vfs_live_sandbox_reconcile_failed",
            wf_id=scope_id,
            exc_info=True,
        )
    return False


async def _ensure_writable_vfs_scope(
    *,
    session: AsyncSession,
    user_id: str,
    wf_id: str,
    path: str | None = None,
    folder: str | None = None,
) -> None:
    """Validate writable namespaces after resource authorization.

    Chat workspaces and user mount are durable VFS scopes, not workflow rows.
    Chat workspaces may only expose user-managed `/data`; user mount may only
    expose `/mount`. Workflow resources are not a separate file namespace.
    """
    if _is_user_mount_scope(wf_id, user_id):
        target = folder or ((path or "").strip("/").split("/", 1)[0] if path else "")
        if target != "mount":
            raise HTTPException(status_code=400, detail="invalid_folder")
        return
    if is_agent_workspace_scope(wf_id):
        target = folder or ((path or "").strip("/").split("/", 1)[0] if path else "")
        if target not in {"data", "chats"}:
            raise HTTPException(status_code=400, detail="invalid_folder")
        return
    meta = await WorkflowRepo(session, user_id).get_meta(wf_id)
    if not meta:
        raise HTTPException(status_code=404, detail="workflow_not_found")
    raise HTTPException(status_code=400, detail="workflow_vfs_not_writable")


def _infer_edit_content_type(path: str) -> str:
    return _EDITABLE_EXT_CONTENT_TYPES.get(os.path.splitext(path.lower())[1], "text/plain")


def _validate_editable_content_type(content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("text/") or ct in _EDITABLE_CONTENT_TYPES:
        return ct
    raise HTTPException(status_code=400, detail="content_type_not_editable")


async def _mirror_live_sandbox_write(tenant_id: str, wf_id: str, path: str,
                                     data: bytes) -> None:
    """Best-effort sync from durable VFS write to an already-running sandbox."""
    try:
        await get_sandbox_manager().mirror_vfs_write(tenant_id, wf_id, path, data)
    except Exception:  # pragma: no cover - fail-soft UI consistency aid
        logger.warning("vfs_live_sandbox_mirror_failed", wf_id=wf_id, path=path,
                       exc_info=True)


async def _mirror_live_sandbox_delete(
    tenant_id: str, wf_id: str, path: str,
) -> None:
    try:
        await get_sandbox_manager().mirror_vfs_delete(tenant_id, wf_id, path)
    except Exception:  # pragma: no cover - durable VFS remains authoritative
        logger.warning(
            "vfs_live_sandbox_delete_failed", wf_id=wf_id, path=path, exc_info=True,
        )


async def _mirror_live_sandbox_rename(
    tenant_id: str, wf_id: str, old_path: str, new_path: str,
) -> None:
    try:
        await get_sandbox_manager().mirror_vfs_rename(
            tenant_id, wf_id, old_path, new_path,
        )
    except Exception:  # pragma: no cover - durable VFS remains authoritative
        logger.warning(
            "vfs_live_sandbox_rename_failed",
            wf_id=wf_id,
            old_path=old_path,
            new_path=new_path,
            exc_info=True,
        )


async def _write_authorized_file(*, request, auth, session, scope_id: str,
                                 path: str, data: bytes, content_type: str) -> bool:
    """Persist a file after the caller's current UPDATE authorization check."""
    tenant = context_for_auth(auth, request).admitted_resource_organization_id or auth.tenant_id
    if config.workspace_storage_backend == "posix":
        from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
        from vibecanvas_api.services.workspace_vfs import write_workspace_file
        return await asyncio.to_thread(
            write_workspace_file, PosixWorkspaceStorage(config.workspace_storage_root),
            tenant_id=tenant, scope_id=scope_id, user_id=auth.user_id, path=path, data=data,
        )
    replaced = await VfsRepo(session, object_store=get_object_store()).upsert_artifact_bytes(
        wf_id=scope_id, tenant=tenant, path=path, data=data, content_type=content_type)
    await _mirror_live_sandbox_write(tenant, scope_id, path, data)
    return replaced


@router.post("/upload", response_model=VfsUploadOut)
async def upload_file(
    request: Request,
    wf_id: str = Query(...),
    file: UploadFile = File(...),
    folder: str = Query(...),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsUploadOut:
    """Upload a file to `/mount` or a Chat's `/data` namespace.

    Both are durable artifact prefixes. Explicit-path, last-writer-wins: re-uploading the
    same name overwrites in place and returns `replaced: true`.

    400 for a workflow scope, bad `folder`, or bad/traversal filename; 413 for
    an oversized upload.
    """
    if folder not in _UPLOAD_FOLDERS:
        raise HTTPException(status_code=400, detail="invalid_folder")

    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    await _ensure_writable_vfs_scope(
        session=session, user_id=ctx.user_id, wf_id=wf_id, folder=folder)

    name = os.path.basename(file.filename or "").strip()
    try:
        safe = _validate_artifact_path(f"/{folder}/{name}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_filename")

    data = await file.read()
    if len(data) > config.storage.vfs_upload_max_bytes:
        raise HTTPException(status_code=413, detail="file_too_large")
    await require_clean_upload(data)

    content_type = file.content_type or "application/octet-stream"
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=wf_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    replaced = await _write_authorized_file(
        request=request, auth=ctx, session=session, scope_id=wf_id,
        path=safe, data=data, content_type=content_type)
    return VfsUploadOut(path=safe, size_bytes=len(data),
                        content_type=content_type, replaced=replaced)


@router.put("/content", response_model=VfsWriteOut)
async def write_vfs_content(
    body: VfsWriteIn,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsWriteOut:
    """Overwrite a user-managed durable VFS file with UTF-8 text.

    The editor surface deliberately does not write agent-owned `/memory` files,
    run-tier files, or binary payloads. Binary ingress remains the multipart
    upload route.
    """
    safe = _validate_user_managed_path(body.path)
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=body.wf_id,
        action=Action.UPDATE,
    )
    await _ensure_writable_vfs_scope(
        session=session, user_id=ctx.user_id, wf_id=body.wf_id, path=safe)
    content_type = _validate_editable_content_type(
        body.content_type or _infer_edit_content_type(safe))
    data = body.content.encode("utf-8")
    if len(data) > config.storage.vfs_upload_max_bytes:
        raise HTTPException(status_code=413, detail="file_too_large")

    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=body.wf_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    replaced = await _write_authorized_file(
        request=request, auth=ctx, session=session, scope_id=body.wf_id,
        path=safe, data=data, content_type=content_type)
    return VfsWriteOut(path=safe, size_bytes=len(data),
                       content_type=content_type, replaced=replaced)


@router.put("/bytes", response_model=VfsWriteOut)
async def write_vfs_bytes(
    body: VfsWriteBytesIn,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsWriteOut:
    """Overwrite a user-managed durable VFS file with raw bytes.

    Used by binary browser editors such as xlsx. Scope remains restricted to
    `/mount` and `/data`; agent-owned memory/logs/run files are not writable.
    """
    safe = _validate_user_managed_path(body.path)
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=body.wf_id,
        action=Action.UPDATE,
    )
    await _ensure_writable_vfs_scope(
        session=session, user_id=ctx.user_id, wf_id=body.wf_id, path=safe)
    try:
        data = base64.b64decode(body.data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="invalid_base64")
    if len(data) > config.storage.vfs_upload_max_bytes:
        raise HTTPException(status_code=413, detail="file_too_large")
    content_type = (body.content_type or "application/octet-stream").split(";")[0].strip()
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=body.wf_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    replaced = await _write_authorized_file(
        request=request, auth=ctx, session=session, scope_id=body.wf_id,
        path=safe, data=data, content_type=content_type)
    return VfsWriteOut(path=safe, size_bytes=len(data),
                       content_type=content_type, replaced=replaced)


def _validate_user_managed_path(path: str) -> str:
    """Validate a user-managed durable VFS path for delete/rename. Reuses the
    upload-route boundary `_validate_artifact_path`: must live under a
    user-writable durable prefix (`/mount/`, `/data/`), no traversal/control
    chars. This deliberately EXCLUDES agent-owned system surfaces (`/memory`,
    run-tier paths) — the user's "cloud computer" manages its OWN durable files.
    A folder prefix (e.g. `/data/sub`, no trailing slash) is accepted; the
    repo expands it to its children. 400 on rejection."""
    try:
        return _validate_artifact_path(path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_path")


@router.delete("", response_model=VfsDeleteOut)
async def delete_vfs(
    request: Request,
    path: str = Query(...),
    wf_id: str = Query(...),
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsDeleteOut:
    """Delete a durable VFS file (or a folder prefix and all its children) the
    user owns. Restricted to `/mount` and `/data`, the same allowlist as upload.
    404 if nothing matched.
    """
    safe = _validate_user_managed_path(path)
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=wf_id,
        action=Action.UPDATE,
    )
    await _ensure_writable_vfs_scope(
        session=session, user_id=ctx.user_id, wf_id=wf_id, path=safe)
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=wf_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    tenant = context_for_auth(ctx, request).admitted_resource_organization_id or ctx.tenant_id
    if config.workspace_storage_backend == "posix":
        from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
        from vibecanvas_api.services.workspace_vfs import remove_workspace_path
        deleted = await asyncio.to_thread(
            remove_workspace_path, PosixWorkspaceStorage(config.workspace_storage_root),
            tenant_id=tenant, scope_id=wf_id, user_id=ctx.user_id, path=safe,
        )
    else:
        repo = VfsRepo(session, object_store=get_object_store())
        deleted = await repo.delete_artifact(wf_id=wf_id, tenant=tenant, path=safe)
        if deleted:
            await _mirror_live_sandbox_delete(tenant, wf_id, safe)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="vfs_path_not_found")
    return VfsDeleteOut(deleted=deleted)


@router.post("/rename", response_model=VfsRenameOut)
async def rename_vfs(
    body: VfsRenameIn,
    request: Request,
    ctx: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    authz: AuthzService = Depends(get_authz_service),
) -> VfsRenameOut:
    """Rename/move a durable VFS file or folder within the user-writable
    durable prefixes (`/mount`, `/data`). 400 on an invalid source/destination
    path; 404 if the source doesn't exist.
    """
    old_path = _validate_user_managed_path(body.old_path)
    new_path = _validate_user_managed_path(body.new_path)
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=body.wf_id,
        action=Action.UPDATE,
    )
    await _ensure_writable_vfs_scope(
        session=session, user_id=ctx.user_id, wf_id=body.wf_id, path=old_path)
    await _ensure_writable_vfs_scope(
        session=session, user_id=ctx.user_id, wf_id=body.wf_id, path=new_path)
    await _ensure_vfs_scope_access(
        request=request,
        session=session,
        auth=ctx,
        service=authz,
        wf_id=body.wf_id,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    if config.workspace_storage_backend == "posix":
        from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage
        from vibecanvas_api.services.workspace_vfs import rename_workspace_path
        try:
            await asyncio.to_thread(
                rename_workspace_path, PosixWorkspaceStorage(config.workspace_storage_root),
                tenant_id=context_for_auth(ctx, request).admitted_resource_organization_id or ctx.tenant_id,
                scope_id=body.wf_id, user_id=ctx.user_id, source=old_path, destination=new_path,
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="vfs_path_not_found") from error
        except (ValueError, NotADirectoryError, IsADirectoryError) as error:
            raise HTTPException(status_code=400, detail="invalid_path") from error
        return VfsRenameOut(path=new_path)
    repo = VfsRepo(session, object_store=get_object_store())
    try:
        await repo.rename_artifact(
            wf_id=body.wf_id, tenant=ctx.tenant_id,
            old_path=old_path, new_path=new_path)
    except ValueError as e:
        # The repo raises ValueError both for a bad new_path (already validated
        # here) and a missing source — map the latter to 404.
        if "not found" in str(e):
            raise HTTPException(status_code=404, detail="vfs_path_not_found")
        raise HTTPException(status_code=400, detail="invalid_path")
    await _mirror_live_sandbox_rename(
        ctx.tenant_id, body.wf_id, old_path, new_path,
    )
    return VfsRenameOut(path=new_path)
