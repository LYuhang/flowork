"""Host-owned Knowledge commands; sandbox files arrive as immutable bytes."""
from __future__ import annotations

import base64
import hashlib
import json
import traceback
import uuid

from vibecanvas_api.services.write_conflicts import WriteConflict

from fastapi import HTTPException
from sqlalchemy import text
import structlog

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.flowork_cli.cli import error, uncertain_result
from vibecanvas_api.flowork_cli.knowledge_cli import WRITE_OPERATIONS, validate
from vibecanvas_api.routes import kb as routes
from vibecanvas_api.services.knowledge_packages import (
    PackageFile, enqueue_package_indexing, package_snapshot, replace_package, validate_package,
)
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.authorization import _require_active_chat_write
from vibecanvas_api.services.agent_runtime.resource_routes import resource_route_params, admitted_resource_route_params, shared_resource_cards
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_kb import KbRepo
from vibecanvas_api.services.knowledge_metadata import package_metadata, with_metadata
from vibecanvas_api.security.upload_scanner import require_clean_upload
from sqlalchemy.exc import IntegrityError

logger = structlog.get_logger(__name__)


def metadata(row):
    row = row.model_dump() if hasattr(row, "model_dump") else row
    return {"knowledge_id": str(row["id"]), **{key: row[key] for key in (
        "name", "description", "package_version", "file_count", "updated_at") if key in row},
        "capabilities": row.get("access", {}).get("capabilities", [])}


def index_summary(files):
    counts = {state: sum(f.status == state for f in files) for state in ("stored", "pending", "indexing", "indexed", "failed")}
    state = "indexing" if counts["indexing"] else ("pending" if counts["pending"] else (
        "failed" if counts["failed"] else ("ready" if counts["indexed"] else "not_indexed")))
    return {"index_status": state, "index_counts": counts,
        "search_complete": not any(counts[k] for k in ("pending", "indexing", "failed", "stored"))}


async def version(session, kb_id):
    return (await session.execute(text("SELECT package_version FROM knowledge_bases WHERE id=:id AND deleted_at IS NULL"), {"id": kb_id})).scalar_one_or_none()


async def _route_params(ctx, session, kb_id):
    if kb_id is None:
        return resource_route_params(ctx, session)
    return await admitted_resource_route_params(ctx, session, "knowledge_base", kb_id)


async def read(ctx, operation, args):
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        params = resource_route_params(ctx, session)
        if operation == "knowledge.list":
            rows = await routes.list_kbs(**params)
            entries = {str(row.id): (row.latest_updated_at, metadata(row)) for row in rows}
            for card in await shared_resource_cards(ctx, session, "knowledge_base"):
                if card.resource_id not in entries:
                    entries[card.resource_id] = (card.updated_at.isoformat(), {
                        "knowledge_id": card.resource_id, "name": card.name,
                        "description": card.description, "updated_at": card.updated_at.isoformat(),
                        "capabilities": list(card.access.capabilities),
                    })
            ordered = sorted(entries.items(), key=lambda item: (item[1][0], item[0]), reverse=True)
            start, limit = args["offset"], args["limit"]
            return {"status": "succeeded", "knowledge": [entry[1][1] for entry in ordered[start:start + limit]],
                "total": len(ordered), "next_offset": start + limit if start + limit < len(ordered) else None,
                "message": "Discoverable Knowledge packages. Check capabilities before reading or writing."}
        kb_id = uuid.UUID(args["knowledge_id"])
        params = await _route_params(ctx, session, kb_id)
        if operation == "knowledge.get":
            row = await routes.get_kb(kb_id, **params)
            files = await routes.list_files(kb_id, file_status=None, **params)
            return {"status": "succeeded", **metadata(row), **index_summary(files),
                "files": [{"path": f.name, "size_bytes": f.file_size, "index_status": f.status,
                    **({"error": f.error_message} if f.error_message else {})} for f in files],
                "message": "Package metadata and file index states loaded. Raw files remain authoritative."}
        await authorize(params, kb_id, Action.USE)
        # Version validation gives one consistent snapshot without locking writers
        # during object-store reads; package versions are monotonic across UI/CLI.
        for _ in range(3):
            before = await version(session, kb_id)
            if before is None:
                raise HTTPException(404, "kb_not_found")
            files = await package_snapshot(session, kb_id)
            current = await KbRepo(session).get_active(kb_id)
            legacy = package_metadata(files, required=False) is None
            files = with_metadata(files, name=current.name, description=current.description, only_if_missing=True)
            if before == await version(session, kb_id):
                return {"status": "succeeded", "knowledge_id": str(kb_id), "package_version": before,
                    "file_count": len(files), "metadata_added_to_local_readme": legacy, "files": [{"path": f.path, "data": base64.b64encode(f.data).decode("ascii")} for f in files],
                    "message": "Knowledge package downloaded. Read README.md first."}
        raise ToolError("package_changing", "The package changed repeatedly during download. Try downloading to a new directory after writes finish.")


