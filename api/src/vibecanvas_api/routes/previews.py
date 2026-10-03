"""Unified Preview descriptor and optimistic text-write HTTP surface."""
from __future__ import annotations

import asyncio
import io
import mimetypes
import os
import zipfile
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
    context_for_auth,
    get_authz_service,
    principal_for_auth,
    scope_authz_service,
)
from vibecanvas_api.authorization.service import AuthzService
from vibecanvas_api.authorization.stream_guard import (
    authorization_lease_is_valid,
)
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.drawio import (
    DRAWIO_MIME_TYPE,
    MAX_DRAWIO_SOURCE_BYTES,
    inspect_drawio,
)
from vibecanvas_api.schemas.preview import (
    ProjectFileRefV1,
    FileRefV1,
    MountFileRefV1,
    PreviewCapabilities,
    PreviewContent,
    PreviewDescriptorV1,
    PreviewErrorInfo,
    PreviewFileWriteOut,
    PreviewFileWriteV1,
    PreviewResolveBody,
    PreviewResourceMount,
    PreviewResourceSession,
    PreviewRendition,
    PreviewTextMetadata,
    RunFileRefV1,
)
from vibecanvas_api.services.chat_workspace import (
    project_workspace_scope_id,
)
from vibecanvas_api.services.file_revision import (
    vfs_content_revision,
    vfs_row_revision,
)
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.services.office_preview import (
    OfficePreviewError,
    render_office_preview_pdf,
)
from vibecanvas_api.services.preview_resource_policy import (
    html_vfs_read_rules,
    markdown_vfs_read_rules,
    rules_for_root,
)
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.user_mount_workspace import mount_scope_id
from vibecanvas_api.services.vfs_signing import (
    issue_vfs_resource_capability,
    sign_vfs_url,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import ChatProject
from vibecanvas_api.storage.models import (
    VfsArtifact,
    VfsArtifactEvent,
    VfsRun,
)
from vibecanvas_api.storage.vfs_store import VfsRepo
from vibecanvas_api.streaming.sse import format_event

router = APIRouter(prefix="/api/v1/previews", tags=["previews"])

INLINE_TEXT_BYTES = 1024 * 1024
LARGE_TEXT_SAMPLE_BYTES = 2 * 1024 * 1024
EDITABLE_TEXT_BYTES = 10 * 1024 * 1024
DETECTION_BYTES = 64 * 1024
OFFICE_AUTO_BYTES = 10 * 1024 * 1024
OFFICE_MANUAL_BYTES = 50 * 1024 * 1024
SPREADSHEET_MAX_BYTES = 10 * 1024 * 1024
ARCHIVE_TOTAL_BYTES = 500 * 1024 * 1024
ARCHIVE_ENTRY_BYTES = 100 * 1024 * 1024
ARCHIVE_ENTRIES = 10_000
ARCHIVE_RATIO = 100
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


TEXT_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".graphql", ".h",
    ".hpp", ".ini", ".java", ".js", ".jsx", ".json", ".jsonl", ".kt",
    ".kts", ".log", ".lua", ".php", ".py", ".pyw", ".rb", ".rs",
    ".scala", ".sh", ".sql", ".svelte", ".swift", ".toml", ".ts",
    ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml", ".csv", ".tsv",
}
LEGACY_OFFICE_EXTENSIONS = {
    ".doc", ".dot", ".odt", ".rtf", ".ppt", ".pps", ".ppsx", ".odp",
    ".xls", ".xlsb", ".xlsm", ".ods",
}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}
EDITABLE_CONTENT_TYPES = {
    "application/json", "application/xml", "application/x-yaml",
    "text/html", "text/markdown", "text/plain", "text/x-python", "text/yaml",
}


@dataclass(slots=True)
class _ResolvedFile:
    file_ref: FileRefV1
    row: VfsArtifact | VfsRun
    scope_id: str
    run_id: str


def _file_resource(file_ref: FileRefV1, auth: AuthContext) -> ResourceRef:
    if isinstance(file_ref, RunFileRefV1):
        return ResourceRef(
            ResourceType.VFS_RUN,
            file_ref.run_id,
            auth.active_organization_id,
        )
    return ResourceRef(
        ResourceType.STORAGE_ROOT,
        auth.user_id,
        auth.active_organization_id,
    )


