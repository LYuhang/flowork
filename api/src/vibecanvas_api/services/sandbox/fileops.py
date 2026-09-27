"""Pure, in-sandbox file-op executor.

``run_fileop(op, roots)`` performs ONE file op against the filesystem, contained
to ``roots`` (the allowed sandbox mount root(s)), and returns a plain result
dict. In production this runs inside the sandbox's serve loop; the function
itself is generic and host-side-testable — a tmp dir stands in for the mount
root in tests.

Trust boundary (mirrors ``sandbox_entry._MalformedRunSubpath``): the op's
``path`` must be ABSOLUTE and, after ``os.path.realpath``, lie inside one of the
realpath'd ``roots``. An escaping path (absolute outside, or ``..`` traversal)
is rejected — ``{"ok": False, "error": "path_outside_roots"}``. NEVER raises:
every op is wrapped; an unexpected error returns
``{"ok": False, "error": "<type>: <msg>"}``.
"""

from __future__ import annotations

import base64
import fnmatch
import os
import re
import subprocess
import time

from vibecanvas_api.services.file_format import (
    _BINARY_SNIFF_BYTES, content_type_for, is_binary_bytes)

# Cap on total matched characters returned by ``grep`` before truncation.
_GREP_CHAR_CAP = 200_000


def _is_within(path: str, roots: list[str]) -> bool:
    """True iff the realpath'd ``path`` is inside one of the realpath'd ``roots``.

    Both sides are realpath'd so symlinks and ``..`` traversal are resolved
    before the containment check (airtight against absolute escapes and
    ``..``-escapes alike). Containment is a path-component prefix test, so
    ``/root-evil`` is NOT treated as inside ``/root``.
    """
    if not os.path.isabs(path):
        return False
    real = os.path.realpath(path)
    for root in roots:
        real_root = os.path.realpath(root)
        if real == real_root:
            return True
        if real.startswith(real_root.rstrip(os.sep) + os.sep):
            return True
    return False


def _sniff_binary(path: str) -> bool:
    """True iff ``path`` is binary (NUL in its first ``_BINARY_SNIFF_BYTES``)."""
    with open(path, "rb") as f:
        return is_binary_bytes(f.read(_BINARY_SNIFF_BYTES))


def _op_read(path: str) -> dict:
    if not os.path.isfile(path):
        return {"ok": False, "error": "not_found"}
    with open(path, "rb") as f:
        head = f.read(_BINARY_SNIFF_BYTES)
    if is_binary_bytes(head):
        return {
            "ok": True,
            "kind": "binary",
            "content_type": content_type_for(path, head),
            "size": os.path.getsize(path),
        }
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return {"ok": True, "kind": "text", "content": f.read()}


def _op_write(path: str, content: str) -> dict:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = content.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    return {"ok": True, "bytes": len(data)}


def _op_read_bytes(path: str) -> dict:
    """Read RAW bytes (any file, text or binary) base64-encoded for JSON transport.
    For binary formats (xlsx, …) the text ``read`` op only returns a descriptor; this
    returns the actual bytes."""
    if not os.path.isfile(path):
        return {"ok": False, "error": "not_found"}
    with open(path, "rb") as f:
        return {"ok": True, "data_b64": base64.b64encode(f.read()).decode("ascii")}


