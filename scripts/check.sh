#!/usr/bin/env bash
# Every check, run locally in clean Linux containers from the files git would publish. Needs only Docker.
#   ./scripts/check.sh
# It installs from the lock file, lints, runs the tests and a mock film on SQLite and on Postgres, builds the interface
# and the image, and starts the whole compose stack on spare ports with a throwaway volume. It never sees your .env or
# data directory, and leaves your own environment and services alone.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."

NET="cinemafabric-check-$$"
DB="cinemafabric-check-db-$$"
PROJECT="cinemafabric-check-$$"
OVERRIDE=""
files() { git ls-files -co --exclude-standard | grep -v '^frontend/node_modules/' | tar -cf - -T -; }
cleanup() {
  docker rm -f "$DB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  if [ -n "$OVERRIDE" ]; then
    docker compose -p "$PROJECT" -f docker-compose.yml -f "$OVERRIDE" down -v >/dev/null 2>&1 || true
    rm -f "$OVERRIDE"
  fi
}
trap cleanup EXIT

echo "== Postgres"
docker network create "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" -e POSTGRES_PASSWORD=check -e POSTGRES_DB=cinemafabric postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec "$DB" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 2; done

echo "== Install, lint, tests and mock films"
files | docker run -i --rm --network "$NET" -e CF_TEST_DATABASE_URL="postgresql://postgres:check@$DB:5432/cinemafabric" \
  python:3.12-slim sh -ec '
    mkdir /app && tar -xf - -C /app && cd /app
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends ffmpeg fonts-dejavu-core >/dev/null 2>&1
    pip install -q uv 2>/dev/null
    uv sync --locked --extra postgres --extra s3 --extra c2pa 2>&1 | tail -1
    uvx ruff@0.8.6 check cinemafabric tests scripts
    uv run pytest -q 2>&1 | tail -1
    CF_MODE=mock uv run python scripts/smoke.py 2>&1 | tail -1
    echo "on Postgres:"
    CF_MODE=mock CF_DATABASE_URL="$CF_TEST_DATABASE_URL" uv run python scripts/smoke.py 2>&1 | tail -1
  '

echo "== Interface"
files | docker run -i --rm node:22-slim sh -ec '
    mkdir /app && tar -xf - -C /app && cd /app/frontend
    npm ci --no-audit --no-fund 2>&1 | tail -1
    npm run build 2>&1 | tail -1
    npm audit --omit=dev 2>&1 | tail -1
  '

echo "== Image and compose stack"
OVERRIDE="$(mktemp ./.check-compose.XXXXXX)"  # beside the compose file, since Docker on Windows can't read a /tmp path
cat > "$OVERRIDE" <<'EOF'
services:
  fabric:
    env_file: !reset []
    environment:
      CF_MODE: mock
    volumes: !override
      - check-data:/data
    ports: !override
      - "127.0.0.1::8100"
  studio:
    env_file: !reset []
    environment:
      CF_MODE: mock
    volumes: !override
      - check-data:/data
    ports: !override
      - "127.0.0.1::8000"
volumes:
  check-data:
EOF
docker compose -p "$PROJECT" -f docker-compose.yml -f "$OVERRIDE" up -d --build --quiet-pull 2>&1 | tail -1
compose() { docker compose -p "$PROJECT" -f docker-compose.yml -f "$OVERRIDE" "$@"; }
FABRIC="$(compose port fabric 8100 | tr -d '\r')"
STUDIO="$(compose port studio 8000 | tr -d '\r')"
up=""
for _ in $(seq 1 45); do
  if curl -fs -m 3 "http://$STUDIO/api/options" >/dev/null && curl -fs -m 3 "http://$FABRIC/health" >/dev/null; then up=1; break; fi
  sleep 2
done
[ -n "$up" ] || { compose logs --tail 20; echo "the compose stack did not come up"; exit 1; }
curl -fs -m 5 "http://$STUDIO/" | grep -q '<div id="root"' || { echo "the studio did not serve the interface"; exit 1; }
[ "$(compose exec -T studio id -u | tr -d '\r')" != "0" ] || { echo "the container runs as root"; exit 1; }
compose exec -T studio sh -c 'touch /data/.writable && [ ! -s /data/.env ]' || { echo "the container can't write its data"; exit 1; }
echo "fabric, studio and interface answer, as a normal user, on a throwaway volume"

echo "All checks passed."
