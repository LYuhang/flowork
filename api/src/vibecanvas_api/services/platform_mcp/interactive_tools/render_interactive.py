"""Platform MCP ``render_interactive`` — durable local-file previews."""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import PurePosixPath
from typing import Any

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools._session_fs import _require_session
from vibecanvas_api.agents.tools._envelope import tool_ok
from vibecanvas_api.agents.tools.decorator import (
    ToolError,
    _approx_tokens,
    _head_tail_preview,
    _offload_from_runtime,
    _serialize,
    tool_error_boundary,
)
from vibecanvas_api.services.platform_mcp.interactive_tools.schema import validate_view
from vibecanvas_api.config import config

logger = logging.getLogger(__name__)

INTERACTIVE_CONTENT_TYPE = "application/vnd.vibecanvas.interactive-artifact+json"

def _default_height(component_type: str) -> int:
    if component_type == "url_preview":
        return 520
    return 360 if component_type == "html_preview" else 320


def _default_preview(component_type: str) -> dict[str, str]:
    # Layout is a client capability, not an Agent decision. Main chat exposes
    # expansion for HTML/file content; the browser side panel stays inline.
    return {
        "mode": "optional"
        if component_type in {"html_preview", "file_preview", "url_preview"}
        else "none"
    }


def _preview_payload(full: dict[str, Any], preview_chars: int) -> dict[str, Any]:
    text = _serialize(full)
    return {
        "artifact_id": full.get("artifact_id"),
        "title": full.get("title"),
        "component_type": full.get("component_type"),
        "preview": _head_tail_preview(text, preview_chars),
    }


async def _prepare_file_preview(*, runtime: ToolRuntime, path: str) -> None:
    """Persist a generated file before publishing its path to Preview."""
    if not path.startswith("/data/"):
        return
    session = await _require_session(runtime.context)
    if not await session.sync_workspace_path(path):
        raise ToolError(
            "file_preview_sync_failed",
            f"Unable to persist {path} before creating its Preview.",
            info={"path": path},
        )


@tool(response_format="content_and_artifact")
@tool_error_boundary(tool="render_interactive")
async def render_interactive(
    path: str,
    title: str = "",
    file_type: str = "auto",
    description: str = "",
    require_human_confirm: bool = False,
    *,
    runtime: ToolRuntime,
) -> tuple[str, dict[str, Any]]:
    """Publish an existing local file as a durable Preview card.

    Pass the absolute ``path`` returned by a file-producing tool. ``file_type``
    defaults to ``auto`` so the browser selects the renderer from the file
    extension and MIME metadata. Set it only for an extensionless or ambiguous
    file. ``title`` is optional and defaults to the file name.

    Examples: ``path="/data/report.pdf"`` or
    ``path="/data/diagrams/system.drawio", description="System diagram"``.
    To preview a web address, use the separate ``render_url_preview`` tool. To
    show generated HTML, save it as an ``.html`` file first and publish that
    file here. Do not add a ``type`` field or a nested ``view`` object.
    """
    return await _render_view(
        type="file_preview",
        path=path,
        title=title,
        file_type=file_type,
        description=description,
        require_human_confirm=require_human_confirm,
        runtime=runtime,
    )


