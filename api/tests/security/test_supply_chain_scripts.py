from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

import yaml
import pytest

_ROOT = Path(__file__).resolve().parents[3]
_INSTALLER = _ROOT / "scripts/security/install_sbom_tools.sh"
_GITLEAKS_INSTALLER = _ROOT / "scripts/security/install_gitleaks.sh"
_SCANNER = _ROOT / "scripts/security/scan_container_images.sh"
_POLICY_EVALUATOR = _ROOT / "scripts/security/evaluate_container_vulnerabilities.py"
_POLICY = _ROOT / "scripts/security/container-vulnerability-policy.json"
_CI_WORKFLOW = _ROOT / ".github/workflows/ci.yml"
_WORKFLOW = _ROOT / ".github/workflows/security.yml"
_RELEASE_WORKFLOW = _ROOT / ".github/workflows/release-images.yml"
_RELEASE_ATTESTATION_GATE = _ROOT / "scripts/security/verify_release_attestations.sh"
_PRODUCTION_RELEASE = _ROOT / "scripts/deploy/production_release.sh"
_RELEASE_COMPOSE = _ROOT / "docker-compose.release.yml"
_CLAMAV_LIVE = _ROOT / "scripts/security/verify_clamav_live.sh"
_CLAMAV_LIVE_PY = _ROOT / "scripts/security/verify_clamav_live.py"
_PYTHON_VERSION = _ROOT / ".python-version"
_PYTHON_SYNC = _ROOT / "scripts/sync_python_env.sh"


def _deployment_image_references() -> set[str]:
    references: set[str] = set()
    for relative_path in ("api/Dockerfile", "engine/Dockerfile", "web/Dockerfile", "postgres/Dockerfile", "postgres/openfga.Dockerfile", "docker/clamav.Dockerfile", "docker/valkey.Dockerfile"):
        text = (_ROOT / relative_path).read_text(encoding="utf-8")
        references.update(
            re.findall(r"^FROM\s+(\S+@sha256:[0-9a-f]{64})", text, re.MULTILINE)
        )
    for compose_path in _ROOT.glob("docker-compose*.yml"):
        compose = compose_path.read_text(encoding="utf-8")
        references.update(
            re.findall(
                r"^\s+image:\s+(\S+@sha256:[0-9a-f]{64})\s*$",
                compose,
                re.MULTILINE,
            )
        )
    return references


@pytest.mark.parametrize("path", ["api/Dockerfile", "scripts/bootstrap_native_linux.sh"])
def test_browser_runtime_version_checks_match_packaged_runtime(path: str) -> None:
    package = json.loads((_ROOT / "api/playwright-runtime/package.json").read_text())
    source = (_ROOT / path).read_text()
    checked_versions = re.findall(r"flowork-browser-runtime (\d+\.\d+\.\d+) \(playwright-core", source)
    assert checked_versions, f"{path} must verify the installed browser runtime"
    assert set(checked_versions) == {package["version"]}


def test_every_pinned_deployment_image_is_scanned() -> None:
    scanner = _SCANNER.read_text(encoding="utf-8")
    scanned = set(re.findall(r"'[^'|]+\|(\S+@sha256:[0-9a-f]{64})'", scanner))
    live_gate = _CLAMAV_LIVE.read_text(encoding="utf-8")
    assert '"$repo_root/docker/clamav.Dockerfile"' in live_gate
    assert scanned == _deployment_image_references()



def test_actual_application_images_are_built_and_scanned() -> None:
    scanner = _SCANNER.read_text(encoding="utf-8")
    assert "build_image postgres postgres/Dockerfile ." in scanner
    assert "scripts/security/verify_postgres_image.sh" in scanner
    assert "build_image api api/Dockerfile ." in scanner
    assert "build_image sandboxd api/Dockerfile . --build-arg VIBECANVAS_RUNTIME_ENV_BUILDER=1" in scanner
    assert "flowork-sandboxd:security-scan -m pip --version" in scanner
    assert "build_image web web/Dockerfile ." in scanner
    assert "build_image engine engine/Dockerfile ." in scanner
    for image in ("api", "sandboxd", "web", "engine", "postgres", "openfga-postgres", "valkey"):
        assert f"'{image}|flowork-{image}:security-scan'" in scanner


