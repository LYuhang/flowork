#!/usr/bin/env bash
set -euo pipefail

# Build the same narrow driver backport as Docker. Official v1.20.0 binaries
# contain pgx #2673; downloading them would reintroduce intermittent auth 503s.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
destination="${1:-${TMPDIR:-/tmp}/flowork-tools/openfga}"

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    artifact="go1.26.9.linux-amd64.tar.gz"
    digest="42d158b4d8f7b61ac0a830567c940a86098fb7aac52e467a5ebec03ef5cc2f8d"
    ;;
  Linux:aarch64|Linux:arm64)
    artifact="go1.26.9.linux-arm64.tar.gz"
    digest="4a97373d49fcacdcf3694fea368a500b00ee3e963974f3e7514132717632f052"
    ;;
  *)
    printf 'Unsupported platform for pinned OpenFGA server: %s:%s\n' \
      "$(uname -s)" "$(uname -m)" >&2
    exit 2
    ;;
esac

work_dir="$(mktemp -d)"
# Go's module cache contains read-only directories. Only make this installer’s
# private temporary tree writable; caller-provided caches are left untouched.
trap 'chmod -R u+w "$work_dir"; rm -r "$work_dir"' EXIT
for tool in curl sha256sum tar patch; do
  command -v "$tool" >/dev/null || { printf 'Required build tool missing: %s\n' "$tool" >&2; exit 2; }
done
archive="$work_dir/$artifact"
curl -fsSL "https://go.dev/dl/$artifact" -o "$archive"
printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$work_dir"
rm "$archive"
curl -fsSL https://codeload.github.com/openfga/openfga/tar.gz/refs/tags/v1.20.0 \
  -o "$work_dir/openfga.tar.gz"
printf '%s  %s\n' be1c5d55f5e995a5a3e583b79e2a8a1520685880ecac7c5803e315f6fb8eaba0 \
  "$work_dir/openfga.tar.gz" | sha256sum -c -
tar -xzf "$work_dir/openfga.tar.gz" -C "$work_dir"
export PATH="$work_dir/go/bin:$PATH" GOTOOLCHAIN=local
export GOMAXPROCS="${GOMAXPROCS:-2}"
export GOCACHE="${GOCACHE:-$work_dir/build-cache}" GOMODCACHE="${GOMODCACHE:-$work_dir/module-cache}"
sh "$script_dir/build_openfga_server.sh" "$work_dir/openfga-1.20.0" "$work_dir/openfga"
mkdir -p "$(dirname "$destination")"
install -m 0755 "$work_dir/openfga" "$destination"
install -m 0644 "$script_dir/patches/pgx.LICENSE" "$destination.pgx.LICENSE"
"$destination" version
