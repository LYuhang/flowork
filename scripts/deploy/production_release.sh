#!/usr/bin/env bash
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
ENV_FILE="${VIBECANVAS_ENV_FILE:-$REPO_ROOT/.env}"
EVIDENCE_VERIFIER="$REPO_ROOT/scripts/security/verify_production_evidence.py"
ATTESTATION_VERIFIER="$REPO_ROOT/scripts/security/verify_release_attestations.sh"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

require_value() {
  local name="$1"
  [[ -n "${!name:-}" ]] || fail "$name is required"
}

validate_release_inputs() {
  require_command docker
  require_command python3
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
  docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable"

  for name in \
    VIBECANVAS_API_IMAGE \
    VIBECANVAS_SANDBOX_IMAGE \
    VIBECANVAS_WEB_IMAGE \
    VIBECANVAS_VALKEY_IMAGE \
    VIBECANVAS_POSTGRES_IMAGE \
    VIBECANVAS_OPENFGA_POSTGRES_IMAGE \
    VIBECANVAS_OPENFGA_IMAGE \
    RELEASE_REPOSITORY \
    RELEASE_SHA \
    RELEASE_REF \
    PRODUCTION_EVIDENCE_MANIFEST; do
    require_value "$name"
  done

  [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || \
    fail "RELEASE_SHA must be an exact lowercase 40-character Git SHA"
  [[ "$RELEASE_REF" =~ ^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || \
    fail "RELEASE_REF must be an exact semantic refs/tags/v* ref"
  local prefix="ghcr.io/${RELEASE_REPOSITORY,,}"
  [[ "${VIBECANVAS_API_IMAGE%@*}" == "$prefix-api" ]] || fail "API image must use the api release repository"
  [[ "${VIBECANVAS_SANDBOX_IMAGE%@*}" == "$prefix-sandboxd" ]] || fail "sandboxd image must use the sandboxd release repository"
  [[ "${VIBECANVAS_WEB_IMAGE%@*}" == "$prefix-web" ]] || fail "Web image must use the web release repository"
  [[ "${VIBECANVAS_VALKEY_IMAGE%@*}" == "$prefix-valkey" ]] || fail "Valkey image must use the valkey release repository"
  [[ "${VIBECANVAS_POSTGRES_IMAGE%@*}" == "$prefix-postgres" ]] || fail "Postgres image must use the postgres release repository"
  [[ "${VIBECANVAS_OPENFGA_POSTGRES_IMAGE%@*}" == "$prefix-openfga-postgres" ]] || fail "OpenFGA Postgres image must use the openfga-postgres release repository"
  [[ "${VIBECANVAS_OPENFGA_IMAGE%@*}" == "$prefix-openfga" ]] || fail "OpenFGA image must use the openfga release repository"
  [[ -f "$PRODUCTION_EVIDENCE_MANIFEST" ]] || \
    fail "PRODUCTION_EVIDENCE_MANIFEST is not a regular file"
  [[ -f "$ENV_FILE" ]] || fail "production env file does not exist: $ENV_FILE"
  chmod 600 "$ENV_FILE"
}

verify_release() {
  validate_release_inputs
  local release_image
  for release_image in "$VIBECANVAS_API_IMAGE" "$VIBECANVAS_SANDBOX_IMAGE" "$VIBECANVAS_WEB_IMAGE" "$VIBECANVAS_VALKEY_IMAGE" "$VIBECANVAS_POSTGRES_IMAGE" "$VIBECANVAS_OPENFGA_POSTGRES_IMAGE" "$VIBECANVAS_OPENFGA_IMAGE"; do
    "$ATTESTATION_VERIFIER" \
      "$release_image" \
      "$RELEASE_REPOSITORY" \
      "$RELEASE_REPOSITORY/.github/workflows/release-images.yml" \
      "$RELEASE_SHA" \
      "$RELEASE_REF"
  done

  "$EVIDENCE_VERIFIER" \
    "$PRODUCTION_EVIDENCE_MANIFEST" \
    --repository "$RELEASE_REPOSITORY" \
    --commit-sha "$RELEASE_SHA" \
    --tag "${RELEASE_REF#refs/tags/}"

  VIBECANVAS_ENV=production \
    VIBECANVAS_ENV_FILE="$ENV_FILE" \
    docker compose \
      --env-file "$ENV_FILE" \
      -f "$REPO_ROOT/docker-compose.yml" \
      -f "$REPO_ROOT/docker-compose.release.yml" \
      config --quiet
  echo "production_release_gate=pass release_sha=$RELEASE_SHA"
}

start_release() {
  verify_release
  VIBECANVAS_ENV=production \
    VIBECANVAS_ENV_FILE="$ENV_FILE" \
    docker compose \
      --env-file "$ENV_FILE" \
      -f "$REPO_ROOT/docker-compose.yml" \
      -f "$REPO_ROOT/docker-compose.release.yml" \
      up -d --no-build --pull always --wait
  echo "production_release=running release_sha=$RELEASE_SHA"
}

case "${1:-verify}" in
  verify)
    verify_release
    ;;
  up|start)
    start_release
    ;;
  *)
    echo "usage: $0 {verify|up}" >&2
    exit 2
    ;;
esac
