#!/usr/bin/env bash
set -euo pipefail
umask 077

# Bootstrap a fresh Debian/Ubuntu/WSL host for the native (non-Docker) stack.
# The script is intentionally idempotent. It installs host packages, creates a
# repo-local uv Python environment, installs pinned project dependencies,
# prepares Node/pnpm and the selected sandbox backend, and starts the app.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PREPARE_ONLY=0
UV_VERSION="${UV_VERSION:-0.12.19}"
NODE_VERSION="${NODE_VERSION:-22.23.3}"
NODE_MAJOR="${NODE_VERSION%%.*}"
CODEX_CLI_VERSION="${CODEX_CLI_VERSION:-0.157.1}"
PLAYWRIGHT_CORE_VERSION="${PLAYWRIGHT_CORE_VERSION:-1.63.0-alpha-2026-08-05}"
DRAWIO_DESKTOP_VERSION="${DRAWIO_DESKTOP_VERSION:-31.1.8}"
DRAWIO_DESKTOP_AMD64_SHA256="${DRAWIO_DESKTOP_AMD64_SHA256:-f4c49ed84422ea4afd95818f53c54bc666e57b33bd036d468c7096619b47ffd9}"
DRAWIO_DESKTOP_ARM64_SHA256="${DRAWIO_DESKTOP_ARM64_SHA256:-62a9ea636accada76076bd5a20f61b707e0e8093d3fd28c6583518663ca795d6}"

usage() {
  cat <<'EOF'
usage: ./scripts/bootstrap_native_linux.sh [--prepare-only]

  --prepare-only  install and configure dependencies without starting services
EOF
}

for argument in "$@"; do
  case "$argument" in
    --prepare-only) PREPARE_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $argument" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || {
  echo "ERROR: this bootstrap supports Linux and WSL only" >&2
  exit 1
}
command -v apt-get >/dev/null || {
  echo "ERROR: apt-get is required; see docs/installation.md for manual installation" >&2
  exit 1
}
[[ "${EUID}" -ne 0 ]] || {
  echo "ERROR: run this script as your normal login user, not root; it will use sudo for apt" >&2
  exit 1
}
command -v sudo >/dev/null || {
  echo "ERROR: sudo is required for host package installation" >&2
  exit 1
}

echo "[1/7] Installing host packages"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  binutils build-essential ca-certificates clang cmake curl file git gnupg jq libpq-dev libssl-dev pkg-config openssh-client \
  openssl patch postgresql postgresql-contrib procps redis-server ripgrep rsync \
  tar unzip util-linux zip \
  bubblewrap \
  fonts-dejavu-core fonts-noto-cjk fonts-wqy-zenhei \
  xvfb xauth \
  libreoffice-writer-nogui libreoffice-impress-nogui libreoffice-calc-nogui \
  poppler-utils

case "$(dpkg --print-architecture)" in
  amd64)
    drawio_arch="amd64"
    drawio_sha256="$DRAWIO_DESKTOP_AMD64_SHA256"
    ;;
  arm64)
    drawio_arch="arm64"
    drawio_sha256="$DRAWIO_DESKTOP_ARM64_SHA256"
    ;;
  *)
    echo "ERROR: draw.io Desktop is unsupported on $(dpkg --print-architecture)" >&2
    exit 1
    ;;
esac
installed_drawio_version="$(dpkg-query -W -f='${Version}' draw.io 2>/dev/null || true)"
if [[ "$installed_drawio_version" != "$DRAWIO_DESKTOP_VERSION" ]]; then
  drawio_deb="$(mktemp /tmp/flowork-drawio.XXXXXX.deb)"
  curl -fsSL --retry 3 -o "$drawio_deb" \
    "https://github.com/jgraph/drawio-desktop/releases/download/v${DRAWIO_DESKTOP_VERSION}/drawio-${drawio_arch}-${DRAWIO_DESKTOP_VERSION}.deb"
  echo "${drawio_sha256}  ${drawio_deb}" | sha256sum -c -
  [[ "$(dpkg-deb -f "$drawio_deb" Package)" == "draw.io" ]]
  [[ "$(dpkg-deb -f "$drawio_deb" Version)" == "$DRAWIO_DESKTOP_VERSION" ]]
  [[ "$(dpkg-deb -f "$drawio_deb" Architecture)" == "$drawio_arch" ]]
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$drawio_deb"
  rm -f "$drawio_deb"
