#!/usr/bin/env bash
set -euo pipefail

# Older distro builds can change credentials process-wide under -allow_other,
# causing concurrent non-root file operations to fail (upstream issue #892).
version=2.6.1
case "$(uname -m)" in
  x86_64) arch=amd64; checksum=49b8c0eb0f6373b6ac99c394a52909d8478e74c08d0961527c1162967cc28c44 ;;
  aarch64) arch=arm64; checksum=64576d550ab8af3f1dc729e93779540c5ecc00967d0185aae51a29a3755d86d0 ;;
  *) echo "Unsupported gocryptfs architecture: $(uname -m)" >&2; exit 1 ;;
esac
[[ "$EUID" == 0 ]] || { echo "Run this installer as root" >&2; exit 1; }
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
archive="$temporary/gocryptfs.tar.gz"
curl -fsSL -o "$archive" \
  "https://github.com/rfjakob/gocryptfs/releases/download/v${version}/gocryptfs_v${version}_linux-static_${arch}.tar.gz"
echo "$checksum  $archive" | sha256sum -c -
tar -xzf "$archive" -C "$temporary" gocryptfs
install -m 0755 "$temporary/gocryptfs" /usr/local/bin/gocryptfs
/usr/local/bin/gocryptfs -version
