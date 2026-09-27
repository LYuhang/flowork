#!/usr/bin/env bash
set -euo pipefail

# Native-development/test fallback for hosts without a container runtime.
# Production uses the independently pinned container image in docker-compose.
version="1.20.0"
destination="${1:-${TMPDIR:-/tmp}/flowork-tools/openfga}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    artifact="openfga_${version}_linux_amd64.tar.gz"
    digest="1974ea836894e4638be6f302f5dd80a87b24a387f3b8d0068b1fb0c62e7033d5"
    ;;
  Linux:aarch64|Linux:arm64)
    artifact="openfga_${version}_linux_arm64.tar.gz"
    digest="20ef76aff5629e87a69003b11fb6cfd3de3f7472d250f603d113bcb9eec2c90b"
    ;;
  *)
    printf 'Unsupported platform for pinned OpenFGA server: %s:%s\n' \
      "$(uname -s)" "$(uname -m)" >&2
    exit 2
    ;;
esac

work_dir="$(mktemp -d)"
trap 'rm -r "$work_dir"' EXIT
archive="$work_dir/$artifact"
curl -fsSL --retry 3 \
  "https://github.com/openfga/openfga/releases/download/v${version}/${artifact}" \
  -o "$archive"
printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$work_dir" openfga
mkdir -p "$(dirname "$destination")"
install -m 0755 "$work_dir/openfga" "$destination"
"$destination" version
