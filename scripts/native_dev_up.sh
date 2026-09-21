#!/usr/bin/env bash
# Bring up the complete Flowork stack as native unprivileged processes.
# stack as NATIVE unprivileged processes (no Docker).
#
# Use this when the host cannot run Docker (e.g. a k8s pod where CLONE_NEWNS
# is blocked). It brings up the WHOLE stack end-to-end as native processes —
# PostgreSQL, Redis, schema migration, the FastAPI app, the DBOS background
# worker/scheduler, AND the web frontend (Vite) — using the SAME config code
# paths the docker-compose deploy uses, just each service as a local process.
#
# One command: `bash scripts/native_dev_up.sh up` → open http://127.0.0.1:5173
#
# Data PERSISTS across down/up (and reboots): the Postgres cluster + object
# store live under $HOME/.vibecanvas by default, and an already-initialised
# cluster is reused — registered accounts/workflows survive a restart. Pass
# RESET=1 to force a clean slate (wipe the db + re-provision).
#
#   bash scripts/native_dev_up.sh up         # bring everything up (keep data)
#   bash scripts/native_dev_up.sh down       # stop api/worker/beat + pg + redis
#   bash scripts/native_dev_up.sh status     # health-check what is running
#   RESET=1 bash scripts/native_dev_up.sh up # nuke the db and start fresh
#
# Env knobs (all have sane defaults):
#   VIBECANVAS_PYTHON repository-local uv Python (default: .venv/bin/python)
#   BACKEND_INSTALL 1=install api+engine wheels before up (default: 1)
#   PGPORT     postgres port                         (default: 5433)
#   REDISPORT  redis port                            (default: 6379)
#   RESET      1=wipe + re-provision the db on up    (default: 0, data persists)
#   PGDATA     postgres data dir                     (default: $HOME/.vibecanvas/pgdata)
#   OBJSTORE   shared object-store root              (default: $HOME/.vibecanvas/objectstore)
#   WEB        1=start web, 0=backend only            (default: 1)
#   WEB_MODE   'preview' (build+static, robust) | 'dev' (HMR)  (default: preview)
#   WEB_PORT   vite port                              (default: 5173)
#   VIBECANVAS_NATIVE_RUNTIME_DIR process state/logs  (default: /tmp/vibecanvas-native)
#   WEB_ALLOWED_HOSTS comma-separated extra Vite host allowlist entries
#   WEB_BASE_PATH optional fixed browser mount (e.g. /studio); leave empty for
#                 automatic runtime inference behind temporary prefix proxies
#   WEB_API_BASE optional API path/origin; defaults to the resolved app mount
#   WEB_REBUILD auto=build when sources changed, 1=always, 0=reuse dist
#                                                        (default: auto)
#   WEB_INSTALL auto|1|0 controls `pnpm install` before web start (default: auto)
set -euo pipefail
umask 077

# ─── repo + paths ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
API_DIR="$REPO_ROOT/api"
ENGINE_DIR="$REPO_ROOT/engine"
DAEMONIZER="$REPO_ROOT/scripts/daemonize_process.py"
SUPERVISOR="$REPO_ROOT/scripts/supervise_process.py"

PGBIN="${PGBIN:-$(pg_config --bindir 2>/dev/null || true)}"
PGPORT="${PGPORT:-5433}"
REDISPORT="${REDISPORT:-6379}"
OPENFGA_HTTP_PORT="${OPENFGA_HTTP_PORT:-8080}"
OPENFGA_GRPC_PORT="${OPENFGA_GRPC_PORT:-8081}"
OPENFGA_METRICS_PORT="${OPENFGA_METRICS_PORT:-2112}"
# Persistent by default: data lives under $HOME (survives `down`/`up` AND a
# machine reboot), NOT /tmp (cleared on reboot). Override to relocate.
PGDATA="${PGDATA:-$HOME/.vibecanvas/pgdata}"
OBJSTORE="${OBJSTORE:-$HOME/.vibecanvas/objectstore}"
BACKEND_INSTALL="${BACKEND_INSTALL:-1}"
WEB_DIR="$REPO_ROOT/web"
WEB_PORT="${WEB_PORT:-5173}"
# WEB_MODE: 'preview' (build once + static serve — robust everywhere, no file
# watcher) or 'dev' (HMR; faster but needs a healthy inotify limit — some
# sandboxes hit ENOSPC on the watcher). Default 'preview' so one command works
# anywhere; set WEB_MODE=dev on a normal dev box for HMR. WEB=0 skips the web.
WEB_MODE="${WEB_MODE:-preview}"
WEB="${WEB:-1}"
RUNDIR="${VIBECANVAS_NATIVE_RUNTIME_DIR:-/tmp/vibecanvas-native}"
mkdir -p "$RUNDIR" "$OBJSTORE"
# The directory can predate the current umask (for example from an older
# launcher version).  It contains the effective process environment, including
# OpenFGA and signing credentials, so repair permissions on every invocation
# instead of relying on creation-time mode alone.
chmod 700 "$RUNDIR"

WEB_BUILD_LOG="$RUNDIR/web-build.log"
WEB_INSTALL_LOG="$RUNDIR/web-install.log"
WEB_URL_FILE="$RUNDIR/web.url"
WEB_RUNTIME_ENV="$RUNDIR/web.env"
WEB_RUNTIME_DIST="$RUNDIR/web-dist"

