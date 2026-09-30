"""Authorized graph transfer and immutable preview references (no sandbox I/O)."""
from __future__ import annotations

import re
from copy import deepcopy

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.services.agent_resources.authorization import (
    _decision,
    _principal,
    _request_context,
    _require_active_chat_write,
    _require_workflow_read,
    _service,
    _workflow_resource,
)
from vibecanvas_api.services.agent_resources.workflow_graph import (
    _auto_tidy_workflow,
    _node_count,
    collect_workflow_warnings,
    validate_workflow_for_context,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import WorkflowVersion
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

from .workflow_target import resolve_target


async def read_workflow_snapshot(ctx, *, workflow_id: str, version: str = "", major: str = "") -> dict:
    """Authorize and freeze an explicit branch, pinned version, or Preview HEAD."""
    async with session_scope(tenant_id=ctx.tenant_id) as session:
        if not workflow_id:
            raise ToolError("invalid_arguments", "An explicit workflow ID is required.")
        if version and major:
            raise ToolError("invalid_arguments", "Specify either an exact version or a major, not both.")
        selection = await resolve_target(session, ctx, workflow_id, major) if major else None
        repo = WorkflowRepo(session, ctx.username)
        if selection is None:
            await _require_workflow_read(session, ctx, workflow_id)
            meta = await repo.get_meta(workflow_id)
        else:
            meta = selection["meta"]
        if not meta:
            raise ToolError("workflow_unavailable", "The workflow is unavailable.")
        if version:
            match = re.fullmatch(r"v([1-9]\d*)\.sv(\d+)", version)
            if not match:
                raise ToolError("invalid_version", "Use a version such as v1.sv3.")
            major, sub = int(match[1]), int(match[2])
        elif selection is not None:
            major, sub = selection["major"], selection["sub"]
        else:
            major, sub = meta["active_major"], meta["active_sub"]
        if await session.get(WorkflowVersion, (workflow_id, major, sub)) is None:
            raise ToolError("version_not_found", "The requested workflow version does not exist.")
        graph = deepcopy(await repo.get_workflow_at(workflow_id, major, sub))
        graph.setdefault("__meta__", {}).update(
            workflow_id=workflow_id, workflow_name=meta.get("workflow_name", ""),
            workflow_version=major, workflow_subversion=sub,
        )
        return {"id": workflow_id, "name": meta.get("workflow_name", ""),
                "version": f"v{major}.sv{sub}", "node_count": _node_count(graph), "workflow": graph}


async def download_workflow(ctx, *, workflow_id: str, major: str) -> dict:
    result = await read_workflow_snapshot(ctx, workflow_id=workflow_id, major=major)
    return {key: result[key] for key in ("id", "version", "node_count", "workflow")}


async def upload_workflow(ctx, workflow: dict, *, workflow_id: str, major: str, note: str = "") -> dict:
    graph = deepcopy(workflow)
    if not isinstance(graph.get("__meta__", {}), dict):
        raise ToolError("invalid_workflow", "__meta__ must be an object.")
    # Validate before taking database locks; permissions/target are checked again
    # within the commit transaction. Failed validation never allocates a version.
    try:
        errors = await validate_workflow_for_context(graph, ctx)
        warnings = collect_workflow_warnings(graph)
        if errors:
            message = "; ".join(f"{item.get('node_id', 'global')}: {item.get('message', 'Invalid workflow')}" for item in errors[:10])
            raise ToolError("invalid_workflow", message[:8000])
        _auto_tidy_workflow(graph)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise ToolError("invalid_workflow", "Repair the workflow graph and node configuration before uploading.") from exc
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        await _require_active_chat_write(session, ctx)
        selection = await resolve_target(session, ctx, workflow_id, major, for_update=True)
        await _decision(ctx=ctx, service=_service(ctx, session), action=Action.UPDATE,
                        resource=_workflow_resource(ctx, workflow_id),
                        consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        metadata = graph.setdefault("__meta__", {})
        if metadata.get("workflow_id") not in (None, "", workflow_id):
            raise ToolError("workflow_mismatch", "The file belongs to another workflow. Specify that workflow ID or use create --file to make a copy.")
        for key in ("description", "tags", "owner_id", "tenant_id", "creator_user_id"):
            metadata.pop(key, None)
        repo = WorkflowRepo(session, ctx.username)
        if not await repo.get_meta(workflow_id):
            raise ToolError("workflow_unavailable", "The workflow is unavailable.")
        from vibecanvas_api.services.workflow_resources import collect_subagent_resources, canonicalize_resource_names
        if collect_subagent_resources(graph):
            graph = await canonicalize_resource_names(session=session, workflow=graph,
                service=_service(ctx, session), principal=_principal(ctx),
                context=_request_context(ctx, consistency=ConsistencyPreference.HIGHER_CONSISTENCY))
        pointer = await repo.commit(workflow_id, graph, note=note or "agent: workflow upload",
                                    target_major=selection["major"], stamp_metadata=True)
        result = {"id": workflow_id, "version": f"v{pointer.parent_v}.sv{pointer.sv}", "node_count": _node_count(graph)}
        if warnings:
            result["warnings"] = warnings[:20]
    # Returning from session_scope confirms durable commit. The Runtime emits
    # canvas refresh and completion evidence only after receiving this result.
    return result
