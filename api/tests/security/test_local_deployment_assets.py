from __future__ import annotations

import os
from pathlib import Path
import subprocess

import yaml

from scripts.security.migrate_strict_content_encryption import _resolve_api_root


REPO_ROOT = Path(__file__).resolve().parents[3]


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_local_server_init_is_secure_and_idempotent(tmp_path: Path) -> None:
    env_file = tmp_path / "local.env"
    env = {**os.environ, "VIBECANVAS_ENV_FILE": str(env_file)}
    command = [str(REPO_ROOT / "scripts/deploy/local_server.sh"), "init"]

    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True, capture_output=True)
    first = env_file.read_bytes()
    values = _dotenv(env_file)

    assert env_file.stat().st_mode & 0o777 == 0o600
    assert values["VIBECANVAS_BIND_ADDRESS"] == "127.0.0.1"
    assert values["VIBECANVAS_INTERNAL_BIND_ADDRESS"] == "127.0.0.1"
    assert values["VIBECANVAS_PUBLIC_URL"] == "http://localhost:9001"
    assert values["ENABLE_TEST_USER"] == "false"
    assert values["ENTERPRISE_SSO_ENABLED"] == "false"
    assert values["SANDBOX_TYPE"] == "rootful-snapshot"

    independent_secrets = {
        values[key]
        for key in (
            "POSTGRES_PASSWORD",
            "VIBECANVAS_APP_PASSWORD",
            "VIBECANVAS_MIGRATOR_PASSWORD",
            "VIBECANVAS_MAINTENANCE_PASSWORD",
            "OPENFGA_POSTGRES_PASSWORD",
            "OPENFGA_ERASURE_PASSWORD",
            "OPENFGA_API_TOKEN",
            "KMS_LOCAL_MASTER_KEY",
            "CONTENT_LOOKUP_HMAC_KEY",
            "BROWSER_TOKEN_SECRET",
            "VIBECANVAS_SIGNING_SECRET",
            "OAUTH_ENCRYPTION_KEY",
        )
    }
    assert len(independent_secrets) == 12
    assert all(len(value) >= 32 for value in independent_secrets)

    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True, capture_output=True)
    assert env_file.read_bytes() == first


def test_local_server_init_backfills_missing_upgrade_settings(tmp_path: Path) -> None:
    env_file = tmp_path / "existing.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=keep-existing-secret\n"
        "VIBECANVAS_BIND_ADDRESS=127.0.0.1\n",
        encoding="utf-8",
    )
    env = {**os.environ, "VIBECANVAS_ENV_FILE": str(env_file)}
    command = [str(REPO_ROOT / "scripts/deploy/local_server.sh"), "init"]

    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True, capture_output=True)
    first = env_file.read_bytes()
    values = _dotenv(env_file)

    assert values["POSTGRES_PASSWORD"] == "keep-existing-secret"
    assert values["VIBECANVAS_INTERNAL_BIND_ADDRESS"] == "127.0.0.1"
    assert len(values["OPENFGA_ERASURE_PASSWORD"]) >= 32
    assert values["SANDBOX_EGRESS_MODE"] == "proxy"
    assert values["SANDBOX_EGRESS_POLICY"] == "public"
    assert env_file.stat().st_mode & 0o777 == 0o600

    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True, capture_output=True)
    assert env_file.read_bytes() == first


def test_compose_published_ports_default_to_loopback() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    for service_name in ("postgres", "redis", "openfga", "api", "background_worker"):
        for port in compose["services"][service_name].get("ports", []):
            assert "${VIBECANVAS_INTERNAL_BIND_ADDRESS:-127.0.0.1}:" in port
    for port in compose["services"]["web"].get("ports", []):
        assert "${VIBECANVAS_BIND_ADDRESS:-127.0.0.1}:" in port