def test_local_python_environment_is_repository_pinned() -> None:
    assert _PYTHON_VERSION.read_text(encoding="utf-8").strip() == "3.11.17"
    sync = _PYTHON_SYNC.read_text(encoding="utf-8")
    assert 'uv_version="0.12.23"' in sync
    assert "uv venv --clear" in sync
    assert "patch_python_tarfile.py" not in sync
    assert "scripts/security/verify_python_tarfile_fix.py" in sync
    assert "--editable ./engine --editable ./api" in sync


def test_container_gate_emits_sboms_and_does_not_hide_high_vulnerabilities() -> None:
    scanner = _SCANNER.read_text(encoding="utf-8")
    assert '--output "syft-json=' in scanner
    assert '--output "spdx-json=' in scanner
    assert "--fail-on high" in scanner
    assert "--only-fixed" not in scanner
    assert "--ignore-states" not in scanner
    assert "evaluate_container_vulnerabilities.py" in scanner
    assert "/usr/local/lib/flowork/verify_python_tarfile_fix.py" in scanner
    assert "--verified-python-tarfile-backport" not in scanner
    assert "Python tarfile regression probe failed for %s" in scanner
    assert "container_supply_chain_gate=pass" in scanner


def test_container_policy_is_dated_and_has_no_wildcard_vulnerability_ids() -> None:
    policy = json.loads(_POLICY.read_text(encoding="utf-8"))
    today = dt.datetime.now(dt.timezone.utc).date()
    assert policy["blocking_severities"] == ["High", "Critical"]
    assert "advisory_labels" not in policy
    for rule in policy["exceptions"]:
        assert dt.date.fromisoformat(rule["expires"]) >= today
        assert rule["reason"].strip()
        assert rule["vulnerabilities"]
        assert "*" not in rule["vulnerabilities"]


def test_sbom_tool_installer_is_pinned_and_checksum_verified() -> None:
    installer = _INSTALLER.read_text(encoding="utf-8")
    assert 'syft_version="1.44.0"' in installer
    assert 'grype_version="0.112.0"' in installer
    assert len(re.findall(r'="[0-9a-f]{64}"', installer)) == 4
    assert "sha256sum -c -" in installer
    assert "get.anchore.io" not in installer
    assert "latest" not in installer.lower()


def test_gitleaks_installer_retries_transport_errors_and_verifies_checksum() -> None:
    installer = _GITLEAKS_INSTALLER.read_text(encoding="utf-8")
    assert "--retry 3 --retry-all-errors --retry-delay 1" in installer
    assert "sha256sum -c -" in installer


def test_supply_chain_shell_scripts_parse() -> None:
    for script in (
        _INSTALLER,
        _SCANNER,
        _CLAMAV_LIVE,
        _RELEASE_ATTESTATION_GATE,
        _ROOT / "scripts/security/verify_postgres_image.sh",
    ):
        subprocess.run(["bash", "-n", str(script)], check=True)
    compile(
        _POLICY_EVALUATOR.read_text(encoding="utf-8"),
        str(_POLICY_EVALUATOR),
        "exec",
    )
    compile(
        _CLAMAV_LIVE_PY.read_text(encoding="utf-8"),
        str(_CLAMAV_LIVE_PY),
        "exec",
    )


def test_postgres_image_retains_storage_contract_and_verified_sources() -> None:
    dockerfile = (_ROOT / "postgres/Dockerfile").read_text()
    assert "FROM debian:bookworm-20260918-slim@sha256:" in dockerfile
    assert "PG_MAJOR=15 PG_VERSION=15.19-1.pgdg12+2" in dockerfile
    assert "PGDATA=/var/lib/postgresql/data LANG=en_US.utf8" in dockerfile
    assert "VOLUME /var/lib/postgresql/data" in dockerfile
    assert "STOPSIGNAL SIGINT" in dockerfile
    assert "EXPOSE 5432" in dockerfile
    assert "--gid=999" in dockerfile and "--uid=999" in dockerfile
    assert "/docker-entrypoint-initdb.d" in dockerfile
    assert dockerfile.count("sha256sum -c -") == 5
    assert "8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c" in dockerfile
    assert "COPY --from=vector-build /out/ /" in dockerfile
    assert "exec setpriv --reuid=postgres --regid=postgres --init-groups" in dockerfile
    assert "signed-by=/usr/local/share/keyrings/postgres.asc" in dockerfile
    workflow = _RELEASE_WORKFLOW.read_text()
    assert "label: postgres" in workflow
    assert 'bash scripts/security/verify_postgres_image.sh "$image_ref"' in workflow
    release = _PRODUCTION_RELEASE.read_text()
    assert '"$VIBECANVAS_POSTGRES_IMAGE"' in release


