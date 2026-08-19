#!/usr/bin/env bash
# One-command demo: tunnel up -> server up -> bot into the meeting.
#
#   scripts/run_demo.sh https://meet.google.com/xxx-yyyy-zzz
#
# Needs in .env: RECALL_API_KEY, GEMINI_API_KEY, NGROK_AUTHTOKEN,
# and ideally NGROK_DOMAIN (your free static domain — survives tunnel restarts).
# Emergency fallback (ngrok down / data cap): set TUNNEL=cloudflared.
set -euo pipefail
MEETING_URL=${1:?usage: scripts/run_demo.sh <google-meet-url>}
cd "$(dirname "$0")/.."
set -a; source .env; set +a
mkdir -p runs bin
PORT=${PORT:-8000}
TUNNEL=${TUNNEL:-ngrok}
cleanup() { [ -n "${TUNNEL_PID:-}" ] && kill "$TUNNEL_PID" 2>/dev/null || true; }
trap cleanup EXIT

if [ "$TUNNEL" = "cloudflared" ]; then
  if ! command -v cloudflared >/dev/null && [ ! -x bin/cloudflared ]; then
    echo "downloading cloudflared..."
    curl -sL -o bin/cloudflared \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz
    tar -xzf bin/cloudflared -C bin 2>/dev/null || true
    chmod +x bin/cloudflared
  fi
  CF=$(command -v cloudflared || echo bin/cloudflared)
  "$CF" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > runs/tunnel.log 2>&1 &
  TUNNEL_PID=$!
  echo "waiting for cloudflared quick tunnel..."
  for _ in $(seq 1 30); do
    PUBLIC_BASE_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' runs/tunnel.log | head -1 || true)
    [ -n "$PUBLIC_BASE_URL" ] && break
    sleep 1
  done
else
  if ! command -v ngrok >/dev/null && [ ! -x bin/ngrok ]; then
    echo "downloading ngrok..."
    curl -sL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin-arm64.zip -o bin/ngrok.zip
    unzip -oq bin/ngrok.zip -d bin && rm bin/ngrok.zip
  fi
  NGROK=$(command -v ngrok || echo bin/ngrok)
  : "${NGROK_AUTHTOKEN:?set NGROK_AUTHTOKEN in .env (free account: dashboard.ngrok.com)}"
  "$NGROK" config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null 2>&1 || true
  if [ -n "${NGROK_DOMAIN:-}" ]; then
    "$NGROK" http --domain "$NGROK_DOMAIN" "$PORT" --log stdout --log-level warn > runs/tunnel.log 2>&1 &
  else
    echo "note: no NGROK_DOMAIN set — using an ephemeral URL (a tunnel restart"
    echo "      mid-meeting would orphan the bot; claim your free static domain)."
    "$NGROK" http "$PORT" --log stdout --log-level warn > runs/tunnel.log 2>&1 &
  fi
  TUNNEL_PID=$!
  echo "waiting for ngrok..."
  for _ in $(seq 1 20); do
    PUBLIC_BASE_URL=$(curl -s localhost:4040/api/tunnels 2>/dev/null \
      | python3 -c "import json,sys;ts=json.load(sys.stdin)['tunnels'];print(ts[0]['public_url'] if ts else '')" 2>/dev/null || true)
    [ -n "$PUBLIC_BASE_URL" ] && break
    sleep 1
  done
fi

[ -n "${PUBLIC_BASE_URL:-}" ] || { echo "tunnel failed to start — see runs/tunnel.log"; exit 1; }
export PUBLIC_BASE_URL
echo "tunnel up: $PUBLIC_BASE_URL"
# no exec: the EXIT trap must survive to tear the tunnel down afterwards
./.venv/bin/gladiator run --meeting-url "$MEETING_URL" --port "$PORT"
