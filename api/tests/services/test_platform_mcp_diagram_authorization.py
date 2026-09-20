"""Credential-free authoring MCPs stay inside the Chat sandbox."""
from __future__ import annotations

from vibecanvas_api.services.agent_runtime.mcp_desired_state import _builtin_server


def test_diagram_is_a_secret_free_sandbox_local_stdio_server() -> None:
    desired = _builtin_server("diagram")

    assert desired.source == "builtin_local"
    assert desired.connection.kind == "stdio"
    assert desired.connection.cwd == "/data"
    assert desired.connection.command == "flowork-diagram-mcp"
    assert not hasattr(desired.connection, "url")
    assert not hasattr(desired.connection, "headers")


def test_document_is_a_secret_free_sandbox_local_stdio_server() -> None:
    desired = _builtin_server("document")

    assert desired.source == "builtin_local"
    assert desired.connection.kind == "stdio"
    assert desired.connection.cwd == "/data"
    assert desired.connection.command == "flowork-document-mcp"
    assert desired.connection.environment_profile == "document-local"
    assert not hasattr(desired.connection, "url")
    assert not hasattr(desired.connection, "headers")