@pytest.mark.parametrize("fail_sql", [False, True])
def test_postgres_smoke_cleans_only_its_anonymous_volume(tmp_path: Path, fail_sql: bool) -> None:
    docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    docker.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        '  *"cat /proc/1/comm"*) echo postgres ;;\n'
        '  *"SELECT count(*)"*) echo 2 ;;\n'
        '  *"SHOW server_version_num"*) echo 150019 ;;\n'
        '  *"ON_ERROR_STOP=1"*) cat >/dev/null; exit "$FAIL_SQL" ;;\n'
        'esac\n',
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(_ROOT / "scripts/security/verify_postgres_image.sh"), "flowork-postgres:test"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "DOCKER_LOG": str(log), "FAIL_SQL": "1" if fail_sql else "0"},
        capture_output=True, text=True,
    )
    assert result.returncode == (1 if fail_sql else 0)
    calls = log.read_text().splitlines()
    assert "--network none" in calls[0]
    assert " -p " not in calls[0] and " -v " not in calls[0]
    name = calls[0].split("--name ", 1)[1].split()[0]
    assert name.startswith("flowork-postgres-check-")
    assert calls[-1] == f"rm -fv {name}"
    assert any(line.startswith("restart --timeout 30 ") for line in calls) is not fail_sql


def test_security_workflow_runs_the_container_gate_and_pins_actions() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "container-supply-chain:" in workflow
    assert "upload-malware-live:" in workflow
    assert "browser-extension-security:" in workflow
    assert "scripts/security/install_sbom_tools.sh" in workflow
    assert "scripts/security/scan_container_images.sh" in workflow
    assert "scripts/security/verify_clamav_live.sh" in workflow
    assert "docker compose --env-file .env.example config --quiet" in workflow
    assert workflow.count("requirements-build.txt") >= 2
    assert workflow.count("--no-build-isolation") >= 2
    assert "if: always()" in workflow
    action_refs = re.findall(r"^\s*uses:\s+([^\s#]+)", workflow, re.MULTILINE)
    assert action_refs
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)


def test_python_ci_probes_rootless_gvisor_without_retired_elk_setup() -> None:
    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    python_job = workflow.split("  web:", maxsplit=1)[0]
    assert "Install the frozen ELK layout dependency graph" not in python_job
    assert "pnpm install --frozen-lockfile" not in python_job
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in workflow
    assert "SANDBOX_GVISOR_PLATFORM: ptrace" in workflow
    assert "rootless_gvisor_full_profile=" in workflow
    assert "_gvisor_runnable()" in workflow
    assert "scripts/sync_python_env.sh" in workflow
    sync = _PYTHON_SYNC.read_text(encoding="utf-8")
    assert "requirements-build.txt" in sync
    assert "--no-build-isolation" in sync


def test_release_workflow_pushes_digest_attested_images_only_from_tags() -> None:
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert 'tags:\n      - "v*"' in workflow
    assert "pull_request:" not in workflow
    assert "workflow_dispatch:" not in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "environment: production-release" in workflow
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow
    assert "packages: write" in workflow
    assert (
        workflow.count("actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d") == 3
    )
    assert workflow.count("push-to-registry: true") == 2
    assert "--fail-on high" in workflow
    assert "verify_release_attestations.sh" in workflow
    assert "extension-package:" in workflow
    assert "pnpm test" in workflow
    assert "pnpm build" in workflow
    assert 'gh attestation verify "$ARCHIVE"' in workflow
    assert "--deny-self-hosted-runners" in workflow
    action_refs = re.findall(r"^\s*uses:\s+([^\s#]+)", workflow, re.MULTILINE)
    assert action_refs
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)


