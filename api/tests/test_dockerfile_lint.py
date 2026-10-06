"""Static lint of api/Dockerfile (Gate 4, real docker build is local-only)."""

from __future__ import annotations

import json
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
        "expected shared Node, official Codex, Playwright, draw.io, and Python stages, "
        f"got {from_lines}"
    )
    assert from_lines[0].startswith("FROM node:24.21.0-bookworm-slim@sha256:")
    assert from_lines[1] == "FROM node-runtime-base AS codex-assets"
    assert from_lines[2] == "FROM node-runtime-base AS playwright-assets"
    assert from_lines[3] == "FROM node-runtime-base AS drawio-assets"
    m = re.match(r"FROM\s+python:3\.(\d+)([-\w.]*)?@sha256:", from_lines[-1])
    assert m, (
        f"final stage must use a digest-pinned python:3.x base, got: {from_lines[-1]}"
    )
    minor = int(m.group(1))
    assert minor >= 10


def test_codex_uses_official_prebuilt_package_without_source_toolchain():
    text = DOCKERFILE_PATH.read_text()
    assert "FROM rust:" not in text
    assert "rustup" not in text
    assert "cargo build" not in text
    assert "github.com/openai/codex.git" not in text
    assert '"@openai/codex@${CODEX_CLI_VERSION}"' in text
    assert "COPY --from=codex-assets /opt/codex /opt/codex" in text


def test_runtime_os_dependencies_precede_codex_bundle_copy():
    text = DOCKERFILE_PATH.read_text()
    assert text.index("libreoffice-writer-nogui=") < text.index(
        "COPY --from=codex-assets /opt/codex /opt/codex"
    )


def test_dockerfile_pins_and_verifies_external_runtime_assets():
    text = DOCKERFILE_PATH.read_text()
    assert "ARG CODEX_CLI_VERSION=0.157.1" in text
    assert "ARG NPM_VERSION=11.19.0" in text
    assert "ARG PLAYWRIGHT_CORE_VERSION=1.63.0-alpha-2026-08-05" in text
    assert "ARG DRAWIO_DESKTOP_VERSION=31.1.8" in text
    assert "ARG DRAWIO_DESKTOP_AMD64_SHA256=" in text
    assert "ARG DRAWIO_DESKTOP_ARM64_SHA256=" in text
    assert 'test "$(npm --version)" = "${NPM_VERSION}"' in text
    assert '"@openai/codex@${CODEX_CLI_VERSION}"' in text
    assert "api/playwright-runtime/package-lock.json" in text
    assert "flowork-browser-runtime --version" in text
    assert "--ignore-scripts" in text
    assert 'test "$(codex --version)" = "codex-cli ${CODEX_CLI_VERSION}"' in text
    assert "api/drawio-runtime/package-lock.json" in text
    assert "npm ci --prefix /opt/drawio-mcp" in text
    assert "COPY --from=node-runtime-base /usr/local/bin/node" in text
    assert "COPY --from=playwright-assets /opt/flowork-browser-runtime" in text
    assert "flowork-diagram-search --version" in text
    assert 'node_modules/@drawio/mcp/package.json' in text
    drawio_package = json.loads((API_ROOT / "drawio-runtime/package.json").read_text())
    drawio_lock = json.loads((API_ROOT / "drawio-runtime/package-lock.json").read_text())
    assert drawio_package["overrides"] == {
        "fast-uri": "4.2.1",
        "hono": "4.13.9",
        "qs": "6.16.0",
    }
    assert drawio_lock["packages"]["node_modules/fast-uri"]["version"] == "4.2.1"
    assert drawio_lock["packages"]["node_modules/hono"]["version"] == "4.13.9"
    assert drawio_lock["packages"]["node_modules/qs"]["version"] == "6.16.0"
    assert "github.com/jgraph/drawio-desktop/releases/download" in text
    assert 'dpkg-deb -f' in text
    assert 'sha256sum -c -' in text
    assert "xvfb=2:21.1.16-1.3+deb13u4" in text
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
    assert "libreoffice-writer-nogui=4:25.2.3-2+deb13u8" in text
    assert "libreoffice-impress-nogui=4:25.2.3-2+deb13u8" in text
    assert "libreoffice-calc-nogui=4:25.2.3-2+deb13u8" in text
    assert "poppler-utils=25.03.0-5+deb13u4" in text
    assert "USER 10001:10001" in text
    assert "patch_python_tarfile.py" in text
    assert "verify_python_tarfile_fix.py" in text


def test_browser_cli_package_has_no_mcp_runtime_dependency():
    package = json.loads((API_ROOT / "playwright-runtime/package.json").read_text())
    lock = json.loads((API_ROOT / "playwright-runtime/package-lock.json").read_text())
    assert package["name"] == "flowork-browser-runtime"
    assert package["bin"] == {"flowork-browser-runtime": "browser-runtime.cjs"}
    assert package["dependencies"] == {"playwright-core": "1.63.0-alpha-2026-08-05"}
    assert set(lock["packages"]) == {"", "node_modules/playwright-core"}
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    for path in package["files"]:
        assert (API_ROOT / "playwright-runtime" / path).is_file()
        assert not path.endswith(".test.cjs")
    assert not (API_ROOT / "playwright-runtime/launch.cjs").exists()
    for path in (DOCKERFILE_PATH, NATIVE_BOOTSTRAP_PATH):
        assert "flowork-playwright-mcp" not in path.read_text()
        assert "flowork-browser-runtime --version" in path.read_text()


def test_browser_runtime_packages_every_local_module_without_retired_interception():
    runtime = API_ROOT / "playwright-runtime"
    package = json.loads((runtime / "package.json").read_text())
    shipped = set(package["files"]) | {"package.json"}
    assert {"browser-native-downloads.cjs", "browser-script-page.cjs"} <= shipped
    assert "browser-downloads.cjs" not in shipped
    docker = DOCKERFILE_PATH.read_text()
    for filename in shipped:
        source = runtime / filename
        assert source.is_file(), f"Missing packaged browser module: {filename}"
        assert f"api/playwright-runtime/{filename}" in docker
        for dependency in re.findall(r'require\([\"\']\./([^\"\']+)[\"\']\)', source.read_text()):
            assert dependency in shipped, f"{filename} depends on unpackaged {dependency}"


def test_docker_build_preserves_official_codex_bundle_and_companions():
    text = DOCKERFILE_PATH.read_text()
    assert "COPY --from=codex-assets /opt/codex /opt/codex" in text
    assert "scripts/build_codex_runtime.py" not in text
    assert "output-subscription" not in text
    assert "CODEX_BUILD_JOBS" not in text


def test_drawio_export_wrapper_allocates_and_reaps_desktop_processes():
    text = DRAWIO_EXPORT_PATH.read_text()

    assert "Xvfb -displayfd 3" in text
    assert "FLOWORK_DRAWIO_EXPORT_TIMEOUT_SECONDS" not in text
    assert "timeout --signal=TERM --kill-after=5s" not in text
    assert 'trap cleanup EXIT HUP INT TERM' in text


def test_diagram_search_launcher_is_executable_and_not_an_mcp_server():
    launcher = API_ROOT / "drawio-runtime" / "search.mjs"
    assert launcher.stat().st_mode & 0o111
    assert "@modelcontextprotocol/sdk" not in launcher.read_text()
    assert not (API_ROOT / "drawio-runtime" / "launch.mjs").exists()


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
