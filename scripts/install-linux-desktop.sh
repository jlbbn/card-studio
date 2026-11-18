#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

rm -f "$BIN_DIR/swift-ledger-card-studio"
ln -s "$ROOT/swift-ledger-card-studio" "$BIN_DIR/swift-ledger-card-studio"

install -m 0644 \
  "$ROOT/us.swiftledger.CardStudio.desktop" \
  "$APP_DIR/us.swiftledger.CardStudio.desktop"

install -m 0644 \
  "$ROOT/assets/swift-ledger-card-studio.png" \
  "$ICON_DIR/us.swiftledger.CardStudio.png"

rm -f "$APP_DIR/swift-ledger-card-studio.desktop"

update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

printf 'Launcher: %s\n' "$BIN_DIR/swift-ledger-card-studio"
printf 'Desktop:  %s\n' "$APP_DIR/us.swiftledger.CardStudio.desktop"
printf 'Icon:     %s\n' "$ICON_DIR/us.swiftledger.CardStudio.png"
