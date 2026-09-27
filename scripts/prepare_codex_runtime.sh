#!/usr/bin/env bash
# Prepare the managed native Codex bundle. No global toolchain/PATH changes,
# service restart or replacement of /usr/bin/codex. Failed staging is retained.
set -euo pipefail
umask 077
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: bash scripts/prepare_codex_runtime.sh"
  echo "Build and verify the pinned Codex bundle in .tools/codex-runtime. Requires the project Python environment and official npm Codex package."
  exit 0
fi
[[ $# == 0 ]] || { echo "ERROR: unknown argument" >&2; exit 2; }
codex_root="$REPO_ROOT/.tools/codex-runtime"
codex_bundle="$codex_root/0.147.0-output-subscription-1"
codex_python="${VIBECANVAS_PYTHON:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$codex_python" ]] || { echo "ERROR: prepare the project Python environment first" >&2; exit 1; }
mkdir -p "$codex_root"
exec 9>"$codex_root/prepare.lock"
flock 9
if [[ -e "$codex_bundle" || -L "$codex_bundle" ]]; then
  exec "$codex_python" "$REPO_ROOT/scripts/build_codex_runtime.py" --verify_bundle "$codex_bundle"
fi
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) codex_target=x86_64-unknown-linux-gnu; codex_npm_arch=x64
    rustup_sha=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c ;;
  Linux:aarch64) codex_target=aarch64-unknown-linux-gnu; codex_npm_arch=arm64
    rustup_sha=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c ;;
  *) echo "ERROR: supported platforms are Linux x86_64/aarch64" >&2; exit 1 ;;
esac
codex_vendor="$(npm root --global)/@openai/codex/node_modules/@openai/codex-linux-$codex_npm_arch/vendor/${codex_target%-gnu}-musl"
[[ -f "$codex_vendor/codex-package.json" ]] || { echo "ERROR: install official @openai/codex@0.147.0 first" >&2; exit 1; }
codex_stage="$(mktemp -d "$codex_root/build.XXXXXX")"
echo "Preparing native Codex; build and diagnostic evidence: $codex_stage" >&2
codex_env=(env "RUSTUP_HOME=$codex_root/rustup" "CARGO_HOME=$codex_root/cargo" "RUSTUP_TOOLCHAIN=1.95.0" "PATH=$codex_root/cargo/bin:$PATH")
if [[ ! -x "$codex_root/cargo/bin/rustup" ]]; then
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
    "https://static.rust-lang.org/rustup/archive/1.28.2/$codex_target/rustup-init" -o "$codex_stage/rustup-init"
  echo "$rustup_sha  $codex_stage/rustup-init" | sha256sum -c - >&2
  chmod 700 "$codex_stage/rustup-init"
  "${codex_env[@]}" "$codex_stage/rustup-init" -y --no-modify-path --profile minimal --default-toolchain 1.95.0 >&2
fi
"${codex_env[@]}" rustup toolchain install 1.95.0 --profile minimal >&2
codex_source="$codex_root/source"
if [[ ! -e "$codex_source" ]]; then
  git init "$codex_source" >&2
  git -C "$codex_source" remote add origin https://github.com/openai/codex.git
  git -C "$codex_source" fetch --depth 1 origin be6e8eac029b183056b7e4402879f15d2c85f61b >&2
  git -C "$codex_source" checkout --detach FETCH_HEAD >&2
fi
"${codex_env[@]}" "$codex_python" "$REPO_ROOT/scripts/build_codex_runtime.py" \
  --source_dir "$codex_source" --vendor_dir "$codex_vendor" \
  --output_dir "$codex_stage/bundle" --jobs "${CODEX_BUILD_JOBS:-4}" > "$codex_stage/build-result.json"
for probe in direct code-mode background; do
  codex_probe_args=()
  [[ "$probe" == direct ]] || codex_probe_args+=(--code_mode)
  [[ "$probe" != background ]] || codex_probe_args+=(--background)
  PYTHONPATH="$REPO_ROOT/api/src" "$codex_python" "$REPO_ROOT/scripts/verify_codex_native_output.py" \
    --codex "$codex_stage/bundle/bin/codex" "${codex_probe_args[@]}" > "$codex_stage/native-output-$probe.json"
done
"$codex_python" "$REPO_ROOT/scripts/build_codex_runtime.py" --verify_bundle "$codex_stage/bundle" > "$codex_stage/verified.json"
# Publish only after every probe passes, without overwriting another runtime.
mv -T -n "$codex_stage/bundle" "$codex_bundle"
"$codex_python" "$REPO_ROOT/scripts/build_codex_runtime.py" --verify_bundle "$codex_bundle"
