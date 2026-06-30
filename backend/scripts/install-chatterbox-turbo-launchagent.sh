#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SERVICE_ROOT="${SERVICE_ROOT:-$HOME/Documents/GWPlaymate-Chatterbox-Turbo}"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
LABEL="${LABEL:-com.gwplaymate.chatterbox-turbo}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/$LABEL.plist}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-4123}"
DEVICE="${DEVICE:-auto}"
VOICE_SAMPLE="${VOICE_SAMPLE:-}"

if [[ ! -x "$UV_BIN" ]]; then
  echo "Installing uv to $HOME/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

mkdir -p "$SERVICE_ROOT/logs" "$HOME/Library/LaunchAgents"
cd "$SERVICE_ROOT"

"$UV_BIN" venv --python 3.11
"$UV_BIN" pip install fastapi==0.115.8 uvicorn==0.34.0 pydantic==2.10.6
"$UV_BIN" pip install chatterbox-tts torch torchaudio

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
    <string>$SERVICE_ROOT/.venv/bin/python</string>
    <string>-m</string>
    <string>backend.chatterbox_turbo_service.app</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$SERVICE_ROOT/logs/chatterbox-turbo.launchd.log</string>

  <key>StandardErrorPath</key>
  <string>$SERVICE_ROOT/logs/chatterbox-turbo.launchd.err.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>$REPO_ROOT</string>
    <key>CHATTERBOX_TTS_HOST</key>
    <string>$HOST</string>
    <key>CHATTERBOX_TTS_PORT</key>
    <string>$PORT</string>
    <key>CHATTERBOX_TURBO_DEVICE</key>
    <string>$DEVICE</string>
    <key>CHATTERBOX_TTS_VOICE_SAMPLE</key>
    <string>$VOICE_SAMPLE</string>
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

echo "Chatterbox Turbo LaunchAgent installed: $LABEL"
echo "Health check: http://$HOST:$PORT/health"
