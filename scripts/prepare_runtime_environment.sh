#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SOURCE_PYTHON="${VIBECANVAS_SOURCE_PYTHON:-${VIBECANVAS_PYTHON:-$REPO_ROOT/.venv/bin/python}}"
CACHE_ROOT="${VIBECANVAS_RUNTIME_CACHE_ROOT:-/tmp/vibecanvas-runtime-python}"
CACHE_PREFIX="$CACHE_ROOT/env"
CACHE_MARKER="$CACHE_ROOT/source.marker"
CACHE_LOCK="$CACHE_ROOT/prepare.lock"

source_prefix() {
  "$SOURCE_PYTHON" -c 'import sys; print(sys.prefix)'
}

source_marker() {
  local prefix="$1"
  local source_site_packages path
  source_site_packages="$("$SOURCE_PYTHON" -c \
    'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  for path in \
    "$prefix/pyvenv.cfg" \
    "$source_site_packages" \
    "$REPO_ROOT/engine/pyproject.toml" \
    "$REPO_ROOT/api/pyproject.toml" \
    "$REPO_ROOT/requirements-dev.txt" \
    "$REPO_ROOT/requirements-sandbox.txt" \
    "$REPO_ROOT/scripts/prepare_runtime_environment.sh"; do
    [[ -e "$path" ]] && stat -c '%Y:%s' "$path"
  done | tr '\n' ':'
}

runtime_self_check() {
  PYTHONNOUSERSITE=1 env -u PYTHONPATH "$CACHE_PREFIX/bin/python" -c \
    'import asyncio, fastapi, jsonlines, matplotlib, networkx, numpy, pandas, psycopg, seaborn, sqlalchemy, tabulate, webauthn; print("runtime environment self-check: ok")'
}

sync_base_environment() {
  local prefix="$1" base_prefix
  base_prefix="$("$SOURCE_PYTHON" -c 'import sys; print(sys.base_prefix)')"
  [[ "$prefix" != "$base_prefix" ]] || {
    echo "ERROR: Runtime preparation requires the repository uv environment, not host Python" >&2
    return 1
  }
  rsync -a --delete "$prefix/" "$CACHE_PREFIX/"
}

sync_project_dependencies() {
  # The cache is deliberately independent from the shared source environment.
  # Resolve project metadata here so a newly declared dependency cannot be
  # hidden by native_dev_up.sh's fast, source-only --no-deps reinstall.
  # The legacy shared environment currently contains two opentelemetry-api
  # dist-info records. Remove every copied record from the disposable cache so
  # pip cannot leave stale metadata ahead of the version selected below.
  local attempt
  for attempt in 1 2 3; do
    if ! PYTHONNOUSERSITE=1 "$CACHE_PREFIX/bin/python" -m pip show \
      opentelemetry-api >/dev/null 2>&1; then
      break
    fi
    PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONNOUSERSITE=1 \
      "$CACHE_PREFIX/bin/python" -m pip uninstall -y opentelemetry-api
  done
  if PYTHONNOUSERSITE=1 "$CACHE_PREFIX/bin/python" -m pip show \
    opentelemetry-api >/dev/null 2>&1; then
    echo "ERROR: unable to clear duplicate opentelemetry-api metadata from Runtime cache" >&2
    return 1
  fi
  PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONNOUSERSITE=1 env -u PYTHONPATH \
    "$CACHE_PREFIX/bin/python" -m pip install \
      --upgrade \
      --upgrade-strategy only-if-needed \
      "$REPO_ROOT/engine" \
      "$REPO_ROOT/api"
}

prepare_tiktoken_cache() {
  local cache_dir="$CACHE_PREFIX/share/tiktoken"
  mkdir -p "$cache_dir"
  TIKTOKEN_CACHE_DIR="$cache_dir" "$CACHE_PREFIX/bin/python" -c \
    'import tiktoken; tiktoken.get_encoding("cl100k_base"); print("tiktoken cache ready")'
}

prepare_application_source() {
  local site_packages
  site_packages="$("$CACHE_PREFIX/bin/python" -c \
    'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  mkdir -p "$site_packages/vibecanvas_api" "$site_packages/vibecanvas_engine"
  rsync -a --delete --delete-excluded \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$REPO_ROOT/api/src/vibecanvas_api/" \
    "$site_packages/vibecanvas_api/"
  rsync -a --delete --delete-excluded \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$REPO_ROOT/engine/src/vibecanvas_engine/" \
    "$site_packages/vibecanvas_engine/"
  PYTHONNOUSERSITE=1 env -u PYTHONPATH "$CACHE_PREFIX/bin/python" -c \
    'import vibecanvas_api, vibecanvas_engine; print("application source cache ready")'
}

prepare_runtime() {
  [[ -x "$SOURCE_PYTHON" ]] || {
    echo "ERROR: source Python is not executable: $SOURCE_PYTHON" >&2
    exit 1
  }
  command -v rsync >/dev/null || {
    echo "ERROR: rsync is required to prepare the local Runtime environment" >&2
    exit 1
  }
  command -v flock >/dev/null || {
    echo "ERROR: flock is required to prepare the local Runtime environment" >&2
    exit 1
  }

  local prefix marker cached_marker=""
  prefix="$(source_prefix)"
  marker="$(source_marker "$prefix")"
  mkdir -p "$CACHE_ROOT"

  exec 9>"$CACHE_LOCK"
  flock 9
  if [[ -f "$CACHE_MARKER" ]]; then
    cached_marker="$(<"$CACHE_MARKER")"
  fi
  if [[ -x "$CACHE_PREFIX/bin/python" && "$cached_marker" == "$marker" ]]; then
    if runtime_self_check; then
      prepare_application_source
      prepare_tiktoken_cache
      echo "runtime environment ready: $CACHE_PREFIX"
      return
    fi
    echo "runtime environment: cached dependency check failed; refreshing"
  fi

  echo "runtime environment: syncing $prefix -> $CACHE_PREFIX"
  mkdir -p "$CACHE_PREFIX"
  sync_base_environment "$prefix"
  sync_project_dependencies
  runtime_self_check
  prepare_application_source
  prepare_tiktoken_cache
  printf '%s' "$marker" >"$CACHE_MARKER"
  echo "runtime environment ready: $CACHE_PREFIX"
}

show_status() {
  local prefix marker cached_marker=""
  prefix="$(source_prefix)"
  marker="$(source_marker "$prefix")"
  if [[ -f "$CACHE_MARKER" ]]; then
    cached_marker="$(<"$CACHE_MARKER")"
  fi
  echo "source_python=$SOURCE_PYTHON"
  echo "source_prefix=$prefix"
  echo "cache_prefix=$CACHE_PREFIX"
  if [[ -x "$CACHE_PREFIX/bin/python" && "$cached_marker" == "$marker" ]]; then
    echo "status=ready"
  elif [[ -x "$CACHE_PREFIX/bin/python" ]]; then
    echo "status=stale"
  else
    echo "status=missing"
  fi
}

case "${1:-prepare}" in
  prepare)
    prepare_runtime
    ;;
  status)
    show_status
    ;;
  *)
    echo "usage: $0 {prepare|status}" >&2
    exit 2
    ;;
esac
