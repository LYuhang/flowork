"""The native launcher must repair sensitive runtime-file permissions."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import venv

import pytest


ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "scripts" / "native_dev_up.sh"


@pytest.mark.parametrize("mode,build", [("0", False), ("false", False), ("no", False), ("1", True), ("auto", True)])
def test_native_prebuilt_mode_ignores_newer_sources(tmp_path, mode, build):
    (tmp_path / "dist").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "public").mkdir()
    marker = tmp_path / "dist/index.html"
    marker.write_text("built")
    source = tmp_path / "src/new.ts"
    source.write_text("changed")
    os.utime(marker, (100, 100))
    os.utime(source, (200, 200))
    function = "web_build_needed() {" + LAUNCHER.read_text().split(
        "web_build_needed() {", 1
    )[1].split("\n}", 1)[0] + "\n}\n"
    command = function + "if web_build_needed; then exit 0; else exit 1; fi"
    env = {**os.environ, "WEB_DIR": str(tmp_path), "WEB_REBUILD": mode}
    result = subprocess.run(["bash", "-eu", "-c", command], env=env)
    assert (result.returncode == 0) is build
    marker.unlink()
    # Even explicit reuse must build when there is no usable artifact.
    assert subprocess.run(["bash", "-eu", "-c", command], env=env).returncode == 0


def test_native_launcher_repairs_preexisting_runtime_permissions() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert 'chmod 700 "$RUNDIR"' in source
    assert 'chmod 600 "$RUNDIR/.env.native"' in source
    assert source.index('chmod 600 "$RUNDIR/.env.native"') > source.index(
        'cat > "$RUNDIR/.env.native"'
    )


def test_native_database_ports_do_not_collide_with_package_services():
    source = LAUNCHER.read_text()
    defaults = "\n".join(
        line for line in source.splitlines()
        if line.startswith(('PGPORT=', 'REDISPORT='))
    )
    script = defaults + '\nprintf "%s:%s" "$PGPORT" "$REDISPORT"'
    clean_env = {key: value for key, value in os.environ.items() if key not in {"PGPORT", "REDISPORT"}}
    result = subprocess.run(
        ["bash", "-eu", "-c", script], env=clean_env,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == "5433:6380"
    overridden = subprocess.run(
        ["bash", "-eu", "-c", script],
        env={**clean_env, "PGPORT": "15433", "REDISPORT": "16379"},
        capture_output=True, text=True, check=True,
    )
    assert overridden.stdout == "15433:16379"


def test_openfga_preserves_capacity_and_cancels_timed_out_database_work() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert '--datastore-max-open-conns "${OPENFGA_DATASTORE_MAX_OPEN_CONNS:-30}"' in source
    assert '--datastore-max-idle-conns "${OPENFGA_DATASTORE_MAX_IDLE_CONNS:-2}"' in source
    assert '--context-propagation-to-datastore=true' in source


def test_native_launcher_checks_managed_codex_before_starting_services() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    startup = source.split("cmd_up() {", 1)[1].split("\n}", 1)[0]
    assert startup.index("resolve_codex_runtime") < startup.index("start_pg")
    assert 'if [[ -z "${CODEX_CLI_PATH:-}" ]]' in source
    assert 'CODEX_CLI_PATH="$(command -v codex || true)"' in source
    assert '"codex-cli ${CODEX_CLI_VERSION}"' in source
    assert 'export CODEX_CLI_PATH' in source
    outer = (ROOT / "launch.sh").read_text().split("start_stack() {", 1)[1].split("\n}", 1)[0]
    assert outer.index('"$NATIVE_LAUNCHER" check-runtime') < outer.index("stop_stack")
    configured = outer.split("configure_debug_stack", 1)[1]
    assert configured.index('"$NATIVE_LAUNCHER" check-runtime') < configured.index("stop_stack")
    assert 'check-runtime) resolve_backend_python; resolve_codex_runtime' in source


def test_native_codex_install_uses_the_exact_official_version_without_source_build() -> None:
    bootstrap = (ROOT / "scripts/bootstrap_native_linux.sh").read_text()
    assert 'CODEX_CLI_VERSION="${CODEX_CLI_VERSION:-0.157.1}"' in bootstrap
    assert '"@openai/codex@${CODEX_CLI_VERSION}"' in bootstrap
    assert 'bash "$REPO_ROOT/scripts/prepare_codex_runtime.sh"' not in bootstrap
    assert "cargo build" not in bootstrap
    assert "rustup" not in bootstrap


@pytest.mark.parametrize(
    "selected,accepted",
    [
        ("repo", True),
        ("foreign", False),
    ],
)
def test_native_python_selection(tmp_path, selected, accepted):
    """Run the real shell resolver with real venvs; no services are started."""
    repo = tmp_path / "checkout"
    locations = {
        "repo": repo / ".venv",
        "foreign": tmp_path / "foreign",
    }
    for location in locations.values():
        venv.EnvBuilder(with_pip=False).create(location)
    resolver = "resolve_backend_python() {" + LAUNCHER.read_text().split(
        "resolve_backend_python() {", 1
    )[1].split("\n}", 1)[0] + "\n}\nresolve_backend_python\n"
    env = os.environ.copy()
    env.update({
        "REPO_ROOT": str(repo),
        "VIBECANVAS_PYTHON": str(locations[selected] / "bin/python"),
    })
    result = subprocess.run(["bash", "-eu", "-c", resolver], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is accepted, result.stderr


def test_native_launch_reuses_uv_without_copying_python():
    source = (ROOT / "launch.sh").read_text()
    assert 'export VIBECANVAS_PYTHON="$configured_python"' in source
    assert "RUNTIME_PREPARER" not in source
    assert "VIBECANVAS_RUNTIME_CACHE_ROOT" not in source


@pytest.mark.parametrize("owned", [True, False])
def test_shutdown_only_signals_owned_process_groups(tmp_path, owned):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    child_env = {**os.environ, "VIBECANVAS_NATIVE_RUNTIME_DIR": str(runtime if owned else tmp_path / "other")}
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=child_env, start_new_session=True,
    )
    (runtime / "api.pid").write_text(str(process.pid))
    stopper = "stop_pidfile() {" + LAUNCHER.read_text().split(
        "stop_pidfile() {", 1
    )[1].split("\n}", 1)[0] + "\n}\nstop_pidfile api\n"
    try:
        stop = subprocess.Popen(
            ["bash", "-eu", "-c", stopper],
            env={**os.environ, "RUNDIR": str(runtime)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if owned:
            assert process.wait(timeout=5) < 0
        stdout, stderr = stop.communicate(timeout=5)
        assert (stop.returncode == 0) is owned, (stdout, stderr)
        if not owned:
            assert process.poll() is None
            assert "refusing to stop" in stderr
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_bootstrap_installs_locked_local_npm_dependencies_and_supported_tools():
    source = (ROOT / "scripts/bootstrap_native_linux.sh").read_text()
    assert 'NODE_VERSION="${NODE_VERSION:-24.21.0}"' in source
    assert 'nodejs=${NODE_VERSION}-1nodesource1' in source
    assert '[[ "$(node --version)" == "v${NODE_VERSION}" ]]' in source
    assert '"fast-uri": "4.2.1"' in source
    assert '"hono": "4.13.9"' in source
    assert '"qs": "6.16.0"' in source
    assert "umask 022; exec npm install" in source
    assert "update_npm_bundled_dependencies.cjs" in source
    assert "install_public_npm_package npm@11.19.0" in source
    assert 'UV_VERSION="${UV_VERSION:-0.12.23}"' in source
    for runtime in ("drawio-runtime",):
        assert f'npm --prefix "$REPO_ROOT/api/{runtime}" ci --ignore-scripts' in source
    assert '${VIBECANVAS_LAUNCH_ENV:-$REPO_ROOT/.env.launch.local}' in source
    assert 'case "${SANDBOX_RUNTIME:-bubblewrap}" in' in source
    assert 'gvisor) bash "$REPO_ROOT/scripts/get_runsc.sh"' in source
    assert 'export SANDBOX_RUNTIME="${SANDBOX_RUNTIME:-bubblewrap}"' in LAUNCHER.read_text()


def test_native_launcher_does_not_sweep_other_instances_or_report_failed_web_as_ready():
    source = LAUNCHER.read_text()
    assert "pkill" not in source
    assert "rm -rf /tmp/vc-sbx-" not in source
    assert '--port "$API_PORT"' in source
    assert "PLATFORM_MCP_INTERNAL_BASE_URL" in source
    assert "--strictPort" in source
    assert 'echo "web: build FAILED (see $WEB_BUILD_LOG)" >&2; return 1;' in source
    assert 'return "$unhealthy"' in source


def test_bootstrap_generated_config_preserves_invocation_values(tmp_path):
    source = (ROOT / "scripts/bootstrap_native_linux.sh").read_text()
    generated = source.split('cat >"$launch_env" <<\'EOF\'\n', 1)[1].split("\nEOF", 1)[0]
    config_file = tmp_path / "deployment.env"
    config_file.write_text(generated)
    expected = {
        "WEB_HOST": "127.0.0.1",
        "WEB_PORT": "19099",
        "VIBECANVAS_PUBLIC_URL": "https://deployment.example.test/app/",
        "ENABLE_TEST_USER": "false",
        "ENTERPRISE_SSO_ENABLED": "true",
        "AGENT_RUNTIME_TYPES": "codex",
        "CODEX_RUNTIME_AUTH_METHODS": "personal_api",
        "CODEX_MANAGED_APIS_JSON": '[{"id":"test-only"}]',
        "SANDBOX_RUNTIME": "gvisor",
        "SANDBOX_TYPE": "rootless-warm",
    }
    result = subprocess.run(
        ["bash", "-eu", "-c", 'source "$1"; env', "bash", str(config_file)],
        env={**os.environ, **expected}, capture_output=True, text=True, check=True,
    )
    actual = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert {key: actual[key] for key in expected} == expected
    defaults = subprocess.run(
        ["bash", "-eu", "-c", 'source "$1"; printf "%s:%s" "$SANDBOX_RUNTIME" "$SANDBOX_TYPE"', "bash", str(config_file)],
        env={key: value for key, value in os.environ.items() if key not in {"SANDBOX_RUNTIME", "SANDBOX_TYPE"}},
        capture_output=True, text=True, check=True,
    )
    assert defaults.stdout == "bubblewrap:rootless-warm"


def test_node_and_pnpm_entrypoints_share_the_pinned_toolchain():
    import json

    root = Path(__file__).resolve().parents[3]
    assert (root / ".nvmrc").read_text().strip() == "24.21.0"
    for package in ("package.json", "web/package.json", "extension/package.json"):
        assert json.loads((root / package).read_text())["packageManager"] == "pnpm@10.34.4"


@pytest.mark.parametrize("failed_stage", ["extension", "prepare-web", "publish", "none"])
def test_launch_prepares_artifacts_before_stopping_services(tmp_path, failed_stage):
    """Execute the launch orchestrator with controlled build failures."""
    launcher = tmp_path / "native.sh"
    log = tmp_path / "stages"
    launcher.write_text('echo "$1" >> "$STAGES"\nif [[ "$1" == "$FAILED_STAGE" ]]; then exit 42; fi\n')
    function = "start_stack() {" + (ROOT / "launch.sh").read_text().split(
        "start_stack() {", 1
    )[1].split("\n}", 1)[0] + "\n}\n"
    script = '''
load_local_env() { :; }
configure_debug_stack() { :; }
stop_stack() { echo stop >> "$STAGES"; }
build_extension() { echo extension >> "$STAGES"; [[ "$FAILED_STAGE" != extension ]]; }
publish_extension_archive() { echo publish >> "$STAGES"; [[ "$FAILED_STAGE" != publish ]]; }
''' + function + "start_stack\n"
    result = subprocess.run(["bash", "-eu", "-c", script], env={
        **os.environ, "NATIVE_LAUNCHER": str(launcher), "STAGES": str(log),
        "FAILED_STAGE": failed_stage, "REPO_ROOT": str(tmp_path), "VIBECANVAS_PUBLIC_URL": "http://localhost",
    }, capture_output=True, text=True)
    stages = log.read_text().splitlines()
    if failed_stage != "none":
        assert result.returncode != 0
        assert "stop" not in stages and "up" not in stages
    else:
        assert result.returncode == 0, result.stderr
        assert stages.index("extension") < stages.index("prepare-web") < stages.index("stop") < stages.index("up")