def test_release_compose_uses_a_separate_verified_sandbox_builder_image() -> None:
    overlay = yaml.safe_load(_RELEASE_COMPOSE.read_text(encoding="utf-8"))
    services = overlay["services"]
    api_consumers = {
        "openfga_bootstrap",
        "migrate",
        "sandbox_prewarm",
        "api",
        "background_worker",
        "browser_gateway",
    }
    assert set(services) == api_consumers | {"web", "sandboxd", "postgres", "openfga_postgres", "openfga_erasure_bootstrap", "redis"}
    assert services["redis"]["image"].startswith("${VIBECANVAS_VALKEY_IMAGE:?")
    assert services["postgres"]["image"].startswith("${VIBECANVAS_POSTGRES_IMAGE:?")
    assert services["postgres"]["pull_policy"] == "always"
    assert services["openfga_postgres"]["image"].startswith("${VIBECANVAS_OPENFGA_POSTGRES_IMAGE:?")
    assert services["openfga_postgres"]["pull_policy"] == "always"
    assert services["openfga_erasure_bootstrap"]["image"] == services["postgres"]["image"]
    assert services["openfga_erasure_bootstrap"]["pull_policy"] == "always"
    assert services["sandboxd"]["image"].startswith("${VIBECANVAS_SANDBOX_IMAGE:?")
    assert services["sandboxd"]["pull_policy"] == "always"
    for service_name in api_consumers:
        service = services[service_name]
        assert service["image"].startswith("${VIBECANVAS_API_IMAGE:?")
        assert service["pull_policy"] == "always"
    assert services["web"]["image"].startswith("${VIBECANVAS_WEB_IMAGE:?")
    assert services["web"]["pull_policy"] == "always"
    openfga_consumers = {
        "openfga_bootstrap",
        "sandboxd",
        "sandbox_prewarm",
        "api",
        "background_worker",
        "browser_gateway",
    }
    for service_name in openfga_consumers:
        environment = services[service_name]["environment"]
        assert environment["VIBECANVAS_ENV"] == "production"
        assert environment["OPENFGA_API_URL"].startswith("${OPENFGA_API_URL:?")
        assert environment["OPENFGA_STORE_ID"].startswith("${OPENFGA_STORE_ID:?")
        assert environment["OPENFGA_AUTHORIZATION_MODEL_ID"].startswith(
            "${OPENFGA_AUTHORIZATION_MODEL_ID:?"
        )
        assert environment["OPENFGA_MODEL_SHA256"].startswith(
            "${OPENFGA_MODEL_SHA256:?"
        )
    deploy = (_ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    production_release = _PRODUCTION_RELEASE.read_text(encoding="utf-8")
    assert "docker-compose.release.yml" in deploy
    assert "--no-build --pull always" in deploy
    assert "scripts/deploy/production_release.sh up" in deploy
    assert "scripts/security/verify_release_attestations.sh" in production_release
    assert "scripts/security/verify_production_evidence.py" in production_release


@pytest.mark.parametrize("label", ["api", "sandboxd", "web", "engine", "postgres"])
def test_release_attestation_gate_binds_digest_repo_workflow_and_source(
    tmp_path: Path, label: str,
) -> None:
    gh_log = tmp_path / "gh.log"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$GH_LOG"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    repository = "Example/flowork"
    digest = "sha256:" + "a" * 64
    source_sha = "b" * 40
    env = os.environ.copy()
    env.update({"GH_BIN": str(fake_gh), "GH_LOG": str(gh_log)})
    subprocess.run(
        [
            str(_RELEASE_ATTESTATION_GATE),
            f"ghcr.io/example/flowork-{label}@{digest}",
            repository,
            f"{repository}/.github/workflows/release-images.yml",
            source_sha,
            "refs/tags/v1.2.3",
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = gh_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    for call in calls:
        assert f"oci://ghcr.io/example/flowork-{label}@{digest}" in call
        assert "--repo Example/flowork" in call
        assert (
            "--signer-workflow Example/flowork/.github/workflows/release-images.yml"
        ) in call
        assert f"--source-digest {source_sha}" in call
        assert "--source-ref refs/tags/v1.2.3" in call
        assert "--deny-self-hosted-runners" in call
    assert "--predicate-type https://spdx.dev/Document/v2.3" in calls[1]


@pytest.mark.parametrize("variable,label,message", [
    ("VIBECANVAS_VALKEY_IMAGE", "valkey", "Valkey"),
    ("VIBECANVAS_POSTGRES_IMAGE", "postgres", "Postgres"),
    ("VIBECANVAS_OPENFGA_POSTGRES_IMAGE", "openfga-postgres", "OpenFGA Postgres"),
])
@pytest.mark.parametrize("missing", [True, False])
def test_production_gate_requires_its_reviewed_postgres_images(
    tmp_path: Path, variable: str, label: str, message: str, missing: bool,
) -> None:
    docker = tmp_path / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "RELEASE_REPOSITORY": "Example/flowork", "RELEASE_SHA": "b" * 40,
           "RELEASE_REF": "refs/tags/v1.2.3", "PRODUCTION_EVIDENCE_MANIFEST": "unused"}
    for name, image_label in (("API", "api"), ("SANDBOX", "sandboxd"), ("WEB", "web"),
                              ("POSTGRES", "postgres"), ("OPENFGA_POSTGRES", "openfga-postgres"), ("VALKEY", "valkey")):
        env[f"VIBECANVAS_{name}_IMAGE"] = f"ghcr.io/example/flowork-{image_label}@sha256:{'a' * 64}"
    if missing:
        env.pop(variable)
        expected = f"{variable} is required"
    else:
        env[variable] = f"ghcr.io/example/other-{label}@sha256:{'a' * 64}"
        expected = f"{message} image must use the {label} release repository"
    result = subprocess.run(["bash", str(_PRODUCTION_RELEASE), "verify"],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert expected in result.stderr



def test_release_attestation_gate_rejects_tags_and_unknown_images(
    tmp_path: Path,
) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    env = os.environ.copy()
    env["GH_BIN"] = str(fake_gh)
    common = [
        "Example/flowork",
        "Example/flowork/.github/workflows/release-images.yml",
        "b" * 40,
        "refs/tags/v1.2.3",
    ]
    for invalid_image in (
        f"ghcr.io/example/flowork-api:latest@sha256:{'a' * 64}",
        f"ghcr.io/example/flowork-worker@sha256:{'a' * 64}",
        "ghcr.io/example/flowork-api:latest",
    ):
        result = subprocess.run(
            [str(_RELEASE_ATTESTATION_GATE), invalid_image, *common],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2


def test_web_image_fails_closed_on_extension_frame_ancestor_configuration() -> None:
    nginx = (_ROOT / "web/nginx.conf").read_text(encoding="utf-8")
    dockerfile = (_ROOT / "web/Dockerfile").read_text(encoding="utf-8")
    validator = _ROOT / "web/15-validate-extension-id.sh"
    assert "chrome-extension://${VIBECANVAS_BROWSER_EXTENSION_ID}" in nginx
    assert "chrome-extension://*" not in nginx
    assert (
        "NGINX_ENVSUBST_FILTER=^(VIBECANVAS_BROWSER_EXTENSION_ID|CSP_SCRIPT_HASHES)$"
        in dockerfile
    )
    assert validator.stat().st_mode & 0o111
    subprocess.run(["sh", "-n", str(validator)], check=True)


def test_web_image_packages_the_locked_extension_for_download() -> None:
    dockerfile = (_ROOT / "web/Dockerfile").read_text(encoding="utf-8")
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "COPY extension/package.json extension/pnpm-lock.yaml ./" in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "python -m zipfile -t /out/flowork-extension.zip" in dockerfile
    assert (
        "/usr/share/nginx/html/downloads/flowork-extension.zip" in dockerfile
    )
    assert "VITE_WEB_BASE: ${VIBECANVAS_EXTENSION_WEB_BASE" in compose
    assert "VITE_EXTENSION_ALLOWED_ORIGINS:" in compose


def test_clamav_compose_overlay_exposes_only_a_read_only_unix_socket() -> None:
    overlay = yaml.safe_load(
        (_ROOT / "docker-compose.security.yml").read_text(encoding="utf-8")
    )
    clamav = overlay["services"]["clamav"]
    api = overlay["services"]["api"]
    assert "ports" not in clamav
    assert clamav["healthcheck"]["test"] == [
        "CMD-SHELL",
        "test -S /tmp/clamd.sock",
    ]
    assert api["environment"]["UPLOAD_SCANNER_PROVIDER"] == "clamd"
    assert api["environment"]["UPLOAD_SCANNER_CLAMD_UNIX_SOCKET"] == (
        "/run/clamav/clamd.sock"
    )
    assert "clamav_socket:/run/clamav:ro" in api["volumes"]
