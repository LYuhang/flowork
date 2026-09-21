"""Platform MCP workflow selection and creation tools."""

from __future__ import annotations

import asyncio
import time

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool
import structlog

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.authorization.types import (
    Action,
    ConsistencyPreference,
)
from vibecanvas_api.services.platform_mcp.authorization import (
    create_authorized_workflow,
    load_authorized_workflow,
    recheck_platform_workflow_action,
)
from vibecanvas_api.storage.sync_repo import SyncChatRepo

logger = structlog.get_logger(__name__)

_DB_STAGE_TIMEOUT_S = 30.0

@register_render("set_workflow")
def _render_set_workflow(raw: dict, ctx) -> Rendered:
    wf_id = raw.get("workflow_id", "")
    name = raw.get("workflow_name") or wf_id
    version = raw.get("version", "?")
    subversion = raw.get("subversion", "?")
    return Rendered(
        content=(
            f"Current workflow changed to {wf_id} ({name}), version "
            f"v{version}.sv{subversion}."
        ),
        content_type="text/plain",
        abstract=f"set_workflow -> {wf_id}",
        extras={"workflow_id": wf_id},
    )


@register_render("create_workflow")
def _render_create_workflow(raw: dict, ctx) -> Rendered:
    wf_id = raw.get("workflow_id", "")
    name = raw.get("workflow_name") or wf_id
    version = raw.get("version", "?")
    subversion = raw.get("subversion", "?")
    description = raw.get("description", "")
    return Rendered(
        content=(
            f"Created workflow {wf_id}: {name}\n"
            f"Version: v{version}.sv{subversion}\n"
            f"Description: {description or '(empty)'}"
        ),
        content_type="text/plain",
        abstract=f"create_workflow -> {wf_id}",
        extras={"workflow_id": wf_id},
    )


def _persist_current_workflow(ctx, wf_id: str) -> None:
    if not getattr(ctx, "chat_id", ""):
        raise ToolError("missing_chat", "Cannot save workflow context without a chat id.")
    started = time.perf_counter()
    logger.info(
        "workflow_context_persist_start",
        chat_id=getattr(ctx, "chat_id", ""),
        workflow_id=wf_id,
    )
    SyncChatRepo(ctx.username).set_current_workflow_id(ctx.chat_id, wf_id)
    ctx.current_workflow_id = wf_id
    logger.info(
        "workflow_context_persist_done",
        chat_id=getattr(ctx, "chat_id", ""),
        workflow_id=wf_id,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


async def _run_stage(stage: str, func):
    started = time.perf_counter()
    logger.info("workflow_context_stage_start", stage=stage)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(func),
            timeout=_DB_STAGE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error(
            "workflow_context_stage_timeout",
            stage=stage,
            timeout_s=_DB_STAGE_TIMEOUT_S,
        )
        raise ToolError("stage_timeout", f"{stage} timed out after {_DB_STAGE_TIMEOUT_S:.0f}s")
    except ToolError:
        raise
    except Exception as exc:
        logger.exception("workflow_context_stage_failed", stage=stage)
        raise ToolError("stage_failed", f"{stage} failed: {exc}")
    logger.info(
        "workflow_context_stage_done",
        stage=stage,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


@tool_output(content_type="text/plain", tool="set_workflow")
async def _do_set_workflow(workflow_id: str, runtime: ToolRuntime) -> dict:
    ctx = runtime.context
    wf_id = (workflow_id or "").strip()
    if not wf_id:
        raise ToolError("bad_input", "workflow_id is required.")
    logger.info(
        "set_workflow_tool_start",
        workflow_id=wf_id,
        chat_id=getattr(ctx, "chat_id", ""),
    )

    snapshot = await load_authorized_workflow(
        ctx,
        wf_id,
        Action.USE,
    )
    meta = snapshot.meta
    workflow = snapshot.workflow

    def _persist() -> None:
        ctx.workflow = workflow
        _persist_current_workflow(ctx, wf_id)

    # Re-check immediately before changing the durable Chat binding.
    await recheck_platform_workflow_action(
        ctx,
        wf_id,
        Action.USE,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    await _run_stage("set_workflow.persist_chat_binding", _persist)
    result = {
        "workflow_id": wf_id,
        "workflow_name": meta.get("workflow_name", ""),
        "description": meta.get("description", ""),
        "version": meta.get("active_major"),
        "subversion": meta.get("active_sub"),
    }
    logger.info(
        "set_workflow_tool_done",
        workflow_id=wf_id,
        chat_id=getattr(ctx, "chat_id", ""),
    )
    return result


@tool(response_format="content_and_artifact")
async def set_workflow(workflow_id: str, *, runtime: ToolRuntime) -> str:
    """Select the workflow that workflow tools operate on in this chat.

    Args:
        workflow_id: workflow id to select.

    Returns:
        content = selected workflow id, name, and active version.
    """
    return await _do_set_workflow(workflow_id, runtime)


@tool_output(content_type="text/plain", tool="create_workflow")
async def _do_create_workflow(
    name: str,
    runtime: ToolRuntime,
    description: str = "",
) -> dict:
    total_started = time.perf_counter()
    ctx = runtime.context
    clean_name = (name or "").strip()
    if not clean_name:
        raise ToolError("bad_input", "name is required.")
    logger.info(
        "create_workflow_tool_start",
        chat_id=getattr(ctx, "chat_id", ""),
        name=clean_name,
        has_description=bool(description),
    )

    db_started = time.perf_counter()
    snapshot = await create_authorized_workflow(
        ctx,
        name=clean_name,
        description=description or "",
    )
    meta = snapshot.meta
    wf_id = meta.get("wf_id")
    if not wf_id:
        raise ToolError("create_failed", "workflow creation returned no workflow id")
    workflow = snapshot.workflow

    def _persist() -> None:
        ctx.workflow = workflow
        _persist_current_workflow(ctx, wf_id)

    await _run_stage("create_workflow.persist_chat_binding", _persist)
    result = {
        "workflow_id": wf_id,
        "workflow_name": meta.get("workflow_name", clean_name),
        "description": meta.get("description", description or ""),
        "version": meta.get("active_major"),
        "subversion": meta.get("active_sub"),
        "db_elapsed_ms": int((time.perf_counter() - db_started) * 1000),
    }

    result["total_elapsed_ms"] = int((time.perf_counter() - total_started) * 1000)
    logger.info(
        "create_workflow_tool_done",
        workflow_id=result["workflow_id"],
        db_elapsed_ms=result.get("db_elapsed_ms"),
        total_elapsed_ms=result["total_elapsed_ms"],
    )
    return result


@tool(response_format="content_and_artifact")
async def create_workflow(
    name: str,
    description: str = "",
    *,
    runtime: ToolRuntime,
) -> str:
    """Create a workflow and select it for this chat.

    Args:
        name: workflow name.
        description: optional workflow description.

    Returns:
        content = created workflow id, name, description, and active version.
    """
    return await _do_create_workflow(name, runtime, description)


CHAT_WORKFLOW_CONTEXT_TOOLS = [set_workflow, create_workflow]
