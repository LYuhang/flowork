"""`@tool_output` — the per-tool decorator (spec 2026-06-22 §2, §2.1, §4.6).

TWO-CHANNEL output (``response_format="content_and_artifact"``):
- **content** (a string) → ``ToolMessage.content`` — what the MODEL reads. The current
  agent-facing form only: fresh = the full body (or head/tail when large); after
  compaction = the selected degraded form. Errors → the human message.
- **artifact** (a dict) → ``ToolMessage.artifact`` — what the model does NOT read;
  the FRONTEND (render) and the COMPACTION middleware consume it. Holds the machinery:
  ``content_type``, ``path``, the degraded ``forms``, ``tool``, ``stale_on_reread``,
  ``auxiliary``, chaining ``handles``.

So machinery (content_type/path/flags) never pollutes what the agent reads; the path
is also woven into the abstract so the agent still knows where the full body is.

Stack under the Runtime-neutral ``@tool`` descriptor::

    @tool(response_format="content_and_artifact")
    @tool_output(content_type="application/json", tool="node_execute")
    async def node_execute(...):
        return result          # success: the RAW payload (render builds content+artifact)
        # error: raise ToolError(code, message)   ← captured into an error (content, artifact)
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
from typing import Callable

def _serialize(content) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


def _approx_tokens(text: str) -> int:
    """Cheap chars≈4 token estimate (soft budget; the real tokenizer refines later)."""
    return max(0, len(text or "") // 4)


def _head_tail_preview(text: str, preview_chars: int) -> str:
    """A small head+tail excerpt of ``text`` (≈2/3 head, 1/3 tail), elision in the
    middle. The full body is re-readable via the file ref, so this is just a signal."""
    if len(text) <= preview_chars:
        return text
    head = preview_chars * 2 // 3
    tail = preview_chars - head
    return f"{text[:head]}\n…\n{text[-tail:]}"


def _token_meta(content: str, abstract: str, ref: str | None = None) -> dict:
    return {
        "content": _approx_tokens(content),
        "content_abstract": _approx_tokens(abstract),
        "ref": _approx_tokens(ref or ""),
    }


def _make_ref(tool: str | None, path: str | None, content_hash: str) -> str:
    if path:
        return path
    name = tool or "tool_output"
    return f"tool://{name}/{content_hash[:12]}"


def _build_envelope(
    *,
    status: str,
    error: dict | None,
    content: str,
    content_abstract: str,
    ref: str | None,
    artifact: dict | None,
    payload: dict | None,
    meta: dict,
) -> dict:
    """Canonical new tool envelope.

    The model sees ``ToolMessage.content``. The full envelope rides
    ``ToolMessage.artifact`` for frontend rendering and memory middleware.
    """
    return {
        "schema_version": 1,
        "status": status,
        "error": error,
        "content": content,
        "content_abstract": content_abstract,
        "ref": ref,
        "artifact": artifact or {},
        "payload": payload or {},
        "meta": meta,
    }


def build_content_and_artifact(
    rendered_content,
    content_type: str,
    *,
    abstract: str,
    auxiliary: list | None = None,
    handles: dict | None = None,
    tool: str | None = None,
    stale_on_reread: bool = False,
    is_viewer: bool = False,
    cfg,
    offload: Callable[[str, str], str] | None = None,
    path: str | None = None,
    inline_chars: int | None = None,
    offload_preview_chars: int | None = None,
) -> tuple[str, dict]:
    """Assemble the ``(content, artifact)`` pair.

    Large-output GUARDRAIL (claim-check): a NON-viewer tool whose serialized output
    exceeds ``cfg.inline_chars`` is truncated to a head+tail PREVIEW + a notice; the
    full body is offloaded to VFS (``offload`` → ``/memory/outputs/…``) and the agent
    re-reads it via read_file. ``is_viewer=True`` (read_file) returns the full body
    (the middleware ages it via the size tiers). All machinery rides ``artifact`` (the
    model never reads it); ``content`` is the only thing the model sees.
    """
    serialized = _serialize(rendered_content)
    # A successful command may be intentionally quiet (mkdir, redirects, mv,
    # validation tools, and empty-file reads are common examples).  Returning
    # an empty ToolMessage is not neutral: several OpenAI-compatible models
    # interpret the empty function result as an end-of-loop signal and may
    # produce an empty terminal AIMessage.  The deterministic render abstract
    # is already the canonical semantic description of the result, so use it
    # as model-visible feedback only when the raw rendering has no meaningful
    # characters.  The payload size/hash below still describe the untouched
    # raw output, and every non-empty output remains byte-for-byte unchanged.
    empty_output_feedback = (abstract or "").strip()
    if not serialized.strip() and not empty_output_feedback:
        empty_output_feedback = "Tool completed successfully with no output."
    inline_limit = int(inline_chars if inline_chars is not None else cfg.inline_chars)
    preview_limit = int(
        offload_preview_chars
        if offload_preview_chars is not None
        else cfg.offload_preview_chars
    )
    is_large = len(serialized) > inline_limit

    if is_large and not is_viewer:
        if path is None and offload is not None:
            path = offload(serialized, content_type)
        preview = _head_tail_preview(serialized, preview_limit)
        where = (f"full content saved to {path} — read_file to view"
                 if path else "full content not stored")
        content = f"{preview}\n…[output too long ({len(serialized)} chars), truncated; {where}]"
        if path:
            abstract = f"{abstract} (full at {path}, read_file to view)"
    else:
        content = serialized                       # small body, or the read_file viewer

    if not serialized.strip():
        content = empty_output_feedback

    content_hash = hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()
    ref = _make_ref(tool, path, content_hash)
    meta = {
        "tool": tool,
        "content_type": content_type,
        "stale_on_reread": stale_on_reread,
        "tokens": _token_meta(content, abstract, ref),
        "content_hash": f"sha256:{content_hash}",
    }
    artifact_body = {
        "kind": "tool_result",
        "target": {"path": path} if path else {},
        "auxiliary": auxiliary or [],
        "handles": handles or {},
    }
    payload = {
        "kind": "inline" if not path else "offloaded",
        "ref": path,
        "hash": f"sha256:{content_hash}",
        "size": {"chars": len(serialized), "tokens": _approx_tokens(serialized)},
    }
    envelope = _build_envelope(
        status="success",
        error=None,
        content=content,
        content_abstract=abstract,
        ref=ref,
        artifact=artifact_body,
        payload=payload,
        meta=meta,
    )
    return content, envelope


class ToolError(Exception):
    """Raise from a tool body for an agent-relevant error (§4.6). ``str()`` == the
    short CODE (e.g. ``"unknown_node"``); ``message`` = the human explanation; ``info``
    = optional structured payload. ``@tool_output`` captures it → an error
    (content, artifact): content = the message (the agent reads it), artifact.status
    = "error", artifact.error = the code. Tools should NOT swallow agent-relevant
    failures — raise this instead."""

    def __init__(self, code: str, message: str | None = None, *, info=None):
        super().__init__(code)
        self.message = message
        self.info = info


def _error_content_and_artifact(exc: BaseException, tool: str | None) -> tuple[str, dict]:
    message = getattr(exc, "message", None)
    info = getattr(exc, "info", None)
    code = str(exc)
    content = message or code                      # the agent reads the human explanation
    abstract = message or code
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    envelope = _build_envelope(
        status="error",
        error={
            "code": code,
            "message": abstract,
            "type": type(exc).__name__,
            "info": info,
        },
        content=content,
        content_abstract=abstract,
        ref=_make_ref(tool, None, content_hash),
        artifact={
            "kind": "tool_error",
            "errors": [{"code": code, "message": abstract, "info": info}],
        },
        payload={"kind": "none"},
        meta={
            "tool": tool,
            "content_type": "text/plain",
            "stale_on_reread": False,
            "tokens": _token_meta(content, abstract),
            "content_hash": f"sha256:{content_hash}",
        },
    )
    return content, envelope


def _unpack(result) -> tuple:
    """A tool returns `payload` | `(payload, auxiliary)` | `(payload, auxiliary, handles)`."""
    if isinstance(result, tuple):
        if len(result) == 3:
            return result[0], result[1], result[2]
        if len(result) == 2:
            return result[0], result[1], None
        return result[0], None, None
    return result, None, None


def _find_runtime(args, kwargs):
    """Locate the ToolRuntime whether it was passed by keyword (``runtime=``) or
    positionally (workers run via ``asyncio.to_thread(fn, …, runtime, session)``),
    so large-output VFS offload works for every tool."""
    rt = kwargs.get("runtime")
    if rt is not None:
        return rt
    return next((a for a in args if hasattr(a, "context")), None)


def _offload_from_runtime(runtime, *, base_dir: str | None = None):
    """Build an offload callable that writes the full body to ``offload_dir`` (under
    /memory — persisted + user-visible + read_file-readable) via VFS scratch, and
    returns the path. None when there's no usable VFS (offload becomes a no-op → the
    guardrail still truncates, just without a re-readable ref)."""
    if runtime is None:
        return None
    ctx = getattr(runtime, "context", None)
    vfs = getattr(ctx, "vfs", None)
    if vfs is None:
        return None

    def offload(serialized: str, content_type: str) -> str | None:
        import uuid
        from vibecanvas_api.config import config
        base = (base_dir or config.agent.compaction_v2.offload_dir).rstrip("/")
        path = f"{base}/out_{uuid.uuid4().hex[:8]}.txt"
        try:
            vfs.write_scratch(wf_id=ctx.wf_id, path=path,
                              content=serialized, content_type=content_type)
        except Exception:  # noqa: BLE001 — offload is best-effort; guardrail still truncates
            return None
        return path
    return offload


def tool_output(
    content_type: str = "text/plain",
    *,
    tool: str | None = None,
    stale_on_reread: bool = False,
    is_viewer: bool = False,
    inline_chars: int | None = None,
    offload_preview_chars: int | None = None,
):
    """The per-tool decorator. The body returns its natural payload (or raises
    ToolError); this dispatches to the tool's RENDER scheme (registry §2.1) and emits
    the ``(content, artifact)`` two-channel pair. Returns that tuple — so the public
    tool must be ``@tool(response_format="content_and_artifact")``."""
    def deco(fn):
        tool_name = tool or getattr(fn, "__name__", None)

        def _finish(result, runtime) -> tuple[str, dict]:
            from vibecanvas_api.config import config
            from vibecanvas_api.agents.tools.render import RenderCtx, render
            payload, auxiliary, handles = _unpack(result)
            ctx = RenderCtx(tool=tool_name, content_type=content_type, extras=handles or {})
            r = render(payload, ctx)
            return build_content_and_artifact(
                r.content, r.content_type, abstract=r.abstract,
                auxiliary=r.auxiliary if r.auxiliary is not None else auxiliary,
                handles=r.extras, tool=tool_name, stale_on_reread=stale_on_reread,
                is_viewer=is_viewer,
                cfg=config.agent.compaction_v2, offload=_offload_from_runtime(runtime),
                path=r.path,
                inline_chars=inline_chars,
                offload_preview_chars=offload_preview_chars)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — capture for the agent (§4.6)
                    return _error_content_and_artifact(exc, tool_name)
                return _finish(result, _find_runtime(args, kwargs))
            return awrapper

        @functools.wraps(fn)                       # sync tools (run via asyncio.to_thread)
        def swrapper(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                return _error_content_and_artifact(exc, tool_name)
            return _finish(result, _find_runtime(args, kwargs))
        return swrapper
    return deco


def tool_error_boundary(*, tool: str):
    """Convert exceptions to the canonical error envelope without rendering success.

    Use this for tools such as ``render_interactive`` that construct a custom
    success artifact but still need the same agent-readable error semantics as
    ``@tool_output``. The wrapped function's successful return value is passed
    through unchanged.
    """
    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — canonical tool error boundary
                    return _error_content_and_artifact(exc, tool)
            return awrapper

        @functools.wraps(fn)
        def swrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                return _error_content_and_artifact(exc, tool)
        return swrapper
    return deco
