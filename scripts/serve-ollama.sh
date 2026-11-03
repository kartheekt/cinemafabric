#!/usr/bin/env bash
# Share this machine's Ollama with a fabric on another machine, behind a secret-checking gate.
#   ./scripts/serve-ollama.sh                      gate on port 11435, models already pulled
#   ./scripts/serve-ollama.sh 11435 llama3.1:8b    pull a model first
# Ollama itself listens on localhost only (port + 1). The gate's secret is kept in ~/.cinemafabric/gate_secret; give it
# to the fabric as the machine's "secret" in your profile, or paste it when adding the machine in the UI.
set -euo pipefail
PORT="${1:-11435}"
LOCAL_PORT=$((PORT + 1))
HERE="$(cd "$(dirname "$0")" && pwd)"
GATE="${CF_GATE_SCRIPT:-$HERE/../cinemafabric/node/ollama_proxy.py}"
OLLAMA="$(command -v ollama || echo /usr/local/bin/ollama)"
SECRET_FILE="$HOME/.cinemafabric/gate_secret"

for model in "${@:2}"; do
  OLLAMA_HOST="127.0.0.1:$LOCAL_PORT" "$OLLAMA" list | grep -q "^${model}" || OLLAMA_HOST="127.0.0.1:$LOCAL_PORT" "$OLLAMA" pull "$model"
done

if ! lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  OLLAMA_HOST="127.0.0.1:$LOCAL_PORT" OLLAMA_KEEP_ALIVE=30m nohup "$OLLAMA" serve > /tmp/ollama-local.log 2>&1 < /dev/null &
fi

mkdir -p "$(dirname "$SECRET_FILE")"
[ -s "$SECRET_FILE" ] || (umask 077 && python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$SECRET_FILE")
if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  CF_PROXY_SECRET="$(cat "$SECRET_FILE")" nohup python3 "$GATE" --port "$PORT" --target "http://127.0.0.1:$LOCAL_PORT" \
    > /tmp/ollama-gate.log 2>&1 < /dev/null &
fi
command -v caffeinate >/dev/null && { pgrep -f "caffeinate -is" >/dev/null || (nohup caffeinate -is >/dev/null 2>&1 < /dev/null &); }
sleep 2
echo "Ollama on 127.0.0.1:$LOCAL_PORT, gate on 0.0.0.0:$PORT (log: /tmp/ollama-gate.log)"
echo "secret: in $SECRET_FILE (cat it when adding this machine)"