fi
command -v drawio >/dev/null || {
  echo "ERROR: draw.io Desktop CLI was not installed" >&2
  exit 1
}
sudo install -m 0755 \
  "$REPO_ROOT/api/drawio-runtime/export.sh" \
  /usr/local/bin/flowork-drawio-export
command -v flowork-drawio-export >/dev/null || {
  echo "ERROR: draw.io feedback export wrapper was not installed" >&2
  exit 1
}

office_command="$(command -v libreoffice || command -v soffice || true)"
[[ -n "$office_command" ]] || {
  echo "ERROR: LibreOffice headless converter was not installed" >&2
  exit 1
}
command -v pdftoppm >/dev/null || {
  echo "ERROR: Poppler pdftoppm was not installed" >&2
  exit 1
}
"$office_command" --version
pdftoppm -v

node_is_compatible() {
  command -v node >/dev/null || return 1
  [[ "$(node --version)" == "v${NODE_VERSION}" ]]
}

if ! node_is_compatible; then
  echo "[2/7] Installing Node.js ${NODE_VERSION}"
  node_setup="$(mktemp)"
  curl -fsSL --retry 3 "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o "$node_setup"
  # APT source/key metadata is public and must remain readable by _apt.
  # Do not propagate the bootstrap's secret-file umask into repository setup.
  sudo -E bash -c 'umask 022; exec bash "$1"' bash "$node_setup"
  # Hosts may pin distribution packages above NodeSource's priority. Select
  # the exact reviewed release, never silently accept Debian's Node 18 or a
  # newer NodeSource package that appeared after this checkout was published.
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    "nodejs=${NODE_VERSION}-1nodesource1"
else
  echo "[2/7] Reusing compatible Node.js $(node --version)"
fi
node_is_compatible || {
  echo "ERROR: expected Node.js v${NODE_VERSION}, got $(node --version 2>/dev/null || echo missing)" >&2
  exit 1
}
command -v npm >/dev/null || {
  echo "ERROR: npm is required; install the complete Node.js distribution" >&2
  exit 1
}

echo "[3/7] Installing the project-pinned pnpm, Codex CLI, Browser CLI runtime, and draw.io CLI runtime"
install_public_npm_package() {
  # Executable packages are public assets, unlike the private .env below.
  sudo bash -c 'umask 022; exec npm install --global --ignore-scripts --no-audit --no-fund "$@"' bash "$@"
}
if command -v corepack >/dev/null; then
  sudo corepack enable
  corepack prepare pnpm@10.34.4 --activate
else
  install_public_npm_package pnpm@10.34.4
fi
if [[ "$(codex --version 2>/dev/null || true)" != "codex-cli ${CODEX_CLI_VERSION}" ]]; then
  install_public_npm_package "@openai/codex@${CODEX_CLI_VERSION}"
fi
[[ "$(codex --version)" == "codex-cli ${CODEX_CLI_VERSION}" ]] || {
  echo "ERROR: expected codex-cli ${CODEX_CLI_VERSION}, got $(codex --version 2>/dev/null || echo missing)" >&2
  exit 1
}
if [[ "$(flowork-browser-runtime --version 2>/dev/null || true)" != "flowork-browser-runtime 0.4.0 (playwright-core ${PLAYWRIGHT_CORE_VERSION})" ]]; then
  npm --prefix "$REPO_ROOT/api/playwright-runtime" ci --ignore-scripts --no-audit --no-fund
  install_public_npm_package "$REPO_ROOT/api/playwright-runtime"
fi
[[ "$(flowork-browser-runtime --version)" == "flowork-browser-runtime 0.4.0 (playwright-core ${PLAYWRIGHT_CORE_VERSION})" ]] || {
  echo "ERROR: expected Browser CLI runtime 0.4.0 with Playwright core ${PLAYWRIGHT_CORE_VERSION}" >&2
  exit 1
}
drawio_runtime_is_compatible() {
  [[ "$(flowork-diagram-search --version 2>/dev/null || true)" == "1.5.0" ]] || return 1
  local global_root
  global_root="$(npm root --global)"
  node - "$global_root/flowork-drawio-runtime" <<'JS'
const root = process.argv[2];
const expected = {
  "fast-uri": "4.2.1",
  "hono": "4.13.9",
  "qs": "6.16.0",
};
for (const [name, version] of Object.entries(expected)) {
  const actual = require(`${root}/node_modules/${name}/package.json`).version;
  if (actual !== version) process.exit(1);
}
JS
}
if ! drawio_runtime_is_compatible; then
  npm --prefix "$REPO_ROOT/api/drawio-runtime" ci --ignore-scripts --no-audit --no-fund
  install_public_npm_package "$REPO_ROOT/api/drawio-runtime"
