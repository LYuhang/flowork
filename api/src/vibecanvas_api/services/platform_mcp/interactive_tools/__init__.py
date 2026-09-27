"""Cross-Runtime interactive artifact tools exposed through Platform MCP.

These tools are available on both the main chat surface and the browser
side-panel surface. They are not browser-control tools; they let the agent show
structured UI to the user inside the conversation transcript.
"""
from __future__ import annotations

from .render_choices import render_choices
from .render_preview import render_preview

INTERACTIVE_TOOLS = [render_preview, render_choices]

__all__ = [
    "INTERACTIVE_TOOLS", "render_preview", "render_choices",
]