cleanup_vibecanvas_sandboxes() {
  # gVisor workers are intentionally long-lived while the app is running, but
  # dev restarts must never leave orphaned runsc/gofer/sandbox processes behind.
  # Match only this platform's temporary bundle/state roots.
  local pattern='(/tmp/vc-sbx-|--root=/tmp/vc-sbx-|/proc/self/exe --root=/tmp/vc-sbx-|runsc-(gofer|sandbox).*vc-sbx)'
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "stopping vibecanvas sandboxes"
    pkill -TERM -f "$pattern" 2>/dev/null || true
    sleep 0.5
    pkill -KILL -f "$pattern" 2>/dev/null || true
  fi
  # Bundle dirs are disposable runtime state. Persistent user/workflow/chat data
  # lives under OBJECT_STORE_FS_ROOT, not under /tmp/vc-sbx-*.
  rm -rf /tmp/vc-sbx-* 2>/dev/null || true
}

cleanup_dead_object_store_materializations() {
  # Encrypted Object Store projections are process-private plaintext caches.
  # Startup also removes stale owners, but an explicit stop must leave no
  # recoverable plaintext behind. Delete only exact process-<pid> children of
  # the configured root and only after that pid no longer exists.
  local materialized_root="${OBJECT_STORE_MATERIALIZED_ROOT:-/tmp/vibecanvas-materialized}"
  [[ -d "$materialized_root" ]] || return 0
  [[ "$materialized_root" != "/" ]] || return 1
  [[ "$materialized_root" != "$HOME" ]] || return 1
  [[ "$materialized_root" != "$REPO_ROOT" ]] || return 1

  local directory name pid
  while IFS= read -r -d '' directory; do
    name="$(basename "$directory")"
    [[ "$name" =~ ^process-([0-9]+)$ ]] || continue
    pid="${BASH_REMATCH[1]}"
    kill -0 "$pid" 2>/dev/null && continue
    rm -rf -- "$directory"
    echo "removed stale Object Store materialization $name"
  done < <(
    find "$materialized_root" -mindepth 1 -maxdepth 1 -type d \
      -name 'process-[0-9]*' -print0
  )
}

stop_pidfile() {
  local name="$1"
  local pidfile="$RUNDIR/$name.pid"
  [[ -f "$pidfile" ]] || return 0
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "stopped $name"
  fi
  rm -f "$pidfile"
}

kill_dev_processes_by_pattern() {
  local pattern="$1"
  pkill -TERM -f "$pattern" 2>/dev/null || true
  sleep 0.2
  pkill -KILL -f "$pattern" 2>/dev/null || true
}

web_deps_need_install() {
  case "${WEB_INSTALL:-auto}" in
    1|true|yes) return 0 ;;
    0|false|no) return 1 ;;
    auto|"") ;;
    *) echo "ERROR: WEB_INSTALL must be auto, 1, or 0"; return 2 ;;
  esac

  local marker="$WEB_DIR/node_modules/.modules.yaml"
  [[ -f "$marker" ]] || return 0

  local f
  for f in package.json pnpm-lock.yaml pnpm-workspace.yaml; do
    [[ "$WEB_DIR/$f" -nt "$marker" ]] && return 0
  done
  return 1
}

web_build_needed() {
  local mode="${WEB_REBUILD:-auto}"
  local marker="$WEB_DIR/dist/index.html"
  case "$mode" in
    1|true|yes) return 0 ;;
    0|false|no) [[ ! -f "$marker" ]] ;;
    auto|"") ;;
    *) echo "ERROR: WEB_REBUILD must be auto, 1, or 0"; return 2 ;;
  esac

  [[ -f "$marker" ]] || return 0

  local f
  for f in \
    index.html package.json pnpm-lock.yaml pnpm-workspace.yaml \
    postcss.config.js tailwind.config.js tsconfig.json tsconfig.app.json \
    tsconfig.node.json vite.config.ts; do
    [[ -f "$WEB_DIR/$f" && "$WEB_DIR/$f" -nt "$marker" ]] && return 0
  done
  if find "$WEB_DIR/src" "$WEB_DIR/public" \
    -type f -newer "$marker" -print -quit 2>/dev/null | grep -q .; then
    return 0
  fi
  return 1
}

# ─── Python environment ────────────────────────────────────────────────────
resolve_backend_python() {
  VIBECANVAS_PYTHON="${VIBECANVAS_PYTHON:-$REPO_ROOT/.venv/bin/python}"
  [[ -x "$VIBECANVAS_PYTHON" ]] || {
    echo "ERROR: the uv environment is missing: $VIBECANVAS_PYTHON" >&2
    echo "Run ./scripts/bootstrap_native_linux.sh --prepare-only first." >&2
    exit 1
  }
  VIBECANVAS_PYTHON="$("$VIBECANVAS_PYTHON" - <<'PY'
import sys
print(sys.executable)
PY
)"
  VIBECANVAS_PY_PREFIX="$("$VIBECANVAS_PYTHON" - <<'PY'
import sys
print(sys.prefix)
PY
)"
  [[ "$VIBECANVAS_PY_PREFIX" == "$REPO_ROOT/.venv" ]] || {
    echo "ERROR: native installation requires $REPO_ROOT/.venv/bin/python" >&2
    exit 1
  }
  export VIBECANVAS_PYTHON VIBECANVAS_PY_PREFIX PYTHONNOUSERSITE=1
  export PATH="$VIBECANVAS_PY_PREFIX/bin:$PATH"
}

