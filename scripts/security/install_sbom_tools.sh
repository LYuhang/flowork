#!/usr/bin/env bash
set -euo pipefail

# Pinned official releases and per-architecture SHA-256 digests. Updating any
# value is a reviewed supply-chain change; CI never executes a remote installer.
syft_version="1.44.0"
grype_version="0.112.0"
destination_dir="${1:-${TMPDIR:-/tmp}/flowork-tools}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    architecture="amd64"
    syft_digest="0e91737aee2b5baf1d255b959630194a302335d848ff97bb07921eb6205b5f5a"
    grype_digest="acb14a030010fe9bdb9594b4ae108d9d14ef2f926d936aa0916dc62c89c058ea"
    ;;
  Linux:aarch64|Linux:arm64)
    architecture="arm64"
    syft_digest="6f6cdcdc695721d91ce756e3b5bc3e3416599c464101f5e32e9c3f33054ee6d9"
    grype_digest="7fdeccf065965cc59386c656e5fcc1eb1bdf820e2433000bca7f010b8e6da155"
    ;;
  *)
    printf 'Unsupported platform for pinned SBOM tools: %s:%s\n' \
      "$(uname -s)" "$(uname -m)" >&2
    exit 2
    ;;
esac

work_dir="$(mktemp -d)"
trap 'rm -r "$work_dir"' EXIT
mkdir -p "$destination_dir"

install_tool() {
  local tool="$1"
  local version="$2"
  local digest="$3"
  local artifact="${tool}_${version}_linux_${architecture}.tar.gz"
  local archive="$work_dir/$artifact"

  curl -fsSL --retry 3 \
    "https://github.com/anchore/${tool}/releases/download/v${version}/${artifact}" \
    -o "$archive"
  printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
  tar -xzf "$archive" -C "$work_dir" "$tool"
  install -m 0755 "$work_dir/$tool" "$destination_dir/$tool"
  rm -f "$work_dir/$tool"
}

install_tool syft "$syft_version" "$syft_digest"
install_tool grype "$grype_version" "$grype_digest"
"$destination_dir/syft" version
"$destination_dir/grype" version
