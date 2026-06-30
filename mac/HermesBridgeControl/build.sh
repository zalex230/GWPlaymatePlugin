#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/build/Hermes Bridge Control.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR"

swiftc \
  -O \
  -framework AppKit \
  "$SCRIPT_DIR/Sources/main.swift" \
  -o "$MACOS_DIR/HermesBridgeControl"

cp "$SCRIPT_DIR/Info.plist" "$CONTENTS_DIR/Info.plist"
chmod +x "$MACOS_DIR/HermesBridgeControl"

echo "$APP_DIR"
