#!/usr/bin/env bash
# Exercise the patched server and health probe without a persistent datastore.
set -euo pipefail
image="${1:?usage: verify_openfga_image.sh IMAGE}"
container="$(docker run --detach --rm --publish 127.0.0.1::8080 "$image" run \
  --datastore-engine memory --authn-method preshared --authn-preshared-keys image-smoke-test \
  --playground-enabled=false)"
trap 'docker stop "$container" >/dev/null 2>&1 || true' EXIT
ready=0
for ((attempt=0; attempt<30; attempt++)); do
  if docker exec "$container" /usr/local/bin/grpc_health_probe -addr=localhost:8081 >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  docker logs "$container" >&2
  echo 'OpenFGA health probe did not become ready' >&2
  exit 1
fi
port="$(docker port "$container" 8080/tcp)"
python3 - "$port" <<'PY'
import json
import sys
import urllib.error
import urllib.request

base = 'http://' + sys.argv[1]

def request(path, payload, authenticated=True):
    headers = {'Content-Type': 'application/json'}
    if authenticated:
        headers['Authorization'] = 'Bearer image-smoke-test'
    req = urllib.request.Request(base + path, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)

try:
    request('/stores', {'name': 'unauthorized'}, authenticated=False)
except urllib.error.HTTPError as error:
    assert error.code == 401, error.code
else:
    raise AssertionError('OpenFGA accepted an unauthenticated mutation')
store = request('/stores', {'name': 'image-smoke-test'})['id']
path = '/stores/' + store
model = request(path + '/authorization-models', {
    'schema_version': '1.1',
    'type_definitions': [
        {'type': 'user'},
        {'type': 'document', 'relations': {'viewer': {'this': {}}},
         'metadata': {'relations': {'viewer': {'directly_related_user_types': [{'type': 'user'}]}}}},
    ],
})['authorization_model_id']
request(path + '/write', {'authorization_model_id': model, 'writes': {
    'tuple_keys': [{'user': 'user:alice', 'relation': 'viewer', 'object': 'document:test'}],
}})
for user, allowed in [('alice', True), ('bob', False)]:
    result = request(path + '/check', {'authorization_model_id': model, 'tuple_key': {
        'user': 'user:' + user, 'relation': 'viewer', 'object': 'document:test',
    }})
    assert result['allowed'] is allowed, result
print('openfga_image_smoke=pass auth=model-write-tuple-check health=grpc')
PY
