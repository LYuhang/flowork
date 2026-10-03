"""Logical Storage namespace API.

This route is the product-facing file browser layer. It accepts stable logical
paths (`/mount`, `/workflow/{id}/data`, `/project/{id}/data`, ...), authorizes the
business-object root through OpenFGA, then maps to VFS scopes/paths.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.auth.deps import AuthContext, current_user, tenant_db
from vibecanvas_api.authorization.dependencies import (
    context_for_auth,
    get_authz_service,
    principal_for_auth,
)
from vibecanvas_api.authorization.service import (
    AuthzService,
    batch_resource_decisions,
)
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
    Decision,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.config import config
from vibecanvas_api.schemas.access import access_from_decision
from vibecanvas_api.schemas.storage import (
    StorageDeleteOut,
    StorageItem,
    StorageListOut,
    StorageMkdirIn,
    StorageReadOut,
    StorageRenameIn,
    StorageRenameOut,
    StorageWriteIn,
    StorageWriteOut,
)
from vibecanvas_api.security.upload_scanner import require_clean_upload
from vibecanvas_api.services.chat_workspace import (
    project_workspace_scope_id as _project_workspace_scope_id,
)
from vibecanvas_api.services.object_store import get_object_store, uri_to_key
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.user_mount_workspace import (
    mount_scope_id as _mount_scope_id,
)
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.models import ChatProject
from vibecanvas_api.storage.repo_tasks import TasksRepo
from vibecanvas_api.storage.vfs_store import (
    VfsEntryMeta,
    VfsRepo,
    _validate_artifact_path,
)
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])

_ROOTS = ("mount", "workflow", "project", "task")
_DIR_MARKER = ".keep"
_HIDDEN_DIR_MARKERS = {".keep", ".vibekeep"}
_MAX_INLINE_BYTES = 5 * 1024 * 1024


def _clean_logical_path(path: str) -> str:
    raw = (path or "/").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    while "//" in raw:
        raw = raw.replace("//", "/")
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="invalid_path")
    parts = [p for p in raw.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise HTTPException(status_code=400, detail="invalid_path")
    return "/" + "/".join(parts) if parts else "/"


def _iso(ts: float | datetime | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _infer_content_type(path: str, fallback: str = "text/plain") -> str:
    ext = os.path.splitext(path.lower())[1]
    return {
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
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
    }.get(ext, fallback)


def _is_text_ct(ct: str) -> bool:
    ct = (ct or "").split(";")[0].strip().lower()
    return ct.startswith("text/") or ct in {"application/json", "json", "text"} or ct.startswith("table/")


@dataclass(slots=True)
class ResolvedPath:
    logical_path: str
    root: Literal["mount", "workflow", "project", "task"]
    scope_id: str | None = None
    vfs_path: str | None = None
    workflow_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    task_artifact_uri: str | None = None
    task_artifact_name: str | None = None
    storage_kind: Literal["artifact", "scratch", "virtual"] = "virtual"
    writable: bool = False
    generated: bool = False


def _join_vfs(prefix: str, rest: list[str]) -> str:
    suffix = "/".join(rest)
    return f"{prefix}/{suffix}" if suffix else prefix


async def _project_storage_scope(session: AsyncSession, project_id: str) -> str | None:
    row = (await session.execute(
        select(ChatProject.project_id, ChatProject.workflow_id).where(
            ChatProject.project_id == project_id,
            ChatProject.deleted_at.is_(None),
        )
    )).one_or_none()
    if row is None:
        return None
    return _project_workspace_scope_id(row.project_id, workflow_id=row.workflow_id)


async def _resolve_path(
    logical_path: str,
    *,
    auth: AuthContext,
    session: AsyncSession,
) -> ResolvedPath:
    path = _clean_logical_path(logical_path)
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ResolvedPath(path, "mount")
    root = parts[0]
    if root not in _ROOTS:
        raise HTTPException(status_code=404, detail="storage_root_not_found")

    if root == "mount":
        return ResolvedPath(
            path, "mount", scope_id=_mount_scope_id(auth.user_id),
            vfs_path=_join_vfs("/mount", parts[1:]), storage_kind="artifact",
            writable=True,
        )

    if root == "workflow":
        if len(parts) == 1:
            return ResolvedPath(path, "workflow")
        workflow_id = parts[1]
        meta = await WorkflowRepo(session, auth.user_id).get_meta(workflow_id)
        if not meta:
            raise HTTPException(status_code=404, detail="workflow_not_found")
        if len(parts) == 2:
            return ResolvedPath(
                path,
                "workflow",
                scope_id=workflow_id,
                workflow_id=workflow_id,
            )
        bucket = parts[2]
        if bucket == "data":
            return ResolvedPath(
                path,
                "workflow",
                scope_id=workflow_id,
                vfs_path=_join_vfs("/data", parts[3:]),
                workflow_id=workflow_id,
                storage_kind="artifact",
                writable=False,
            )
        if bucket == "memory":
            return ResolvedPath(
                path,
                "workflow",
                scope_id=workflow_id,
                vfs_path=_join_vfs("/memory", parts[3:]),
                workflow_id=workflow_id,
                storage_kind="scratch",
                writable=False,
                generated=True,
            )
        if bucket == "logs":
            return ResolvedPath(
                path,
                "workflow",
                scope_id=workflow_id,
                vfs_path=_join_vfs("/logs", parts[3:]),
                workflow_id=workflow_id,
                storage_kind="artifact",
                writable=False,
                generated=True,
            )
        raise HTTPException(status_code=404, detail="storage_path_not_found")

    if root == "project":
        if len(parts) == 1:
            return ResolvedPath(path, "project")
        project_id = parts[1]
        scope_id = await _project_storage_scope(session, project_id)
        if scope_id is None:
            raise HTTPException(status_code=404, detail="project_not_found")
        if len(parts) == 2:
            return ResolvedPath(path, "project", scope_id=scope_id, project_id=project_id)
        bucket = parts[2]
        if bucket in {"data", "chats"}:
            return ResolvedPath(
                path, "project", scope_id=scope_id,
                vfs_path=_join_vfs(f"/{bucket}", parts[3:]),
                project_id=project_id, storage_kind="artifact", writable=True,
            )
        if bucket == "memory":
            return ResolvedPath(
                path, "project", scope_id=scope_id,
                vfs_path=_join_vfs("/memory", parts[3:]),
                project_id=project_id, storage_kind="scratch", writable=False,
                generated=True,
            )
        if bucket == "logs":
            return ResolvedPath(
                path, "project", scope_id=scope_id,
                vfs_path=_join_vfs("/logs", parts[3:]),
                project_id=project_id, storage_kind="artifact", writable=False,
                generated=True,
            )
        raise HTTPException(status_code=404, detail="storage_path_not_found")

    if root == "task":
        if len(parts) == 1:
            return ResolvedPath(path, "task")
        try:
            task_uuid = uuid.UUID(parts[1])
        except ValueError:
            raise HTTPException(status_code=404, detail="task_not_found")
        task = await TasksRepo(session).get(task_uuid)
        if task is None or task.task_type != "batch_exec":
            raise HTTPException(status_code=404, detail="task_not_found")
        task_id = str(task.id)
        if len(parts) == 2:
            return ResolvedPath(path, "task", task_id=task_id)
        if len(parts) != 3:
            raise HTTPException(status_code=404, detail="storage_path_not_found")
        artifact_name = parts[2]
        summary = task.result if isinstance(task.result, dict) else {}
        artifact_uris = summary.get("artifact_uris")
        artifact_uris = artifact_uris if isinstance(artifact_uris, dict) else {}
        artifacts = {
            "results.csv": artifact_uris.get("csv") or task.results_uri,
            "results.jsonl": artifact_uris.get("jsonl"),
            "summary.json": artifact_uris.get("summary"),
        }
        artifact_uri = artifacts.get(artifact_name)
        if not isinstance(artifact_uri, str) or not artifact_uri:
            raise HTTPException(status_code=404, detail="storage_path_not_found")
        return ResolvedPath(
            path,
            "task",
            task_id=task_id,
            task_artifact_uri=artifact_uri,
            task_artifact_name=artifact_name,
            storage_kind="artifact",
            generated=True,
        )

    raise HTTPException(status_code=404, detail="storage_path_not_found")


def _logical_resource(
    logical_path: str,
    *,
    auth: AuthContext,
) -> ResourceRef | None:
    path = _clean_logical_path(logical_path)
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    if parts[0] == "mount":
        return ResourceRef(
            ResourceType.STORAGE_ROOT,
            auth.user_id,
            auth.active_organization_id,
        )
    if parts[0] == "workflow" and len(parts) >= 2:
        return ResourceRef(
            ResourceType.WORKFLOW,
            parts[1],
            auth.active_organization_id,
        )
    if parts[0] == "project":
        return ResourceRef(
            ResourceType.STORAGE_ROOT,
            auth.user_id,
            auth.active_organization_id,
        )
    if parts[0] == "task" and len(parts) >= 2:
        return ResourceRef(
            ResourceType.TASK,
            parts[1],
            auth.active_organization_id,
        )
    return None


def _task_artifact_size(summary: dict, artifact_name: str) -> int | None:
    """Return one persisted Task artifact size without reading its blob."""
    size_key = {
        "results.csv": "csv",
        "results.jsonl": "jsonl",
        "summary.json": "summary",
    }.get(artifact_name)
    sizes = summary.get("artifact_sizes")
    if not size_key or not isinstance(sizes, dict):
        return None
    value = sizes.get(size_key)
    return value if isinstance(value, int) and value >= 0 else None


async def _authorize_logical_path(
    logical_path: str,
    *,
    request: Request,
    auth: AuthContext,
    service: AuthzService,
    action: Action,
    consistency: ConsistencyPreference = (
        ConsistencyPreference.MINIMIZE_LATENCY
    ),
) -> Decision | None:
    resource = _logical_resource(logical_path, auth=auth)
    if resource is None:
        return None
    decision = await service.check(
        principal_for_auth(auth),
        action,
        resource,
        context_for_auth(auth, request, consistency=consistency),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=404,
            detail="storage_path_not_found",
        )
    return decision


def _caps(path: str, *, kind: str, writable: bool) -> dict:
    return {
        "can_create_child": kind == "folder" and writable,
        "can_rename": writable and path not in {"/mount", "/workflow", "/project", "/task"},
        "can_delete": writable and path not in {"/mount", "/workflow", "/project", "/task"},
        "can_write": writable and kind == "file",
    }


def _folder_item(
    name: str,
    path: str,
    *,
    writable: bool,
    modified_at: str | None = None,
    decision: Decision | None = None,
) -> StorageItem:
    return StorageItem(
        name=name, path=path, kind="folder", modified_at=modified_at,
        source="system" if path in {"/mount", "/workflow", "/project", "/task"} else None,
        access=access_from_decision(decision) if decision else None,
        **_caps(path, kind="folder", writable=writable),
    )


def _paginate(items: list[StorageItem], limit: int, cursor: str | None) -> StorageListOut:
    try:
        offset = max(0, int(cursor or "0"))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_cursor")
    page = items[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(items) else None
    return StorageListOut(path="", items=page, next_cursor=next_cursor, total_estimate=len(items))


def _sort_items(items: list[StorageItem], sort: str) -> list[StorageItem]:
    def folders_first(item: StorageItem) -> int:
        return 0 if item.kind == "folder" else 1

    if sort == "size":
        return sorted(items, key=lambda i: (folders_first(i), i.size_bytes or 0, i.name.lower()))
    if sort == "modified":
        return sorted(items, key=lambda i: (folders_first(i), i.modified_at or "", i.name.lower()), reverse=True)
    if sort == "type":
        return sorted(items, key=lambda i: (folders_first(i), i.content_type or "", i.name.lower()))
    return sorted(items, key=lambda i: (folders_first(i), i.name.lower()))


def _direct_children(
    *,
    logical_parent: str,
    vfs_parent: str,
    entries: list[VfsEntryMeta],
    writable: bool,
    search: str,
    decision: Decision | None,
) -> list[StorageItem]:
    parent = vfs_parent.rstrip("/")
    prefix = parent + "/"
    logical_base = logical_parent.rstrip("/")
    folder_map: dict[str, StorageItem] = {}
    files: list[StorageItem] = []
    q = search.strip().lower()
    for e in entries:
        if e.path == parent:
            continue
        if not e.path.startswith(prefix):
            continue
        rel = e.path[len(prefix):]
        marker = next((m for m in _HIDDEN_DIR_MARKERS if rel == m or rel.endswith("/" + m)), None)
        if not rel or marker:
            if marker and rel.endswith("/" + marker):
                folder_name = rel[: -len("/" + marker)].split("/", 1)[0]
                folder_path = f"{logical_base}/{folder_name}" if logical_base else f"/{folder_name}"
                if folder_name and (not q or q in folder_name.lower()):
                    folder_map.setdefault(
                        folder_name,
                        _folder_item(
                            folder_name,
                            folder_path,
                            writable=writable,
                            modified_at=_iso(e.last_access),
                            decision=decision,
                        ),
                    )
            continue
        head, sep, _tail = rel.partition("/")
        child_path = f"{logical_base}/{head}" if logical_base else f"/{head}"
        if sep:
            if not q or q in head.lower():
                existing = folder_map.get(head)
                modified = _iso(e.last_access)
                if existing is None:
                    folder_map[head] = _folder_item(
                        head,
                        child_path,
                        writable=writable,
                        modified_at=modified,
                        decision=decision,
                    )
                elif modified and (existing.modified_at is None or modified > existing.modified_at):
                    existing.modified_at = modified
            continue
        if q and q not in head.lower():
            continue
        files.append(StorageItem(
            name=head, path=child_path, kind="file", size_bytes=e.size_bytes,
            modified_at=_iso(e.last_access), content_type=e.content_type,
            source="user" if writable else "agent generated",
            access=access_from_decision(decision) if decision else None,
            **_caps(child_path, kind="file", writable=writable),
        ))
    return [*folder_map.values(), *files]


@router.get("/list", response_model=StorageListOut)
async def list_storage(
    request: Request,
    path: str = Query(default="/"),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    search: str = Query(default=""),
    sort: str = Query(default="name"),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> StorageListOut:
    logical = _clean_logical_path(path)
    decision = await _authorize_logical_path(
        logical,
        request=request,
        auth=auth,
        service=service,
        action=Action.VIEW,
    )
    resolved = await _resolve_path(logical, auth=auth, session=session)
    items: list[StorageItem]
    effective_writable = bool(
        resolved.writable
        and decision is not None
        and Action.UPDATE in decision.capabilities
    )

    if logical == "/":
        mount_decision = await _authorize_logical_path(
            "/mount",
            request=request,
            auth=auth,
            service=service,
            action=Action.VIEW_METADATA,
        )
        items = [
            _folder_item(
                "mount",
                "/mount",
                writable=bool(
                    mount_decision
                    and Action.UPDATE in mount_decision.capabilities
                ),
                decision=mount_decision,
            ),
            _folder_item("workflow", "/workflow", writable=False),
            _folder_item("project", "/project", writable=False),
            _folder_item("task", "/task", writable=False),
        ]
    elif logical == "/workflow":
        principal = principal_for_auth(auth)
        context = context_for_auth(auth, request)
        authorized_ids = await service.list_authorized_ids(
            principal,
            Action.VIEW_METADATA,
            ResourceType.WORKFLOW,
            context,
        )
        workflows, _ = await WorkflowRepo(
            session,
            auth.user_id,
        ).list_authorized_workflows(
            authorized_ids,
            limit=500,
            offset=0,
        )
        resources = [
            ResourceRef(
                ResourceType.WORKFLOW,
                str(workflow["wf_id"]),
                auth.active_organization_id,
            )
            for workflow in workflows
        ]
        decisions = await batch_resource_decisions(
            service,
            principal=principal,
            resources=resources,
            context=context,
        )
        q = search.strip().lower()
        items = [
            _folder_item(
                str(workflow["wf_id"]),
                f"/workflow/{workflow['wf_id']}",
                writable=False,
                modified_at=_iso(workflow.get("updated_at")),
                decision=decisions[resource],
            )
            for workflow, resource in zip(
                workflows,
                resources,
                strict=True,
            )
            if not q
            or q in str(workflow["wf_id"]).lower()
            or q in str(workflow.get("workflow_name") or "").lower()
        ]
    elif resolved.root == "workflow" and resolved.workflow_id and resolved.vfs_path is None:
        items = [
            _folder_item(
                "data",
                f"/workflow/{resolved.workflow_id}/data",
                writable=False,
                decision=decision,
            ),
            _folder_item(
                "memory",
                f"/workflow/{resolved.workflow_id}/memory",
                writable=False,
                decision=decision,
            ),
            _folder_item(
                "logs",
                f"/workflow/{resolved.workflow_id}/logs",
                writable=False,
                decision=decision,
            ),
        ]
    elif logical == "/project":
        repo = ChatProjectRepo(session, auth.user_id)
        projects = [*await repo.list(), *await repo.list(surface="browser")]
        items = [
            _folder_item(
                project["name"],
                f"/project/{project['project_id']}",
                writable=False,
                modified_at=project["updated_at"],
                decision=decision,
            )
            for project in projects
            if not search or search.lower() in project["project_id"].lower()
            or search.lower() in project["name"].lower()
        ]
    elif resolved.root == "project" and resolved.project_id and resolved.vfs_path is None:
        items = [
            _folder_item(
                "chats", f"/project/{resolved.project_id}/chats",
                writable=False, decision=decision,
            ),
            _folder_item(
                "data",
                f"/project/{resolved.project_id}/data",
                writable=effective_writable,
                decision=decision,
            ),
            _folder_item(
                "memory",
                f"/project/{resolved.project_id}/memory",
                writable=False,
                decision=decision,
            ),
            _folder_item(
                "logs",
                f"/project/{resolved.project_id}/logs",
                writable=False,
                decision=decision,
            ),
        ]
    elif logical == "/task":
        principal = principal_for_auth(auth)
        context = context_for_auth(auth, request)
        authorized_ids = await service.list_authorized_ids(
            principal,
            Action.VIEW_METADATA,
            ResourceType.TASK,
            context,
        )
        tasks, _ = await TasksRepo(session).list_for_tenant(
            task_ids=authorized_ids,
            task_type=["batch_exec"],
            limit=500,
            offset=0,
        )
        tasks = [
            task for task in tasks
            if isinstance(task.result, dict)
            and isinstance(task.result.get("artifact_uris"), dict)
        ]
        resources = [
            ResourceRef(
                ResourceType.TASK,
                str(task.id),
                auth.active_organization_id,
            )
            for task in tasks
        ]
        decisions = await batch_resource_decisions(
            service,
            principal=principal,
            resources=resources,
            context=context,
        )
        q = search.strip().lower()
        items = [
            _folder_item(
                str(task.id),
                f"/task/{task.id}",
                writable=False,
                modified_at=_iso(task.finished_at or task.submitted_at),
                decision=decisions[resource],
            )
            for task, resource in zip(tasks, resources, strict=True)
            if not q
            or q in str(task.id).lower()
            or q in str(task.workflow_id or "").lower()
        ]
    elif resolved.root == "task" and resolved.task_id and not resolved.task_artifact_uri:
        task = await TasksRepo(session).get(uuid.UUID(resolved.task_id))
        summary = task.result if task and isinstance(task.result, dict) else {}
        artifact_uris = summary.get("artifact_uris")
        artifact_uris = artifact_uris if isinstance(artifact_uris, dict) else {}
        available = {
            "results.csv": artifact_uris.get("csv") or (task.results_uri if task else None),
            "results.jsonl": artifact_uris.get("jsonl"),
            "summary.json": artifact_uris.get("summary"),
        }
        q = search.strip().lower()
        content_types = {
            "results.csv": "text/csv",
            "results.jsonl": "application/x-ndjson",
            "summary.json": "application/json",
        }
        items = [
            StorageItem(
                name=name,
                path=f"/task/{resolved.task_id}/{name}",
                kind="file",
                size_bytes=_task_artifact_size(summary, name),
                modified_at=_iso(task.finished_at if task else None),
                content_type=content_types[name],
                source="task generated",
                access=access_from_decision(decision) if decision else None,
                **_caps(
                    f"/task/{resolved.task_id}/{name}",
                    kind="file",
                    writable=False,
                ),
            )
            for name, uri in available.items()
            if isinstance(uri, str) and uri and (not q or q in name.lower())
        ]
    elif resolved.scope_id and resolved.vfs_path:
        rows = await VfsRepo(session, object_store=get_object_store()).ls_meta(
            wf_id=resolved.scope_id, prefix=resolved.vfs_path.rstrip("/") + "/")
        items = _direct_children(
            logical_parent=logical, vfs_parent=resolved.vfs_path,
            entries=rows,
            writable=effective_writable,
            search=search,
            decision=decision,
        )
    else:
        items = []

    items = _sort_items(items, sort)
    out = _paginate(items, limit, cursor)
    out.path = logical
    out.readonly = not effective_writable
    out.access = access_from_decision(decision) if decision else None
    return out


@router.get("/content", response_model=StorageReadOut)
async def read_storage_content(
    request: Request,
    path: str = Query(...),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> StorageReadOut:
    logical = _clean_logical_path(path)
    parts = [part for part in logical.split("/") if part]
    decision = await _authorize_logical_path(
        logical,
        request=request,
        auth=auth,
        service=service,
        action=(
            Action.EXPORT
            if len(parts) == 3 and parts[0] == "task"
            else Action.VIEW
        ),
    )
    resolved = await _resolve_path(path, auth=auth, session=session)
    if resolved.root == "task" and resolved.task_artifact_uri:
        try:
            data = await asyncio.to_thread(
                get_object_store().fetch_bytes,
                uri_to_key(resolved.task_artifact_uri),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="storage_path_not_found") from exc
        ct = _infer_content_type(resolved.task_artifact_name or "")
        truncated = len(data) > _MAX_INLINE_BYTES
        return StorageReadOut(
            path=_clean_logical_path(path),
            content_type=ct,
            content=data[:_MAX_INLINE_BYTES].decode("utf-8", "replace"),
            size_bytes=len(data),
            truncated=truncated,
            access=access_from_decision(decision) if decision else None,
        )
    if not resolved.vfs_path:
        raise HTTPException(status_code=400, detail="not_a_file")
    entry = await VfsRepo(session, object_store=get_object_store()).read(
        wf_id=resolved.scope_id, path=resolved.vfs_path, touch=False)
    if entry is None:
        raise HTTPException(status_code=404, detail="storage_path_not_found")
    ct = entry.content_type
    data = await VfsRepo(session, object_store=get_object_store()).read_bytes(
        wf_id=resolved.scope_id, path=resolved.vfs_path) or b""
    if not _is_text_ct(ct):
        return StorageReadOut(
            path=_clean_logical_path(path), content_type=ct, content=None,
            size_bytes=len(data), truncated=False,
            access=access_from_decision(decision) if decision else None,
        )
    truncated = len(data) > _MAX_INLINE_BYTES
    return StorageReadOut(
        path=_clean_logical_path(path), content_type=ct,
        content=data[:_MAX_INLINE_BYTES].decode("utf-8", "replace"),
        size_bytes=len(data), truncated=truncated,
        access=access_from_decision(decision) if decision else None,
    )


@router.get("/raw")
async def raw_storage_content(
    request: Request,
    path: str = Query(...),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> Response:
    logical = _clean_logical_path(path)
    parts = [part for part in logical.split("/") if part]
    await _authorize_logical_path(
        logical,
        request=request,
        auth=auth,
        service=service,
        action=(
            Action.EXPORT
            if len(parts) == 3 and parts[0] == "task"
            else Action.VIEW
        ),
    )
    resolved = await _resolve_path(path, auth=auth, session=session)
    if resolved.root == "task" and resolved.task_artifact_uri:
        try:
            data = await asyncio.to_thread(
                get_object_store().fetch_bytes,
                uri_to_key(resolved.task_artifact_uri),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="storage_path_not_found") from exc
        name = resolved.task_artifact_name or "download"
        return Response(
            content=data,
            media_type=_infer_content_type(name, "application/octet-stream"),
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    if not resolved.vfs_path:
        raise HTTPException(status_code=400, detail="not_a_file")
    entry = await VfsRepo(session, object_store=get_object_store()).read(
        wf_id=resolved.scope_id, path=resolved.vfs_path, touch=False)
    if entry is None:
        raise HTTPException(status_code=404, detail="storage_path_not_found")
    data = await VfsRepo(session, object_store=get_object_store()).read_bytes(
        wf_id=resolved.scope_id, path=resolved.vfs_path) or b""
    ct = entry.content_type
    return Response(
        content=data,
        media_type=ct or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(resolved.vfs_path)}"'},
    )


def _require_writable_file(resolved: ResolvedPath) -> None:
    if not resolved.writable or not resolved.scope_id or not resolved.vfs_path:
        raise HTTPException(status_code=403, detail="storage_path_readonly")
    if resolved.vfs_path.rstrip("/") in {"/mount", "/data"}:
        raise HTTPException(status_code=400, detail="not_a_file")


@router.put("/content", response_model=StorageWriteOut)
async def write_storage_content(
    body: StorageWriteIn,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> StorageWriteOut:
    await _authorize_logical_path(
        body.path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
    )
    resolved = await _resolve_path(body.path, auth=auth, session=session)
    _require_writable_file(resolved)
    try:
        _validate_artifact_path(resolved.vfs_path or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_path")
    data = body.content.encode("utf-8")
    if len(data) > config.storage.vfs_upload_max_bytes:
        raise HTTPException(status_code=413, detail="file_too_large")
    content_type = body.content_type or _infer_content_type(resolved.vfs_path or "")
    decision = await _authorize_logical_path(
        body.path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    replaced = await VfsRepo(session, object_store=get_object_store()).upsert_artifact_bytes(
        wf_id=resolved.scope_id, tenant=auth.tenant_id, path=resolved.vfs_path or "",
        data=data, content_type=content_type)
    await get_sandbox_manager().mirror_vfs_write(
        auth.tenant_id, resolved.scope_id, resolved.vfs_path or "", data)
    return StorageWriteOut(
        path=_clean_logical_path(body.path), size_bytes=len(data),
        content_type=content_type, replaced=replaced,
        access=access_from_decision(decision) if decision else None,
    )


@router.post("/upload", response_model=StorageWriteOut)
async def upload_storage_file(
    request: Request,
    path: str = Query(...),
    file: UploadFile = File(...),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> StorageWriteOut:
    await _authorize_logical_path(
        path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
    )
    parent = await _resolve_path(path, auth=auth, session=session)
    if not parent.writable or not parent.scope_id or not parent.vfs_path:
        raise HTTPException(status_code=403, detail="storage_path_readonly")
    name = os.path.basename(file.filename or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="invalid_filename")
    child_logical = _clean_logical_path(parent.logical_path.rstrip("/") + "/" + name)
    child = await _resolve_path(child_logical, auth=auth, session=session)
    _require_writable_file(child)
    try:
        _validate_artifact_path(child.vfs_path or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_filename")
    data = await file.read()
    if len(data) > config.storage.vfs_upload_max_bytes:
        raise HTTPException(status_code=413, detail="file_too_large")
    await require_clean_upload(data)
    content_type = file.content_type or _infer_content_type(child.vfs_path or "", "application/octet-stream")
    decision = await _authorize_logical_path(
        child_logical,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    replaced = await VfsRepo(session, object_store=get_object_store()).upsert_artifact_bytes(
        wf_id=child.scope_id, tenant=auth.tenant_id, path=child.vfs_path or "",
        data=data, content_type=content_type)
    await get_sandbox_manager().mirror_vfs_write(
        auth.tenant_id, child.scope_id, child.vfs_path or "", data)
    return StorageWriteOut(
        path=child_logical, size_bytes=len(data), content_type=content_type,
        replaced=replaced,
        access=access_from_decision(decision) if decision else None,
    )


@router.post("/mkdir", response_model=StorageWriteOut)
async def mkdir_storage(
    body: StorageMkdirIn,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> StorageWriteOut:
    await _authorize_logical_path(
        body.path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
    )
    resolved = await _resolve_path(body.path, auth=auth, session=session)
    if not resolved.writable or not resolved.scope_id or not resolved.vfs_path:
        raise HTTPException(status_code=403, detail="storage_path_readonly")
    marker = resolved.vfs_path.rstrip("/") + "/" + _DIR_MARKER
    try:
        _validate_artifact_path(marker)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_path")
    decision = await _authorize_logical_path(
        body.path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    replaced = await VfsRepo(session, object_store=get_object_store()).upsert_artifact_bytes(
        wf_id=resolved.scope_id, tenant=auth.tenant_id, path=marker, data=b"",
        content_type="application/x-directory")
    await get_sandbox_manager().mirror_vfs_write(
        auth.tenant_id, resolved.scope_id, marker, b"",
    )
    return StorageWriteOut(
        path=_clean_logical_path(body.path), size_bytes=0,
        content_type="application/x-directory", replaced=replaced,
        access=access_from_decision(decision) if decision else None,
    )


@router.delete("", response_model=StorageDeleteOut)
async def delete_storage(
    request: Request,
    path: str = Query(...),
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> StorageDeleteOut:
    await _authorize_logical_path(
        path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
    )
    resolved = await _resolve_path(path, auth=auth, session=session)
    if not resolved.writable or not resolved.scope_id or not resolved.vfs_path:
        raise HTTPException(status_code=403, detail="storage_path_readonly")
    try:
        _validate_artifact_path(resolved.vfs_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_path")
    decision = await _authorize_logical_path(
        path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    deleted = await VfsRepo(session, object_store=get_object_store()).delete_artifact(
        wf_id=resolved.scope_id, tenant=auth.tenant_id, path=resolved.vfs_path)
    marker = resolved.vfs_path.rstrip("/") + "/" + _DIR_MARKER
    deleted += await VfsRepo(session, object_store=get_object_store()).delete_artifact(
        wf_id=resolved.scope_id, tenant=auth.tenant_id, path=marker)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="storage_path_not_found")
    await get_sandbox_manager().mirror_vfs_delete(
        auth.tenant_id, resolved.scope_id, resolved.vfs_path,
    )
    return StorageDeleteOut(
        deleted=deleted,
        access=access_from_decision(decision) if decision else None,
    )


@router.post("/rename", response_model=StorageRenameOut)
async def rename_storage(
    body: StorageRenameIn,
    request: Request,
    auth: AuthContext = Depends(current_user),
    session: AsyncSession = Depends(tenant_db),
    service: AuthzService = Depends(get_authz_service),
) -> StorageRenameOut:
    await _authorize_logical_path(
        body.old_path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
    )
    await _authorize_logical_path(
        body.new_path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
    )
    old = await _resolve_path(body.old_path, auth=auth, session=session)
    new = await _resolve_path(body.new_path, auth=auth, session=session)
    if not old.writable or not new.writable or old.scope_id != new.scope_id:
        raise HTTPException(status_code=403, detail="storage_path_readonly")
    if not old.vfs_path or not new.vfs_path:
        raise HTTPException(status_code=400, detail="invalid_path")
    decision = await _authorize_logical_path(
        body.old_path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    await _authorize_logical_path(
        body.new_path,
        request=request,
        auth=auth,
        service=service,
        action=Action.UPDATE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    try:
        await VfsRepo(session, object_store=get_object_store()).rename_artifact(
            wf_id=old.scope_id, tenant=auth.tenant_id,
            old_path=old.vfs_path, new_path=new.vfs_path)
    except ValueError as e:
        marker_old = old.vfs_path.rstrip("/") + "/" + _DIR_MARKER
        marker_new = new.vfs_path.rstrip("/") + "/" + _DIR_MARKER
        try:
            await VfsRepo(session, object_store=get_object_store()).rename_artifact(
                wf_id=old.scope_id, tenant=auth.tenant_id,
                old_path=marker_old, new_path=marker_new)
        except ValueError:
            if "not found" in str(e):
                raise HTTPException(status_code=404, detail="storage_path_not_found")
            raise HTTPException(status_code=400, detail="invalid_path")
    await get_sandbox_manager().mirror_vfs_rename(
        auth.tenant_id, old.scope_id, old.vfs_path, new.vfs_path,
    )
    return StorageRenameOut(
        path=_clean_logical_path(body.new_path),
        access=access_from_decision(decision) if decision else None,
    )
