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

mkdir -p "$output_dir/sbom" "$output_dir/vulnerabilities" "$output_dir/policy" "$output_dir/build"
manifest="$output_dir/image-manifest.tsv"
printf 'label\tsource\timage_id\trepo_digests\trole\n' > "$manifest"

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
  local role="${3:-runtime}"
  local image_id
  local repo_digests
  local scan_status

  image_id="$($docker_bin image inspect --format '{{.Id}}' "$source")"
  repo_digests="$($docker_bin image inspect --format '{{join .RepoDigests ","}}' "$source")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$label" "$source" "$image_id" "$repo_digests" "$role" \
    >> "$manifest"

  if ! "$syft_bin" "docker:$source" \
      --output "syft-json=$output_dir/sbom/$label.syft.json" \
      --output "spdx-json=$output_dir/sbom/$label.spdx.json"; then
    return 2
  fi

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
  if [[ "$label" == api || "$label" == sandboxd || "$label" == engine ]]; then
    if ! "$docker_bin" run --rm --entrypoint python "$source" \
        /usr/local/lib/flowork/verify_python_tarfile_fix.py; then
      printf 'Python tarfile regression probe failed for %s.\n' "$label" >&2
      return 2
    fi
  fi
  # Upstream layers are provenance, not independently deployed artifacts.
  # Retain their complete findings. Admission below scans the patched build
  # stages and every final/directly-deployed image with the blocking policy.
  if [[ "$role" == provenance ]]; then
    printf 'upstream_provenance_recorded label=%s grype_status=%s\n' "$label" "$scan_status"
    return 0
  fi
  if ! "$python_bin" "$policy_evaluator" \
      --label "$label" \
      --report "$output_dir/vulnerabilities/$label.json" 2>&1 | tee "$output_dir/policy/$label.txt"; then
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      {
        printf '### Container vulnerability gate: %s\n\n```text\n' "$label"
        cat "$output_dir/policy/$label.txt"
        printf '\n```\n'
      } >> "$GITHUB_STEP_SUMMARY"
    fi
    printf 'Container vulnerability gate failed for %s.\n' "$label" >&2
    cat "$output_dir/vulnerabilities/$label.txt" >&2
    return 2
  fi
}

cd "$repo_root"
gate_failed=0

build_image node-runtime-patched api/Dockerfile . --target node-runtime-base
build_image node-build-patched web/Dockerfile . --target node-build-base
build_image clamav docker/clamav.Dockerfile .
build_image valkey docker/valkey.Dockerfile .
build_image openfga-postgres postgres/openfga.Dockerfile .
build_image postgres postgres/Dockerfile .
bash "$repo_root/scripts/security/verify_postgres_image.sh" flowork-postgres:security-scan
build_image api api/Dockerfile .
build_image sandboxd api/Dockerfile . --build-arg VIBECANVAS_RUNTIME_ENV_BUILDER=1
"$docker_bin" run --rm --entrypoint python flowork-sandboxd:security-scan -m pip --version
build_image web web/Dockerfile .
build_image engine engine/Dockerfile .

# These are the exact tag+digest references used by Dockerfiles/Compose. Keep
# this list synchronized through test_supply_chain_scripts.py so a newly added
# deployment image cannot silently bypass SBOM/vulnerability scanning.
readonly pinned_images=(
  'golang-build|golang:1.26.9-alpine3.24@sha256:3082400e369fa24d5fc60bca20edab3f6d604e0c5a690ec66b295eff4dd87ade'
  'rust-build|debian:bookworm-20260918-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251'
  'python-base|python:3.11.17-slim-trixie@sha256:6f31d6e9ba2b0a787a3f81c37b004155b87b9efa1b771182bd550c1615745be5'
  'node-runtime|node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'
  'node-build|node:24.21.0-alpine3.23@sha256:9ec4a2e289874ed0d722e1772ec2de45d2801541db8612f3638b26f128c69ac2'
  'nginx-runtime|nginx:1.30.5-trixie@sha256:b972f831f200b19ef0767938224f9711e74cd783718738cd7405d5cabf75c442'
  'valkey-base|valkey/valkey:9.1.2-alpine3.24@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11'
  'openfga-postgres-base|postgres:17.11-trixie@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f'
  'openfga|openfga/openfga:v1.20.0@sha256:d53ce5c48413d01e75ecf375f3f74eb35c50f155fc028c41d03dbc7c9838fb38'
  'clamav-base|clamav/clamav:1.5.4-debian13-slim@sha256:9bb8712a50f0e75166e936c452cd82dd5e5be0b85586598930b5bbb84a99a578'
)

for entry in "${pinned_images[@]}"; do
  label="${entry%%|*}"
  source="${entry#*|}"
  "$docker_bin" pull "$source"
  case "$label" in
    openfga) role=runtime ;;
    golang-build|rust-build|python-base|node-runtime|node-build|nginx-runtime|openfga-postgres-base|clamav-base|valkey-base)
      role=provenance ;;
    *) printf 'Unclassified image: %s\n' "$label" >&2; exit 2 ;;
  esac
  if ! scan_image "$label" "$source" "$role"; then
    gate_failed=1
  fi
done

for entry in \
  'node-runtime-patched|flowork-node-runtime-patched:security-scan' \
  'node-build-patched|flowork-node-build-patched:security-scan' \
  'valkey|flowork-valkey:security-scan' \
  'clamav|flowork-clamav:security-scan' \
  'postgres|flowork-postgres:security-scan' \
  'openfga-postgres|flowork-openfga-postgres:security-scan' \
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

sha256sum "$output_dir"/sbom/*.json "$output_dir"/vulnerabilities/*.json "$output_dir"/policy/*.txt \
  > "$output_dir/report-checksums.sha256"
if [[ "$gate_failed" -ne 0 ]]; then
  printf 'container_supply_chain_gate=fail images=%s output=%s\n' "$((${#pinned_images[@]} + 10))" "$output_dir" >&2
  exit 2
fi
printf 'container_supply_chain_gate=pass images=%s output=%s\n' "$((${#pinned_images[@]} + 10))" "$output_dir"
