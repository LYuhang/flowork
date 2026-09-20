#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s IMAGE@sha256:DIGEST OWNER/REPO SIGNER_WORKFLOW SOURCE_SHA SOURCE_REF\n' \
    "$0" >&2
  exit 2
}

if (( $# != 5 )); then
  usage
fi

image="$1"
repository="$2"
signer_workflow="$3"
source_sha="$4"
source_ref="$5"
gh_bin="${GH_BIN:-$(command -v gh || true)}"

if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  printf 'Expected GitHub repository must be OWNER/REPO.\n' >&2
  exit 2
fi
expected_workflow="$repository/.github/workflows/release-images.yml"
if [[ "$signer_workflow" != "$expected_workflow" ]]; then
  printf 'Signer workflow must be exactly %s.\n' "$expected_workflow" >&2
  exit 2
fi
if [[ ! "$source_sha" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Source digest must be a full lowercase Git SHA.\n' >&2
  exit 2
fi
if [[ ! "$source_ref" =~ ^refs/tags/v[^[:space:]]+$ ]]; then
  printf 'Source ref must be an immutable v* release tag.\n' >&2
  exit 2
fi

repository_lower="${repository,,}"
expected_prefix="ghcr.io/${repository_lower}-"
image_name="${image%@*}"
digest="${image##*@}"
case "$image_name" in
  "${expected_prefix}api"|"${expected_prefix}web"|"${expected_prefix}engine") ;;
  *)
    printf 'Image is outside the admitted Flowork release repositories.\n' >&2
    exit 2
    ;;
esac
if [[ "$image_name" == *:* || ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'Admission requires an exact digest reference and rejects tags.\n' >&2
  exit 2
fi
if [[ -z "$gh_bin" || ! -x "$gh_bin" ]]; then
  printf 'GitHub CLI is required to verify release attestations.\n' >&2
  exit 2
fi

common=(
  attestation verify "oci://$image"
  --repo "$repository"
  --signer-workflow "$signer_workflow"
  --cert-oidc-issuer "https://token.actions.githubusercontent.com"
  --source-digest "$source_sha"
  --source-ref "$source_ref"
  --deny-self-hosted-runners
)

"$gh_bin" "${common[@]}"
"$gh_bin" "${common[@]}" --predicate-type "https://spdx.dev/Document/v2.3"
printf 'release_attestation_gate=pass image=%s source=%s ref=%s\n' \
  "$image" "$source_sha" "$source_ref"
