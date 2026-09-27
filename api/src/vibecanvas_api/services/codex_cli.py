"""Codex CLI installation discovery shared by auth and sandbox launchers."""

from __future__ import annotations

import os
import shutil

from vibecanvas_api.config import config


def resolve_codex_executable() -> str | None:
    """Resolve the configured/PATH executable without reading credential state."""
    configured = str(getattr(config, "codex_cli_path", "") or "").strip()
    candidate = configured or shutil.which("codex") or ""
    if not candidate:
        return None
    resolved = os.path.realpath(candidate)
    if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        return None
    return resolved


def codex_cli_readonly_root(executable: str) -> str:
    """Return the smallest directory containing the CLI and native payloads."""
    resolved = os.path.realpath(executable)
    parent = os.path.dirname(resolved)
    # npm's official package entrypoint is ``<package>/bin/codex.js`` and loads
    # the platform binary from ``<package>/vendor``. Mounting only ``bin`` would
    # make the script visible but leave its native executable unavailable.
    if os.path.basename(parent) == "bin" and os.path.basename(resolved) == "codex.js":
        return os.path.dirname(parent)
    # Official native bundles keep their package marker and bundled rg/zsh/
    # bwrap beside bin/, not inside it. Codex resolves these through that marker;
    # mounting just bin/ silently strips the package layout inside the sandbox.
    # Match the native entrypoint and marker rather than widening every binary's
    # mount to its grandparent (which could otherwise expose unrelated files).
    package_root = os.path.dirname(parent)
    if (
        os.path.basename(parent) == "bin"
        and os.path.basename(resolved) == "codex"
        and os.path.isfile(os.path.join(package_root, "codex-package.json"))
    ):
        return package_root
    return parent


def codex_cli_node_runtime(executable: str) -> str | None:
    """Resolve the Node interpreter required by an npm-installed Codex CLI.

    ``resolve_codex_executable`` intentionally resolves symlinks, so the common
    npm installation becomes ``.../@openai/codex/bin/codex.js``. Mounting that
    package into gVisor is not enough: its ``#!/usr/bin/env node`` interpreter
    may live in an nvm directory that is outside the sandbox's standard binds.
    Native Codex binaries return ``None`` and need no additional mount.
    """
    resolved = os.path.realpath(executable)
    try:
        with open(resolved, "rb") as stream:
            shebang = stream.readline(256)
    except OSError:
        return None
    if not shebang.startswith(b"#!") or b"node" not in shebang:
        return None
    node = shutil.which("node")
    if not node:
        return None
    resolved_node = os.path.realpath(node)
    if not os.path.isfile(resolved_node) or not os.access(resolved_node, os.X_OK):
        return None
    return resolved_node


__all__ = [
    "codex_cli_node_runtime",
    "codex_cli_readonly_root",
    "resolve_codex_executable",
]
