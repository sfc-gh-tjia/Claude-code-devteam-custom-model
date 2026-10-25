#!/usr/bin/env bash
# Start the inference gateway on 127.0.0.1:$GATEWAY_PORT (default 4000).
#   gateway/start.sh            # foreground
#   gateway/start.sh --daemon   # background; log in .loop/gateway.log, pid in .loop/gateway.pid
# Reads gateway/.env (keys, PROFILE), renders the LiteLLM config from gateway/tiers.toml, validates
# the profile, then starts LiteLLM.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
[ -f "$HERE/.env" ] || { echo "error: copy gateway/.env.example to gateway/.env and add your keys" >&2; exit 1; }
set -a; . "$HERE/.env"; set +a
export GATEWAY_KEY="${GATEWAY_KEY:-sk-devteam-local}"
PORT="${GATEWAY_PORT:-4000}"
command -v litellm >/dev/null || { echo "error: litellm not found: pip install -r gateway/requirements.txt" >&2; exit 1; }
# every key a tier names must be set (a keyless local tier sets api_key_env = "")
missing=$(python3 - "$HERE" <<'PY'
import os, sys
sys.path[:0] = [sys.argv[1]]
from tiers import load_tiers
print(" ".join(sorted({t["api_key_env"] for t in load_tiers().values() if t["api_key_env"] and not os.environ.get(t["api_key_env"])})))
PY
)
[ -z "$missing" ] || { echo "error: set these keys in gateway/.env (or remove the tiers that use them): $missing" >&2; exit 1; }
CFG="$HERE/.litellm.rendered.yaml"
python3 "$HERE/render.py" --out "$CFG"
cd "$HERE"   # LiteLLM resolves `callbacks: hooks.normalize` relative to the config's directory
if [ "${1:-}" = "--daemon" ]; then
  mkdir -p "$ROOT/.loop"
  nohup litellm --config "$CFG" --host 127.0.0.1 --port "$PORT" > "$ROOT/.loop/gateway.log" 2>&1 &
  echo $! > "$ROOT/.loop/gateway.pid"
  for _ in $(seq 1 60); do
    curl -fs "http://127.0.0.1:$PORT/health/liveliness" >/dev/null 2>&1 && { echo "gateway up on :$PORT (pid $(cat "$ROOT/.loop/gateway.pid"), profile ${PROFILE:-baseline})"; exit 0; }
    sleep 1
  done
  echo "error: gateway did not come up; see .loop/gateway.log" >&2; tail -20 "$ROOT/.loop/gateway.log" >&2; exit 1
fi
exec litellm --config "$CFG" --host 127.0.0.1 --port "$PORT"
