"""read_images tool — load image file(s) so the agent can actually SEE them.

Unlike most tools (which return a text envelope), this one decodes the image(s)
and stages them on the turn context; ``ImageInjectionMiddleware`` then injects
them as multimodal content before the next model call, so the model sees the pixels.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
from urllib.parse import urlparse

from vibecanvas_api.services.platform_mcp.tool_runtime import ToolRuntime, tool

from vibecanvas_api.agents.tools.decorator import ToolError, tool_output
from vibecanvas_api.agents.tools.render import Rendered, register_render
from vibecanvas_api.services.file_format import _CONTENT_TYPE_BY_EXT, content_type_for

_MAX_IMAGES = 8
# Derive supported image extensions from the unified file_format registry.
_IMAGE_EXTS: list[str] = sorted(
    ext.lstrip(".") for ext, ct in _CONTENT_TYPE_BY_EXT.items()
    if ct.startswith("image/")
)
_SUPPORTED_FMTS = ", ".join(_IMAGE_EXTS)

# Default vision token cost per image: pixels / (32 * 32) — i.e. one token per
# 32×32 patch. A cheap, model-agnostic estimate the token accounting can record
# instead of (wrongly) tokenizing the base64 blob.
_PATCH = 32 * 32



def _mime_for(path: str) -> str | None:
    ct = content_type_for(path)
    return ct if ct.startswith("image/") else None


def _is_http_url(path: str) -> bool:
    parsed = urlparse(path)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _run_id_from_path(path: str) -> str:
    """Run-tier media lives at ``{run_prefix}/{run_id}/media/{hash}.{ext}`` — the store
    keys by run_id, which the path embeds as its first segment after ``run_prefix``."""
    from vibecanvas_api.config import config
    rest = path[len(config.vfs_paths.run_prefix + "/"):]
    return rest.split("/", 1)[0]


def _read_bytes(path: str, ctx) -> bytes | None:
    # local sandbox / host file
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    # run-tier (browser screenshots / run media)
    from vibecanvas_api.config import config
    if config.vfs_paths.is_run_path(path):
        store = getattr(ctx, "vfs_run", None)
        if store is None:
            return None
        run_id = _run_id_from_path(path) or getattr(ctx, "run_id", "") or ""
        return store.read_bytes_sync(run_id=run_id, path=path)
    # durable/workspace tiers (/mount /data /memory /logs)
    vfs = getattr(ctx, "vfs", None)
    if vfs is not None and hasattr(vfs, "read_bytes"):
        return vfs.read_bytes(wf_id=ctx.wf_id, path=path)
    return None


def _image_tokens(data: bytes) -> int:
    """Pixel-based token estimate: ceil(width * height / (32*32)). Fail-soft to a
    bytes-based rough estimate when the dimensions can't be read."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        return max(1, -(-(w * h) // _PATCH))  # ceil division
    except Exception:
        return max(1, len(data) // 1000)


@register_render("read_images")
def _render_read_images(raw: dict, ctx) -> Rendered:
    data = raw if isinstance(raw, dict) else {}
    loaded = data.get("loaded") or []
    errors = data.get("errors") or []
    if loaded:
        paths_str = "\n".join(f"  {p}" for p in loaded)
        content = f"Loaded {len(loaded)} image(s):\n{paths_str}"
        if errors:
            shown = "\n".join(f"  - {err}" for err in errors[:8])
            more = f"\n  - ... {len(errors) - 8} more" if len(errors) > 8 else ""
            content += f"\n{len(errors)} path(s) could not be read:\n{shown}{more}"
    else:
        content = "No images loaded"
    abstract = f"read_images → {len(loaded)} image(s) loaded"
    return Rendered(content=content, content_type="text/plain", abstract=abstract)


@tool_output(content_type="text/plain", tool="read_images")
async def _do_read_images(paths: list[str], runtime: ToolRuntime) -> dict:
    ctx = runtime.context
    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in (paths or []) if p]
    if not paths:
        raise ToolError("no_paths", "pass one or more image paths to read")
    if len(paths) > _MAX_IMAGES:
        raise ToolError("too_many", f"read at most {_MAX_IMAGES} images at once")

    staged, loaded, errors = [], [], []
    for p in paths:
        if _is_http_url(p):
            errors.append(
                f"{p}: URLs are not supported. read_images only reads local "
                "image files accessible in the current environment. Save the "
                "image to a local path first, then call read_images with that path."
            )
            continue
        mime = _mime_for(p)
        if mime is None:
            errors.append(f"{p}: unsupported format ({_SUPPORTED_FMTS})")
            continue
        try:
            data = await asyncio.to_thread(_read_bytes, p, ctx)
        except Exception as e:
            errors.append(f"{p}: {e}")
            continue
        if not data:
            errors.append(f"{p}: not found")
            continue
        tokens = await asyncio.to_thread(_image_tokens, data)
        staged.append({"mime": mime, "path": p, "tokens": tokens,
                       "b64": base64.b64encode(data).decode("ascii")})
        loaded.append(p)

    if not staged:
        raise ToolError("no_images", "; ".join(errors) or "no images loaded")

    pend = getattr(ctx, "pending_images", None)
    if isinstance(pend, list):
        pend.extend(staged)
    else:
        try:
            ctx.pending_images = list(staged)
        except Exception:
            raise ToolError("no_staging", "cannot stage images on this context")

    return {"loaded": loaded, "errors": errors}


@tool(response_format="content_and_artifact")
async def read_images(paths: list[str], *, runtime: ToolRuntime) -> str:
    """View one or more local image files to examine their visual content.

    This tool only accepts local image file paths accessible in the current
    environment. It does not accept http:// or https:// URLs. If an image is only
    available by URL, first save/download it to a local file, then call
    read_images with that local path. Up to 8 images can be read at once. The
    images are shown to you for analysis: inspect a chart or diagram, verify
    what a graphic looks like, compare images, or check visual details that
    cannot be inferred from text.
    Supported formats: png, jpg, jpeg, gif, webp, bmp, tiff.
    """
    return await _do_read_images(paths, runtime)
