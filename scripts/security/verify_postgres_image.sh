#!/usr/bin/env bash
# Fresh initialization, vector execution and restart persistence; never uses
# operator data or a published TCP port. The image's anonymous data volume is
# removed with the disposable container, including on test failure.
set -euo pipefail
image="${1:?Usage: verify_postgres_image.sh IMAGE}"
container="flowork-postgres-check-${RANDOM}-$$"
cleanup() { docker rm -fv "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$container" --network none \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  -e POSTGRES_DB=vibecanvas \
  -e VIBECANVAS_APP_PASSWORD="$(openssl rand -hex 24)" \
  -e VIBECANVAS_MIGRATOR_PASSWORD="$(openssl rand -hex 24)" \
  -e VIBECANVAS_MAINTENANCE_PASSWORD="$(openssl rand -hex 24)" \
  "$image" >/dev/null
wait_ready() {
  local attempt
  for attempt in $(seq 1 60); do
    # The temporary init server also answers pg_isready; wait for the final
    # entrypoint exec before issuing persistent SQL or testing shutdown.
    if [[ "$(docker exec "$container" sh -c 'cat /proc/1/comm' 2>/dev/null)" == postgres ]] && \
        docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  docker logs "$container" >&2
  return 1
}
wait_ready
docker exec -i "$container" psql -U postgres -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION vector;
CREATE TABLE smoke_vectors (id integer PRIMARY KEY, embedding vector(3));
INSERT INTO smoke_vectors VALUES (1, '[1,0,0]'), (2, '[0,1,0]');
CREATE INDEX smoke_vectors_hnsw ON smoke_vectors USING hnsw (embedding vector_cosine_ops);
DO $$ BEGIN
  IF (SELECT extversion FROM pg_extension WHERE extname='vector') <> '0.8.6' THEN
    RAISE EXCEPTION 'Unexpected pgvector version';
  END IF;
  IF (SELECT id FROM smoke_vectors ORDER BY embedding <=> '[1,0,0]' LIMIT 1) <> 1 THEN
    RAISE EXCEPTION 'Vector lookup failed';
  END IF;
END $$;
SQL
docker restart --timeout 30 "$container" >/dev/null
wait_ready
test "$(docker exec "$container" psql -U postgres -Atc 'SELECT count(*) FROM smoke_vectors')" = 2
test "$(docker exec "$container" psql -U postgres -Atc 'SHOW server_version_num')" = 150019
printf 'postgres_image_live=pass image=%s vector=0.8.6\n' "$image"
