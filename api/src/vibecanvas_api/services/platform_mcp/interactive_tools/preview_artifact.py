"""Durable Preview artifact publication, independent of public tool aliases."""
from __future__ import annotations

import hashlib
import logging
import uuid
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any

from vibecanvas_api.agents.tool_runtime import ToolRuntime
from vibecanvas_api.agents.tools._envelope import tool_ok
from vibecanvas_api.agents.tools._session_fs import _require_session
from vibecanvas_api.agents.tools.decorator import (
    ToolError,
    _approx_tokens,
    _head_tail_preview,
    _offload_from_runtime,
    _serialize,
)
from vibecanvas_api.config import config
from vibecanvas_api.services.platform_mcp.interactive_tools.schema import validate_view

logger = logging.getLogger(__name__)

INTERACTIVE_CONTENT_TYPE = "application/vnd.vibecanvas.interactive-artifact+json"

def _default_height(component_type: str) -> int:
    if component_type == "url_preview":
        return 520
    return 320


def _default_preview(component_type: str) -> dict[str, str]:
    # Layout is a client capability, not an Agent decision. Main chat exposes
    # expansion for file/HTML/URL content; the browser side panel stays inline.
    return {
        "mode": "optional"
        if component_type in {"file_preview", "url_preview", "workflow_preview"}
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


class _HtmlMediaReferences(HTMLParser):
    """Match the image/media resources blocked by the Preview CSP."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.external_count = 0

    def handle_starttag(self, tag, attrs):
        if tag not in {"img", "source", "video", "audio", "image"}:
            return
        for name, value in attrs:
            if not value:
                continue
            if name in {"src", "poster", "href", "xlink:href"}:
                self.external_count += bool(re.match(r"^(?:https?:)?//", value.strip(), re.I))
            elif name == "srcset":
                self.external_count += len(re.findall(r"(?:^|,)\s*(?:https?:)?//", value, re.I))


async def _validate_html_media(session, path: str) -> None:
    result = await session.read_file(path)
    if not result.get("ok") or not isinstance(result.get("content"), str):
        raise ToolError("file_preview_read_failed", f"Unable to read HTML before publishing Preview: {path}")
    parser = _HtmlMediaReferences()
    parser.feed(result["content"])
    if parser.external_count:
        raise ToolError(
            "file_preview_external_media",
            "HTML Preview blocks external image/media URLs. Save these assets in the sandbox "
            "and reference their absolute sandbox paths or paths relative to this HTML file "
            "(data: images are also supported), then publish again. Keep original website URLs "
            "as clickable source links, not img src values.",
            info={"path": path, "external_media_count": parser.external_count},
        )


async def _prepare_file_preview(*, runtime: ToolRuntime, path: str) -> None:
    """Persist a generated file before publishing its path to Preview."""
    is_html = PurePosixPath(path).suffix.lower() in {".html", ".htm"}
    if not path.startswith("/data/") and not is_html:
        return
    session = await _require_session(runtime.context)
    if is_html:
        await _validate_html_media(session, path)
    if not path.startswith("/data/"):
        return
    if not await session.sync_workspace_path(path):
        raise ToolError(
            "file_preview_sync_failed",
            f"Unable to persist {path} before creating its Preview.",
            info={"path": path},
        )


async def _render_view(
    type: str,
    path: str = "",
    title: str = "",
    file_type: str = "auto",
    description: str = "",
    url: str = "",
    *,
    workflow_id: str = "",
    version: str = "",
    publisher_tool: str = "render_preview",
    runtime: ToolRuntime,
) -> tuple[str, dict[str, Any]]:
    """Build and persist one Preview artifact behind a flat public tool.

    Used by the sole public render_preview entrypoint.
    Historical artifact schemas remain readable; new HTML is published as a file.
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
    elif component_type == "url_preview":
        view_input.update(url=url, description=description)
    elif component_type == "workflow_preview":
        view_input.update(workflow_id=workflow_id, version=version, description=description)
    view_obj = validate_view(view_input)
    component_type = view_obj.type
    props_obj = view_obj.model_dump(exclude={"type"}, exclude_none=True, mode="json")
    title_clean = (title or "").strip()
    if not title_clean:
        if component_type == "file_preview":
            title_clean = PurePosixPath(str(props_obj.get("path") or "")).name
        elif component_type == "url_preview":
            title_clean = "Web preview"
        elif component_type == "workflow_preview":
            title_clean = f"Workflow {version}"
        else:
            title_clean = "Interactive preview"
    if component_type == "file_preview":
        preview_path = str(props_obj.get("path") or "")
        await _prepare_file_preview(
            runtime=runtime,
            path=preview_path,
        )
    completion_mode = "render_only"
    artifact_id = f"ia_{uuid.uuid4().hex[:12]}"

    definition: dict[str, Any] = {
        "kind": "interactive_artifact",
        "schema_version": 1,
        "artifact_id": artifact_id,
        "title": title_clean,
        "component_type": component_type,
        "props": props_obj,
        "interaction_schema": {},
        "completion_mode": completion_mode,
        "require_human_confirm": False,
        "height": _default_height(component_type),
        "preview": _default_preview(component_type),
        "widget_state": {},
        "hitl_request_id": None,
        "interaction_state": {
            "is_interacted": False,
            "status": "none",
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
    if component_type == "workflow_preview":
        # Small trusted reference remains available even if the card is offloaded.
        artifact["meta"]["workflow_preview"] = {"id": workflow_id, "version": version}
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
        logger.warning("preview_artifact_persist_failed", exc_info=True)
        raise ToolError(
            "interactive_persistence_failed",
            "The interactive content could not be saved, so no non-recoverable card was shown.",
        ) from exc
