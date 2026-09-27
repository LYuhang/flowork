"""The native launcher must repair sensitive runtime-file permissions."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "scripts" / "native_dev_up.sh"


def test_native_launcher_repairs_preexisting_runtime_permissions() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert 'chmod 700 "$RUNDIR"' in source
    assert 'chmod 600 "$RUNDIR/.env.native"' in source
    assert source.index('chmod 600 "$RUNDIR/.env.native"') > source.index(
        'cat > "$RUNDIR/.env.native"'
    )


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
    assert '--verify_bundle "$codex_bundle"' in source
    assert 'export CODEX_CLI_PATH="$codex_bundle/bin/codex"' in source
    outer = (ROOT / "launch.sh").read_text().split("start_stack() {", 1)[1].split("\n}", 1)[0]
    assert outer.index('"$NATIVE_LAUNCHER" check-runtime') < outer.index("stop_stack")
    assert 'check-runtime) resolve_backend_python; resolve_codex_runtime' in source


def test_native_codex_preparation_is_isolated_and_probe_gated() -> None:
    source = (ROOT / "scripts/prepare_codex_runtime.sh").read_text()
    bootstrap = (ROOT / "scripts/bootstrap_native_linux.sh").read_text()
    assert 'bash "$REPO_ROOT/scripts/prepare_codex_runtime.sh"' in bootstrap
    assert '--no-modify-path --profile minimal --default-toolchain 1.95.0' in source
    assert '"RUSTUP_TOOLCHAIN=1.95.0"' in source
    assert '"RUSTUP_HOME=$codex_root/rustup"' in source
    assert '"CARGO_HOME=$codex_root/cargo"' in source
    assert 'flock 9' in source
    assert 'sha256sum -c -' in source
    assert 'for probe in direct code-mode background' in source
    assert 'codex_probe_args+=(--background)' in source
    assert source.index('verify_codex_native_output.py') < source.index('mv -T -n')
    assert 'sudo' not in source
