#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_version="$(tr -d '[:space:]' < "$repo_root/.python-version")"
uv_version="0.12.23"

if [[ "$(uv --version 2>/dev/null | awk '{print $2}')" != "$uv_version" ]]; then
  printf 'Expected uv %s. Install that version before synchronizing the environment.\n' \
    "$uv_version" >&2
  exit 2
fi
if [[ "$python_version" != "3.11.17" ]]; then
  printf 'Unsupported .python-version: expected 3.11.17, got %s.\n' \
    "$python_version" >&2
  exit 2
fi

cd "$repo_root"
uv python install "$python_version"

current_version=""
if [[ -x .venv/bin/python ]]; then
  current_version="$(.venv/bin/python -c 'import platform; print(platform.python_version())')"
fi
if [[ "$current_version" != "$python_version" ]]; then
  uv venv --clear --python "$python_version" --seed .venv
fi

# Verify the upstream tarfile fix in the pinned native runtime.
.venv/bin/python scripts/security/verify_python_tarfile_fix.py

uv pip install --python .venv/bin/python --require-hashes \
  --requirement requirements-build.txt
uv pip install --python .venv/bin/python \
  --requirement requirements-dev.txt
uv pip install --python .venv/bin/python --require-hashes \
  --requirement requirements-sandbox.txt
uv pip install --python .venv/bin/python --no-build-isolation --no-deps \
  --editable ./engine --editable ./api

.venv/bin/python -c \
  'import platform; assert platform.python_version() == "3.11.17", platform.python_version()'
.venv/bin/python scripts/verify_dependency_locks.py
.venv/bin/python -m pip check
printf 'python_environment=ready python=%s uv=%s path=%s\n' \
  "$python_version" "$uv_version" "$repo_root/.venv"