backend_deps_need_install() {
  case "$BACKEND_INSTALL" in
    1|true|yes) return 0 ;;
    0|false|no) return 1 ;;
    *) echo "ERROR: BACKEND_INSTALL must be 1 or 0"; return 2 ;;
  esac
}

install_backend_packages() {
  backend_deps_need_install || return 0
  echo "backend: refreshing api + engine source packages in $VIBECANVAS_PYTHON"
  # Dependency resolution belongs to prepare_runtime_environment.sh. Keeping
  # this step source-only makes repeated native starts fast without allowing a
  # newly declared dependency to be silently skipped.
  "$VIBECANVAS_PYTHON" -m pip install --upgrade --force-reinstall \
    --no-build-isolation --no-deps "$REPO_ROOT/engine" "$REPO_ROOT/api"
}

resolve_sandbox_python_paths() {
  if [[ -z "${SANDBOX_PYTHON_PATHS:-}" ]]; then
    SANDBOX_PYTHON_PATHS="$("$VIBECANVAS_PYTHON" - <<'PY'
import sysconfig
paths = []
for key in ("purelib", "platlib"):
    p = sysconfig.get_paths().get(key)
    if p:
        paths.append(p)
print(":".join(dict.fromkeys(paths)))
PY
)"
  fi
  export SANDBOX_PYTHON_PATHS
}

