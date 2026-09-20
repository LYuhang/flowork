#!/usr/bin/env bash
set -euo pipefail

# Pinned release + digest: updating either value is a reviewed supply-chain
# change. The destination is explicit so CI never needs root.
version="8.30.1"
destination="${1:-${TMPDIR:-/tmp}/flowork-tools/gitleaks}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    artifact="gitleaks_${version}_linux_x64.tar.gz"
    digest="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    ;;
  Linux:aarch64|Linux:arm64)
    artifact="gitleaks_${version}_linux_arm64.tar.gz"
    digest="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
    ;;
  *)
    printf 'Unsupported platform for pinned Gitleaks installer: %s:%s\n' \
      "$(uname -s)" "$(uname -m)" >&2
    exit 2
    ;;
esac

work_dir="$(mktemp -d)"
trap 'rm -r "$work_dir"' EXIT
archive="$work_dir/$artifact"
curl -fsSL --retry 3 --retry-all-errors --retry-delay 1 \
  "https://github.com/gitleaks/gitleaks/releases/download/v${version}/${artifact}" \
  -o "$archive"
printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$work_dir" gitleaks
mkdir -p "$(dirname "$destination")"
install -m 0755 "$work_dir/gitleaks" "$destination"
"$destination" version
