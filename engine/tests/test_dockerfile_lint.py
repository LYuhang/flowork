"""Static lint of engine/Dockerfile.

This dev env runs inside a managed container, so docker / podman /
buildah are unavailable — Gate 4's real `docker build .` is
deferred to whichever machine the user runs it on. But the
Dockerfile is part of the repo and silently rotting would defeat
the point of shipping it.

This test parses the Dockerfile as a string and asserts the load-
bearing structural pieces are still present:
  - exactly one FROM line, python 3.10+ base image
  - COPY pyproject.toml + COPY src/
  - RUN pip install
  - no deprecated standalone CLI entry point
  - direct container runs perform a Python-package import smoke check
  - every COPY source path actually exists on disk

It is NOT a substitute for the real build — it can't catch e.g.
`pip install` failing inside the image. But it catches the
highest-frequency drift modes (typos, removed paths, swapped
base image) at the cost of a normal pytest run.
"""

from __future__ import annotations

import re
from pathlib import Path


DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"
ENGINE_ROOT = DOCKERFILE_PATH.parent
REPO_ROOT = ENGINE_ROOT.parent


def _dockerfile_instructions() -> list[str]:
    """Return non-empty, non-comment lines from the Dockerfile."""
    text = DOCKERFILE_PATH.read_text()
    return [
        ln.strip() for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_dockerfile_exists():
    assert DOCKERFILE_PATH.is_file(), f"Dockerfile missing at {DOCKERFILE_PATH}"


def test_dockerfile_uses_python_3_10_or_later_base():
    """Single FROM line, must be python:3.{10,11,12,13,...}[-variant]."""
    lines = _dockerfile_instructions()
    from_lines = [ln for ln in lines if ln.upper().startswith("FROM ")]
    assert len(from_lines) == 1, f"expected exactly one FROM, got {from_lines}"
    m = re.match(r"FROM\s+python:3\.(\d+)([-\w.]*)?", from_lines[0])
    assert m, f"FROM must use python:3.x base, got: {from_lines[0]}"
    minor = int(m.group(1))
    assert minor >= 10, (
        f"pyproject requires-python=>=3.10 but Dockerfile uses 3.{minor}"
    )


def test_dockerfile_copies_pyproject_and_src():
    text = DOCKERFILE_PATH.read_text()
    assert "pyproject.toml" in text, "Dockerfile must COPY pyproject.toml"
    assert re.search(r"\bsrc/?\b", text), "Dockerfile must COPY src/"


def test_dockerfile_runs_pip_install():
    text = DOCKERFILE_PATH.read_text()
    assert re.search(r"pip\s+install\b", text), (
        "Dockerfile must RUN pip install to materialize the package"
    )
    assert "requirements-build.txt" in text
    assert "--require-hashes" in text
    assert "--no-build-isolation" in text
    assert "hatchling editables" in text


def test_dockerfile_has_no_deprecated_cli_entrypoint():
    text = DOCKERFILE_PATH.read_text()
    assert "ENTRYPOINT" not in text
    assert "vibecanvas run" not in text
    assert "import vibecanvas_engine" in text


def test_dockerfile_copy_paths_exist_on_disk():
    """Every COPY source path (relative to the repository) must exist —
    otherwise `docker build` would fail with `path not found`."""
    missing: list[str] = []
    for ln in _dockerfile_instructions():
        if not ln.upper().startswith("COPY "):
            continue
        tokens = ln.split()[1:]  # drop "COPY"
        # Skip flag tokens like --chown=, --from=
        tokens = [t for t in tokens if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        sources = tokens[:-1]  # last token is the dest
        for src in sources:
            if src.startswith("/"):
                continue  # absolute paths are inside the image, not in the build context
            full = REPO_ROOT / src
            if not full.exists():
                missing.append(src)
    assert not missing, (
        f"Dockerfile COPY references paths missing from the build context: {missing}"
    )