async def _authorize_file_ref(
    *,
    file_ref: FileRefV1,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    action: Action,
    consistency: ConsistencyPreference = ConsistencyPreference.MINIMIZE_LATENCY,
) -> None:
    decision = await service.check(
        principal_for_auth(auth),
        action,
        _file_resource(file_ref, auth),
        context_for_auth(auth, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail="preview_file_not_found")


async def _resolve_file(
    *,
    file_ref: FileRefV1,
    auth: AuthContext,
    session: AsyncSession,
    lock: bool = False,
) -> _ResolvedFile:
    if isinstance(file_ref, ProjectFileRefV1):
        project = (await session.execute(
            select(ChatProject.project_id, ChatProject.workflow_id).where(
                ChatProject.project_id == file_ref.project_id,
                ChatProject.creator_user_id == auth.user_id,
                ChatProject.deleted_at.is_(None),
            )
        )).one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="preview_file_not_found")
        scope_id = project_workspace_scope_id(project.project_id)
        query = select(VfsArtifact).where(
            VfsArtifact.scope_id == scope_id,
            VfsArtifact.path == file_ref.path,
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        run_id = ""
    elif isinstance(file_ref, MountFileRefV1):
        scope_id = mount_scope_id(auth.user_id)
        query = select(VfsArtifact).where(
            VfsArtifact.scope_id == scope_id,
            VfsArtifact.path == file_ref.path,
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        run_id = ""
    elif isinstance(file_ref, RunFileRefV1):
        scope_id = ""
        query = select(VfsRun).where(
            VfsRun.run_id == file_ref.run_id,
            VfsRun.path == file_ref.path,
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        run_id = file_ref.run_id
    else:  # pragma: no cover - Pydantic discriminator is exhaustive
        raise HTTPException(status_code=400, detail="invalid_file_ref")
    if row is None or not row.object_key:
        raise HTTPException(status_code=404, detail="preview_file_not_found")
    return _ResolvedFile(file_ref=file_ref, row=row, scope_id=scope_id, run_id=run_id)


def _safe_inline_url(*, resolved: _ResolvedFile, auth: AuthContext) -> str:
    return sign_vfs_url(
        tenant_id=auth.tenant_id,
        path=resolved.file_ref.path,
        wf_id=resolved.scope_id,
        run_id=resolved.run_id,
        expires_in_s=300,
    )


def _office_rendition_url(
    *, file_ref: FileRefV1, revision: str
) -> str:
    params: dict[str, str] = {
        "scope": file_ref.scope,
        "path": file_ref.path,
        "revision": revision,
    }
    if isinstance(file_ref, ProjectFileRefV1):
        params["project_id"] = file_ref.project_id
    elif isinstance(file_ref, RunFileRefV1):
        params["run_id"] = file_ref.run_id
    return "/api/v1/previews/office-rendition?" + urlencode(params)


def _preview_error(code: str, **params) -> PreviewErrorInfo:
    return PreviewErrorInfo(code=code, params=params)


def _zip_error(data: bytes) -> PreviewErrorInfo | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > ARCHIVE_ENTRIES:
                return _preview_error(
                    "too_many_archive_entries",
                    actual=len(infos),
                    limit=ARCHIVE_ENTRIES,
                )
            total = 0
            compressed = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    return _preview_error("encrypted_file")
                if info.file_size > ARCHIVE_ENTRY_BYTES:
                    return _preview_error(
                        "archive_entry_too_large",
                        actualBytes=info.file_size,
                        limitBytes=ARCHIVE_ENTRY_BYTES,
                    )
                total += info.file_size
                compressed += max(1, info.compress_size)
                if total > ARCHIVE_TOTAL_BYTES:
                    return _preview_error(
                        "archive_expanded_too_large",
                        actualBytes=total,
                        limitBytes=ARCHIVE_TOTAL_BYTES,
                    )
            if total / max(1, compressed) > ARCHIVE_RATIO:
                return _preview_error(
                    "archive_compression_ratio_too_high",
                    limit=ARCHIVE_RATIO,
                )
    except (zipfile.BadZipFile, OSError):
        return _preview_error("invalid_file")
    return None


def _text_metadata(data: bytes) -> tuple[str | None, PreviewTextMetadata | None, str | None]:
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    try:
        value = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "unsupported_text_encoding"
    crlf = value.count("\r\n")
    lf = value.count("\n") - crlf
    newline = "CRLF" if crlf > lf else "LF"
    mixed = crlf > 0 and lf > 0
    return value, PreviewTextMetadata(bom=bom, newline=newline, mixedNewlines=mixed), None


def _detect(path: str, content_type: str, data: bytes) -> tuple[str, str]:
    ext = os.path.splitext(path.lower())[1]
    mime = (content_type or "").split(";", 1)[0].lower()
    if ext == ".drawio" or mime == DRAWIO_MIME_TYPE:
        return "drawio", DRAWIO_MIME_TYPE
    if data.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf", "application/pdf"
    if ext in {".csv"} or mime in {"table/csv", "text/csv"}:
        return "csv", "text/csv"
    if ext in {".tsv"} or mime in {"table/tsv", "text/tab-separated-values"}:
        return "tsv", "text/tab-separated-values"
    if ext in {".jsonl", ".ndjson"} or mime in {"table/jsonl", "application/x-ndjson"}:
        return "jsonl", "application/x-ndjson"
    if ext == ".md" or mime in {"text/markdown", "text/x-markdown"}:
        return "markdown", "text/markdown"
    if ext in {".html", ".htm"} or mime in {"text/html", "application/xhtml+xml"}:
        return "html", "text/html"
    if ext == ".docx" or "wordprocessingml.document" in mime:
        return "docx", mime or "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".pptx" or "presentationml.presentation" in mime:
        return "pptx", mime or "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if ext == ".xlsx" or "spreadsheetml.sheet" in mime:
        return "spreadsheet", mime or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext in LEGACY_OFFICE_EXTENSIONS:
        return "legacy_office", mime or mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive", mime or "application/octet-stream"
    if mime.startswith("image/"):
        return "image", mime
    if mime.startswith("audio/"):
        return "audio", mime
    if mime.startswith("video/"):
        return "video", mime
    if (
        ext in TEXT_EXTENSIONS
        or mime.startswith("text/")
        or mime in {"application/json", "application/xml", "application/x-yaml"}
        or mime.startswith("table/")
    ):
        return "text", mime or mimetypes.guess_type(path)[0] or "text/plain"
    return "unsupported", mime or mimetypes.guess_type(path)[0] or "application/octet-stream"


def _descriptor(
    *,
    resolved: _ResolvedFile,
    auth: AuthContext,
    data: bytes,
) -> PreviewDescriptorV1:
    row = resolved.row
    path = resolved.file_ref.path
    detected, content_type = _detect(path, row.content_type, data)
    size = int(row.size_bytes)
    error = None
    text_meta = None
    content = None
    renderer = detected
    load_policy = "unsupported"
    editable = False
    rendition = None

    diagram = None
    if detected == "drawio":
        renderer = "drawio"
        load_policy = "range"
        diagram = inspect_drawio(data).preview_metadata()
        content = PreviewContent(
            url=_safe_inline_url(resolved=resolved, auth=auth),
            rangeSupported=True,
        )
    elif (
        detected in {"csv", "tsv", "jsonl"}
        and size > SPREADSHEET_MAX_BYTES
    ):
        renderer = "unsupported"
        error = _preview_error(
            "file_too_large",
            actualBytes=size,
            limitBytes=SPREADSHEET_MAX_BYTES,
        )
    elif detected in {"text", "markdown", "html", "csv", "tsv", "jsonl"}:
        text_value, text_meta, text_error = _text_metadata(data)
        renderer = (
            detected
            if detected in {"markdown", "html"}
            else "spreadsheet"
            if detected in {"csv", "tsv", "jsonl"}
            else "text"
        )
        if text_value is not None:
            editable = (
                detected in {"text", "markdown", "html"}
                and not isinstance(resolved.file_ref, RunFileRefV1)
                and (
                    not isinstance(resolved.file_ref, ProjectFileRefV1)
                    or resolved.file_ref.path.startswith(("/data/", "/chats/"))
                )
                and size <= EDITABLE_TEXT_BYTES
            )
            if size <= INLINE_TEXT_BYTES:
                load_policy = "inline"
                content = PreviewContent(
                    inlineText=text_value,
                    # Inline content avoids a preview fetch; retain the signed
                    # original-byte URL for downloading small text/CSV files.
                    url=_safe_inline_url(resolved=resolved, auth=auth),
                )
            else:
                load_policy = "stream"
                content = PreviewContent(
                    url=_safe_inline_url(resolved=resolved, auth=auth),
                    truncated=True,
                    rangeSupported=True,
                )
        else:
            renderer = "unsupported"
            error = _preview_error(text_error or "invalid_file")
    elif detected in {"pdf", "image", "audio", "video"}:
        load_policy = "range"
        content = PreviewContent(
            url=_safe_inline_url(resolved=resolved, auth=auth),
            rangeSupported=True,
        )
    elif detected in {"docx", "pptx", "spreadsheet"}:
        size_limit = (
            SPREADSHEET_MAX_BYTES
            if detected == "spreadsheet"
            else OFFICE_MANUAL_BYTES
        )
        if size > size_limit:
            renderer = "unsupported"
            load_policy = "unsupported"
            error = _preview_error(
                "file_too_large",
                actualBytes=size,
                limitBytes=size_limit,
            )
        else:
            error = _zip_error(data)
            if error:
                renderer = "unsupported"
            else:
                load_policy = (
                    "inline"
                    if detected == "spreadsheet" or size <= OFFICE_AUTO_BYTES
                    else "manual"
                )
                content = PreviewContent(
                    url=_safe_inline_url(resolved=resolved, auth=auth),
                    rangeSupported=True,
                )
                if detected in {"docx", "pptx"}:
                    revision = vfs_row_revision(row)
                    rendition = PreviewRendition(
                        format="pdf",
                        contentType="application/pdf",
                        url=_office_rendition_url(
                            file_ref=resolved.file_ref,
                            revision=revision,
                        ),
                        sourceRevision=revision,
                    )
    elif detected == "legacy_office":
        renderer = "unsupported"
        error = _preview_error(
            "unsupported_file_type",
            extension=os.path.splitext(path.lower())[1],
        )
    else:
        renderer = "unsupported"
        error = _preview_error(
            "archive_preview_not_supported"
            if detected == "archive"
            else "unsupported_file_type"
        )

    # Download remains available for unsupported files. This URL is still
    # scoped, short-lived and object-key-free; renderers may ignore it.
    if content is None:
        content = PreviewContent(
            url=_safe_inline_url(resolved=resolved, auth=auth),
            rangeSupported=True,
        )
    preview = renderer != "unsupported"
    return PreviewDescriptorV1(
        fileRef=resolved.file_ref,
        name=path.rsplit("/", 1)[-1],
        sizeBytes=size,
        contentType=content_type,
        detectedType=detected,
        revision=vfs_row_revision(row),
        renderer=renderer,
        loadPolicy=load_policy,
        capabilities=PreviewCapabilities(
            preview=preview,
            edit=editable,
            download=True,
        ),
        content=content,
        rendition=rendition,
        text=text_meta,
        diagram=diagram,
        error=error,
    )


def _object_prefix(object_key: str, size_bytes: int, limit: int) -> bytes:
    if size_bytes <= 0 or limit <= 0:
        return b""
    end = min(size_bytes, limit) - 1
    return b"".join(
        get_object_store().iter_bytes(object_key, start=0, end=end)
    )


def _event_file_ref(
    *,
    scope: Literal["project", "mount", "run"],
    path: str,
    project_id: str | None,
    run_id: str | None,
) -> FileRefV1:
    """Build the validated FileRef represented by an SSE query string."""
    try:
        if scope == "project":
            return ProjectFileRefV1(
                schema_version=1,
                scope="project",
                project_id=project_id or "",
                path=path,
            )
        if scope == "mount":
            return MountFileRefV1(
                schema_version=1,
                scope="mount",
                path=path,
            )
        return RunFileRefV1(
            schema_version=1,
            scope="run",
            run_id=run_id or "",
            path=path,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid_preview_event_subscription",
        ) from exc


async def _event_scope(
    *, file_ref: FileRefV1, user_id: str, session: AsyncSession
) -> tuple[str, str]:
    if isinstance(file_ref, ProjectFileRefV1):
        project = (await session.execute(
            select(ChatProject.project_id, ChatProject.workflow_id).where(
                ChatProject.project_id == file_ref.project_id,
                ChatProject.creator_user_id == user_id,
                ChatProject.deleted_at.is_(None),
            )
        )).one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="preview_file_not_found")
        return "artifact", project_workspace_scope_id(project.project_id)
    if isinstance(file_ref, MountFileRefV1):
        return "artifact", mount_scope_id(user_id)
    return "run", file_ref.run_id


def _event_paths(path: str) -> tuple[str, ...]:
    return (path,)


@router.get("/events")
async def stream_preview_file_events(
    request: Request,
    scope: Literal["project", "mount", "run"] = Query(),
    path: str = Query(min_length=1),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    auth: AuthContext = Depends(current_user),
) -> StreamingResponse:
    """Reconcile once, then follow durable changes for one Preview FileRef.

    The browser never polls descriptors. Database triggers append a cursor in
    the same transaction as every VFS content mutation, so any API worker can
    replay changes after reconnect or another worker's write. The initial
    ``preview_ready`` frame carries the authoritative revision and closes the
    resolve/subscribe race without replaying unbounded historical changes.
    """

    file_ref = _event_file_ref(
        scope=scope,
        path=path,
        project_id=project_id,
        run_id=run_id,
    )
    watched_paths = _event_paths(path)
    raw_cursor = request.headers.get("last-event-id", "")
    try:
        cursor = max(0, int(raw_cursor or 0))
    except ValueError:
        cursor = 0

    async with session_scope(tenant_id=auth.tenant_id, user_id=auth.user_id) as initial_session:
        scope_kind, storage_scope_id = await _event_scope(
            file_ref=file_ref,
            user_id=auth.user_id,
            session=initial_session,
        )
        # A StreamingResponse delays FastAPI dependency teardown until the
        # client disconnects. Building AuthzService through the ordinary
        # request-scoped tenant_db dependency would therefore retain an idle
        # transaction for the entire Preview subscription. Authorize and
        # reconcile inside this explicitly bounded session, then close it
        # before returning the long-lived response.
        initial_service = scope_authz_service(
            authz_service_for_session(
                session=initial_session,
                organization_id=auth.active_organization_id,
                openfga_client=getattr(
                    request.app.state, "openfga_client", None
                ),
            ),
            session=initial_session,
            auth=auth,
            request=request,
        )
        await _authorize_file_ref(
            file_ref=file_ref,
            request=request,
            auth=auth,
            service=initial_service,
            action=Action.VIEW,
        )
        # For a fresh subscription, establish the tail cursor before resolving
        # the current row. A write in either side of these statements is then
        # caught by the ready revision or by replay after the cursor.
        latest_event = (
            await initial_session.execute(
                select(VfsArtifactEvent)
                .where(
                    VfsArtifactEvent.scope_kind == scope_kind,
                    VfsArtifactEvent.scope_id == storage_scope_id,
                    VfsArtifactEvent.path.in_(watched_paths),
                )
                .order_by(VfsArtifactEvent.event_id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if cursor == 0:
            cursor = int(latest_event.event_id) if latest_event is not None else 0
        resolved = await _resolve_file(
            file_ref=file_ref,
            auth=auth,
            session=initial_session,
        )
        current_revision = vfs_row_revision(resolved.row)
        current_commit_event = (
            latest_event
            if latest_event is not None
            and latest_event.event_type == "upsert"
            and latest_event.content_revision
            and vfs_content_revision(latest_event.content_revision) == current_revision
            else None
        )

    async def event_stream():
        nonlocal cursor
        stream_resource = _file_resource(file_ref, auth)
        next_authorization_check = 0.0

        async def authorized() -> bool:
            nonlocal next_authorization_check
            now = asyncio.get_running_loop().time()
            if now < next_authorization_check:
                return True
            allowed = await authorization_lease_is_valid(
                auth=auth,
                openfga_client=getattr(
                    request.app.state, "openfga_client", None
                ),
                resource=stream_resource,
                action=Action.VIEW,
            )
            next_authorization_check = now + 5.0
            return allowed

        if not await authorized():
            return
        yield format_event(
            "preview_ready",
            {
                "event_id": cursor,
                "path": path,
                "revision": current_revision,
                "committed_at": (
                    current_commit_event.created_at.isoformat()
                    if current_commit_event is not None
                    else None
                ),
                "committed_event_id": (
                    int(current_commit_event.event_id)
                    if current_commit_event is not None
                    else None
                ),
            },
            event_id=cursor,
        )
        idle_ticks = 0
        while not await request.is_disconnected():
            if not await authorized():
                return
            async with session_scope(tenant_id=auth.tenant_id, user_id=auth.user_id) as event_session:
                events = (
                    await event_session.execute(
                        select(VfsArtifactEvent)
                        .where(
                            VfsArtifactEvent.scope_kind == scope_kind,
                            VfsArtifactEvent.scope_id == storage_scope_id,
                            VfsArtifactEvent.path.in_(watched_paths),
                            VfsArtifactEvent.event_id > cursor,
                        )
                        .order_by(VfsArtifactEvent.event_id)
                        .limit(100)
                    )
                ).scalars().all()
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = int(event.event_id)
                    is_source = event.path == path
                    revision = (
                        vfs_content_revision(event.content_revision)
                        if is_source
                        and event.event_type == "upsert"
                        and event.content_revision
                        else None
                    )
                    yield format_event(
                        "preview_file",
                        {
                            "event_id": cursor,
                            "path": path,
                            "changed_path": event.path,
                            "event_type": event.event_type,
                            "revision": revision,
                            "derived": not is_source,
                            "created_at": event.created_at.isoformat(),
                        },
                        event_id=cursor,
                    )
                continue
            idle_ticks += 1
            if idle_ticks >= 15:
                idle_ticks = 0
                yield b": heartbeat\n\n"
            # Only the backend observes its durable event cursor. The browser
            # receives frames on change and never refetches on a timer.
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/resolve", response_model=PreviewDescriptorV1)
async def resolve_preview(
    body: PreviewResolveBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> PreviewDescriptorV1:
    await _authorize_file_ref(
        file_ref=body.file_ref,
        request=request,
        auth=auth,
        service=service,
        action=Action.VIEW,
    )
    resolved = await _resolve_file(
        file_ref=body.file_ref,
        auth=auth,
        session=session,
    )
    size = int(resolved.row.size_bytes)
    prefix = _object_prefix(resolved.row.object_key, size, DETECTION_BYTES)
    detected, _content_type = _detect(
        resolved.file_ref.path,
        resolved.row.content_type,
        prefix,
    )
    if detected == "drawio":
        data = _object_prefix(
            resolved.row.object_key,
            size,
            MAX_DRAWIO_SOURCE_BYTES + 1,
        )
    elif detected in {"text", "markdown", "html", "csv", "tsv", "jsonl"}:
        # Full strict UTF-8 validation is required for bounded text and
        # structured-table previews. Source-like text remains editable up to
        # the explicit editor limit; larger files use a bounded read-only
        # sample so Preview remains responsive.
        limit = size if size <= EDITABLE_TEXT_BYTES else LARGE_TEXT_SAMPLE_BYTES
        data = _object_prefix(resolved.row.object_key, size, limit)
    elif (
        detected in {"docx", "pptx"}
        and size <= OFFICE_MANUAL_BYTES
    ) or (
        detected == "spreadsheet"
        and size <= SPREADSHEET_MAX_BYTES
    ):
        # OOXML preflight must inspect the complete bounded ZIP.
        data = get_object_store().fetch_bytes(resolved.row.object_key)
    else:
        data = prefix
    descriptor = _descriptor(resolved=resolved, auth=auth, data=data)
    return descriptor


@router.get("/office-rendition")
async def office_preview_rendition(
    request: Request,
    scope: Literal["project", "mount", "run"] = Query(),
    path: str = Query(min_length=1),
    revision: str = Query(min_length=1),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> Response:
    """Render an authorized DOCX/PPTX revision as a read-only PDF.

    This endpoint intentionally requires the normal Bearer session.  The
    browser fetches the complete bounded rendition before handing it to PDF.js,
    while the original short-lived VFS URL remains the download target.
    """

    file_ref = _event_file_ref(
        scope=scope,
        path=path,
        project_id=project_id,
        run_id=run_id,
    )
    await _authorize_file_ref(
        file_ref=file_ref,
        request=request,
        auth=auth,
        service=service,
        action=Action.VIEW,
    )
    resolved = await _resolve_file(
        file_ref=file_ref,
        auth=auth,
        session=session,
    )
    current_revision = vfs_row_revision(resolved.row)
    if revision != current_revision:
        raise HTTPException(status_code=409, detail="preview_revision_conflict")
    size = int(resolved.row.size_bytes)
    if size > OFFICE_MANUAL_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")
    data = get_object_store().fetch_bytes(resolved.row.object_key)
    detected, _content_type = _detect(
        resolved.file_ref.path,
        resolved.row.content_type,
        data,
    )
    if detected not in {"docx", "pptx"}:
        raise HTTPException(status_code=415, detail="unsupported_office_preview_type")
    try:
        pdf = await asyncio.to_thread(
            render_office_preview_pdf,
            data,
            os.path.splitext(resolved.file_ref.path)[1],
        )
    except OfficePreviewError as exc:
        status = 503 if exc.code == "office_renderer_unavailable" else 422
        raise HTTPException(status_code=status, detail=exc.code) from exc
    filename = resolved.file_ref.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": (
                "inline; filename*=UTF-8''" + quote(f"{filename}.pdf", safe="")
            ),
            "ETag": f'"{current_revision}:pdf"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/resource-session", response_model=PreviewResourceSession)
async def create_preview_resource_session(
    body: PreviewResolveBody,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> PreviewResourceSession:
    """Mint opaque resource mounts for a file Preview's local dependencies.

    FileRef ownership is resolved before capabilities are issued. The iframe
    only receives virtual path mounts and never learns workspace/object-store
    identifiers.
    """
    await _authorize_file_ref(
        file_ref=body.file_ref,
        request=request,
        auth=auth,
        service=service,
        action=Action.VIEW,
    )
    resolved = await _resolve_file(
        file_ref=body.file_ref,
        auth=auth,
        session=session,
    )
    ttl = 3600
    mounts: list[PreviewResourceMount] = []
    source_path = body.file_ref.path
    rules: set[str] = {source_path}
    content_type = str(resolved.row.content_type or "").lower()
    is_html = content_type in {"text/html", "application/xhtml+xml"} or source_path.lower().endswith(
        (".html", ".htm")
    )
    is_markdown = content_type in {"text/markdown", "text/x-markdown"} or source_path.lower().endswith(
        (".md", ".markdown")
    )
    if is_html or is_markdown:
        size_bytes = int(resolved.row.size_bytes or 0)
        sample_size = min(size_bytes, 2 * 1024 * 1024)
        source = _object_prefix(resolved.row.object_key, size_bytes, sample_size).decode(
            "utf-8", "replace"
        )
        if is_html:
            rules.update(html_vfs_read_rules(source, source_path))
        else:
            rules.update(markdown_vfs_read_rules(source, source_path))
    sorted_rules = tuple(sorted(rules))
    if isinstance(body.file_ref, ProjectFileRefV1):
        workspace_rules = tuple(
            rule
            for root in ("data", "memory", "logs", "chats")
            for rule in rules_for_root(sorted_rules, root)
        )
        workspace_capability = issue_vfs_resource_capability(
            tenant_id=auth.tenant_id,
            audience="file-preview",
            allowed_paths=workspace_rules,
            wf_id=resolved.scope_id,
            expires_in_s=ttl,
        )
        workspace_root = (
            f"/api/v1/vfs/resources/file-preview/"
            f"{quote(workspace_capability, safe='-_')}/"
        )
        mounts = [PreviewResourceMount(pathPrefix="/", rootUrl=workspace_root)]
        mount_rules = rules_for_root(sorted_rules, "mount")
        if mount_rules:
            mount_capability = issue_vfs_resource_capability(
                tenant_id=auth.tenant_id,
                audience="file-preview",
                allowed_paths=mount_rules,
                wf_id=mount_scope_id(auth.user_id),
                expires_in_s=ttl,
            )
            mounts.insert(0, PreviewResourceMount(
                pathPrefix="/mount/",
                rootUrl=(
                    f"/api/v1/vfs/resources/file-preview/"
                    f"{quote(mount_capability, safe='-_')}/mount/"
                ),
            ))
        source_dir = source_path.rsplit("/", 1)[0].lstrip("/")
        base_url = workspace_root + (source_dir + "/" if source_dir else "")
    elif isinstance(body.file_ref, MountFileRefV1):
        mount_rules = rules_for_root(sorted_rules, "mount")
        capability = issue_vfs_resource_capability(
            tenant_id=auth.tenant_id,
            audience="file-preview",
            allowed_paths=mount_rules,
            wf_id=resolved.scope_id,
            expires_in_s=ttl,
        )
        root = (
            f"/api/v1/vfs/resources/file-preview/"
            f"{quote(capability, safe='-_')}/mount/"
        )
        mounts = [PreviewResourceMount(pathPrefix="/mount/", rootUrl=root)]
        source_dir = source_path[len("/mount/"):].rsplit("/", 1)[0]
        base_url = root + (source_dir + "/" if source_dir else "")
    else:
        run_rules = rules_for_root(sorted_rules, "run")
        capability = issue_vfs_resource_capability(
            tenant_id=auth.tenant_id,
            audience="file-preview",
            allowed_paths=run_rules,
            run_id=resolved.run_id,
            expires_in_s=ttl,
        )
        root = (
            f"/api/v1/vfs/resources/file-preview/"
            f"{quote(capability, safe='-_')}/run/"
        )
        mounts = [PreviewResourceMount(pathPrefix="/run/", rootUrl=root)]
        source_dir = source_path[len("/run/"):].rsplit("/", 1)[0]
        base_url = root + (source_dir + "/" if source_dir else "")
    return PreviewResourceSession(
        resourceMounts=mounts,
        baseUrl=base_url,
        expiresIn=ttl,
    )


@router.put("/file", response_model=PreviewFileWriteOut)
async def write_preview_file(
    body: PreviewFileWriteV1,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> PreviewFileWriteOut:
    """Persist an explicitly saved UTF-8 source draft with revision safety."""
    if (
        isinstance(body.file_ref, RunFileRefV1)
        or (
            isinstance(body.file_ref, ProjectFileRefV1)
            and not body.file_ref.path.startswith(("/data/", "/chats/"))
        )
    ):
        raise HTTPException(status_code=403, detail="preview_file_read_only")
    await _authorize_file_ref(
        file_ref=body.file_ref,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    lock_key = body.file_ref.model_dump_json(by_alias=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"preview-write:{lock_key}"},
    )
    resolved = await _resolve_file(
        file_ref=body.file_ref,
        auth=auth,
        session=session,
        lock=True,
    )
    if vfs_row_revision(resolved.row) != body.expected_revision:
        raise HTTPException(status_code=409, detail="preview_revision_conflict")
    if int(resolved.row.size_bytes) > EDITABLE_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="preview_text_too_large")
    original = get_object_store().fetch_bytes(resolved.row.object_key)
    detected, _detected_content_type = _detect(
        body.file_ref.path,
        resolved.row.content_type,
        original,
    )
    requested_content_type = body.content_type.split(";", 1)[0].strip().lower()
    if (
        detected not in {"text", "markdown", "html"}
        or (
            not requested_content_type.startswith("text/")
            and requested_content_type not in EDITABLE_CONTENT_TYPES
        )
    ):
        raise HTTPException(status_code=403, detail="preview_file_read_only")
    _original_text, metadata, warning = _text_metadata(original)
    if metadata is None or warning:
        raise HTTPException(status_code=415, detail="preview_text_encoding_not_editable")
    normalized = body.content.replace("\r\n", "\n").replace("\r", "\n")
    if metadata.newline == "CRLF":
        normalized = normalized.replace("\n", "\r\n")
    data = normalized.encode("utf-8")
    if len(data) > EDITABLE_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="preview_text_too_large")
    if metadata.bom:
        data = b"\xef\xbb\xbf" + data
    repo = VfsRepo(session, object_store=get_object_store())
    await repo.upsert_artifact_bytes(
        wf_id=resolved.scope_id,
        tenant=auth.tenant_id,
        path=body.file_ref.path,
        data=data,
        content_type=requested_content_type,
    )
    await session.refresh(resolved.row)
    await session.commit()
    await get_sandbox_manager().mirror_vfs_write(
        auth.tenant_id,
        resolved.scope_id,
        body.file_ref.path,
        data,
    )
    return PreviewFileWriteOut(
        fileRef=body.file_ref,
        revision=vfs_row_revision(resolved.row),
        sizeBytes=len(data),
        contentType=requested_content_type,
    )