async def _render_view(
    type: str,
    path: str = "",
    title: str = "",
    file_type: str = "auto",
    description: str = "",
    html: str = "",
    url: str = "",
    require_human_confirm: bool = False,
    *,
    runtime: ToolRuntime,
) -> tuple[str, dict[str, Any]]:
    """Build and persist one Preview artifact behind a flat public tool.

    Agents never call this helper. ``render_interactive`` exposes only a local
    file path, while ``render_url_preview`` exposes only an HTTP(S) URL. Keeping
    the shared artifact/persistence machinery internal prevents heterogeneous
    models from seeing a nested or discriminated Preview protocol. The legacy
    HTML branch remains an internal data-model path for durable artifacts; it is
    not part of either current MCP input schema.
    """
    cfg = config.agent.compaction_v2
    component_type = (type or "").strip()
    view_input: dict[str, Any] = {"type": component_type}
    if component_type == "file_preview":
        view_input.update(
            path=path,
            file_type=file_type or "auto",
            description=description,
        )
    elif component_type == "html_preview":
        view_input["html"] = html
    elif component_type == "url_preview":
        view_input.update(url=url, description=description)
    view_obj = validate_view(view_input)
    component_type = view_obj.type
    props_obj = view_obj.model_dump(exclude={"type"}, exclude_none=True, mode="json")
    title_clean = (title or "").strip()
    if not title_clean:
        if component_type == "file_preview":
            title_clean = PurePosixPath(str(props_obj.get("path") or "")).name
        elif component_type == "url_preview":
            title_clean = "Web preview"
        else:
            title_clean = "Interactive preview"
    if component_type == "file_preview":
        preview_path = str(props_obj.get("path") or "")
        await _prepare_file_preview(
            runtime=runtime,
            path=preview_path,
        )
    schema_obj = (
        {
            "interaction_type": "continue",
            "submit_label": "Continue",
        }
        if require_human_confirm
        else {}
    )
    completion_mode = (
        "wait_for_submit"
        if require_human_confirm
        else "render_only"
    )
    artifact_id = f"ia_{uuid.uuid4().hex[:12]}"

    definition: dict[str, Any] = {
        "kind": "interactive_artifact",
        "schema_version": 1,
        "artifact_id": artifact_id,
        "title": title_clean,
        "component_type": component_type,
        "props": props_obj,
        "interaction_schema": schema_obj,
        "completion_mode": completion_mode,
        "require_human_confirm": bool(require_human_confirm),
        "height": _default_height(component_type),
        "preview": _default_preview(component_type),
        "widget_state": {},
        # The outer Agent Loop creates and binds the post-tool HITL request
        # after it observes this durable ToolMessage. Tools describe content;
        # they do not own pause/resume control flow.
        "hitl_request_id": None,
        "interaction_state": {
            "is_interacted": False,
            # This value is never used to drive control flow. For a Continue
            # gate the outer Agent Loop replaces it with ``pending`` only
            # after the durable HITL row and checkpoint reference both exist.
            "status": (
                "awaiting_loop_gate"
                if completion_mode == "wait_for_submit"
                else "none"
            ),
            "result": {},
        },
    }

    serialized = _serialize(definition)
    content_hash = hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()
    inline_limit = int(cfg.interactive_artifact_inline_chars)
    preview_chars = int(cfg.interactive_artifact_offload_preview_chars)
    path: str | None = None
    inline_definition: dict[str, Any] | None = definition

    if len(serialized) > inline_limit:
        offload = _offload_from_runtime(
            runtime,
            base_dir=cfg.interactive_artifact_offload_dir,
        )
        if offload is not None:
            path = offload(serialized, INTERACTIVE_CONTENT_TYPE)
            if path:
                inline_definition = None

    output: dict[str, Any] = {
        "content_type": INTERACTIVE_CONTENT_TYPE,
        "data": inline_definition if inline_definition is not None else _preview_payload(definition, preview_chars),
        "artifact_id": artifact_id,
        "component_type": component_type,
        "completion_mode": completion_mode,
        "full_chars": len(serialized),
        "full_tokens": _approx_tokens(serialized),
        "hash": f"sha256:{content_hash}",
    }
    if path:
        output["path"] = path

    publisher_tool = (
        "render_url_preview"
        if component_type == "url_preview"
        else "render_interactive"
    )
    abstract = f"{publisher_tool} → {component_type}: {title_clean}"
    content = tool_ok(abstract, output)
    artifact = {
        "schema_version": 1,
        "status": "success",
        "error": None,
        "content": content,
        "content_abstract": abstract,
        "ref": path or f"tool://{publisher_tool}/{content_hash[:12]}",
        "artifact": {
            "kind": "interactive_artifact",
            "target": {"path": path} if path else {},
        },
        "payload": {
            "kind": "interactive_artifact",
            "content_type": INTERACTIVE_CONTENT_TYPE,
            "artifact": inline_definition,
            "artifact_preview": None if inline_definition is not None else output["data"],
            "artifact_ref": path,
            "hitl_request_id": None,
            "hash": f"sha256:{content_hash}",
            "size": {"chars": len(serialized), "tokens": _approx_tokens(serialized)},
        },
        "meta": {
            "tool": publisher_tool,
            "content_type": INTERACTIVE_CONTENT_TYPE,
            "stale_on_reread": False,
            "tokens": {
                "content": _approx_tokens(content),
                "content_abstract": _approx_tokens(abstract),
                "ref": _approx_tokens(path or ""),
            },
            "content_hash": f"sha256:{content_hash}",
            "protect_recent_rounds": cfg.interactive_artifact_protect_recent_rounds,
        },
    }
    await _persist_interactive_state(
        runtime=runtime,
        artifact_id=artifact_id,
        definition=definition,
        component_type=component_type,
        completion_mode=completion_mode,
        title=title_clean,
        path=path,
        content_hash=f"sha256:{content_hash}",
    )
    return content, artifact


async def _persist_interactive_state(
    *,
    runtime: ToolRuntime,
    artifact_id: str,
    definition: dict[str, Any],
    component_type: str,
    completion_mode: str,
    title: str,
    path: str | None,
    content_hash: str,
) -> None:
    ctx = runtime.context
    tenant_id = getattr(ctx, "tenant_id", None)
    chat_id = getattr(ctx, "chat_id", None)
    run_id = getattr(ctx, "turn_id", None)
    if not tenant_id or not chat_id:
        raise ToolError(
            "interactive_persistence_context_missing",
            "Interactive content requires a durable chat context and was not rendered.",
        )
    try:
        from vibecanvas_api.storage.db import session_scope
        from vibecanvas_api.storage.hitl_repo import HitlRepo

        async with session_scope(tenant_id=str(tenant_id)) as session:
            repo = HitlRepo(session)
            await repo.create_interactive_artifact(
                artifact_id=artifact_id,
                tenant_id=str(tenant_id),
                chat_id=str(chat_id),
                run_id=str(run_id) if run_id else None,
                component_type=component_type,
                completion_mode=completion_mode,
                title=title,
                definition_json=definition,
                artifact_ref=path,
                content_hash=content_hash,
                hitl_request_id=None,
            )
            # The outer Loop may observe this ToolMessage immediately after the
            # tool returns, so the artifact fact must already be committed.
            await repo.commit()
    except ToolError:
        raise
    except Exception as exc:
        logger.warning("render_interactive_persist_failed", exc_info=True)
        raise ToolError(
            "interactive_persistence_failed",
            "The interactive content could not be saved, so no non-recoverable card was shown.",
        ) from exc
