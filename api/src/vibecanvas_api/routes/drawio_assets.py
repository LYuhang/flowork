"""Serve the pinned Desktop package's Web renderer without copying its assets.

This public endpoint serves installation-owned static files only. Diagram XML
stays in the browser and reaches the renderer via its existing embed protocol.
"""
from __future__ import annotations

from functools import lru_cache
import json
import mimetypes
import os
from pathlib import Path
import struct

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/v1/preview/drawio-assets", tags=["preview"])


@lru_cache(maxsize=1)
def _directory(path: str, modified_ns: int, size: int):
    with open(path, "rb") as source:
        prefix = source.read(16)
        if len(prefix) != 16:
            raise ValueError("invalid archive header")
        magic, header_size, _, json_size = struct.unpack("<4I", prefix)
        if magic != 4 or not 0 < json_size <= header_size <= min(size - 8, 16 * 1024 * 1024):
            raise ValueError("invalid archive header")
        node = json.loads(source.read(json_size))
    for part in ("drawio", "src", "main", "webapp"):
        node = node["files"][part]
    return node, 8 + header_size


@router.get("/{asset:path}")
def drawio_asset(asset: str):
    parts = asset.split("/")
    if not asset or any(part in {"", ".", ".."} for part in parts) or "\\" in asset:
        raise HTTPException(404, "not_found")
    path = Path(os.environ.get("DRAWIO_ASAR_PATH", "/opt/drawio/resources/app.asar"))
    try:
        metadata = path.stat()
        node, start = _directory(str(path), metadata.st_mtime_ns, metadata.st_size)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "drawio_renderer_unavailable") from None
    try:
        for part in parts:
            node = node["files"][part]
        offset, length = int(node["offset"]), int(node["size"])
        if node.get("unpacked") or offset < 0 or length < 0 or start + offset + length > metadata.st_size:
            raise KeyError(asset)
    except (KeyError, ValueError, TypeError):
        raise HTTPException(404, "not_found") from None

    def chunks():
        with path.open("rb") as source:
            source.seek(start + offset)
            remaining = length
            while remaining:
                data = source.read(min(remaining, 64 * 1024))
                if not data:
                    raise OSError("drawio asset truncated")
                remaining -= len(data)
                yield data

    return StreamingResponse(chunks(), media_type=mimetypes.guess_type(asset)[0] or "application/octet-stream",
        headers={
            "Content-Length": str(length),
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'self'; form-action 'none'",
        })
