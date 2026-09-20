#!/usr/bin/env bash
set -euo pipefail

# Pinned official release and per-architecture SHA-256. Updating these values is
# a reviewed authorization supply-chain change.
version="0.7.19"
destination="${1:-${TMPDIR:-/tmp}/flowork-tools/fga}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    artifact="fga_${version}_linux_amd64.tar.gz"
    digest="21da629e0f9d29e97d60a11c860e763915c57c354beda25b6e350168c86f67be"
    ;;
  Linux:aarch64|Linux:arm64)
    artifact="fga_${version}_linux_arm64.tar.gz"
    digest="32196f0f45c046057caab854778c84f05cdef87bfa8c3df1cadee56e31fed85c"
    ;;
  *)
    printf 'Unsupported platform for pinned OpenFGA CLI installer: %s:%s\n' \
      "$(uname -s)" "$(uname -m)" >&2
    exit 2
    ;;
esac

work_dir="$(mktemp -d)"
trap 'rm -r "$work_dir"' EXIT
archive="$work_dir/$artifact"
curl -fsSL --retry 3 \
  "https://github.com/openfga/cli/releases/download/v${version}/${artifact}" \
  -o "$archive"
printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$work_dir" fga
mkdir -p "$(dirname "$destination")"
install -m 0755 "$work_dir/fga" "$destination"
"$destination" version
