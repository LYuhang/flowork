#!/usr/bin/env bash
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
ENV_FILE="${VIBECANVAS_ENV_FILE:-$REPO_ROOT/.env}"
PREFLIGHT="$REPO_ROOT/scripts/deploy/preflight.sh"
VERIFY="$REPO_ROOT/scripts/deploy/verify_local.sh"
ACTION="up"
REQUESTED_PUBLIC_URL=""
REQUESTED_BIND_ADDRESS=""
POSITIONAL_ARGS=()

usage() {
  cat >&2 <<EOF
usage: $0 [command] [options]

commands:
  init | up | start | restart | preflight | verify | status | logs [service]
  stop | down | config

deployment options for init/up/start/restart:
  --public-url URL       Browser-visible HTTP(S) URL. Host, CORS, cookie, and
                         extension build settings are derived automatically.
  --bind-address ADDRESS Exact host interface used for the Web entry point.
                         Use the VM private IP behind cloud NAT; never 0.0.0.0.
EOF
}

parse_arguments() {
  if [[ $# -gt 0 && "$1" != --* ]]; then
    ACTION="$1"
    shift
  fi
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --public-url)
        [[ $# -ge 2 ]] || { echo "ERROR: --public-url requires a value" >&2; exit 2; }
        REQUESTED_PUBLIC_URL="$2"
        shift 2
        ;;
      --bind-address)
        [[ $# -ge 2 ]] || { echo "ERROR: --bind-address requires a value" >&2; exit 2; }
        REQUESTED_BIND_ADDRESS="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        POSITIONAL_ARGS+=("$@")
        break
        ;;
      --*)
        echo "ERROR: unknown option: $1" >&2
        usage
        exit 2
        ;;
      *)
        POSITIONAL_ARGS+=("$1")
        shift
        ;;
    esac
  done
  if [[ -n "$REQUESTED_PUBLIC_URL$REQUESTED_BIND_ADDRESS" ]]; then
    case "$ACTION" in
      init|up|start|restart) ;;
      *)
        echo "ERROR: deployment options are supported only by init, up, start, and restart" >&2
        exit 2
        ;;
    esac
  fi
}

random_hex() {
  openssl rand -hex "${1:-32}"
}

random_urlsafe_key() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'
}

set_env_value() {
  local key="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { replaced = 0 }
    index($0, key "=") == 1 {
      if (!replaced) print key "=" value
      replaced = 1
      next
    }
    { print }
    END { if (!replaced) print key "=" value }
  ' "$ENV_FILE" >"$temporary"
  chmod 600 "$temporary"
  mv "$temporary" "$ENV_FILE"
}

env_has_nonempty_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key && length(substr($0, index($0, "=") + 1)) > 0 { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$ENV_FILE"
}

env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key { sub(/^[^=]*=/, ""); value = $0 }
    END { print value }
  ' "$ENV_FILE"
}

