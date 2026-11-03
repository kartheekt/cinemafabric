#!/usr/bin/env bash
# Start the fabric and the studio, and join this machine as a node.
#   ./scripts/dev.sh           live
#   ./scripts/dev.sh mock      simulated workers
set -euo pipefail
cd "$(dirname "$0")/.."
export CF_MODE="${1:-live}"
trap 'kill 0' EXIT
uv run uvicorn cinemafabric.fabric.app:app --host 127.0.0.1 --port 8100 &
uv run uvicorn cinemafabric.studio.app:app --host 127.0.0.1 --port 8000 &
if [ "$CF_MODE" = "live" ]; then
  for _ in $(seq 1 60); do curl -sf http://127.0.0.1:8100/health >/dev/null && break; sleep 1; done
  CF_JOIN_TOKEN="${CF_JOIN_TOKEN:-$(cat data/join_token)}" uv run python -m cinemafabric.node --join http://127.0.0.1:8100 &
fi
echo "open http://127.0.0.1:8000"
wait
