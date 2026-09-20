"""Stdio MCP entry point for the sandbox-contained Document capability."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .render import render_document_feedback as _render_document_feedback
from .review import review_document as _review_document


mcp = FastMCP(
    "flowork-document",
    instructions=(
        "Review native office documents inside the current Chat sandbox. "
        "Final output paths are selected by the user or Agent; feedback images "
        "are revision-bound temporary evidence under /memory."
    ),
)


@mcp.tool(
    description=(
        "Inspect a generated DOCX, PPTX, XLSX, PDF, Markdown, HTML, CSV, TSV, "
        "SVG, or text file. Returns format facts plus deterministic errors and "
        "warnings. The path may be absolute within the sandbox workspace or "
        "relative to the current working directory."
    ),
    structured_output=True,
)
def review_document(path: str) -> dict[str, Any]:
    return _review_document(path)


@mcp.tool(
    description=(
        "Render DOCX, PPTX, XLSX, ODT, ODP, ODS, or PDF pages to revision-bound "
        "PNG files for visual inspection. Read every returned image with the "
        "Runtime image tool before claiming visual acceptance."
    ),
    structured_output=True,
)
def render_document_feedback(
    path: str,
    dpi: Annotated[int, Field(ge=96, le=220)] = 144,
    max_pages: Annotated[int, Field(ge=1, le=20)] = 8,
) -> dict[str, Any]:
    return _render_document_feedback(path, dpi=dpi, max_pages=max_pages)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