def _op_write_bytes(path: str, data_b64: str) -> dict:
    """Write RAW bytes (base64-decoded from ``data_b64``) — the binary write path
    (xlsx, …) the text ``write`` op can't carry."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = base64.b64decode(data_b64)
    with open(path, "wb") as f:
        f.write(data)
    return {"ok": True, "bytes": len(data)}


def _op_list(path: str) -> dict:
    if not os.path.isdir(path):
        return {"ok": False, "error": "not_a_directory"}
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        is_dir = os.path.isdir(full)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        entries.append({"name": name, "is_dir": is_dir, "size": size})
    return {"ok": True, "entries": entries}


def _op_grep(pattern: str, path: str, glob: str = "", context: int = 0) -> dict:
    """Recursive regex search (grep -rn). Optional ``glob`` filters files by name;
    optional ``context`` (>=0) adds N lines of surrounding context per match.

    Output (ripgrep convention): a match line is ``path:lineno:text`` (``:`` separator),
    a context line is ``path-lineno-text`` (``-`` separator), and ``--`` separates
    non-contiguous groups — emitted ONLY when ``context > 0`` (with no context the
    result is the flat ``path:lineno:text`` list). ``match_count`` counts real matches
    (not context/separator lines)."""
    try:
        rx = re.compile(pattern)
    except re.error:
        return {"ok": False, "error": "invalid_regex"}
    context = max(0, context)

    if os.path.isdir(path):
        files = []
        for dirpath, _dirnames, filenames in os.walk(path):
            for fn in sorted(filenames):
                if glob and not fnmatch.fnmatch(fn, glob):
                    continue
                files.append(os.path.join(dirpath, fn))
        files.sort()
    else:
        files = [path]

    group_sep = context > 0                        # ripgrep-style `--` only with context
    out: list[str] = []
    match_count = 0
    total = 0
    truncated = False
    for fpath in files:
        if truncated:
            break
        try:
            if _sniff_binary(fpath):
                continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except (OSError, IsADirectoryError):
            continue
        hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            continue
        # Display set: each hit plus ±context lines; remember which lines are matches.
        is_match: dict[int, bool] = {}
        for h in hits:
            for j in range(max(0, h - context), min(len(lines), h + context + 1)):
                is_match.setdefault(j, False)
            is_match[h] = True
        if group_sep and out:                      # separate this file's block from the last
            out.append("--")
        prev = None
        for idx in sorted(is_match):
            if group_sep and prev is not None and idx > prev + 1:
                out.append("--")
            sep = ":" if is_match[idx] else "-"
            entry = f"{fpath}{sep}{idx + 1}{sep}{lines[idx]}"
            out.append(entry)
            if is_match[idx]:
                match_count += 1
            total += len(entry)
            if total >= _GREP_CHAR_CAP:
                truncated = True
                break
            prev = idx

    result = {"ok": True, "matches": out, "match_count": match_count}
    if truncated:
        result["truncated"] = True
    return result


def _op_exec(op: dict, roots: list[str]) -> dict:
    started = time.perf_counter()
    proc = subprocess.run(
        # A login shell can reset PATH through /etc/profile, losing the
        # mounted uv interpreter selected by the sandbox environment.
        ["bash", "-c", op["command"]],
        cwd=op.get("cwd") or roots[0],
        capture_output=True,
        text=True,
        timeout=op.get("timeout", 60),
    )
    return {
        "ok": True,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
        "exec_elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def run_fileop(op: dict, roots: list[str]) -> dict:
    """Execute ONE file op against the filesystem, contained to ``roots``.

    Never raises — every path is wrapped; an unexpected error is returned as
    ``{"ok": False, "error": "<ExceptionType>: <message>"}``.
    """
    try:
        kind = op.get("op")

        # ``exec`` carries no ``path`` (it runs a shell command, cwd-confined).
        if kind == "exec":
            return _op_exec(op, roots)

        path = op.get("path")
        if not isinstance(path, str) or not _is_within(path, roots):
            return {"ok": False, "error": "path_outside_roots"}

        if kind == "read":
            return _op_read(path)
        if kind == "write":
            return _op_write(path, op.get("content", ""))
        if kind == "read_bytes":
            return _op_read_bytes(path)
        if kind == "write_bytes":
            return _op_write_bytes(path, op.get("data_b64", ""))
        if kind == "list":
            return _op_list(path)
        if kind == "grep":
            return _op_grep(op.get("pattern", ""), path,
                            op.get("glob", ""), op.get("context", 0))

        return {"ok": False, "error": f"unknown_op: {kind!r}"}
    except Exception as e:  # never raise — wrap any unexpected failure.
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
