#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
ENV_FILE="${VIBECANVAS_ENV_FILE:-$REPO_ROOT/.env}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

env_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE"
}

require_nonplaceholder() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  [[ -n "$value" ]] || fail "$key is missing from $ENV_FILE"
  case "$value" in
    dev|vc_app|vc_migrator|vc_maintenance|sk-...|*development-only*|*change-me*)
      fail "$key still uses a placeholder/default value in $ENV_FILE"
      ;;
  esac
}

require_command docker
require_command curl
require_command openssl
require_command awk
require_command df

docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 plugin is unavailable"
docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable to the current user"

# The complete stack includes an Agent sandbox, document conversion, diagram
# export, databases, and background workers. Check the resources assigned to
# the Docker engine rather than the host totals so Docker Desktop limits are
# accounted for. An 8 GiB allocation commonly reports about 7.5 GiB after VM
# reservation, which is the supported lower bound used here.
minimum_cpu_count=4
minimum_memory_bytes=8053063680
minimum_disk_kib=20971520
docker_cpu_count="$(docker info --format '{{.NCPU}}')"
docker_memory_bytes="$(docker info --format '{{.MemTotal}}')"
repository_free_kib="$(df -Pk "$REPO_ROOT" | awk 'NR == 2 { print $4 }')"

[[ "$docker_cpu_count" =~ ^[0-9]+$ ]] || fail "Docker returned an invalid CPU count"
[[ "$docker_memory_bytes" =~ ^[0-9]+$ ]] || fail "Docker returned an invalid memory limit"
[[ "$repository_free_kib" =~ ^[0-9]+$ ]] || fail "unable to determine free repository disk space"

resource_failures=()
if (( docker_cpu_count < minimum_cpu_count )); then
  resource_failures+=("Docker has ${docker_cpu_count} CPU(s); at least ${minimum_cpu_count} are required")
fi
if (( docker_memory_bytes < minimum_memory_bytes )); then
  resource_failures+=("Docker has $((docker_memory_bytes / 1024 / 1024)) MiB memory; an 8 GiB allocation is required")
fi
if (( repository_free_kib < minimum_disk_kib )); then
  resource_failures+=("the repository filesystem has $((repository_free_kib / 1024 / 1024)) GiB free; at least 20 GiB is required")
fi

allow_unsupported_resources="${FLOWORK_ALLOW_UNSUPPORTED_RESOURCES:-false}"
if (( ${#resource_failures[@]} > 0 )); then
  for resource_failure in "${resource_failures[@]}"; do
    echo "RESOURCE ERROR: $resource_failure" >&2
  done
  case "${allow_unsupported_resources,,}" in
    1|true|yes)
      echo "WARNING: continuing with unsupported resources because FLOWORK_ALLOW_UNSUPPORTED_RESOURCES is enabled" >&2
      ;;
    *)
      fail "resource preflight failed; see docs/installation.md or explicitly opt into an unsupported test run"
      ;;
  esac
fi

[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE does not exist; run scripts/deploy/local_server.sh init"
chmod 600 "$ENV_FILE"

require_nonplaceholder POSTGRES_PASSWORD
require_nonplaceholder VIBECANVAS_APP_PASSWORD
require_nonplaceholder VIBECANVAS_MIGRATOR_PASSWORD
require_nonplaceholder VIBECANVAS_MAINTENANCE_PASSWORD
require_nonplaceholder OPENFGA_POSTGRES_PASSWORD
require_nonplaceholder OPENFGA_ERASURE_PASSWORD
require_nonplaceholder OPENFGA_API_TOKEN
require_nonplaceholder KMS_LOCAL_MASTER_KEY
require_nonplaceholder CONTENT_LOOKUP_HMAC_KEY
require_nonplaceholder BROWSER_TOKEN_SECRET
require_nonplaceholder VIBECANVAS_SIGNING_SECRET

if [[ "$(env_value KMS_PROVIDER)" != "local" ]]; then
  fail "the local-server entrypoint expects KMS_PROVIDER=local"
fi

bind_address="$(env_value VIBECANVAS_BIND_ADDRESS)"
if [[ -z "$bind_address" ]]; then
  fail "VIBECANVAS_BIND_ADDRESS must be explicit"
fi
if [[ "$bind_address" == "0.0.0.0" || "$bind_address" == "::" ]]; then
  fail "wildcard network binding is not allowed by the local-server entrypoint"
fi

internal_bind_address="$(env_value VIBECANVAS_INTERNAL_BIND_ADDRESS)"
internal_bind_address="${internal_bind_address:-127.0.0.1}"
if [[ "$internal_bind_address" == "0.0.0.0" || "$internal_bind_address" == "::" ]]; then
  fail "wildcard internal service binding is not allowed by the local-server entrypoint"
fi

if [[ -r /proc/sys/kernel/unprivileged_userns_clone ]] &&
   [[ "$(< /proc/sys/kernel/unprivileged_userns_clone)" == "0" ]]; then
  fail "kernel.unprivileged_userns_clone=0; workflow gVisor sandboxes cannot start"
fi

VIBECANVAS_ENV_FILE="$ENV_FILE" \
  docker compose --env-file "$ENV_FILE" config --quiet

echo "preflight=pass"
echo "docker_cpu_count=$docker_cpu_count"
echo "docker_memory_mib=$((docker_memory_bytes / 1024 / 1024))"
echo "repository_free_gib=$((repository_free_kib / 1024 / 1024))"
echo "env_file=$ENV_FILE"
echo "bind_address=$bind_address"
echo "internal_bind_address=$internal_bind_address"
