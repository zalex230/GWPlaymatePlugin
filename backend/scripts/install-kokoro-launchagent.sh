#!/usr/bin/env bash
set -euo pipefail

KOKORO_REPO_URL="${KOKORO_REPO_URL:-https://github.com/remsky/Kokoro-FastAPI.git}"
KOKORO_ROOT="${KOKORO_ROOT:-$HOME/Documents/Kokoro-FastAPI}"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
LABEL="${LABEL:-com.gwplaymate.kokoro-fastapi}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${PORT:-8880}"
HOST="${HOST:-127.0.0.1}"

if [[ ! -x "$UV_BIN" ]]; then
  echo "Installing uv to $HOME/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if [[ ! -d "$KOKORO_ROOT/.git" ]]; then
  git clone "$KOKORO_REPO_URL" "$KOKORO_ROOT"
fi

cd "$KOKORO_ROOT"
mkdir -p logs "$HOME/Library/LaunchAgents"

"$UV_BIN" venv --python 3.10
"$UV_BIN" pip install -e ".[cpu]"

USE_GPU=false \
USE_ONNX=false \
PYTHONPATH="$KOKORO_ROOT:$KOKORO_ROOT/api" \
MODEL_DIR=src/models \
VOICES_DIR=src/voices/v1_0 \
WEB_PLAYER_PATH="$KOKORO_ROOT/web" \
"$UV_BIN" run --no-sync python docker/scripts/download_model.py --output api/src/models/v1_0

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$UV_BIN</string>
    <string>run</string>
    <string>--no-sync</string>
    <string>uvicorn</string>
    <string>api.src.main:app</string>
    <string>--host</string>
    <string>$HOST</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$KOKORO_ROOT</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$KOKORO_ROOT/logs/kokoro-fastapi.launchd.log</string>

  <key>StandardErrorPath</key>
  <string>$KOKORO_ROOT/logs/kokoro-fastapi.launchd.err.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>USE_GPU</key>
    <string>false</string>
    <key>USE_ONNX</key>
    <string>false</string>
    <key>PYTHONPATH</key>
    <string>$KOKORO_ROOT:$KOKORO_ROOT/api</string>
    <key>MODEL_DIR</key>
    <string>src/models</string>
    <key>VOICES_DIR</key>
    <string>src/voices/v1_0</string>
    <key>WEB_PLAYER_PATH</key>
    <string>$KOKORO_ROOT/web</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
</dict>
</plist>
PLIST

plutil -lint "$PLIST_PATH"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Kokoro-FastAPI LaunchAgent installed: $LABEL"
echo "Health check: http://$HOST:$PORT/health"