def test_only_sandboxd_is_privileged_and_owns_snapshot_storage() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    privileged = {
        name for name, service in services.items() if service.get("privileged") is True
    }

    assert privileged == {"sandboxd"}
    sandboxd = services["sandboxd"]
    assert sandboxd["user"] == "0:10001"
    assert sandboxd["environment"]["SANDBOX_SERVICE_SOCKET_MODE"] == "0660"
    assert sandboxd["environment"]["SANDBOX_SERVICE_SOCKET_DIR_MODE"] == "0770"
    assert sandboxd["environment"]["SANDBOX_SERVICE_SOCKET_GID"] == "10001"
    assert sandboxd["environment"]["SANDBOX_NETWORK"].endswith(":-none}")
    assert sandboxd["environment"]["SANDBOX_TYPE"].endswith(
        ":-rootful-snapshot}"
    )
    snapshot_mount = "sandbox_snapshot_data:/var/lib/vibecanvas/snapshots"
    assert snapshot_mount in sandboxd["volumes"]
    for name, service in services.items():
        if name != "sandboxd":
            assert snapshot_mount not in service.get("volumes", [])
    for name in (
        "openfga_bootstrap", "migrate", "sandbox_prewarm", "api",
        "background_worker",
    ):
        assert services[name]["user"] == "10001:10001"
        assert (
            services[name]["environment"]["VIBECANVAS_STORAGE_ROOT"]
            == "/var/lib/vibecanvas/local-data"
        )
    assert services["web"]["user"] == "101:101"
    assert services["web"]["ports"] == [
        "${VIBECANVAS_BIND_ADDRESS:-127.0.0.1}:${VIBECANVAS_HTTP_PORT:-9001}:8080"
    ]


def test_compose_networks_separate_edge_control_data_and_authorization() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    networks = compose["networks"]

    assert services["web"]["networks"] == ["edge"]
    assert set(services["api"]["networks"]) == {
        "edge", "control", "data", "authorization",
    }
    assert set(services["sandboxd"]["networks"]) == {
        "control", "data", "runtime_egress",
    }
    assert services["postgres"]["networks"] == ["data"]
    assert services["redis"]["networks"] == ["data"]
    assert services["openfga_postgres"]["networks"] == ["authorization_data"]
    assert set(services["background_worker"]["networks"]) == {
        "data",
        "authorization",
        "authorization_data",
        "worker_egress",
    }
    assert networks["control"]["internal"] is True
    assert networks["data"]["internal"] is True
    assert networks["authorization"]["internal"] is True
    assert networks["authorization_data"]["internal"] is True


def test_nginx_refreshes_compose_service_dns() -> None:
    nginx = (REPO_ROOT / "web/nginx.conf").read_text(encoding="utf-8")

    assert "resolver 127.0.0.11" in nginx
    assert "server api:8000 resolve;" in nginx
    assert nginx.count("proxy_pass http://flowork_api;") == 3


def test_nginx_preserves_browser_authority_for_origin_validation() -> None:
    nginx = (REPO_ROOT / "web/nginx.conf").read_text(encoding="utf-8")

    websocket = nginx.split("location = /api/v1/browser/ws", 1)[1].split(
        "location /api/", 1
    )[0]
    api_proxy = nginx.split("location /api/", 1)[1].split(
        "location /healthz", 1
    )[0]
    assert "proxy_set_header Host $http_host;" in websocket
    assert "proxy_set_header Host $http_host;" in api_proxy
    assert "proxy_set_header Host $host;" not in websocket
    assert "proxy_set_header Host $host;" not in api_proxy


def test_nginx_serves_module_workers_with_javascript_mime() -> None:
    nginx = (REPO_ROOT / "web/nginx.conf").read_text(encoding="utf-8")
    pdf_renderer = (
        REPO_ROOT / "web/src/pages/chat/preview/PdfPreviewRenderer.tsx"
    ).read_text(encoding="utf-8")

    assert "location ~* \\.mjs$" in nginx
    assert "types { application/javascript mjs; }" in nginx
    assert "try_files $uri =404;" in nginx
    assert "PDF_WORKER_CACHE_REVISION = 'module-mime-v1'" in pdf_renderer
    assert "pdfWorkerUrl}?v=${PDF_WORKER_CACHE_REVISION}" in pdf_renderer


