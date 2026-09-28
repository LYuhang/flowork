#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
ENV_FILE="${VIBECANVAS_ENV_FILE:-$REPO_ROOT/.env}"

env_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE"
}

[[ -f "$ENV_FILE" ]] || {
  echo "ERROR: $ENV_FILE does not exist" >&2
  exit 1
}

bind_address="$(env_value VIBECANVAS_BIND_ADDRESS)"
internal_bind_address="$(env_value VIBECANVAS_INTERNAL_BIND_ADDRESS)"
http_port="$(env_value VIBECANVAS_HTTP_PORT)"
api_port="$(env_value VIBECANVAS_API_PORT)"
bind_address="${bind_address:-127.0.0.1}"
internal_bind_address="${internal_bind_address:-127.0.0.1}"
http_port="${http_port:-9001}"
api_port="${api_port:-8000}"

VIBECANVAS_ENV_FILE="$ENV_FILE" \
  docker compose --env-file "$ENV_FILE" ps

web_health="http://$bind_address:$http_port/healthz"
api_health="http://$internal_bind_address:$api_port/healthz"

# Deployment verification must reach the loopback-published containers even on
# developer machines with HTTP(S)_PROXY/ALL_PROXY configured globally.
curl --noproxy '*' --fail --silent --show-error --max-time 10 "$web_health" >/dev/null
curl --noproxy '*' --fail --silent --show-error --max-time 10 "$api_health" >/dev/null

if ! VIBECANVAS_ENV_FILE="$ENV_FILE" \
  docker compose --env-file "$ENV_FILE" exec -T sandboxd \
  sh -eu -c 'case "${SANDBOX_RUNTIME:-bubblewrap}" in
    bubblewrap) bwrap --version ;;
    gvisor) test -x /usr/local/bin/runsc; runsc --version ;;
    *) exit 1 ;;
  esac'; then
  echo "ERROR: sandboxd is missing its selected runtime binary." >&2
  exit 1
fi

if ! VIBECANVAS_ENV_FILE="$ENV_FILE" \
  docker compose --env-file "$ENV_FILE" exec -T sandboxd \
  python -m vibecanvas_api.services.sandbox.service \
  --socket /run/vibecanvas-sandbox/sandboxd.sock --health; then
  echo "ERROR: sandboxd's private control socket is unhealthy." >&2
  exit 1
fi

# Run the same credential-free readiness gate used during startup. In
# rootful-snapshot mode this cold boots gVisor, checkpoints it, restores it,
# executes a command after restore, and validates the one-shot Workflow base.
if ! VIBECANVAS_ENV_FILE="$ENV_FILE" \
  docker compose --env-file "$ENV_FILE" run --rm --no-deps sandbox_prewarm; then
  echo "ERROR: selected sandbox runtime/lifecycle verification failed." >&2
  echo "Inspect: ./scripts/deploy/local_server.sh logs sandboxd" >&2
  exit 1
fi

echo "verify=pass"
echo "web_health=$web_health"
echo "api_health=$api_health"
if [[ "$bind_address" == "127.0.0.1" || "$bind_address" == "::1" ]]; then
  echo "access=ssh -N -L ${http_port}:127.0.0.1:${http_port} <user>@<server-ip>"
  echo "browser=http://localhost:${http_port}"
else
  echo "browser=http://${bind_address}:${http_port}"
fi
