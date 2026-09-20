#!/usr/bin/env bash
set -euo pipefail

# Native-development/test fallback for hosts without a container runtime.
# Production uses the independently pinned container image in docker-compose.
version="1.18.1"
destination="${1:-${TMPDIR:-/tmp}/flowork-tools/openfga}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    artifact="openfga_${version}_linux_amd64.tar.gz"
    digest="a5b53556d47b80226190aa0087561ea114a2487f68cc2210dbfc1c11d21bcbe4"
    ;;
  Linux:aarch64|Linux:arm64)
    artifact="openfga_${version}_linux_arm64.tar.gz"
    digest="864325fc98aaa10c006d4841c0911e901cd8c5eb564af3e242075f19da331d92"
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