parse_public_url() {
  local value="${1%/}"
  if [[ ! "$value" =~ ^(https?)://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(:([0-9]{1,5}))?(/[^?#[:space:]]*)?$ ]]; then
    echo "ERROR: public URL must be an absolute HTTP(S) URL without credentials, query, or fragment: $1" >&2
    exit 2
  fi
  PUBLIC_SCHEME="${BASH_REMATCH[1]}"
  PUBLIC_HOST="${BASH_REMATCH[2]}"
  PUBLIC_PORT="${BASH_REMATCH[4]}"
  PUBLIC_PATH="${BASH_REMATCH[5]}"
  if [[ -n "$PUBLIC_PORT" ]] && (( PUBLIC_PORT < 1 || PUBLIC_PORT > 65535 )); then
    echo "ERROR: public URL port must be between 1 and 65535" >&2
    exit 2
  fi
  PUBLIC_HOST="${PUBLIC_HOST#[}"
  PUBLIC_HOST="${PUBLIC_HOST%]}"
  PUBLIC_ORIGIN="${PUBLIC_SCHEME}://${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
  PUBLIC_URL="${PUBLIC_ORIGIN}${PUBLIC_PATH%/}"
}

validate_requested_config() {
  if [[ -n "$REQUESTED_PUBLIC_URL" ]]; then
    parse_public_url "$REQUESTED_PUBLIC_URL"
  fi
  if [[ -n "$REQUESTED_BIND_ADDRESS" ]]; then
    if [[ "$REQUESTED_BIND_ADDRESS" == "0.0.0.0" || "$REQUESTED_BIND_ADDRESS" == "::" ]]; then
      echo "ERROR: --bind-address must be one exact host interface, not a wildcard" >&2
      exit 2
    fi
    if [[ "$REQUESTED_BIND_ADDRESS" == *[[:space:]/]* ]]; then
      echo "ERROR: --bind-address must be an address, not a CIDR or list" >&2
      exit 2
    fi
  fi
}

apply_public_url_config() {
  parse_public_url "$1"
  set_env_value VIBECANVAS_PUBLIC_URL "$PUBLIC_URL"
  set_env_value WEB_ALLOWED_HOSTS "$PUBLIC_HOST"
  set_env_value VIBECANVAS_API_CORS_ORIGINS "$PUBLIC_ORIGIN"
  set_env_value VIBECANVAS_EXTENSION_WEB_BASE "$PUBLIC_URL"
  set_env_value VIBECANVAS_EXTENSION_ALLOWED_ORIGINS "$PUBLIC_URL"
  if [[ "$PUBLIC_SCHEME" == "https" ]]; then
    set_env_value WEB_SESSION_COOKIE_SECURE true
  else
    set_env_value WEB_SESSION_COOKIE_SECURE false
  fi
}

backfill_public_url_config() {
  local value
  value="$(env_value VIBECANVAS_PUBLIC_URL)"
  [[ -n "$value" ]] || return 0
  parse_public_url "$value"
  ensure_env_value WEB_ALLOWED_HOSTS "$PUBLIC_HOST"
  ensure_env_value VIBECANVAS_API_CORS_ORIGINS "$PUBLIC_ORIGIN"
  ensure_env_value VIBECANVAS_EXTENSION_WEB_BASE "$PUBLIC_URL"
  ensure_env_value VIBECANVAS_EXTENSION_ALLOWED_ORIGINS "$PUBLIC_URL"
  if [[ "$PUBLIC_SCHEME" == "https" ]]; then
    ensure_env_value WEB_SESSION_COOKIE_SECURE true
  else
    ensure_env_value WEB_SESSION_COOKIE_SECURE false
  fi
}

ensure_env_value() {
  local key="$1"
  local value="$2"
  if ! env_has_nonempty_value "$key"; then
    set_env_value "$key" "$value"
  fi
}

backfill_env() {
  ensure_env_value VIBECANVAS_INTERNAL_BIND_ADDRESS "${VIBECANVAS_INTERNAL_BIND_ADDRESS:-127.0.0.1}"
  ensure_env_value POSTGRES_PASSWORD "$(random_hex 24)"
  ensure_env_value VIBECANVAS_APP_PASSWORD "$(random_hex 24)"
  ensure_env_value VIBECANVAS_MIGRATOR_PASSWORD "$(random_hex 24)"
  ensure_env_value VIBECANVAS_MAINTENANCE_PASSWORD "$(random_hex 24)"
  ensure_env_value OPENFGA_POSTGRES_PASSWORD "$(random_hex 24)"
  ensure_env_value OPENFGA_ERASURE_PASSWORD "$(random_hex 24)"
  ensure_env_value OPENFGA_API_TOKEN "$(random_hex 32)"
  ensure_env_value KMS_PROVIDER local
  ensure_env_value KMS_KEY_ID vibecanvas-local-development
  ensure_env_value KMS_LOCAL_MASTER_KEY "$(random_urlsafe_key)"
  ensure_env_value CONTENT_LOOKUP_HMAC_KEY "$(random_urlsafe_key)"
  ensure_env_value BROWSER_TOKEN_SECRET "$(random_urlsafe_key)"
  ensure_env_value VIBECANVAS_SIGNING_SECRET "$(random_urlsafe_key)"
  ensure_env_value OAUTH_ENCRYPTION_KEY "$(random_urlsafe_key)"
  ensure_env_value ENABLE_TEST_USER false
  ensure_env_value ENTERPRISE_SSO_ENABLED false
  ensure_env_value AGENT_RUNTIME_TYPES codex
  ensure_env_value CODEX_RUNTIME_AUTH_METHODS chatgpt,managed_api,personal_api
  ensure_env_value CODEX_MANAGED_APIS_JSON '[]'
  ensure_env_value AGENT_DEBUG_VIEW_ENABLED false
  ensure_env_value SANDBOX_TYPE rootful-snapshot
  ensure_env_value SANDBOX_EGRESS_MODE "${SANDBOX_EGRESS_MODE:-proxy}"
  ensure_env_value SANDBOX_EGRESS_POLICY "${SANDBOX_EGRESS_POLICY:-public}"
  backfill_public_url_config
}

initialize_env() {
  command -v openssl >/dev/null 2>&1 || {
    echo "ERROR: openssl is required to generate local secrets" >&2
    exit 1
  }
  if [[ -e "$ENV_FILE" ]]; then
    chmod 600 "$ENV_FILE"
    backfill_env
    if [[ -n "$REQUESTED_BIND_ADDRESS" ]]; then
      set_env_value VIBECANVAS_BIND_ADDRESS "$REQUESTED_BIND_ADDRESS"
    fi
    if [[ -n "$REQUESTED_PUBLIC_URL" ]]; then
      apply_public_url_config "$REQUESTED_PUBLIC_URL"
    fi
    echo "existing env preserved; missing settings initialized: $ENV_FILE"
    if [[ -n "$REQUESTED_PUBLIC_URL" ]]; then
      echo "public deployment settings derived from: $PUBLIC_URL"
    fi
    return
  fi

  install -m 600 "$REPO_ROOT/.env.example" "$ENV_FILE"
  set_env_value VIBECANVAS_BIND_ADDRESS "${REQUESTED_BIND_ADDRESS:-${VIBECANVAS_BIND_ADDRESS:-127.0.0.1}}"
  set_env_value VIBECANVAS_INTERNAL_BIND_ADDRESS "${VIBECANVAS_INTERNAL_BIND_ADDRESS:-127.0.0.1}"
  set_env_value VIBECANVAS_HTTP_PORT "${VIBECANVAS_HTTP_PORT:-9001}"
  apply_public_url_config "${REQUESTED_PUBLIC_URL:-${VIBECANVAS_PUBLIC_URL:-http://localhost:${VIBECANVAS_HTTP_PORT:-9001}}}"
  set_env_value POSTGRES_PASSWORD "$(random_hex 24)"
  set_env_value VIBECANVAS_APP_PASSWORD "$(random_hex 24)"
  set_env_value VIBECANVAS_MIGRATOR_PASSWORD "$(random_hex 24)"
  set_env_value VIBECANVAS_MAINTENANCE_PASSWORD "$(random_hex 24)"
  set_env_value OPENFGA_POSTGRES_PASSWORD "$(random_hex 24)"
  set_env_value OPENFGA_ERASURE_PASSWORD "$(random_hex 24)"
  set_env_value OPENFGA_API_TOKEN "$(random_hex 32)"
  set_env_value KMS_PROVIDER local
  set_env_value KMS_KEY_ID vibecanvas-local-development
  set_env_value KMS_LOCAL_MASTER_KEY "$(random_urlsafe_key)"
  set_env_value CONTENT_LOOKUP_HMAC_KEY "$(random_urlsafe_key)"
  set_env_value BROWSER_TOKEN_SECRET "$(random_urlsafe_key)"
  set_env_value VIBECANVAS_SIGNING_SECRET "$(random_urlsafe_key)"
  set_env_value OAUTH_ENCRYPTION_KEY "$(random_urlsafe_key)"
  set_env_value ENABLE_TEST_USER false
  set_env_value ENTERPRISE_SSO_ENABLED false
  set_env_value AGENT_RUNTIME_TYPES codex
  set_env_value CODEX_RUNTIME_AUTH_METHODS chatgpt,managed_api,personal_api
  set_env_value CODEX_MANAGED_APIS_JSON '[]'
  set_env_value AGENT_DEBUG_VIEW_ENABLED false
  set_env_value SANDBOX_TYPE rootful-snapshot
  set_env_value SANDBOX_EGRESS_MODE "${SANDBOX_EGRESS_MODE:-proxy}"
  set_env_value SANDBOX_EGRESS_POLICY "${SANDBOX_EGRESS_POLICY:-public}"

  echo "created $ENV_FILE with mode 0600"
  echo "public deployment settings derived from: $PUBLIC_URL"
  echo "optional: edit model credentials in $ENV_FILE before first AI request"
}

compose() {
  VIBECANVAS_ENV_FILE="$ENV_FILE" docker compose --env-file "$ENV_FILE" "$@"
}

configure_synthetic_dns() {
  local egress_mode trusted_cidrs suggestion
  egress_mode="$(sed -n 's/^SANDBOX_EGRESS_MODE=//p' "$ENV_FILE" | tail -n 1)"
  trusted_cidrs="$(sed -n 's/^SANDBOX_EGRESS_TRUSTED_PROXY_CIDRS=//p' "$ENV_FILE" | tail -n 1)"
  if [[ "$egress_mode" != "proxy" || -n "$trusted_cidrs" ]]; then
    return
  fi
  suggestion="$(compose exec -T sandboxd \
    python -m vibecanvas_api.services.sandbox.network_diagnostics \
    --suggest-cidr 2>/dev/null || true)"
  if [[ -z "$suggestion" ]]; then
    return
  fi
  echo "detected reachable VPN/transparent-proxy fake-IP range: $suggestion"
  set_env_value SANDBOX_EGRESS_TRUSTED_PROXY_CIDRS "$suggestion"
  echo "recreating sandboxd with the explicit trusted synthetic DNS range"
  compose up -d --force-recreate --wait sandboxd
}

start_stack() {
  initialize_env
  VIBECANVAS_ENV_FILE="$ENV_FILE" bash "$PREFLIGHT"
  compose up -d --build --wait
  configure_synthetic_dns
  VIBECANVAS_ENV_FILE="$ENV_FILE" bash "$VERIFY"
}

parse_arguments "$@"
validate_requested_config

case "$ACTION" in
  init)
    initialize_env
    ;;
  preflight)
    VIBECANVAS_ENV_FILE="$ENV_FILE" bash "$PREFLIGHT"
    ;;
  up|start)
    start_stack
    ;;
  restart)
    initialize_env
    compose down
    start_stack
    ;;
  verify)
    VIBECANVAS_ENV_FILE="$ENV_FILE" bash "$VERIFY"
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f "${POSITIONAL_ARGS[@]}"
    ;;
  stop|down)
    compose down
    ;;
  config)
    compose config
    ;;
  *)
    usage
    exit 2
    ;;
esac