# ─── shared env (api + worker + beat MUST share these — esp. OBJECT_STORE_FS_ROOT) ──
write_env() {
  # Persist the effective Runtime credentials/configuration, not merely the
  # short shell alias.  Replacement API/worker processes are launched through
  # run.sh and therefore cannot inherit unexported variables from the original
  # interactive shell.
  local default_agent_api="${VIBECANVAS_DEFAULT_AGENT_API:-${DEFAULT_API:-}}"
  local local_secret_dir="${VIBECANVAS_LOCAL_SECRET_DIR:-$HOME/.vibecanvas/secrets}"
  local agent_runtime_root="${AGENT_RUNTIME_ROOT:-$HOME/.vibecanvas/agent-runtime}"
  local vfs_volume_root="${VFS_VOLUME_ROOT:-$HOME/.vibecanvas/vfs-volumes}"
  local local_kms_key_file="${KMS_LOCAL_MASTER_KEY_FILE:-$local_secret_dir/kms-master.key}"
  local local_lookup_key_file="${CONTENT_LOOKUP_HMAC_KEY_FILE:-$local_secret_dir/content-lookup-hmac.key}"
  local cors_origins="${VIBECANVAS_API_CORS_ORIGINS:-http://127.0.0.1:${WEB_PORT},http://localhost:${WEB_PORT},http://[::1]:${WEB_PORT}}"
  "$VIBECANVAS_PYTHON" \
    "$REPO_ROOT/scripts/security/ensure_local_kms_key.py" \
    "$local_kms_key_file"
  "$VIBECANVAS_PYTHON" \
    "$REPO_ROOT/scripts/security/ensure_local_kms_key.py" \
    "$local_lookup_key_file"
  install -d -m 700 "$agent_runtime_root"
  install -d -m 700 "$vfs_volume_root"
  export KMS_PROVIDER="${KMS_PROVIDER:-local}"
  export KMS_KEY_ID="${KMS_KEY_ID:-vibecanvas-local-development}"
  export KMS_LOCAL_MASTER_KEY_FILE="$local_kms_key_file"
  export CONTENT_LOOKUP_HMAC_KEY_FILE="$local_lookup_key_file"
  local signing_secret="${VIBECANVAS_SIGNING_SECRET:-}"
  if [[ -z "$signing_secret" ]]; then
    signing_secret="$(openssl rand -hex 32)"
  fi
  local default_agent_api_q signing_secret_q codex_cli_path_q public_url_q
  local agent_runtime_types_q codex_auth_methods_q codex_managed_apis_q
  local codex_access_token_q codex_id_token_q mount_path_q
  printf -v default_agent_api_q '%q' "$default_agent_api"
  printf -v signing_secret_q '%q' "$signing_secret"
  printf -v codex_cli_path_q '%q' "${CODEX_CLI_PATH:-}"
  printf -v public_url_q '%q' "${VIBECANVAS_PUBLIC_URL:-}"
  printf -v codex_access_token_q '%q' "${CODEX_ACCESS_TOKEN:-}"
  printf -v codex_id_token_q '%q' "${CODEX_ID_TOKEN:-}"
  printf -v mount_path_q '%q' "${MOUNT_PATH:-}"
  printf -v agent_runtime_types_q '%q' "${AGENT_RUNTIME_TYPES:-codex}"
  printf -v codex_auth_methods_q '%q' "${CODEX_RUNTIME_AUTH_METHODS:-chatgpt,managed_api,personal_api}"
  printf -v codex_managed_apis_q '%q' "${CODEX_MANAGED_APIS_JSON:-[]}"
  cat > "$RUNDIR/.env.native" <<EOF
export VIBECANVAS_PYTHON="${VIBECANVAS_PYTHON}"
export VIBECANVAS_PY_PREFIX="${VIBECANVAS_PY_PREFIX}"
export VIBECANVAS_REPO_ROOT="${REPO_ROOT}"
export VIBECANVAS_API_ROOT="${API_DIR}"
export PATH="${VIBECANVAS_PY_PREFIX}/bin:\$PATH"
export PYTHONPATH="${API_DIR}/src:${ENGINE_DIR}/src:${REPO_ROOT}:\${PYTHONPATH:-}"
export PYTHONNOUSERSITE=1
export DATABASE_URL="postgresql+asyncpg://vibecanvas_app:vibecanvas_app@localhost:${PGPORT}/vibecanvas"
export DBOS_SYSTEM_DATABASE_URL="postgresql+psycopg://vibecanvas_app:vibecanvas_app@localhost:${PGPORT}/vibecanvas"
export DBOS_RUN_MIGRATIONS="false"
export MAINTENANCE_DATABASE_URL="postgresql+asyncpg://vibecanvas_maintenance:vibecanvas_maintenance@localhost:${PGPORT}/vibecanvas"
export REDIS_URL="redis://localhost:${REDISPORT}/0"
export OPENFGA_API_URL="${OPENFGA_API_URL}"
export OPENFGA_API_TOKEN="${OPENFGA_API_TOKEN}"
export OPENFGA_BOOTSTRAP_CONFIG_FILE="${OPENFGA_BOOTSTRAP_CONFIG_FILE}"
export KMS_PROVIDER="${KMS_PROVIDER}"
export KMS_KEY_ID="${KMS_KEY_ID}"
export KMS_LOCAL_MASTER_KEY_FILE="${KMS_LOCAL_MASTER_KEY_FILE}"
export CONTENT_LOOKUP_HMAC_KEY_FILE="${CONTENT_LOOKUP_HMAC_KEY_FILE}"
export VIBECANVAS_API_CORS_ORIGINS="${cors_origins}"
export VIBECANVAS_PUBLIC_URL=${public_url_q}
export WEB_SESSION_COOKIE_ENABLED="${WEB_SESSION_COOKIE_ENABLED:-0}"
export WEB_SESSION_COOKIE_SECURE="${WEB_SESSION_COOKIE_SECURE:-}"
export EXTENSION_SCOPED_TOKEN_ENABLED="${EXTENSION_SCOPED_TOKEN_ENABLED:-0}"
export DISTRIBUTED_AUTH_RATE_LIMIT_ENABLED="${DISTRIBUTED_AUTH_RATE_LIMIT_ENABLED:-0}"
export RESOURCE_SHARING_ENABLED="${RESOURCE_SHARING_ENABLED:-1}"
export OBJECT_STORE_PROVIDER="filesystem"
export OBJECT_STORE_FS_ROOT="${OBJSTORE}"
export VIBECANVAS_STORAGE_ROOT="${HOME}/.vibecanvas/local_data"
export MOUNT_PATH=${mount_path_q}
export MOUNT_SYNC_INTERVAL_SECONDS="${MOUNT_SYNC_INTERVAL_SECONDS:-1.0}"
export DBOS_MAX_EXECUTOR_THREADS="${DBOS_MAX_EXECUTOR_THREADS:-2}"
export BACKGROUND_QUEUE_CONCURRENCY="${BACKGROUND_QUEUE_CONCURRENCY:-1}"
export PURGE_WORKER_ENABLED="${PURGE_WORKER_ENABLED:-true}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
# OPENAI_API_KEY is optional and only used by explicitly configured Agent runtimes.
# Workflow execution and debugging use the same sandbox manager and session RPC.
export SANDBOX_DEBUG_EXECUTE_ENABLED="${SANDBOX_DEBUG_EXECUTE_ENABLED:-1}"
export SANDBOX_NETWORK="${SANDBOX_NETWORK:-}"
export SANDBOX_SERVICE_MODE="service"
export SANDBOX_SERVICE_SOCKET="${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock}"
export SANDBOX_TYPE="${SANDBOX_TYPE:-rootless-warm}"
export AGENT_DEBUG_VIEW_ENABLED="${AGENT_DEBUG_VIEW_ENABLED:-0}"
export ENABLE_TEST_USER="${ENABLE_TEST_USER:-0}"
export ENTERPRISE_SSO_ENABLED="${ENTERPRISE_SSO_ENABLED:-0}"
export VIBECANVAS_DEFAULT_AGENT_API=${default_agent_api_q}
export DEFAULT_API=${default_agent_api_q}
export VIBECANVAS_SIGNING_SECRET=${signing_secret_q}
export CODEX_CLI_PATH=${codex_cli_path_q}
export CODEX_ACCESS_TOKEN=${codex_access_token_q}
export CODEX_ID_TOKEN=${codex_id_token_q}
export AGENT_RUNTIME_TYPES=${agent_runtime_types_q}
export CODEX_RUNTIME_AUTH_METHODS=${codex_auth_methods_q}
export CODEX_MANAGED_APIS_JSON=${codex_managed_apis_q}
export AGENT_RUNTIME_ROOT="${agent_runtime_root}"
export VFS_VOLUME_ROOT="${vfs_volume_root}"
export RUNSC_PATH="${RUNSC_PATH:-$HOME/.cache/vibecanvas/runsc/runsc}"
export SANDBOX_PYTHON_PATHS="${SANDBOX_PYTHON_PATHS}"
export VIBECANVAS_SANDBOX_USE_INSTALLED_APP="${VIBECANVAS_SANDBOX_USE_INSTALLED_APP:-0}"
EOF
  chmod 600 "$RUNDIR/.env.native"
  echo "$RUNDIR/.env.native"
}

