#!/usr/bin/env bash
# One-command Free Cloudflare Quick Tunnel launcher for ZYS GoldFields PRO Intake Portal.
# Usage (on any standard Linux/macOS host with outbound UDP/TCP 7844 enabled):
#   ./start_public_tunnel.sh
set -euo pipefail

PORT="${PORT:-8080}"
PYTHON_BIN="./venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

if ! curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  echo "[1/2] Starting ZYS GoldFields PRO Intake Portal on 0.0.0.0:${PORT}..."
  PUBLIC_MODE=1 "$PYTHON_BIN" intake_app.py --host 0.0.0.0 --port "$PORT" &
  SERVER_PID=$!
  trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
  sleep 2
else
  echo "[1/2] Intake Portal already healthy on port ${PORT}."
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "[info] Installing cloudflared into ./bin/cloudflared..."
  mkdir -p ./bin
  ARCH="$(uname -m)"
  OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
  if [ "$OS" = "darwin" ]; then
    curl -fsSL -o ./bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
  else
    BIN_ARCH="amd64"
    [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ] && BIN_ARCH="arm64"
    curl -fsSL -o ./bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${BIN_ARCH}"
    chmod +x ./bin/cloudflared
  fi
  export PATH="$PWD/bin:$PATH"
fi

echo "[2/2] Opening free public HTTPS Cloudflare Quick Tunnel (*.trycloudflare.com)..."
exec cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate
