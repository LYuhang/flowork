"""Small platform boundary for native draw.io files.

Agents edit native XML; flowork-cli owns review and render feedback.
Flowork only needs to recognise an ordinary ``.drawio`` VFS file and reject
unsafe or structurally broken XML before publishing it in Preview.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final


DRAWIO_MIME_TYPE: Final = "application/vnd.jgraph.mxfile"
MAX_DRAWIO_SOURCE_BYTES: Final = 8 * 1024 * 1024


@dataclass(frozen=True)
class DrawioInspection:
    issues: tuple[dict[str, Any], ...]
    cells: int
    vertices: int
    edges: int
    pages: int
    source_hash: str
    warnings: tuple[dict[str, Any], ...] = ()
    page_details: tuple[dict[str, Any], ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues

    def preview_metadata(self) -> dict[str, Any]:
        return {
            "status": "valid" if self.valid else "invalid",
            "format": "drawio",
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "sourceHash": self.source_hash,
            "summary": {
                "pages": self.pages,
                "cells": self.cells,
                "vertices": self.vertices,
                "edges": self.edges,
            },
        }


def _issue(code: str, message: str, *, stage: str = "schema") -> dict[str, str]:
    return {
        "severity": "error",
        "stage": stage,
        "code": code,
        "json_pointer": "",
        "message": message,
    }


def inspect_drawio(data: bytes) -> DrawioInspection:
    """Use the same page-scoped structural rules as the Agent CLI."""
    from vibecanvas_api.diagram_runtime.structure import inspect_structure

    source_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
    if len(data) > MAX_DRAWIO_SOURCE_BYTES:
        return DrawioInspection((_issue("source-too-large", "The draw.io source exceeds the existing 8 MiB preview limit."),), 0, 0, 0, 0, source_hash)
    errors, warnings, pages = inspect_structure(data)
    issues = tuple({**item, "severity": "error", "stage": "schema", "json_pointer": ""} for item in errors)
    return DrawioInspection(
        issues, sum(page["cells"] for page in pages),
        sum(page["vertices"] for page in pages), sum(page["edges"] for page in pages),
        len(pages), source_hash, tuple(warnings), tuple(pages),
    )


__all__ = [
    "DRAWIO_MIME_TYPE",
    "MAX_DRAWIO_SOURCE_BYTES",
    "DrawioInspection",
    "inspect_drawio",
]
