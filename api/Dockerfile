# Flowork API data plane and FastAPI service container.
#
# Build:
#   cd /path/to/flowork
#   docker build -f api/Dockerfile -t vibecanvas-api:dev .
#
# Run:
#   docker run --rm -p 8000:8000 \
#     -e VIBECANVAS_API_DEV_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))') \
#     -e VIBECANVAS_API_CORS_ORIGINS=http://localhost:3000 \
#     vibecanvas-api:dev
#
# Build context is the repository root (NOT api/) so the Dockerfile can COPY
# the engine package from ../engine.

ARG CODEX_CLI_VERSION=0.147.0
ARG NPM_VERSION=11.19.0
ARG PLAYWRIGHT_MCP_VERSION=0.0.79
ARG DRAWIO_DESKTOP_VERSION=31.1.8
ARG DRAWIO_DESKTOP_AMD64_SHA256=f4c49ed84422ea4afd95818f53c54bc666e57b33bd036d468c7096619b47ffd9
ARG DRAWIO_DESKTOP_ARM64_SHA256=62a9ea636accada76076bd5a20f61b707e0e8093d3fd28c6583518663ca795d6

# Node-based runtime assets share a digest-pinned Node/npm base, but are built
# in independent stages. Updating Playwright MCP therefore cannot invalidate
# the Codex or draw.io dependency caches.
FROM node:22.23.2-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS node-runtime-base
ARG NPM_VERSION
RUN set -eux; \
    npm install --global --ignore-scripts --no-audit --no-fund "npm@${NPM_VERSION}"; \
    test "$(npm --version)" = "${NPM_VERSION}"

FROM node-runtime-base AS codex-assets
ARG CODEX_CLI_VERSION
ARG TARGETARCH
RUN set -eux; \
    npm install --global --ignore-scripts --no-audit --no-fund \
        "@openai/codex@${CODEX_CLI_VERSION}"; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64|x86_64) package_arch="x64"; target="x86_64-unknown-linux-musl" ;; \
        arm64|aarch64) package_arch="arm64"; target="aarch64-unknown-linux-musl" ;; \
        *) echo "Unsupported Codex CLI architecture: ${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    vendor="/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-${package_arch}/vendor/${target}"; \
    test -x "${vendor}/bin/codex"; \
    mkdir -p /opt/codex; \
    cp -a "${vendor}/." /opt/codex/; \
    test "$(/opt/codex/bin/codex --version)" = "codex-cli ${CODEX_CLI_VERSION}"; \
    mkdir -p /tmp/codex-catalog-home; \
    CODEX_HOME=/tmp/codex-catalog-home \
        /opt/codex/bin/codex debug models > /opt/codex/model-catalog.json; \
    node -e 'const c=require("/opt/codex/model-catalog.json"); if (!Array.isArray(c.models) || !c.models.length) process.exit(1)'

FROM node-runtime-base AS playwright-assets
ARG PLAYWRIGHT_MCP_VERSION
COPY api/playwright-runtime/package.json api/playwright-runtime/package-lock.json /opt/playwright-mcp/
RUN set -eux; \
    npm ci --prefix /opt/playwright-mcp --ignore-scripts --no-audit --no-fund; \
    test "$(node -p 'require("/opt/playwright-mcp/node_modules/@playwright/mcp/package.json").version')" = "${PLAYWRIGHT_MCP_VERSION}"
COPY api/playwright-runtime/launch.cjs /opt/playwright-mcp/launch.cjs
RUN chmod 0755 /opt/playwright-mcp/launch.cjs

FROM node-runtime-base AS drawio-assets
COPY api/drawio-runtime/package.json api/drawio-runtime/package-lock.json /opt/drawio-mcp/
RUN npm ci --prefix /opt/drawio-mcp --ignore-scripts --no-audit --no-fund \
        --omit=dev && \
    test "$(node -p 'require("/opt/drawio-mcp/node_modules/@drawio/mcp/package.json").version')" = "1.5.0"
COPY api/drawio-runtime/launch.mjs /opt/drawio-mcp/launch.mjs
RUN chmod 0755 /opt/drawio-mcp/launch.mjs && \
    test "$(node /opt/drawio-mcp/launch.mjs --version)" = "1.5.0"

FROM python:3.11.16-slim-trixie@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7
ARG CODEX_CLI_VERSION
ARG PLAYWRIGHT_MCP_VERSION
ARG DRAWIO_DESKTOP_VERSION
ARG DRAWIO_DESKTOP_AMD64_SHA256
ARG DRAWIO_DESKTOP_ARM64_SHA256
ARG TARGETARCH
ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security

WORKDIR /app

