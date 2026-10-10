# Keep the deployed service release; rebuild its binaries with security fixes.
FROM golang:1.26.9-alpine3.24@sha256:3082400e369fa24d5fc60bca20edab3f6d604e0c5a690ec66b295eff4dd87ade AS build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
ADD --checksum=sha256:be1c5d55f5e995a5a3e583b79e2a8a1520685880ecac7c5803e315f6fb8eaba0 https://codeload.github.com/openfga/openfga/tar.gz/refs/tags/v1.20.0 /tmp/openfga.tar.gz
ADD --checksum=sha256:d1b9f0f0a189a741b39a7e830de8a027dececf0a9e18548e01d4d796ea3e97e1 https://codeload.github.com/grpc-ecosystem/grpc-health-probe/tar.gz/refs/tags/v0.4.57 /tmp/probe.tar.gz
RUN mkdir /src /probe && tar -xzf /tmp/openfga.tar.gz -C /src --strip-components=1 && \
    tar -xzf /tmp/probe.tar.gz -C /probe --strip-components=1
WORKDIR /src
RUN go get golang.org/x/net@v0.60.0 google.golang.org/grpc@v1.83.2 && \
    go build -p 2 -trimpath -ldflags="-s -w" -o /out/openfga ./cmd/openfga
WORKDIR /probe
RUN go get golang.org/x/net@v0.60.0 google.golang.org/grpc@v1.83.2 && \
    go build -p 2 -trimpath -ldflags="-s -w" -o /out/grpc_health_probe .

# Retain upstream's user, CA certificates, entrypoint and healthcheck contract.
FROM openfga/openfga:v1.20.0@sha256:d53ce5c48413d01e75ecf375f3f74eb35c50f155fc028c41d03dbc7c9838fb38
COPY --from=build /out/openfga /openfga
COPY --from=build /out/grpc_health_probe /usr/local/bin/grpc_health_probe
