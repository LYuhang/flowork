"""Return image pixels in tool-message content, not in an unused context queue."""
from __future__ import annotations

import asyncio
import base64
import io
from urllib.parse import urlparse

from vibecanvas_api.agents.tools import workspace_fs

_MAX_IMAGES = 8


def _load_image(path: str) -> tuple[dict, dict]:
    from PIL import Image

    from vibecanvas_api.services.sandbox.fileops import run_fileop

    result = run_fileop(
        {"op": "read_bytes", "path": path},
        workspace_fs.roots(include_read_only=True),
    )
    if not result.get("ok"):
        raise ValueError(str(result.get("error") or "Could not read image"))
    data = base64.b64decode(result["data_b64"])
    with Image.open(io.BytesIO(data)) as image:
        image.load()  # Reject damaged/non-image files before reporting success.
        width, height = image.size
        image_format = image.format
        # Normalize other Pillow-readable formats to PNG for vision APIs;
        # animated images use their first frame.
        if image_format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "is_animated", False):
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
            data, image_format = output.getvalue(), "PNG"
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image_format]
    return (
        {"type": "image", "base64": base64.b64encode(data).decode("ascii"), "mime_type": mime},
        {"path": path, "width": width, "height": height, "mime_type": mime},
    )


async def _do_read_images(paths: list[str]) -> tuple:
    if not paths or len(paths) > _MAX_IMAGES:
        message = f"Pass between 1 and {_MAX_IMAGES} local image paths."
        return message, {"status": "error", "error": {"code": "invalid_paths", "message": message}}

    blocks, loaded, errors = [], [], []
    for path in paths:
        if urlparse(path).scheme in {"http", "https"}:
            errors.append(
                f"{path}: URLs are not supported. Save the image to a local path "
                "accessible in the current environment, then call read_images."
            )
            continue
        try:
            image, metadata = await asyncio.to_thread(_load_image, path)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            continue
        loaded.append(metadata)
        blocks.extend([
            {"type": "text", "text": f"Image {len(loaded)}: {path} ({metadata['width']} x {metadata['height']})"},
            image,
        ])

    if not loaded:
        message = "No images loaded. " + "; ".join(errors)
        return message, {"status": "error", "error": {"code": "no_images", "message": message}}
    summary = f"Loaded {len(loaded)} image(s)."
    if errors:
        summary += " Some paths could not be read:\n" + "\n".join(errors)
    return (
        [{"type": "text", "text": summary}, *blocks],
        {"status": "success", "loaded": loaded, "errors": errors},
    )


async def read_images(paths: list[str]) -> tuple:
    """View up to 8 local images using the model's vision capability.

    Pass absolute image paths in the current sandbox's workspace mounts.
    Download URL images to the workspace first; URLs are not accepted here.
    PNG, JPEG, WEBP, GIF, BMP and TIFF are supported; animated files show their
    first frame. Pixels are returned to the model, along with path and dimensions.
    Requires a model that supports image inputs.
    """
    return await _do_read_images(paths)