fi
drawio_runtime_is_compatible || {
  echo "ERROR: expected draw.io search 1.5.0 with reviewed transitive dependencies" >&2
  exit 1
}

if [[ "$(uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]]; then
  echo "[4/7] Installing uv ${UV_VERSION}"
  uv_installer="$(mktemp)"
  curl -LsSf --retry 3 "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$uv_installer"
  UV_NO_MODIFY_PATH=1 sh "$uv_installer"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
else
  echo "[4/7] Reusing pinned $(uv --version)"
fi
command -v uv >/dev/null || {
  echo "ERROR: uv was installed but is not on PATH; add ~/.local/bin and retry" >&2
  exit 1
}

echo "[5/7] Creating the repo-local Python 3.11.16 environment"
cd "$REPO_ROOT"
bash "$REPO_ROOT/scripts/sync_python_env.sh"
.venv/bin/python -c \
  'import fastapi, jsonlines, matplotlib, networkx, numpy, pandas, psycopg, seaborn, sqlalchemy, tabulate, vibecanvas_api, vibecanvas_engine; print("Python environment: ok")'

echo "[6/7] Installing Web and extension packages"
pnpm --dir web install --frozen-lockfile
pnpm --dir extension install --frozen-lockfile

echo "[7/7] Preparing local config and the selected sandbox runtime"
launch_env="${VIBECANVAS_LAUNCH_ENV:-$REPO_ROOT/.env.launch.local}"
if [[ ! -e "$launch_env" ]]; then
  mkdir -p "$(dirname "$launch_env")"
  cat >"$launch_env" <<'EOF'
# Local native deployment. This file is mode 0600 and is never committed.
# Defaults only: deployment-specific values are supplied by the operator.
# You may export overrides when invoking bootstrap/launch, or edit this file.
WEB_HOST="${WEB_HOST:-::}"
WEB_PORT="${WEB_PORT:-9001}"
VIBECANVAS_PUBLIC_URL="${VIBECANVAS_PUBLIC_URL:-http://localhost:${WEB_PORT}/}"
SANDBOX_RUNTIME="${SANDBOX_RUNTIME:-bubblewrap}"
SANDBOX_TYPE="${SANDBOX_TYPE:-rootless-warm}"
ENABLE_TEST_USER="${ENABLE_TEST_USER:-false}"
ENTERPRISE_SSO_ENABLED="${ENTERPRISE_SSO_ENABLED:-false}"
AGENT_RUNTIME_TYPES="${AGENT_RUNTIME_TYPES:-codex}"
CODEX_RUNTIME_AUTH_METHODS="${CODEX_RUNTIME_AUTH_METHODS:-chatgpt,managed_api,personal_api}"
CODEX_MANAGED_APIS_JSON="${CODEX_MANAGED_APIS_JSON:-[]}"
EOF
  chmod 600 "$launch_env"
  echo "Created $launch_env"
else
  echo "Kept existing $launch_env unchanged"
fi

# bubblewrap is installed with the host packages. Only an explicit gVisor
# deployment needs runsc; source the same operator config used by launch.sh.
(
  source "$launch_env"
  case "${SANDBOX_RUNTIME:-bubblewrap}" in
    bubblewrap) command -v bwrap >/dev/null ;;
    gvisor) bash "$REPO_ROOT/scripts/get_runsc.sh" >/dev/null ;;
    *) echo "ERROR: SANDBOX_RUNTIME must be bubblewrap or gvisor" >&2; exit 1 ;;
  esac
)

if [[ "$PREPARE_ONLY" == "1" ]]; then
  echo "Preparation complete. Start later with: ./launch.sh start"
  exit 0
fi

exec "$REPO_ROOT/launch.sh" start
