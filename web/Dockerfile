# Flowork Web application: Vite/React SPA served by nginx
#
# Build:
#   cd /path/to/flowork
#   docker build -f web/Dockerfile -t vibecanvas-web:dev .
#
# Run (standalone — nginx proxies /api/ to a host/service named `api` on :8000):
#   docker run --rm -p 9001:8080 vibecanvas-web:dev
#
# Build context is the repository root (NOT web/) to mirror the api/Dockerfile
# convention and keep room for future sibling assets.

# ---------- Stage 1: build the static bundle ----------
FROM node:22.23.2-alpine3.23@sha256:46825fbbd4e996a78b7a2cdc08d75e38a5a505bdab95dcda55605359bf124bc6 AS build

WORKDIR /app

# Match package.json's packageManager exactly; upgrades are reviewed with the
# lockfile instead of silently changing the package manager during an image build.
RUN corepack enable && corepack prepare pnpm@10.34.4 --activate

# Install deps with a deterministic lockfile first so this layer caches.
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Copy the rest of the frontend tree.
COPY web/ ./

# Bake the API base URL into the bundle at build time. Vite statically inlines
# any `import.meta.env.VITE_*` it reads (src/lib/api/client.ts et al.), so this
# ARG must be set as an ENV BEFORE `pnpm build`. Empty default => same-origin
# (nginx proxies /api/), overridable via `--build-arg VITE_API_BASE=...`.
ARG VITE_API_BASE=""
ENV VITE_API_BASE=$VITE_API_BASE
ARG VIBECANVAS_BROWSER_EXTENSION_ID=mkfldhmlgdbpmhplaphhcfcdcoaakcik
ENV VITE_EXTENSION_ID=$VIBECANVAS_BROWSER_EXTENSION_ID

# Regenerate the API client from the committed openapi.json (offline-safe)
# and produce the production bundle.
RUN pnpm codegen:api:offline && pnpm build

# Build the companion browser extension from its independent frozen graph.
# The public Web origin is compiled into the MV3 allowlist, so deployments
# reached through a non-local origin must pass the same canonical URL here.
FROM node:22.23.2-alpine3.23@sha256:46825fbbd4e996a78b7a2cdc08d75e38a5a505bdab95dcda55605359bf124bc6 AS extension-build

WORKDIR /app/extension
RUN corepack enable && corepack prepare pnpm@10.34.4 --activate
COPY extension/package.json extension/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY extension/ ./
COPY web/public/branding/ /app/web/public/branding/

ARG VITE_WEB_BASE="http://localhost:9001"
ARG VITE_EXTENSION_ALLOWED_ORIGINS="http://localhost:9001"
ENV VITE_WEB_BASE=$VITE_WEB_BASE \
    VITE_EXTENSION_ALLOWED_ORIGINS=$VITE_EXTENSION_ALLOWED_ORIGINS
RUN pnpm build && test -f dist/manifest.json && test -f dist/sidepanel.html

# Python's standard library creates the deployment-local ZIP without adding a
# package manager or archive utility to the final nginx runtime image.
FROM python:3.11.16-slim-trixie@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7 AS extension-package
COPY --from=extension-build /app/extension/dist /extension
RUN mkdir -p /out && \
    cd /extension && \
    python -m zipfile -c /out/flowork-extension.zip * && \
    python -m zipfile -t /out/flowork-extension.zip

# ---------- Stage 2: serve via nginx ----------
FROM nginx:1.30.4-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46

# Vite emits the bundle to /app/dist in the build stage.
COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=extension-package /out/flowork-extension.zip \
  /usr/share/nginx/html/downloads/flowork-extension.zip
# Preserve old bookmarked download URLs during the product rename.
RUN ln -sf flowork-extension.zip /usr/share/nginx/html/downloads/vibecanvas-extension.zip
RUN mkdir -p /etc/flowork && \
    mv /usr/share/nginx/html/csp-script-hashes.txt \
      /etc/flowork/csp-script-hashes.txt && \
    chmod 0444 /etc/flowork/csp-script-hashes.txt

# SPA + /api/ reverse-proxy config (SSE-safe: proxy_buffering off). The nginx
# image's standard entrypoint renders templates at container startup. Restrict
# envsubst to this one deployment value so nginx variables such as `$host` and
# `$uri` remain intact.
ARG VIBECANVAS_BROWSER_EXTENSION_ID=mkfldhmlgdbpmhplaphhcfcdcoaakcik
ENV VIBECANVAS_BROWSER_EXTENSION_ID=$VIBECANVAS_BROWSER_EXTENSION_ID \
    NGINX_ENVSUBST_FILTER=^(VIBECANVAS_BROWSER_EXTENSION_ID|CSP_SCRIPT_HASHES)$
COPY web/nginx.conf /etc/nginx/templates/default.conf.template
COPY web/14-load-csp-hashes.envsh /docker-entrypoint.d/14-load-csp-hashes.envsh
COPY web/15-validate-extension-id.sh /docker-entrypoint.d/15-validate-extension-id.sh

# The official entrypoint renders templates at startup, so both its output
# directory and nginx's temporary cache must be writable by the unprivileged
# nginx identity. Keep the PID under /tmp instead of the root-owned /run.
RUN sed -i 's#pid        /run/nginx.pid;#pid        /tmp/nginx.pid;#' \
      /etc/nginx/nginx.conf && \
    chown -R 101:101 \
      /etc/nginx/conf.d \
      /etc/nginx/templates \
      /var/cache/nginx && \
    chmod 0755 \
      /docker-entrypoint.d/14-load-csp-hashes.envsh \
      /docker-entrypoint.d/15-validate-extension-id.sh

EXPOSE 8080
USER 101:101
CMD ["nginx", "-g", "daemon off;"]
