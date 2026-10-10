# Rebuild the unchanged gosu release with the patched Go standard library.
FROM golang:1.26.9-alpine3.24@sha256:3082400e369fa24d5fc60bca20edab3f6d604e0c5a690ec66b295eff4dd87ade AS gosu-build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
ADD --checksum=sha256:cd9719b775dbfedae53923c9b0dc792b66d42c51e0b36652ed6f747fbadc0164 https://codeload.github.com/tianon/gosu/tar.gz/refs/tags/1.19 /tmp/gosu.tar.gz
RUN mkdir /src && tar -xzf /tmp/gosu.tar.gz -C /src --strip-components=1
WORKDIR /src
RUN go build -trimpath -ldflags="-s -w" -o /out/gosu .

# Preserve PostgreSQL 17 and the official entrypoint/data-volume contract.
# Apply distribution security updates independently of upstream image rebuilds.
FROM postgres:17.11-trixie@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*
COPY --from=gosu-build /out/gosu /usr/local/bin/gosu
RUN gosu nobody true
