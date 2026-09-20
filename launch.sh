#!/usr/bin/env bash
umask 077
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
NATIVE_LAUNCHER="$REPO_ROOT/scripts/native_dev_up.sh"
RUNTIME_PREPARER="$REPO_ROOT/scripts/prepare_runtime_environment.sh"
LOCAL_ENV="${VIBECANVAS_LAUNCH_ENV:-$REPO_ROOT/.env.launch.local}"
NATIVE_RUNTIME_DIR="${VIBECANVAS_NATIVE_RUNTIME_DIR:-/tmp/vibecanvas-native}"

source_local_env() {
  if [[ -f "$LOCAL_ENV" ]]; then
    chmod 600 "$LOCAL_ENV"
    set -a
    # shellcheck disable=SC1090
    source "$LOCAL_ENV"
    set +a
  fi
}

load_local_env() {
  local requested_disable_platform_api="${VIBECANVAS_DISABLE_PLATFORM_DEFAULT_API:-}"
  local requested_model_egress_policy="${RUNTIME_MODEL_EGRESS_POLICY:-}"
  local requested_sandbox_egress_mode="${SANDBOX_EGRESS_MODE:-}"
  source_local_env
  # A command-line safety override wins over a legacy value in the local env
  # file. This is useful when validating a BYO-API deployment without deleting
  # the operator's encrypted/local configuration.
  if [[ -n "$requested_disable_platform_api" ]]; then
    export VIBECANVAS_DISABLE_PLATFORM_DEFAULT_API="$requested_disable_platform_api"
  fi
  if [[ -n "$requested_model_egress_policy" ]]; then
    export RUNTIME_MODEL_EGRESS_POLICY="$requested_model_egress_policy"
  fi
  if [[ -n "$requested_sandbox_egress_mode" ]]; then
    export SANDBOX_EGRESS_MODE="$requested_sandbox_egress_mode"
  fi
}