def test_interactive_preview_has_a_dedicated_response_sandbox() -> None:
    nginx = (REPO_ROOT / "web/nginx.conf").read_text(encoding="utf-8")
    loader = (
        REPO_ROOT / "web/public/interactive-sandbox.html"
    ).read_text(encoding="utf-8")
    location = nginx.split(
        "location = /interactive-sandbox.html", 1
    )[1].split("location /", 1)[0]

    # The application shell retains the strict no-unsafe-inline policy. Only
    # the exact loader response can execute Agent-authored inline behavior.
    server_headers = nginx.split("location ^~ /embed/", 1)[0]
    assert "script-src 'self' ${CSP_SCRIPT_HASHES}" in server_headers
    assert "script-src 'self' 'unsafe-inline'" not in server_headers
    assert "sandbox allow-scripts allow-forms" in location
    assert "script-src 'unsafe-inline'" in location
    assert "frame-ancestors 'self'" in location
    assert 'X-Frame-Options "SAMEORIGIN"' in location
    assert 'Cache-Control "no-store"' in location
    assert "connect-src 'self' data: blob:" in location
    assert "frame-src 'none'" in location
    assert "object-src 'none'" in location

    # The document arrives over postMessage rather than a URL/query/history
    # channel, and the bootstrap accepts messages only from its parent frame.
    assert "vibecanvas:interactive-loader:v1" in loader
    assert "event.source !== window.parent" in loader
    assert "typeof payload.html !== 'string'" in loader
    assert "MAX_DOCUMENT_CHARS = 2 * 1024 * 1024" in loader
    assert "document.write(html)" in loader
    assert "location.search" not in loader
    assert "location.hash" not in loader
    assert "localStorage" not in loader


def test_business_schema_precedes_dbos_schema_migration() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    migrate_command = compose["services"]["migrate"]["command"][-1]
    strict = "migrate_strict_content_encryption.py"
    compose_dbos = "dbos migrate"
    assert migrate_command.index(strict) < migrate_command.index(compose_dbos)
    assert migrate_command.count("dbos migrate") == 1
    assert "-r vibecanvas_app" in migrate_command
    assert "provision_dbos_roles.py" in migrate_command

    native = (REPO_ROOT / "scripts/native_dev_up.sh").read_text()
    migrate_function = native.split("migrate() {", 1)[1].split(
        "start_services() {", 1
    )[0]
    native_dbos = 'bin/dbos" migrate'
    assert migrate_function.index(strict) < migrate_function.index(native_dbos)
    assert migrate_function.count('bin/dbos" migrate') == 1
    assert "-r vibecanvas_app" in migrate_function
    assert "provision_dbos_roles.py" in migrate_function


def test_dbos_roles_are_reprovisioned_for_transactional_enqueue() -> None:
    script = (
        REPO_ROOT / "scripts/security/provision_dbos_roles.py"
    ).read_text()
    assert '_ROLES = ("vibecanvas_app", "vibecanvas_maintenance")' in script
    assert 'GRANT USAGE ON SCHEMA "dbos"' in script
    assert 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA "dbos"' in script
    assert 'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "dbos"' in script
    assert 'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "dbos"' in script
    assert "DBOS schema is missing; run dbos migrate first" in script


def test_local_verifier_bypasses_host_proxy_for_loopback_health() -> None:
    verifier = (REPO_ROOT / "scripts/deploy/verify_local.sh").read_text()
    assert verifier.count("curl --noproxy '*'") == 2


def test_postgres_role_passwords_are_environment_bound() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    postgres_env = compose["services"]["postgres"]["environment"]
    init_sql = (REPO_ROOT / "scripts/postgres-init/01-create-app-role.sql").read_text()

    for key in (
        "VIBECANVAS_APP_PASSWORD",
        "VIBECANVAS_MIGRATOR_PASSWORD",
        "VIBECANVAS_MAINTENANCE_PASSWORD",
    ):
        assert key in postgres_env
        assert "\\getenv" in init_sql
        assert key in init_sql
    assert "PASSWORD 'vc_app'" not in init_sql


def test_openfga_erasure_role_cannot_access_live_tuples() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    sql = (REPO_ROOT / "scripts/security/openfga_erasure.sql").read_text()

    worker = services["background_worker"]
    assert "OPENFGA_ERASURE_DATABASE_URL" in worker["environment"]
    assert "openfga_erasure_bootstrap" in worker["depends_on"]
    assert "SECURITY DEFINER" in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public" in sql
    assert "GRANT EXECUTE ON FUNCTION public.flowork_erase_changelog" in sql
    assert "GRANT SELECT" not in sql
    assert "GRANT DELETE" not in sql


def test_strict_migrator_supports_source_and_image_layouts(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (source_root / "api/alembic").mkdir(parents=True)
    (source_root / "api/alembic.ini").touch()
    assert _resolve_api_root(source_root) == source_root / "api"

    image_root = tmp_path / "image"
    (image_root / "alembic").mkdir(parents=True)
    (image_root / "alembic.ini").touch()
    assert _resolve_api_root(image_root) == image_root
