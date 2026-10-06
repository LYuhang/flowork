#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
python_bin="${VIBECANVAS_PYTHON:-$repo_root/.venv/bin/python}"
docker_bin="${DOCKER_BIN:-$(command -v docker || true)}"
clamav_image="flowork-clamav:security-live"

if [[ -z "$docker_bin" || ! -x "$docker_bin" ]]; then
  printf 'Docker is required for the live ClamAV gate.\n' >&2
  exit 2
fi
if ! command -v "$python_bin" >/dev/null 2>&1 && [[ ! -x "$python_bin" ]]; then
  printf 'Configured Python is not executable: %s\n' "$python_bin" >&2
  exit 2
fi

"$docker_bin" build --pull --file "$repo_root/docker/clamav.Dockerfile" \
  --tag "$clamav_image" "$repo_root"

socket_dir="$(mktemp -d)"
container_name="vibecanvas-clamav-${RANDOM}-$$"
chmod 0777 "$socket_dir"

cleanup() {
  "$docker_bin" rm --force "$container_name" >/dev/null 2>&1 || true
  rm -r "$socket_dir"
}
trap cleanup EXIT

"$docker_bin" run --detach --name "$container_name" \
  --mount "type=bind,source=$socket_dir,target=/tmp" \
  "$clamav_image" >/dev/null

socket_path="$socket_dir/clamd.sock"
ready=0
for _attempt in $(seq 1 120); do
  if [[ -S "$socket_path" ]]; then
    if UPLOAD_SCANNER_PROVIDER=clamd \
      UPLOAD_SCANNER_CLAMD_UNIX_SOCKET="$socket_path" \
      UPLOAD_SCANNER_TIMEOUT_SECONDS=2 \
      PYTHONPATH="$repo_root/api/src:$repo_root/engine/src" \
      "$python_bin" "$script_dir/verify_clamav_live.py" ready \
      >/dev/null 2>&1; then
      ready=1
      break
    fi
  fi
  sleep 2
done

if [[ "$ready" -ne 1 ]]; then
  "$docker_bin" logs "$container_name" >&2 || true
  printf 'Real clamd did not become ready or pass the live scanner gate.\n' >&2
  exit 1
fi

UPLOAD_SCANNER_PROVIDER=clamd \
UPLOAD_SCANNER_CLAMD_UNIX_SOCKET="$socket_path" \
UPLOAD_SCANNER_TIMEOUT_SECONDS=5 \
PYTHONPATH="$repo_root/api/src:$repo_root/engine/src" \
"$python_bin" "$script_dir/verify_clamav_live.py" live

"$docker_bin" stop --time 10 "$container_name" >/dev/null
UPLOAD_SCANNER_PROVIDER=clamd \
UPLOAD_SCANNER_CLAMD_UNIX_SOCKET="$socket_path" \
UPLOAD_SCANNER_TIMEOUT_SECONDS=0.2 \
PYTHONPATH="$repo_root/api/src:$repo_root/engine/src" \
"$python_bin" "$script_dir/verify_clamav_live.py" unavailable