configure_debug_stack() {
  local configured_python="${VIBECANVAS_PYTHON:-$REPO_ROOT/.venv/bin/python}"
  [[ -x "$configured_python" ]] || {
    echo "ERROR: the repository-local uv environment is missing: $REPO_ROOT/.venv" >&2
    echo "Run ./scripts/bootstrap_native_linux.sh --prepare-only first." >&2
    exit 1
  }
  local runtime_cache_root="${VIBECANVAS_RUNTIME_CACHE_ROOT:-/tmp/vibecanvas-runtime-python}"
  local runtime_cache_prefix="$runtime_cache_root/env"
  local local_secret_dir="${VIBECANVAS_LOCAL_SECRET_DIR:-$HOME/.vibecanvas/secrets}"
  local local_kms_key_file="${KMS_LOCAL_MASTER_KEY_FILE:-$local_secret_dir/kms-master.key}"
  local local_lookup_key_file="${CONTENT_LOOKUP_HMAC_KEY_FILE:-$local_secret_dir/content-lookup-hmac.key}"

  # Cloud IDEs commonly reserve 8080. Keep the local authorization control
  # plane on dedicated loopback-only ports so starting Flowork never
  # requires stopping the IDE or exposing OpenFGA to the network.
  export OPENFGA_HTTP_PORT="${OPENFGA_HTTP_PORT:-18080}"
  export OPENFGA_GRPC_PORT="${OPENFGA_GRPC_PORT:-18081}"
  export OPENFGA_METRICS_PORT="${OPENFGA_METRICS_PORT:-12112}"

  # gVisor imports thousands of small Python dependency files. The configured
  # development environment lives on NFS, so a cold Chat otherwise pays remote
  # metadata latency for every import. Keep an immutable, process-local SSD
  # mirror and mount that prefix read-only into every sandbox. Refresh only
  # when the source environment or dependency manifests change.
  if [[ "${VIBECANVAS_LOCAL_RUNTIME_CACHE:-1}" == "1" ]]; then
    VIBECANVAS_SOURCE_PYTHON="$configured_python" \
      VIBECANVAS_RUNTIME_CACHE_ROOT="$runtime_cache_root" \
      bash "$RUNTIME_PREPARER" prepare
    export VIBECANVAS_PYTHON="$runtime_cache_prefix/bin/python"
    # The preparer mirrors the current api/engine source into this prefix.
    # Sandboxes import that local read-only copy instead of walking NFS.
    export VIBECANVAS_SANDBOX_USE_INSTALLED_APP=1
  else
    export VIBECANVAS_PYTHON="$configured_python"
    export VIBECANVAS_SANDBOX_USE_INSTALLED_APP=0
  fi

  # Public proxies forward to the machine's IPv6 port 9001. Keep the browser
  # path dynamic: index.html infers an opaque path prefix when present and the
  # same prefix is then used for Router, HTTP, SSE, and media URLs.
  export WEB_HOST="${WEB_HOST:-::}"
  export WEB_PORT="${WEB_PORT:-9001}"
  export VIBECANVAS_PUBLIC_URL="${VIBECANVAS_PUBLIC_URL:-http://localhost:${WEB_PORT}/}"
  local public_origin public_host
  public_origin="$($configured_python -c 'import sys; from urllib.parse import urlsplit; p=urlsplit(sys.argv[1]); print(f"{p.scheme}://{p.netloc}")' "$VIBECANVAS_PUBLIC_URL")"
  public_host="$($configured_python -c 'import sys; from urllib.parse import urlsplit; print(urlsplit(sys.argv[1]).hostname or "")' "$VIBECANVAS_PUBLIC_URL")"
  export WEB_ALLOWED_HOSTS="${WEB_ALLOWED_HOSTS:-$public_host}"
  # Direct private-IP access is a supported local deployment path. Build an
  # exact allowlist from this host's current interfaces instead of permitting
  # arbitrary Origins; otherwise the login page loads over the advertised
  # network URL but its Origin-protected POST is rejected with HTTP 403.
  local host_ip_origins
  host_ip_origins="$($configured_python - "$WEB_PORT" <<'PY'
import ipaddress
import socket
import sys

port = int(sys.argv[1])
addresses: set[str] = set()
for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
    if family not in {socket.AF_INET, socket.AF_INET6}:
        continue
    try:
        address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
    except ValueError:
        continue
    if address.is_loopback or address.is_unspecified:
        continue
    host = f"[{address}]" if address.version == 6 else str(address)
    addresses.add(f"http://{host}:{port}")
print(",".join(sorted(addresses)))
PY
)"
  local default_cors_origins="http://127.0.0.1:${WEB_PORT},http://localhost:${WEB_PORT},http://[::1]:${WEB_PORT},${public_origin}"
  if [[ -n "$host_ip_origins" ]]; then
    default_cors_origins="${default_cors_origins},${host_ip_origins}"
  fi
  export VIBECANVAS_API_CORS_ORIGINS="${VIBECANVAS_API_CORS_ORIGINS:-$default_cors_origins}"
  export WEB_BASE_PATH=""
  export WEB_API_BASE=""

  # Preview mode avoids shared-filesystem watcher pressure. In auto mode the
  # native launcher rebuilds only when frontend inputs are newer than dist.
  export WEB_MODE="${WEB_MODE:-preview}"
  export WEB_REBUILD="${WEB_REBUILD:-auto}"
  export WEB_INSTALL="${WEB_INSTALL:-auto}"

  # Source is mounted through PYTHONPATH by native_dev_up.sh. Set
  # BACKEND_INSTALL=1 explicitly when package metadata/dependencies changed.
  export BACKEND_INSTALL="${BACKEND_INSTALL:-0}"

  # Development SecretService uses a stable host-only wrapping key. The file
  # is never mounted into a sandbox and survives API restarts.
  "$configured_python" \
    "$REPO_ROOT/scripts/security/ensure_local_kms_key.py" \
    "$local_kms_key_file"
  "$configured_python" \
    "$REPO_ROOT/scripts/security/ensure_local_kms_key.py" \
    "$local_lookup_key_file"
  export KMS_PROVIDER="${KMS_PROVIDER:-local}"
  export KMS_KEY_ID="${KMS_KEY_ID:-vibecanvas-local-development}"
  export KMS_LOCAL_MASTER_KEY_FILE="$local_kms_key_file"
  export CONTENT_LOOKUP_HMAC_KEY_FILE="$local_lookup_key_file"
  export ENABLE_TEST_USER="${ENABLE_TEST_USER:-1}"
  export ENTERPRISE_SSO_ENABLED="${ENTERPRISE_SSO_ENABLED:-0}"
  export AGENT_RUNTIME_TYPES="${AGENT_RUNTIME_TYPES:-langchain,codex}"
  export CODEX_RUNTIME_AUTH_METHODS="${CODEX_RUNTIME_AUTH_METHODS:-chatgpt,managed_api,personal_api}"
  export CODEX_MANAGED_APIS_JSON="${CODEX_MANAGED_APIS_JSON:-[]}"
  export AGENT_RUNTIME_ROOT="${AGENT_RUNTIME_ROOT:-$HOME/.vibecanvas/agent-runtime}"
  export VFS_VOLUME_ROOT="${VFS_VOLUME_ROOT:-$HOME/.vibecanvas/vfs-volumes}"
  export AGENT_DEBUG_VIEW_ENABLED="${AGENT_DEBUG_VIEW_ENABLED:-1}"
  export WEB_SESSION_COOKIE_ENABLED="${WEB_SESSION_COOKIE_ENABLED:-1}"
  # This development stack is intentionally reachable both through the HTTPS
  # workspace proxy and direct localhost/private-IP HTTP. Production ignores
  # this override and always emits Secure __Host- cookies.
  export WEB_SESSION_COOKIE_SECURE="${WEB_SESSION_COOKIE_SECURE:-0}"
  export EXTENSION_SCOPED_TOKEN_ENABLED="${EXTENSION_SCOPED_TOKEN_ENABLED:-1}"
  export DISTRIBUTED_AUTH_RATE_LIMIT_ENABLED="${DISTRIBUTED_AUTH_RATE_LIMIT_ENABLED:-1}"
  export SANDBOX_DEBUG_EXECUTE_ENABLED="${SANDBOX_DEBUG_EXECUTE_ENABLED:-1}"
}

