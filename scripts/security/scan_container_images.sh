#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
output_dir="${1:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/flowork-container-security}"
syft_bin="${SYFT_BIN:-$(command -v syft || true)}"
grype_bin="${GRYPE_BIN:-$(command -v grype || true)}"
docker_bin="${DOCKER_BIN:-$(command -v docker || true)}"
python_bin="${PYTHON_BIN:-$(command -v python3 || true)}"
policy_evaluator="$repo_root/scripts/security/evaluate_container_vulnerabilities.py"

if [[ -z "$syft_bin" || ! -x "$syft_bin" ]]; then
  printf 'Syft is required; run scripts/security/install_sbom_tools.sh first.\n' >&2
  exit 2
fi
if [[ -z "$grype_bin" || ! -x "$grype_bin" ]]; then
  printf 'Grype is required; run scripts/security/install_sbom_tools.sh first.\n' >&2
  exit 2
fi
if [[ -z "$docker_bin" || ! -x "$docker_bin" ]]; then
  printf 'Docker is required to build and scan the actual production images.\n' >&2
  exit 2
fi
if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
  printf 'Python 3 is required to evaluate the container vulnerability policy.\n' >&2
  exit 2
fi

mkdir -p "$output_dir/sbom" "$output_dir/vulnerabilities" "$output_dir/build"
manifest="$output_dir/image-manifest.tsv"
printf 'label\tsource\timage_id\trepo_digests\n' > "$manifest"

build_image() {
  local label="$1"
  local dockerfile="$2"
  local context="$3"
  shift 3
  local tag="flowork-${label}:security-scan"
  "$docker_bin" build --pull --file "$dockerfile" --tag "$tag" "$@" "$context" \
    2>&1 | tee "$output_dir/build/$label.log"
}

scan_image() {
  local label="$1"
  local source="$2"
  local image_id
  local repo_digests
  local scan_status

  image_id="$($docker_bin image inspect --format '{{.Id}}' "$source")"
  repo_digests="$($docker_bin image inspect --format '{{join .RepoDigests ","}}' "$source")"
  printf '%s\t%s\t%s\t%s\n' "$label" "$source" "$image_id" "$repo_digests" \
    >> "$manifest"

  "$syft_bin" "docker:$source" \
    --output "syft-json=$output_dir/sbom/$label.syft.json" \
    --output "spdx-json=$output_dir/sbom/$label.spdx.json"

  set +e
  "$grype_bin" "sbom:$output_dir/sbom/$label.syft.json" \
    --fail-on high \
    --output "table=$output_dir/vulnerabilities/$label.txt" \
    --output "json=$output_dir/vulnerabilities/$label.json"
  scan_status=$?
  set -e
  if [[ "$scan_status" -ne 0 && "$scan_status" -ne 2 ]]; then
    printf 'Container vulnerability scan errored for %s (status %s).\n' \
      "$label" "$scan_status" >&2
    cat "$output_dir/vulnerabilities/$label.txt" >&2
    return "$scan_status"
  fi
  if ! "$python_bin" "$policy_evaluator" \
      --label "$label" \
      --report "$output_dir/vulnerabilities/$label.json"; then
    printf 'Container vulnerability gate failed for %s.\n' "$label" >&2
    cat "$output_dir/vulnerabilities/$label.txt" >&2
    return 2
  fi
}

cd "$repo_root"
gate_failed=0

build_image api api/Dockerfile .
build_image sandboxd api/Dockerfile . --build-arg VIBECANVAS_RUNTIME_ENV_BUILDER=1
"$docker_bin" run --rm --entrypoint python flowork-sandboxd:security-scan -m pip --version
build_image web web/Dockerfile .
build_image engine engine/Dockerfile engine

# These are the exact tag+digest references used by Dockerfiles/Compose. Keep
# this list synchronized through test_supply_chain_scripts.py so a newly added
# deployment image cannot silently bypass SBOM/vulnerability scanning.
readonly pinned_images=(
  'rust-build|debian:bookworm-20260918-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251'
  'python-base|python:3.11.16-slim-trixie@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e'
  'node-runtime|node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'
  'node-build|node:24.21.0-alpine3.23@sha256:9ec4a2e289874ed0d722e1772ec2de45d2801541db8612f3638b26f128c69ac2'
  'nginx-runtime|nginx:1.30.4-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
  'pgvector|pgvector/pgvector:0.8.6-pg15-bookworm@sha256:a947c45cdc5906a1bc951f20a8709e321256343ee0f251e4ae00b5e7def4e6da'
  'valkey|valkey/valkey:9.1.1-alpine3.24@sha256:ee91f7a174ac4d6a6b0685b3a60e321f0a9dbbb691f9b0e285be2ba1d1be8328'
  'openfga-postgres|postgres:17.11-trixie@sha256:e38411452a464af89e5adadb8d223bf53b898d47d6ef918b2d58c08707350449'
  'openfga|openfga/openfga:v1.18.3@sha256:01a6000aa6040a4d0bde6ea1d3359ac3b9f21dc972ac6103a87b38659a836776'
  'clamav|clamav/clamav:1.5.3-debian13-slim@sha256:741e6c447241220e0792a901befcaec1d55a755c5097fc9cd88d7fd8be251a5c'
)

for entry in "${pinned_images[@]}"; do
  label="${entry%%|*}"
  source="${entry#*|}"
  "$docker_bin" pull "$source"
  if ! scan_image "$label" "$source"; then
    gate_failed=1
  fi
done

for entry in \
  'api|flowork-api:security-scan' \
  'sandboxd|flowork-sandboxd:security-scan' \
  'web|flowork-web:security-scan' \
  'engine|flowork-engine:security-scan'; do
  label="${entry%%|*}"
  source="${entry#*|}"
  if ! scan_image "$label" "$source"; then
    gate_failed=1
  fi
done

sha256sum "$output_dir"/sbom/*.json "$output_dir"/vulnerabilities/*.json \
  > "$output_dir/report-checksums.sha256"
if [[ "$gate_failed" -ne 0 ]]; then
  printf 'container_supply_chain_gate=fail images=%s output=%s\n' "$((${#pinned_images[@]} + 4))" "$output_dir" >&2
  exit 2
fi
printf 'container_supply_chain_gate=pass images=%s output=%s\n' "$((${#pinned_images[@]} + 4))" "$output_dir"
