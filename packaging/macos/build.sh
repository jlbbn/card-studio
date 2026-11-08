#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "El APP y el DMG deben compilarse en macOS; PyInstaller no genera binarios cruzados." >&2
  exit 2
fi

HOST_ARCH="$(uname -m)"
TARGET_ARCH="${CARD_STUDIO_ARCH:-$HOST_ARCH}"
if [[ "$TARGET_ARCH" != "arm64" && "$TARGET_ARCH" != "x86_64" ]]; then
  echo "Arquitectura no admitida: $TARGET_ARCH" >&2
  exit 1
fi
if [[ "$TARGET_ARCH" != "$HOST_ARCH" ]]; then
  echo "Compila en una Mac $TARGET_ARCH o usa esa arquitectura nativamente; host actual: $HOST_ARCH" >&2
  exit 1
fi

for command in brew hdiutil codesign swig; do
  command -v "$command" >/dev/null || { echo "Falta $command" >&2; exit 1; }
done

BREW_PYTHON="$(brew --prefix python)/bin/python3"
if [[ ! -x "$BREW_PYTHON" ]]; then
  echo "No se encontró el Python de Homebrew descrito en docs/MACOS.md" >&2
  exit 1
fi
"$BREW_PYTHON" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' || {
  echo "Faltan PyGObject, GTK4 o libadwaita de Homebrew." >&2
  exit 1
}

VERSION=$("$BREW_PYTHON" -c 'from card_studio import __version__; print(__version__)')
VENV="$ROOT/.venv-macos-$TARGET_ARCH"
"$BREW_PYTHON" -m venv --system-site-packages "$VENV"
PYTHON="$VENV/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check -r requirements.txt -r requirements-build.txt
"$PYTHON" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); import smartcard'
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" scripts/validate_applet_hardware.py --dry-run --evidence "$ROOT/dist/dry-run-$TARGET_ARCH.json"

DIST="$ROOT/dist/macos-$TARGET_ARCH"
WORK="$ROOT/build/macos-$TARGET_ARCH"
mkdir -p "$DIST" "$WORK"
"$PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$DIST" \
  --workpath "$WORK" \
  packaging/macos/card-studio.spec

APP="$DIST/Swift Ledger Card Studio.app"
if [[ ! -d "$APP" ]]; then
  echo "PyInstaller no generó el APP esperado." >&2
  exit 1
fi
if [[ "${CARD_STUDIO_UNSIGNED:-0}" == "1" ]]; then
  echo "CODE_SIGNING = NOT_CONFIGURED"
  echo "NOTARIZATION = NOT_CONFIGURED"
else
  codesign --force --deep --sign "${CODESIGN_IDENTITY:--}" "$APP"
  codesign --verify --deep --strict "$APP"
fi

STAGE="$DIST/dmg-content"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$ROOT/docs/MACOS.md" "$STAGE/LEEME.md"
DMG_NAME="Card-Studio-$VERSION-macOS-$TARGET_ARCH.dmg"
hdiutil create -volname "Swift Ledger Card Studio" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$DMG_NAME"

RELEASE_DIR="$ROOT/release/$VERSION/macos-$TARGET_ARCH"
mkdir -p "$RELEASE_DIR"
cp "$DIST/$DMG_NAME" "$RELEASE_DIR/$DMG_NAME"
cp "$ROOT/packaging/macos/RELEASE_NOTES.md" "$RELEASE_DIR/RELEASE_NOTES.md"
(cd "$RELEASE_DIR" && shasum -a 256 "$DMG_NAME" > checksums.txt)

printf '\nAPP: %s\nDMG: %s\nRELEASE: %s\n' "$APP" "$DIST/$DMG_NAME" "$RELEASE_DIR/$DMG_NAME"