start_pg() {
  [[ -n "$PGBIN" && -x "$PGBIN/pg_isready" ]] || {
    echo "ERROR: PostgreSQL server tools are missing; install postgresql and libpq-dev, or set PGBIN" >&2
    return 1
  }
  if "$PGBIN/pg_isready" -h localhost -p "$PGPORT" >/dev/null 2>&1; then
    echo "postgres already up on :$PGPORT"; return
  fi
  # RESET=1 forces a clean slate (wipe + re-init); otherwise an already-
  # initialised cluster (PG_VERSION present) is REUSED — registered accounts,
  # workflows, etc. persist across down/up. This is the data-persistence fix:
  # the old script wiped $PGDATA + DROP/CREATE'd the DB on every up.
  if [[ "${RESET:-0}" == "1" ]]; then
    echo "RESET=1 → wiping $PGDATA"; rm -rf "$PGDATA"
  fi
  local fresh=0
  if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
    fresh=1
    mkdir -p "$PGDATA"
    # Postgres refuses to run as root; this assumes an unprivileged user. trust
    # auth on localhost only — this is a dev/verify cluster, never production.
    "$PGBIN/initdb" -D "$PGDATA" -U "$(whoami)" --auth=trust --encoding=UTF8 >/dev/null
  fi
  "$PGBIN/pg_ctl" -D "$PGDATA" -o "-p $PGPORT -k /tmp" -l "$RUNDIR/pg.log" start
  for _ in $(seq 1 30); do
    "$PGBIN/pg_isready" -h localhost -p "$PGPORT" >/dev/null 2>&1 && break; sleep 1
  done
  if [[ "$fresh" == "1" ]]; then
    # FIRST-TIME provisioning only — never re-run against an existing cluster,
    # or it would DROP the user's database. Mirror api/tests/conftest.py:
    # Initial role creation is completed by the idempotent role provisioner in
    # migrate(); keep only the database bootstrap here.
    "$PGBIN/psql" -h localhost -p "$PGPORT" -U "$(whoami)" -d postgres -v ON_ERROR_STOP=1 <<'SQL'
DROP DATABASE IF EXISTS vibecanvas;
CREATE DATABASE vibecanvas;
SQL
  fi
  echo "postgres up on :$PGPORT (data: $PGDATA, fresh=$fresh)"
}

start_redis() {
  redis-cli -p "$REDISPORT" ping >/dev/null 2>&1 && { echo "redis already up on :$REDISPORT"; return; }
  command -v redis-server >/dev/null || { echo "installing redis-server"; sudo apt-get install -y redis-server; }
  redis-server --daemonize yes --port "$REDISPORT" --dir /tmp
  echo "redis up on :$REDISPORT"
}

start_openfga() {
  local openfga_bin="${OPENFGA_BIN:-$HOME/.cache/vibecanvas/openfga/openfga}"
  local token_file="$RUNDIR/openfga-token"
  local erasure_password_file="$RUNDIR/openfga-erasure-password"
  local datastore="vibecanvas_openfga"
  if [[ ! -x "$openfga_bin" ]]; then
    "$REPO_ROOT/scripts/security/install_openfga_server.sh" "$openfga_bin"
  fi
  if [[ ! -s "$token_file" ]]; then
    openssl rand -hex 32 > "$token_file"
    chmod 600 "$token_file"
  fi
  if [[ ! -s "$erasure_password_file" ]]; then
    openssl rand -hex 32 > "$erasure_password_file"
    chmod 600 "$erasure_password_file"
  fi
  export OPENFGA_API_TOKEN="${OPENFGA_API_TOKEN:-$(cat "$token_file")}"
  export OPENFGA_API_URL="http://127.0.0.1:${OPENFGA_HTTP_PORT}"
  export OPENFGA_BOOTSTRAP_CONFIG_FILE="$RUNDIR/openfga-bootstrap.json"
  export OPENFGA_AUTHN_METHOD=preshared
  export OPENFGA_AUTHN_PRESHARED_KEYS="$OPENFGA_API_TOKEN"

  if ! "$PGBIN/psql" -h localhost -p "$PGPORT" -U "$(whoami)" -d postgres \
      -Atqc "SELECT 1 FROM pg_database WHERE datname='$datastore'" | grep -q 1; then
    "$PGBIN/createdb" -h localhost -p "$PGPORT" -U "$(whoami)" "$datastore"
  fi
  local datastore_uri="postgres://$(whoami)@127.0.0.1:${PGPORT}/${datastore}?sslmode=disable"
  "$openfga_bin" migrate --datastore-engine postgres \
    --datastore-uri "$datastore_uri" --timeout 30s
  "$PGBIN/psql" -h localhost -p "$PGPORT" -U "$(whoami)" -d "$datastore" \
    --set=ON_ERROR_STOP=1 \
    --set=erasure_database="$datastore" \
    --set=erasure_password="$(cat "$erasure_password_file")" \
    --file="$REPO_ROOT/scripts/security/openfga_erasure.sql"
  export OPENFGA_ERASURE_DATABASE_URL="postgresql://flowork_openfga_erasure:$(cat "$erasure_password_file")@127.0.0.1:${PGPORT}/${datastore}?sslmode=disable"
  "$VIBECANVAS_PYTHON" "$DAEMONIZER" \
    --pid-file "$RUNDIR/openfga.pid" \
    --log-file "$RUNDIR/openfga.log" -- \
    "$openfga_bin" run --datastore-engine postgres \
    --datastore-uri "$datastore_uri" \
    --http-addr "127.0.0.1:${OPENFGA_HTTP_PORT}" \
    --grpc-addr "127.0.0.1:${OPENFGA_GRPC_PORT}" \
    --metrics-addr "127.0.0.1:${OPENFGA_METRICS_PORT}" \
    --playground-enabled=false --log-format json
  for _ in $(seq 1 40); do
    curl --noproxy '*' -fsS -H "Authorization: Bearer $OPENFGA_API_TOKEN" \
      "$OPENFGA_API_URL/healthz" >/dev/null 2>&1 && break
    sleep 0.25
  done
  if ! curl --noproxy '*' -fsS -H "Authorization: Bearer $OPENFGA_API_TOKEN" \
      "$OPENFGA_API_URL/healthz" >/dev/null 2>&1; then
    echo "ERROR: OpenFGA did not become healthy; see $RUNDIR/openfga.log"
    return 1
  fi
  PYTHONPATH="$API_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$VIBECANVAS_PYTHON" -m vibecanvas_api.authorization.bootstrap
  echo "openfga up on :$OPENFGA_HTTP_PORT (independent database: $datastore)"
}

migrate() {
  ( cd "$API_DIR"
    # shellcheck disable=SC1091
    source "$RUNDIR/.env.native"
    "$PGBIN/psql" -h localhost -p "$PGPORT" -U "$(whoami)" -d vibecanvas \
      -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  CREATE ROLE vibecanvas_app LOGIN PASSWORD 'vibecanvas_app';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE ROLE vibecanvas_migrator LOGIN PASSWORD 'vibecanvas_migrator';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE ROLE vibecanvas_maintenance LOGIN PASSWORD 'vibecanvas_maintenance';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
SQL
    "$PGBIN/psql" -h localhost -p "$PGPORT" -U "$(whoami)" -d vibecanvas \
      -f "$REPO_ROOT/scripts/security/provision_database_roles.sql"
    local migration_url="postgresql+asyncpg://vibecanvas_migrator:vibecanvas_migrator@localhost:${PGPORT}/vibecanvas"
    DATABASE_URL="$migration_url" MIGRATION_DATABASE_URL="$migration_url" \
      PYTHONPATH="$API_DIR/src:$ENGINE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$VIBECANVAS_PYTHON" \
      "$REPO_ROOT/scripts/security/migrate_strict_content_encryption.py"
    DATABASE_URL="$migration_url" MIGRATION_DATABASE_URL="$migration_url" \
      PYTHONPATH="$API_DIR/src:$ENGINE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$VIBECANVAS_PYTHON" \
      "$REPO_ROOT/scripts/security/migrate_filesystem_object_store.py"
    local dbos_admin_url="postgresql+psycopg://vibecanvas_migrator:vibecanvas_migrator@localhost:${PGPORT}/vibecanvas"
    "$VIBECANVAS_PY_PREFIX/bin/dbos" migrate -s "$dbos_admin_url" -r vibecanvas_app
    DBOS_ADMIN_DATABASE_URL="$dbos_admin_url" "$VIBECANVAS_PYTHON" \
      "$REPO_ROOT/scripts/security/provision_dbos_roles.py" )
  echo "database and filesystem Object Store at ciphertext-only head"
}

start_services() {
  cat > "$RUNDIR/run.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
source "$RUNDIR/.env.native"
cd "$API_DIR"
exec "\$@"
EOF
  chmod +x "$RUNDIR/run.sh"

  "$VIBECANVAS_PYTHON" "$DAEMONIZER" \
    --pid-file "$RUNDIR/api.pid" --log-file "$RUNDIR/api.log" -- \
    "$RUNDIR/run.sh" "$VIBECANVAS_PYTHON" -m uvicorn vibecanvas_api.app:build_app \
    --factory --host 127.0.0.1 --port 8000
  "$VIBECANVAS_PYTHON" "$DAEMONIZER" \
    --pid-file "$RUNDIR/worker.pid" --log-file "$RUNDIR/worker.log" -- \
    "$RUNDIR/run.sh" "$VIBECANVAS_PYTHON" -m vibecanvas_api.background_worker

  local api_start_timeout_seconds="${API_START_TIMEOUT_SECONDS:-120}"
  for _ in $(seq 1 "$api_start_timeout_seconds"); do
    curl --noproxy '*' -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1
  done
  if ! curl --noproxy '*' -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "ERROR: API did not become healthy; see $RUNDIR/api.log"
    return 1
  fi
  echo "api    pid=$(cat "$RUNDIR/api.pid")    http://127.0.0.1:8000  (log: $RUNDIR/api.log)"
  echo "worker pid=$(cat "$RUNDIR/worker.pid") (log: $RUNDIR/worker.log)"
}

start_sandbox_service() {
  cat > "$RUNDIR/run.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
source "$RUNDIR/.env.native"
cd "$API_DIR"
exec "\$@"
EOF
  chmod +x "$RUNDIR/run.sh"
  rm -f "${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock}"
  "$VIBECANVAS_PYTHON" "$DAEMONIZER" \
    --pid-file "$RUNDIR/sandboxd.pid" --log-file "$RUNDIR/sandboxd.log" -- \
    "$VIBECANVAS_PYTHON" "$SUPERVISOR" -- \
    "$RUNDIR/run.sh" "$VIBECANVAS_PYTHON" \
    -m vibecanvas_api.services.sandbox.service \
    --socket "${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock}"

  local sandbox_start_timeout_seconds="${SANDBOX_START_TIMEOUT_SECONDS:-60}"
  for _ in $(seq 1 "$sandbox_start_timeout_seconds"); do
    "$RUNDIR/run.sh" "$VIBECANVAS_PYTHON" \
      -m vibecanvas_api.services.sandbox.service \
      --socket "${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock}" --health \
      >/dev/null 2>&1 && break
    sleep 1
  done
  if ! "$RUNDIR/run.sh" "$VIBECANVAS_PYTHON" \
      -m vibecanvas_api.services.sandbox.service \
      --socket "${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock}" --health \
      >/dev/null 2>&1; then
    echo "ERROR: sandboxd did not become healthy; see $RUNDIR/sandboxd.log"
    return 1
  fi
  if [[ "${SANDBOX_PREWARM_ON_START:-1}" == "1" ]]; then
    echo "sandboxd: prewarming the base gVisor/Python file-operation runtime"
    if ! "$RUNDIR/run.sh" "$VIBECANVAS_PYTHON" \
        -m vibecanvas_api.services.sandbox.service \
        --socket "${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock}" --prewarm \
        >>"$RUNDIR/sandboxd.log" 2>&1; then
      echo "ERROR: sandboxd base prewarm failed; see $RUNDIR/sandboxd.log"
      stop_pidfile sandboxd
      return 1
    fi
  fi
  echo "sandboxd pid=$(cat "$RUNDIR/sandboxd.pid") unix://${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock} (log: $RUNDIR/sandboxd.log)"
}

# ─── web frontend (Vite) ────────────────────────────────────────────────────
# 'preview' builds once then serves static dist/ (no file watcher → robust in
# sandboxes where `vite dev` hits inotify ENOSPC). vite.config's `preview.proxy`
# forwards /api + /healthz to the api on :8000, same as the dev server.
start_web() {
  [[ "$WEB" == "1" ]] || { echo "web: skipped (WEB=0)"; return; }
  command -v pnpm >/dev/null || { echo "web: pnpm not found — skipping"; return; }
  ( cd "$WEB_DIR"
    # Fixed deployments may opt into explicit build-time coordinates. The
    # default remains empty so short-lived workspace proxy prefixes are
    # inferred in the browser from the actual loaded chunk URL.
    export VITE_APP_BASE_PATH="${WEB_BASE_PATH:-${VITE_APP_BASE_PATH:-}}"
    export VITE_API_BASE="${WEB_API_BASE:-${VITE_API_BASE:-}}"
    if web_deps_need_install; then
      echo "web: ensuring dependencies (log: $WEB_INSTALL_LOG)"
      CI=true pnpm install --frozen-lockfile > "$WEB_INSTALL_LOG" 2>&1 \
        || { echo "web: install FAILED (see $WEB_INSTALL_LOG)"; return; }
    else
      echo "web: dependencies unchanged; skipping install"
    fi
    if [[ "$WEB_MODE" == "dev" ]]; then
      "$VIBECANVAS_PYTHON" "$DAEMONIZER" \
        --pid-file "$RUNDIR/web.pid" --log-file "$RUNDIR/web.log" -- \
        pnpm exec vite --port "$WEB_PORT" --host "${WEB_HOST:-127.0.0.1}"
    else
      if web_build_needed; then
        echo "web: building (vite build, ~1-2 min)… log: $WEB_BUILD_LOG"
        ROLLDOWN_WORKER_THREADS="${ROLLDOWN_WORKER_THREADS:-4}" \
          ROLLDOWN_MAX_BLOCKING_THREADS="${ROLLDOWN_MAX_BLOCKING_THREADS:-16}" \
          RAYON_NUM_THREADS="${RAYON_NUM_THREADS:-4}" \
          pnpm exec vite build > "$WEB_BUILD_LOG" 2>&1 \
          && pnpm run lint:deployment-paths >> "$WEB_BUILD_LOG" 2>&1 \
          || { echo "web: build FAILED (see $WEB_BUILD_LOG)"; return; }
      else
        echo "web: frontend inputs unchanged; reusing dist"
      fi
      # Source trees are commonly mounted from a high-latency network volume.
      # Serving hundreds of lazy-loaded chunks directly from that mount made a
      # fresh page or first route open take tens of seconds even though the API
      # was healthy. Publish the immutable, reproducible build to the local
      # runtime disk before starting Vite Preview. No user data or secrets are
      # stored here; this directory is disposable and rebuilt on every start.
      [[ "$WEB_RUNTIME_DIST" == "$RUNDIR/"* ]] || {
        echo "web: unsafe runtime dist path: $WEB_RUNTIME_DIST"; return;
      }
      rm -rf -- "$WEB_RUNTIME_DIST"
      mkdir -p "$WEB_RUNTIME_DIST"
      cp -a "$WEB_DIR/dist/." "$WEB_RUNTIME_DIST/"
      echo "web: published static assets to local runtime cache"
      "$VIBECANVAS_PYTHON" "$DAEMONIZER" \
        --pid-file "$RUNDIR/web.pid" --log-file "$RUNDIR/web.log" -- \
        pnpm exec vite preview --outDir "$WEB_RUNTIME_DIST" \
          --port "$WEB_PORT" --host "${WEB_HOST:-127.0.0.1}"
    fi )
  # Loopback host for the health-check + printed URL. When WEB_HOST is an
  # IPv6/all-interfaces bind (:: / ::0 / 0.0.0.0), curl the IPv6 loopback.
  local _check="${WEB_HOST:-127.0.0.1}"
  case "$_check" in
    ::|::0|0.0.0.0|"") _check="[::1]" ;;
  esac
  for _ in $(seq 1 30); do
    curl --noproxy '*' -fsS "http://$_check:$WEB_PORT/" >/dev/null 2>&1 && break; sleep 1
  done
  echo "http://$_check:$WEB_PORT/" > "$WEB_URL_FILE"
  cat > "$WEB_RUNTIME_ENV" <<EOF
WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="$WEB_PORT"
WEB_MODE="$WEB_MODE"
WEB_URL="http://$_check:$WEB_PORT/"
EOF
  echo "web    pid=$(cat "$RUNDIR/web.pid" 2>/dev/null) http://$_check:$WEB_PORT  (host: ${WEB_HOST:-127.0.0.1}, mode: $WEB_MODE, log: $RUNDIR/web.log)"
}

cmd_up() {
  resolve_backend_python
  install_backend_packages
  resolve_sandbox_python_paths
  start_pg
  start_redis
  start_openfga
  write_env >/dev/null
  migrate
  # sandboxd owns every resident gVisor process and must be ready before any
  # API or background worker can accept work. Application startup fails closed if
  # this readiness gate is bypassed.
  start_sandbox_service
  start_services
  start_web
  echo "--- up. healthz:"; curl --noproxy '*' -s http://127.0.0.1:8000/healthz; echo
  if [[ "$WEB" == "1" ]]; then
    local web_url="http://127.0.0.1:$WEB_PORT/"
    [[ -f "$WEB_URL_FILE" ]] && web_url="$(cat "$WEB_URL_FILE")"
    echo "    open the app at  $web_url  (register or paste a token to log in)"
  fi
}

cmd_down() {
  # Stop request producers first, then let sandboxd drain/terminate its owned
  # sessions. This preserves the process ownership boundary during shutdown.
  for s in api worker web; do
    stop_pidfile "$s"
  done
  stop_pidfile sandboxd
  stop_pidfile openfga
  if [[ -f "$WEB_RUNTIME_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$WEB_RUNTIME_ENV"
  fi
  # Also clean up manually-started dev processes that are outside pidfile
  # tracking. Keep these patterns project-specific enough for local dev.
  kill_dev_processes_by_pattern "uvicorn vibecanvas_api.app:build_app"
  kill_dev_processes_by_pattern "vibecanvas_api.background_worker"
  kill_dev_processes_by_pattern "vibecanvas_api.services.sandbox.service"
  kill_dev_processes_by_pattern "pnpm --dir .*web exec vite (preview|--host|--port)"
  pkill -f "vite (preview|--port $WEB_PORT)" 2>/dev/null || true   # vite spawns children
  # One more pass after processes receive TERM in case a still-running API/worker
  # spawned or retained a sandbox while shutting down.
  cleanup_vibecanvas_sandboxes
  cleanup_dead_object_store_materializations
  "$PGBIN/pg_ctl" -D "$PGDATA" stop >/dev/null 2>&1 && echo "stopped postgres" || true
  redis-cli -p "$REDISPORT" shutdown nosave 2>/dev/null && echo "stopped redis" || true
}

cmd_status() {
  if [[ -f "$RUNDIR/.env.native" ]]; then
    # shellcheck disable=SC1090
    source "$RUNDIR/.env.native"
  fi
  "$PGBIN/pg_isready" -h localhost -p "$PGPORT" || true
  redis-cli -p "$REDISPORT" ping || true
  curl --noproxy '*' -s http://127.0.0.1:8000/healthz && echo " <- api" || echo "api down"
  if [[ -x "$RUNDIR/run.sh" ]]; then
    "$RUNDIR/run.sh" "${VIBECANVAS_PYTHON:-$REPO_ROOT/.venv/bin/python}" \
      -m vibecanvas_api.services.sandbox.service \
      --socket "${SANDBOX_SERVICE_SOCKET:-$RUNDIR/sandboxd.sock}" --health \
      2>/dev/null && echo " <- sandboxd" || echo "sandboxd down"
  else
    echo "sandboxd down"
  fi
  local web_url="http://127.0.0.1:$WEB_PORT/"
  if [[ -f "$WEB_RUNTIME_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$WEB_RUNTIME_ENV"
    web_url="$WEB_URL"
  elif [[ -f "$WEB_URL_FILE" ]]; then
    web_url="$(cat "$WEB_URL_FILE")"
  fi
  curl --noproxy '*' -fsS "$web_url" >/dev/null 2>&1 && echo "web alive $web_url" || echo "web down"
  for s in sandboxd api worker beat web openfga; do
    [[ -f "$RUNDIR/$s.pid" ]] && kill -0 "$(cat "$RUNDIR/$s.pid")" 2>/dev/null \
      && echo "$s alive (pid $(cat "$RUNDIR/$s.pid"))" || echo "$s down"
  done
}

case "${1:-up}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  status) cmd_status ;;
  *) echo "usage: $0 {up|down|status}"; exit 1 ;;
esac
