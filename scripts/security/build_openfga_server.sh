#!/bin/sh
set -eu

# Shared by Docker and native installation. Keep this backport until a pinned
# OpenFGA release includes https://github.com/jackc/pgx/pull/2675.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_dir=$1
output=$2
cd "$source_dir"

go get golang.org/x/net@v0.60.0 google.golang.org/grpc@v1.83.2
test "$(go list -m -f '{{.Version}}' github.com/jackc/pgx/v5)" = v5.10.0
go mod download github.com/jackc/pgx/v5
driver_source=$(go list -m -f '{{.Dir}}' github.com/jackc/pgx/v5)
driver_dir="$source_dir/flowork-pgx"
mkdir "$driver_dir"
cp -R "$driver_source/." "$driver_dir/"
chmod -R u+w "$driver_dir"
patch --batch --forward -p1 -d "$driver_dir" < "$script_dir/patches/pgx-background-timeout.patch"
(cd "$driver_dir" && go test -p 1 ./pgconn/internal/bgreader)
# Keep the upstream module version in build metadata/SBOMs rather than replacing
# it with an unversioned local module. Vendor only for this temporary build.
go mod vendor
cp "$driver_dir/pgconn/internal/bgreader/bgreader.go" \
  vendor/github.com/jackc/pgx/v5/pgconn/internal/bgreader/bgreader.go
mkdir -p "$(dirname "$output")"
CGO_ENABLED=0 go build -mod=vendor -buildvcs=false -p 1 -trimpath \
  -ldflags="-s -w -X github.com/openfga/openfga/internal/build.Version=v1.20.0-flowork-pgx2675" \
  -o "$output" ./cmd/openfga