COPY --from=codex-assets /opt/codex /opt/codex
COPY --from=node-runtime-base /usr/local/bin/node /usr/local/bin/node
RUN ln -s /opt/codex/bin/codex /usr/local/bin/codex && \
    test "$(codex --version)" = "codex-cli ${CODEX_CLI_VERSION}" && \
    test "$(node --version)" = "v22.23.2"

# gVisor (runsc) — workflow execution is sandbox-ONLY since P2 (no in-process
# fallback), so the runtime image MUST carry the runsc binary. Fetch the SAME
# pinned, sha512-verified release as scripts/get_runsc.sh (keep the pin in sync)
# to /usr/local/bin/runsc (on PATH → resolved by which(runsc); RUNSC_PATH in
# compose makes it explicit). The image is shared by the unprivileged API/worker
# services and the dedicated rootful sandboxd service. Only sandboxd launches
# runsc and receives the required host capabilities; its startup probe validates
# the selected platform plus checkpoint/restore support on the actual host.
ARG RUNSC_RELEASE=20260601.0
# Small, version-pinned command-line baseline exposed inside every gVisor
# sandbox. These are runtime inspection/editing tools; compilers and language
# package managers remain opt-in environment dependencies to keep the trusted
# base image lean and its attack surface bounded.
RUN set -eux; \
    case "${DEBIAN_MIRROR}" in http://*|https://*) ;; *) exit 2 ;; esac; \
    case "${DEBIAN_SECURITY_MIRROR}" in http://*|https://*) ;; *) exit 2 ;; esac; \
    sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates=20250419 \
        curl=8.14.1-2+deb13u4 \
        file=1:5.46-5 \
        git=1:2.47.3-0+deb13u1 \
        jq=1.7.1-6+deb13u3 \
        openssh-client=1:10.0p1-7+deb13u4 \
        patch=2.8-2 \
        procps=2:4.0.4-9 \
        ripgrep=14.1.1-1+b4 \
        unzip=6.0-29 \
        zip=3.0-15 \
        fonts-dejavu-core=2.37-8 \
        fonts-wqy-zenhei=0.9.45-8 \
        xvfb=2:21.1.16-1.3+deb13u3 \
        xauth=1:1.1.2-1.1 \
        libgtk-3-0t64=3.24.49-3 \
        libnotify4=0.8.6-1 \
        libnss3=2:3.110-1+deb13u4 \
        libxss1=1:1.2.3-1+b3 \
        libxtst6=2:1.2.5-1 \
        xdg-utils=1.2.1-2 \
        libatspi2.0-0t64=2.56.2-1+deb13u1 \
        libuuid1=2.41.5-0+deb13u1 \
        libsecret-1-0=0.21.7-1 \
        libgbm1=25.0.7-2+deb13u1 \
        libasound2t64=1.2.14-1 \
        libreoffice-writer-nogui=4:25.2.3-2+deb13u6 \
        libreoffice-impress-nogui=4:25.2.3-2+deb13u6 \
        libreoffice-calc-nogui=4:25.2.3-2+deb13u6 \
        poppler-utils=25.03.0-5+deb13u4; \
    arch="$(uname -m)"; \
    base="https://storage.googleapis.com/gvisor/releases/release/${RUNSC_RELEASE}/${arch}"; \
    curl -fsSL --retry 3 -o /tmp/runsc "${base}/runsc"; \
    curl -fsSL --retry 3 -o /tmp/runsc.sha512 "${base}/runsc.sha512"; \
    echo "$(awk '{print $1}' /tmp/runsc.sha512 | head -n1)  /tmp/runsc" | sha512sum -c -; \
    chmod 0755 /tmp/runsc && mv /tmp/runsc /usr/local/bin/runsc; \
    rm -f /tmp/runsc.sha512; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64|x86_64) drawio_arch="amd64"; drawio_sha="${DRAWIO_DESKTOP_AMD64_SHA256}" ;; \
        arm64|aarch64) drawio_arch="arm64"; drawio_sha="${DRAWIO_DESKTOP_ARM64_SHA256}" ;; \
        *) echo "Unsupported draw.io Desktop architecture: ${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    drawio_deb="drawio-${drawio_arch}-${DRAWIO_DESKTOP_VERSION}.deb"; \
    curl -fsSL --retry 3 -o "/tmp/${drawio_deb}" \
        "https://github.com/jgraph/drawio-desktop/releases/download/v${DRAWIO_DESKTOP_VERSION}/${drawio_deb}"; \
    echo "${drawio_sha}  /tmp/${drawio_deb}" | sha256sum -c -; \
    test "$(dpkg-deb -f "/tmp/${drawio_deb}" Package)" = "draw.io"; \
    test "$(dpkg-deb -f "/tmp/${drawio_deb}" Version)" = "${DRAWIO_DESKTOP_VERSION}"; \
    test "$(dpkg-deb -f "/tmp/${drawio_deb}" Architecture)" = "${drawio_arch}"; \
    dpkg-deb -x "/tmp/${drawio_deb}" /tmp/drawio-desktop-root; \
    mv /tmp/drawio-desktop-root/opt/drawio /opt/drawio; \
    ln -s /opt/drawio/drawio /usr/local/bin/drawio; \
    test -x /opt/drawio/drawio; \
    ! ldd /opt/drawio/drawio | grep -q "not found"; \
    rm -f "/tmp/${drawio_deb}"; \
    rm -rf /var/lib/apt/lists/*

# Install reviewed, hash-locked build tooling and runtime dependencies before
# local source. Local packages disable build isolation so no undeclared package
# downloads can occur during their wheel builds.
COPY requirements-build.txt /tmp/requirements-build.txt
COPY requirements-runtime.txt /tmp/requirements-runtime.txt
COPY requirements-sandbox.txt /tmp/requirements-sandbox.txt
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --require-hashes -r /tmp/requirements-build.txt && \
    pip install --require-hashes -r /tmp/requirements-runtime.txt && \
    pip install --require-hashes -r /tmp/requirements-sandbox.txt

# Engine is installed from this repository without resolving dependencies again.
COPY engine /tmp/engine
RUN pip install --no-cache-dir --no-deps --no-build-isolation /tmp/engine

# api package
COPY api/pyproject.toml api/README.md /app/
COPY api/src/ /app/src/

# Alembic migrations (required for entrypoint `alembic upgrade head`)
COPY api/alembic.ini /app/alembic.ini
COPY api/alembic/ /app/alembic/
# Strict cutover commands are deployment assets, not Runtime compatibility
# readers. They run before the application image is admitted.
COPY scripts/security/ /app/scripts/security/

RUN pip install --no-cache-dir --no-deps --no-build-isolation .

# Keep the independently versioned browser MCP after the large OS/Python
# dependency layers. Updating browser automation must not invalidate apt or pip
# caches and force those dependencies to download again.
COPY --from=playwright-assets /opt/playwright-mcp /opt/playwright-mcp
RUN ln -s /opt/playwright-mcp/launch.cjs /usr/local/bin/flowork-playwright-mcp && \
    flowork-playwright-mcp --version | grep -q "${PLAYWRIGHT_MCP_VERSION}"

# Diagram authoring is the pinned official draw.io MCP. Flowork supplies only
# sandbox activation and platform file/preview adapters around this runtime.
COPY --from=drawio-assets /opt/drawio-mcp /opt/drawio-mcp
COPY api/drawio-runtime/export.sh /usr/local/bin/flowork-drawio-export
RUN chmod 0755 /usr/local/bin/flowork-drawio-export && \
    ln -s /opt/drawio-mcp/launch.mjs /usr/local/bin/flowork-diagram-mcp && \
    test "$(flowork-diagram-mcp --version)" = "1.5.0"

# API/worker images do not need a package installer. The dedicated sandboxd
# image opts into pip because it owns the narrow, wheel-only, content-addressed
# Workflow dependency builder; third-party code still executes only in gVisor.
ARG VIBECANVAS_RUNTIME_ENV_BUILDER=0
RUN if [ "${VIBECANVAS_RUNTIME_ENV_BUILDER}" = "1" ]; then \
      python -m pip --version; \
    else \
      python -m pip uninstall -y \
        pip setuptools wheel hatchling editables pathspec pluggy trove-classifiers \
        jaraco.context && \
      test ! -e /usr/local/bin/pip && \
      test ! -e /usr/local/bin/pip3; \
    fi

# Every API-derived service is unprivileged by default. Compose explicitly
# overrides only sandboxd to UID 0 for rootful gVisor checkpoint/restore.
RUN groupadd --gid 10001 vibecanvas && \
    useradd --uid 10001 --gid 10001 --create-home \
      --home-dir /home/vibecanvas --shell /usr/sbin/nologin vibecanvas && \
    install -d -o 10001 -g 10001 -m 0770 \
      /var/lib/vibecanvas/local-data \
      /var/lib/vibecanvas/objectstore \
      /var/lib/vibecanvas/lib-overlay \
      /var/lib/vibecanvas/agent-runtime \
      /var/lib/vibecanvas/vfs-volumes \
      /run/vibecanvas-openfga \
      /run/vibecanvas-sandbox

EXPOSE 8000
ENV VIBECANVAS_API_CORS_ORIGINS=http://localhost:3000
USER 10001:10001

# Production-ready default. Override VIBECANVAS_API_DEV_TOKEN at run time.
CMD ["uvicorn", "vibecanvas_api.app:build_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
