#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
INSTALLED="${CARD_STUDIO_APP:-/Applications/Card Studio.app}"
if [[ ! -d "$INSTALLED" && -d "$HOME/Applications/Card Studio.app" ]]; then INSTALLED="$HOME/Applications/Card Studio.app"; fi
REFERENCE="$ROOT/Card Studio.app"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/card-studio-paco-compare.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
if [[ ! -d "$REFERENCE" ]]; then
  ditto -x -k "$ROOT/Card-Studio-P4-PACO-app.zip" "$WORK"
  REFERENCE="$WORK/Card Studio.app"
fi
if command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
elif command -v brew >/dev/null 2>&1; then
  PYTHON="$(brew --prefix python)/bin/python3"
else
  echo "PARITY: FAIL — Python 3 no disponible para calcular hashes" >&2
  exit 1
fi
if [[ ! -d "$INSTALLED" ]]; then
  echo "PARITY: FAIL — APP instalado ausente: $INSTALLED" >&2
  exit 1
fi
"$PYTHON" "$ROOT/paco_release.py" compare --reference-app "$REFERENCE" --installed-app "$INSTALLED"
