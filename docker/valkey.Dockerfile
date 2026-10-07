# Retain the upstream entrypoint while applying Alpine security fixes.
FROM valkey/valkey:9.1.2-alpine3.24@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11
RUN apk upgrade --no-cache && apk info -v zlib
