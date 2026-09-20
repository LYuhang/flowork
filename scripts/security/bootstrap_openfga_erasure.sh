#!/bin/sh
set -eu

: "${OPENFGA_ADMIN_DATABASE_URL:?OPENFGA_ADMIN_DATABASE_URL is required}"
: "${OPENFGA_ERASURE_DATABASE_NAME:?OPENFGA_ERASURE_DATABASE_NAME is required}"
: "${OPENFGA_ERASURE_PASSWORD:?OPENFGA_ERASURE_PASSWORD is required}"

psql "$OPENFGA_ADMIN_DATABASE_URL" \
  --set=ON_ERROR_STOP=1 \
  --set=erasure_database="$OPENFGA_ERASURE_DATABASE_NAME" \
  --set=erasure_password="$OPENFGA_ERASURE_PASSWORD" \
  --file=/opt/flowork/openfga_erasure.sql
