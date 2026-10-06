from __future__ import annotations

import ast
from pathlib import Path

import pytest


_API_SRC = Path(__file__).resolve().parents[2] / "src" / "vibecanvas_api"


@pytest.mark.parametrize(
    ("relative_path", "function_name", "scanner"),
    [
        ("routes/vfs.py", "upload_file", "require_clean_upload"),
        ("routes/storage.py", "upload_storage_file", "require_clean_upload"),
        ("routes/chats.py", "upload_chat_attachment", "require_clean_upload"),
        ("routes/kb.py", "upload_file", "_write_file"),
        ("routes/kb.py", "_write_file", "require_clean_upload"),
        ("routes/skills.py", "_read_custom_bundle", "require_clean_upload"),
    ],
)
def test_every_user_file_ingress_requires_clean_scan(
    relative_path: str,
    function_name: str,
    scanner: str,
) -> None:
    path = _API_SRC / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    assert len(functions) == 1
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == scanner
        for node in ast.walk(functions[0])
    ), f"{relative_path}:{function_name} bypasses the upload scanner"
