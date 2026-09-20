"""Small platform boundary for native draw.io files.

The official draw.io MCP owns authoring, page operations, layout and routing.
Flowork only needs to recognise an ordinary ``.drawio`` VFS file and reject
unsafe or structurally broken XML before publishing it in Preview.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
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

    @property
    def valid(self) -> bool:
        return not self.issues

    def preview_metadata(self) -> dict[str, Any]:
        return {
            "status": "valid" if self.valid else "invalid",
            "format": "drawio",
            "issues": list(self.issues),
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


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def inspect_drawio(data: bytes) -> DrawioInspection:
    """Inspect bounded native draw.io XML without interpreting its layout."""
    source_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
    issues: list[dict[str, Any]] = []
    if len(data) > MAX_DRAWIO_SOURCE_BYTES:
        issues.append(_issue(
            "source-too-large",
            "The draw.io source exceeds the 8 MiB preview limit.",
        ))
        return DrawioInspection(tuple(issues), 0, 0, 0, 0, source_hash)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        issues.append(_issue(
            "unsafe-xml-declaration",
            "DOCTYPE and ENTITY declarations are not allowed.",
        ))
        return DrawioInspection(tuple(issues), 0, 0, 0, 0, source_hash)
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        issues.append(_issue(
            "invalid-drawio-xml",
            "The file is not well-formed draw.io XML.",
        ))
        return DrawioInspection(tuple(issues), 0, 0, 0, 0, source_hash)

    root_name = _local_name(root)
    if root_name not in {"mxGraphModel", "mxfile"}:
        issues.append(_issue(
            "invalid-drawio-root",
            "Expected an mxGraphModel or mxfile root element.",
        ))

    cells = [element for element in root.iter() if _local_name(element) == "mxCell"]
    identifiers = [cell.get("id") for cell in cells if cell.get("id")]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for identifier in identifiers:
        if identifier in seen:
            duplicates.add(identifier)
        seen.add(identifier)
    if duplicates:
        issues.append(_issue(
            "duplicate-drawio-cell-id",
            "Duplicate mxCell IDs: " + ", ".join(sorted(duplicates)[:10]),
            stage="semantic",
        ))

    dangling = [
        f"{cell.get('id', '(unknown)')}.{terminal}={reference}"
        for cell in cells
        if cell.get("edge") == "1"
        for terminal in ("source", "target")
        if (reference := cell.get(terminal)) and reference not in seen
    ]
    if dangling:
        issues.append(_issue(
            "dangling-drawio-terminal",
            "Dangling edge terminals: " + ", ".join(dangling[:10]),
            stage="semantic",
        ))

    pages = (
        sum(_local_name(element) == "diagram" for element in root)
        if root_name == "mxfile"
        else 1
    )
    return DrawioInspection(
        tuple(issues),
        len(cells),
        sum(cell.get("vertex") == "1" for cell in cells),
        sum(cell.get("edge") == "1" for cell in cells),
        pages,
        source_hash,
    )


__all__ = [
    "DRAWIO_MIME_TYPE",
    "MAX_DRAWIO_SOURCE_BYTES",
    "DrawioInspection",
    "inspect_drawio",
]