stop_stack() {
  # native_dev_up.sh performs PID-file shutdown, project-scoped fallback kills,
  # gVisor sandbox cleanup, and finally stops the local PostgreSQL and Redis.
  WEB_PORT="${WEB_PORT:-9001}" bash "$NATIVE_LAUNCHER" down
}

build_extension() {
  local extension_dir="$REPO_ROOT/extension"
  local extension_web_base="${VIBECANVAS_EXTENSION_WEB_BASE:-${VIBECANVAS_PUBLIC_URL%/}}"
  local extension_allowed_origins="${VIBECANVAS_EXTENSION_ALLOWED_ORIGINS:-$extension_web_base}"
  local extension_archive="$extension_dir/vibecanvas-extension.zip"
  local packaging_python="${VIBECANVAS_PYTHON:-$REPO_ROOT/.venv/bin/python}"

  if [[ ! -d "$extension_dir/node_modules" ]]; then
    pnpm --dir "$extension_dir" install --frozen-lockfile
  fi
  echo "extension: building for $extension_web_base"
  VITE_WEB_BASE="$extension_web_base" \
    VITE_EXTENSION_ALLOWED_ORIGINS="$extension_allowed_origins" \
    pnpm --dir "$extension_dir" build
  "$packaging_python" - "$extension_dir/dist" "$extension_archive" <<'PY'
from pathlib import Path
import sys
import zipfile

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(source))
print(f"extension: package ready: {target}")
PY
}

publish_extension_archive() {
  local source="$REPO_ROOT/extension/vibecanvas-extension.zip"
  local public_target="$REPO_ROOT/web/public/downloads/vibecanvas-extension.zip"
  local dist_target="$REPO_ROOT/web/dist/downloads/vibecanvas-extension.zip"
  mkdir -p "$(dirname "$public_target")" "$(dirname "$dist_target")"
  cp "$source" "$public_target"
  cp "$source" "$dist_target"
  chmod 644 "$public_target" "$dist_target"
  echo "extension: published download: $dist_target"
}

start_stack() {
  load_local_env
  configure_debug_stack
  stop_stack
  build_extension
  # Dev mode serves web/public directly; preview mode's Vite build copies the
  # same archive into dist. Publishing before up supports both paths.
  publish_extension_archive
  bash "$NATIVE_LAUNCHER" up
  # A production rebuild replaces dist atomically. Re-publish afterward so a
  # skipped build and a fresh build expose the exact same latest ZIP.
  publish_extension_archive
  bash "$NATIVE_LAUNCHER" status
  echo
  echo "Flowork is ready:"
  echo "  $VIBECANVAS_PUBLIC_URL"
  echo "Chrome extension:"
  echo "  unpacked: $REPO_ROOT/extension/dist"
  echo "  package:  $REPO_ROOT/extension/vibecanvas-extension.zip"
}

show_logs() {
  echo "Sandbox: $NATIVE_RUNTIME_DIR/sandboxd.log"
  echo "API:    $NATIVE_RUNTIME_DIR/api.log"
  echo "Worker: $NATIVE_RUNTIME_DIR/worker.log"
  echo "Beat:   $NATIVE_RUNTIME_DIR/beat.log"
  echo "Web:    $NATIVE_RUNTIME_DIR/web.log"
  tail -n 80 \
    "$NATIVE_RUNTIME_DIR/sandboxd.log" \
    "$NATIVE_RUNTIME_DIR/api.log" \
    "$NATIVE_RUNTIME_DIR/worker.log" \
    "$NATIVE_RUNTIME_DIR/beat.log" \
    "$NATIVE_RUNTIME_DIR/web.log"
}

case "${1:-restart}" in
  start|restart)
    start_stack
    ;;
  stop|down)
    # Stopping must happen immediately. Do not refresh the Python runtime cache
    # while old API/sandbox processes are still alive.
    export WEB_PORT="${WEB_PORT:-9001}"
    stop_stack
    ;;
  status)
    source_local_env
    export WEB_PORT="${WEB_PORT:-9001}"
    bash "$NATIVE_LAUNCHER" status
    echo "public URL: ${VIBECANVAS_PUBLIC_URL:-http://localhost:${WEB_PORT}/}"
    ;;
  logs)
    show_logs
    ;;
  prepare-runtime)
    load_local_env
    configure_debug_stack
    VIBECANVAS_SOURCE_PYTHON="${VIBECANVAS_SOURCE_PYTHON:-$VIBECANVAS_PYTHON}" \
      bash "$RUNTIME_PREPARER" status
    ;;
  *)
    echo "usage: $0 {start|restart|stop|status|logs|prepare-runtime}" >&2
    exit 2
    ;;
esac