async def authorize(params, kb_id, action):
    auth = {k: v for k, v in params.items() if k != "session"}
    if kb_id is None:
        await routes._authorize_organization_create(**auth)
    else:
        await routes._authorize_knowledge_base(knowledge_base_id=kb_id, action=action,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY, **auth)


def publication(kb_id, version_number, files, *, pending=True):
    return {"status": "succeeded", "knowledge_id": str(kb_id), "package_version": version_number,
        "file_count": len(files), "index_status": "pending" if pending else "not_indexed",
        "message": "Knowledge package published. Search indexing is asynchronous." if pending else "Knowledge package created with README.md.",
        "next_step": f"flowork-cli knowledge get --knowledge-id {kb_id}. Do not publish again to wait for indexing."}


async def execute(call, arguments):
    cap, operation = call.capability, call.operation
    started = False
    committed = None
    request = None
    try:
        args = validate(operation, arguments)
        ctx = await agent_context.resolve_context(cap)
        files = None
        if "files" in args:
            files = validate_package([PackageFile(f["path"], base64.b64decode(f["data"], validate=True), "") for f in args["files"]])
            info = package_metadata(files)
            for item in files:
                await require_clean_upload(item.data)
        if operation == "knowledge.check":
            return {"status": "succeeded", **info, "file_count": len(files),
                "size_bytes": sum(len(item.data) for item in files),
                "message": "Knowledge package is valid. Nothing was saved or published."}
        if operation not in WRITE_OPERATIONS:
            return await read(ctx, operation, args)
        kb_id = uuid.UUID(args["knowledge_id"]) if "knowledge_id" in args else None
        action = Action.DELETE if operation == "knowledge.delete" else Action.UPDATE
        if cap.approval_mode not in {"agent", "always_ask", "always_allow"}:
            raise ToolError("invalid_approval_mode", "Unknown approval mode. No changes were made.")
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            await authorize(await _route_params(ctx, session, kb_id), kb_id, action)
            await session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(ctx.tenant_id)})
            await session.execute(text("""INSERT INTO knowledge_cli_leases(call_id,tenant_id,run_id,operation,expires_at)
                VALUES (:id,CAST(:tenant AS uuid),:run,:operation,now()+interval '30 seconds')"""),
                {"id": call.call_id, "tenant": cap.tenant_id, "run": cap.turn_id, "operation": operation})
        call.durable_lease = True
        needs_approval = cap.approval_mode in {"always_ask", "agent"}
        if needs_approval:
            from .cli_delete import _approve
            summary = {k: v for k, v in args.items() if k != "files"}
            if files is not None:
                summary.update(file_count=len(files), size_bytes=sum(len(f.data) for f in files),
                    content_sha256=hashlib.sha256(json.dumps(args["files"], sort_keys=True).encode()).hexdigest())
            warning = " Publication requires the expected version from download, rejects intervening publications and unpublished drafts, and removes absent files." if operation == "knowledge.publish" else ""
            await _approve(call, summary, prompt="Approve " + operation.replace(".", " ") + "? " + json.dumps(summary) + warning)
        await call.emit({"progress": {"status": "approved" if needs_approval else "auto_approved", "message": "Operation approved. Rechecking current permissions before publication."}})
        ctx = await agent_context.resolve_context(cap)
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            await _require_active_chat_write(session, ctx)
            if not (await session.execute(text("SELECT 1 FROM knowledge_cli_leases WHERE call_id=:id AND expires_at>now()"), {"id": call.call_id})).first():
                raise ToolError("approval_cancelled", "The CLI command is no longer active.")
            params = await _route_params(ctx, session, kb_id)
            request = params["request"]
            request.state.cli_knowledge = True
            await authorize(params, kb_id, action)
            started = True
            if operation == "knowledge.create":
                body = routes.KbCreate(**info)
                result = await routes._create_knowledge_package(body=body, package_files=files, derive_index=True, **params)
                return publication(result.id, result.package_version, files)
            if operation == "knowledge.delete":
                await routes.delete_kb(kb_id, **params)
                return {"status": "succeeded", "knowledge_id": str(kb_id), "message": "Knowledge package deleted. Local downloads were not removed."}
            await call.emit({"progress": {"status": "publishing", "knowledge_id": str(kb_id), "message": "Publishing the complete package snapshot."}})
            owner_organization_id = str(getattr(request.state, "admitted_resource_organization_id", None) or ctx.tenant_id)
            number, pending = await replace_package(session, kb_id=kb_id, actor_user_id=uuid.UUID(cap.user_id), expected_version=args["expected_version"], files=files, protect_draft=True)
            await session.commit()
            committed = publication(kb_id, number, files, pending=bool(pending))
        await enqueue_package_indexing(tenant_id=owner_organization_id, user_id=cap.user_id, file_ids=pending)
        return committed
    except WriteConflict as exc:
        return exc.cli_result()
    except Exception as exc:
        receipt = getattr(request.state, "cli_knowledge_receipt", None) if request is not None else None
        if committed or receipt:
            return {**(committed or receipt), "warning": "post_commit_work_pending",
                "message": "The package change committed, but authorization projection or search indexing follow-up failed.",
                "next_step": "Inspect knowledge list/get. Do not repeat the mutation; contact support if follow-up remains pending."}
        if isinstance(exc, RuntimeError) and str(exc).startswith("knowledge_version_conflict:"):
            return {"status": "failed", **error("state_conflict", "The published version changed. Nothing was published.",
                "Download the latest version, reconcile your edits, then publish with its package_version.")}
        if isinstance(exc, IntegrityError):
            return {"status": "failed", **error("state_conflict", "A package already uses this name. Nothing was published.", "Choose a distinct README.md name.")}
        if isinstance(exc, ToolError):
            return {"status": "failed", **error(str(exc), exc.message, "Respect approval decisions; inspect status and permissions before retrying.")}
        if isinstance(exc, HTTPException):
            code = {403: "permission_denied", 404: "resource_unavailable", 409: "state_conflict", 422: "invalid_arguments"}.get(exc.status_code, "knowledge_error")
            messages = {
                "kb_name_conflict": "An active Knowledge package already uses this name. No package was published.",
                "kb_not_found": "The Knowledge package does not exist or is not accessible.",
                "kb_delete_in_progress": "A file is currently being indexed. The package was not deleted.",
            }
            message = messages.get(exc.detail, str(exc.detail)) if isinstance(exc.detail, str) else str(exc.detail)
            hints = {
                "kb_name_conflict": "Choose a distinct name, or inspect existing packages with flowork-cli knowledge list. Do not overwrite an existing package merely to resolve a naming conflict.",
                "kb_not_found": "Use flowork-cli knowledge list to find an accessible knowledge_id, then check status with that exact ID.",
                "kb_delete_in_progress": "Use flowork-cli knowledge get --knowledge-id ID to check indexing. Wait for indexing to finish before retrying deletion.",
            }
            hint = hints.get(exc.detail) if isinstance(exc.detail, str) else None
            if not hint:
                hint = {
                    403: "Check the user's current permissions for this Knowledge package before retrying.",
                    404: "Use flowork-cli knowledge list to find a currently accessible package.",
                    409: "Inspect the package's current status before retrying this operation.",
                    422: "Check this command's --help and correct the reported arguments.",
                }.get(exc.status_code, "Inspect knowledge get and the reported error before retrying.")
            return {"status": "failed", **error(code, message, hint)}
        if isinstance(exc, (ValueError, LookupError)):
            return {"status": "failed", **error("invalid_arguments", str(exc), "Check this command's --help. No package was published.")}
        if isinstance(exc, PermissionError):
            return {"status": "failed", **error("permission_denied", "The identity or command is no longer authorized.", "Use a new authorized Agent turn.")}
        # Keep package bytes, credentials and exception/SQL parameter values
        # out of logs while retaining actionable internal failure locations.
        logger.error("knowledge_cli_failed", operation=operation,
            error_type=type(exc).__name__, publication_started=started,
            frames=[{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                    for frame in traceback.extract_tb(exc.__traceback__)])
        return uncertain_result() if started else error("knowledge_unavailable", "The Knowledge operation is unavailable.", "Inspect permissions/status and report the failure.")
