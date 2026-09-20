#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
LAUNCHER="$REPO_ROOT/scripts/deploy/local_server.sh"
TEMPORARY_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/flowork-local-config-test.XXXXXX")"

cleanup() {
  rm -rf -- "$TEMPORARY_DIRECTORY"
}
trap cleanup EXIT

env_value() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); value = $0 } END { print value }' "$file"
}

assert_value() {
  local file="$1"
  local key="$2"
  local expected="$3"
  local actual
  actual="$(env_value "$file" "$key")"
  if [[ "$actual" != "$expected" ]]; then
    printf 'expected %s=%s, got %s\n' "$key" "$expected" "$actual" >&2
    exit 1
  fi
}

https_env="$TEMPORARY_DIRECTORY/https.env"
VIBECANVAS_ENV_FILE="$https_env" "$LAUNCHER" init \
  --public-url https://flowork.example.com/workspace/ \
  --bind-address 10.0.0.4 >/dev/null
assert_value "$https_env" VIBECANVAS_BIND_ADDRESS 10.0.0.4
assert_value "$https_env" VIBECANVAS_INTERNAL_BIND_ADDRESS 127.0.0.1
assert_value "$https_env" VIBECANVAS_PUBLIC_URL https://flowork.example.com/workspace
assert_value "$https_env" WEB_ALLOWED_HOSTS flowork.example.com
assert_value "$https_env" VIBECANVAS_API_CORS_ORIGINS https://flowork.example.com
assert_value "$https_env" VIBECANVAS_EXTENSION_WEB_BASE https://flowork.example.com/workspace
assert_value "$https_env" VIBECANVAS_EXTENSION_ALLOWED_ORIGINS https://flowork.example.com/workspace
assert_value "$https_env" WEB_SESSION_COOKIE_SECURE true
[[ "$(stat -c '%a' "$https_env")" == "600" ]]

original_postgres_password="$(env_value "$https_env" POSTGRES_PASSWORD)"
sed -i '/^VIBECANVAS_INTERNAL_BIND_ADDRESS=/d' "$https_env"
VIBECANVAS_ENV_FILE="$https_env" "$LAUNCHER" init \
  --public-url http://203.0.113.20:9001 >/dev/null
assert_value "$https_env" VIBECANVAS_INTERNAL_BIND_ADDRESS 127.0.0.1
assert_value "$https_env" VIBECANVAS_PUBLIC_URL http://203.0.113.20:9001
assert_value "$https_env" WEB_ALLOWED_HOSTS 203.0.113.20
assert_value "$https_env" VIBECANVAS_API_CORS_ORIGINS http://203.0.113.20:9001
assert_value "$https_env" VIBECANVAS_EXTENSION_WEB_BASE http://203.0.113.20:9001
assert_value "$https_env" VIBECANVAS_EXTENSION_ALLOWED_ORIGINS http://203.0.113.20:9001
assert_value "$https_env" WEB_SESSION_COOKIE_SECURE false
assert_value "$https_env" POSTGRES_PASSWORD "$original_postgres_password"

legacy_env="$TEMPORARY_DIRECTORY/legacy.env"
printf '%s\n' \
  'POSTGRES_PASSWORD=keep-existing-secret' \
  'VIBECANVAS_BIND_ADDRESS=127.0.0.1' >"$legacy_env"
VIBECANVAS_ENV_FILE="$legacy_env" "$LAUNCHER" init >/dev/null
assert_value "$legacy_env" POSTGRES_PASSWORD keep-existing-secret
assert_value "$legacy_env" VIBECANVAS_INTERNAL_BIND_ADDRESS 127.0.0.1

if VIBECANVAS_ENV_FILE="$TEMPORARY_DIRECTORY/invalid.env" \
  "$LAUNCHER" init --public-url 'https://user@example.com' \
  >/dev/null 2>&1; then
  echo "invalid public URL was accepted" >&2
  exit 1
fi
[[ ! -e "$TEMPORARY_DIRECTORY/invalid.env" ]]

echo "local_server_config_test=pass"
