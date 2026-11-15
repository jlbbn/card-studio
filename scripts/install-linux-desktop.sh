#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$BIN_DIR" "$APP_DIR"

ln -sfn \
  "$ROOT/run.sh" \
  "$BIN_DIR/swift-ledger-card-studio"

install -m 0644 \
  "$ROOT/swift-ledger-card-studio.desktop" \
  "$APP_DIR/swift-ledger-card-studio.desktop"

printf 'Launcher: %s\n' "$BIN_DIR/swift-ledger-card-studio"
printf 'Desktop:  %s\n' "$APP_DIR/swift-ledger-card-studio.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi
