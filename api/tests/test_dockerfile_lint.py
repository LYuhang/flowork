"""Static lint of api/Dockerfile (Gate 4, real docker build is local-only)."""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE_PATH = Path(__file__).resolve().parents[1] / "Dockerfile"
API_ROOT = DOCKERFILE_PATH.parent
REPO_ROOT = API_ROOT.parent
DRAWIO_EXPORT_PATH = API_ROOT / "drawio-runtime" / "export.sh"
NATIVE_BOOTSTRAP_PATH = REPO_ROOT / "scripts" / "bootstrap_native_linux.sh"


def _dockerfile_instructions() -> list[str]:
    text = DOCKERFILE_PATH.read_text()
    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_dockerfile_exists():
    assert DOCKERFILE_PATH.is_file()


def test_dockerfile_uses_python_3_10_or_later_base():
    lines = _dockerfile_instructions()
    from_lines = [ln for ln in lines if ln.upper().startswith("FROM ")]
    assert len(from_lines) == 5, (
        "expected shared Node, Codex, Playwright, draw.io, and Python stages, "
        f"got {from_lines}"
    )
    assert from_lines[0].startswith("FROM node:22.23.2-bookworm-slim@sha256:")
    assert from_lines[1] == "FROM node-runtime-base AS codex-assets"
    assert from_lines[2] == "FROM node-runtime-base AS playwright-assets"
    assert from_lines[3] == "FROM node-runtime-base AS drawio-assets"
    m = re.match(r"FROM\s+python:3\.(\d+)([-\w.]*)?@sha256:", from_lines[-1])
    assert m, (
        f"final stage must use a digest-pinned python:3.x base, got: {from_lines[-1]}"
    )
    minor = int(m.group(1))
    assert minor >= 10


def test_dockerfile_pins_and_verifies_external_runtime_assets():
    text = DOCKERFILE_PATH.read_text()
    assert "ARG CODEX_CLI_VERSION=0.147.0" in text
    assert "ARG NPM_VERSION=11.19.0" in text
    assert "ARG PLAYWRIGHT_MCP_VERSION=0.0.79" in text
    assert "ARG DRAWIO_DESKTOP_VERSION=31.1.8" in text
    assert "ARG DRAWIO_DESKTOP_AMD64_SHA256=" in text
    assert "ARG DRAWIO_DESKTOP_ARM64_SHA256=" in text
    assert 'test "$(npm --version)" = "${NPM_VERSION}"' in text
    assert '"@openai/codex@${CODEX_CLI_VERSION}"' in text
    assert "api/playwright-runtime/package-lock.json" in text
    assert "flowork-playwright-mcp --version" in text
    assert "--ignore-scripts" in text
    assert 'test "$(codex --version)" = "codex-cli ${CODEX_CLI_VERSION}"' in text
    assert "api/drawio-runtime/package-lock.json" in text
    assert "npm ci --prefix /opt/drawio-mcp" in text
    assert "COPY --from=node-runtime-base /usr/local/bin/node" in text
    assert "COPY --from=playwright-assets /opt/playwright-mcp" in text
    assert "flowork-diagram-mcp --version" in text
    assert 'node_modules/@drawio/mcp/package.json' in text
    assert "github.com/jgraph/drawio-desktop/releases/download" in text
    assert 'dpkg-deb -f' in text
    assert 'sha256sum -c -' in text
    assert "xvfb=" in text
    assert "xauth=" in text
    assert "ln -s /opt/drawio/drawio /usr/local/bin/drawio" in text
    assert "COPY api/drawio-runtime/export.sh /usr/local/bin/flowork-drawio-export" in text
    assert "chmod 0755 /usr/local/bin/flowork-drawio-export" in text
    assert "ARG RUNSC_RELEASE=20260601.0" in text
    assert "sha512sum -c -" in text
    assert "fonts-dejavu-core=2.37-8" in text
    assert "fonts-wqy-zenhei=0.9.45-8" in text
    assert "ARG DEBIAN_MIRROR=http://deb.debian.org/debian" in text
    assert "ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security" in text
    assert "libreoffice-writer-nogui=4:25.2.3-2+deb13u6" in text
    assert "libreoffice-impress-nogui=4:25.2.3-2+deb13u6" in text
    assert "libreoffice-calc-nogui=4:25.2.3-2+deb13u6" in text
    assert "poppler-utils=25.03.0-5+deb13u4" in text
    assert "USER 10001:10001" in text


def test_drawio_export_wrapper_allocates_and_reaps_desktop_processes():
    text = DRAWIO_EXPORT_PATH.read_text()

    assert "Xvfb -displayfd 3" in text
    assert "FLOWORK_DRAWIO_EXPORT_TIMEOUT_SECONDS" in text
    assert "timeout --signal=TERM --kill-after=5s" in text
    assert 'trap cleanup EXIT HUP INT TERM' in text


def test_native_bootstrap_installs_verified_diagram_feedback_runtime():
    text = NATIVE_BOOTSTRAP_PATH.read_text()

    assert 'DRAWIO_DESKTOP_VERSION="${DRAWIO_DESKTOP_VERSION:-31.1.8}"' in text
    assert "DRAWIO_DESKTOP_AMD64_SHA256" in text
    assert "DRAWIO_DESKTOP_ARM64_SHA256" in text
    assert "xvfb xauth" in text
    assert "github.com/jgraph/drawio-desktop/releases/download" in text
    assert "sha256sum -c -" in text
    assert "dpkg-deb -f" in text
    assert '[[ "$(dpkg-deb -f "$drawio_deb" Package)" == "draw.io" ]]' in text
    assert 'sudo install -m 0755' in text
    assert "/usr/local/bin/flowork-drawio-export" in text


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
    assert text.count("--require-hashes") >= 2
    assert text.count("--no-build-isolation") >= 2
    assert "pip uninstall -y" in text
    assert "hatchling editables" in text


def test_dockerfile_installs_shared_sandbox_python_baseline():
    text = DOCKERFILE_PATH.read_text()
    assert "COPY requirements-sandbox.txt" in text
    assert "--require-hashes -r /tmp/requirements-sandbox.txt" in text
