#!/usr/bin/env bash
set -euo pipefail

SOURCE_ONLY=0
if [[ "${1:-}" == "--source-only" ]]; then
    SOURCE_ONLY=1
fi

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
DMG="$ROOT/Card-Studio-P4-PACO.dmg"
MANIFEST="$ROOT/release-manifest.json"
APP="${CARD_STUDIO_APP:-/Applications/Card Studio.app}"
if [[ ! -d "$APP" && -d "$HOME/Applications/Card Studio.app" ]]; then APP="$HOME/Applications/Card Studio.app"; fi
if [[ ! -d "$APP" && -d "$ROOT/Card Studio.app" ]]; then APP="$ROOT/Card Studio.app"; fi
WORK="$(mktemp -d "${TMPDIR:-/tmp}/card-studio-paco-verify.XXXXXXXX")"
trap 'rm -rf "$WORK"' EXIT
FAIL=0
fail() { 
if [[ "$SOURCE_ONLY" == "1" ]]; then
    ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

    python3 - <<'PY2'
import json
from pathlib import Path

root = Path.cwd()

required = [
    "README-PACO.md",
    "paco_release.py",
    "build-macos.sh",
    "compare-release.sh",
    "verify-release.sh",
    "card-studio-paco.spec",
    "release-manifest.json",
    "source-freeze.json",
    "SBOM.json",
    "requirements.txt",
    "requirements-build.txt",
    "requirements-p3-lock.txt",
    "requirements-p4-lock.txt",
    "requirements-paco-macos.lock",
    "requirements-postgres.txt",
    "config.example",
    ".env.example",
    "source/TRANSFER-MANIFEST.json",
]

missing = [name for name in required if not (root / name).exists()]
if missing:
    raise SystemExit("SOURCE_REQUIRED_MISSING: " + ", ".join(missing))

for name in (
    "release-manifest.json",
    "source-freeze.json",
    "SBOM.json",
    "source/TRANSFER-MANIFEST.json",
    "evidence/clean-source-validation.json",
    "evidence/verifier-self-test.json",
):
    json.loads((root / name).read_text(encoding="utf-8"))

print("SOURCE_STRUCTURE: PASS")
print("SOURCE_JSON: PASS")
PY2

    python3 -m py_compile "$ROOT/paco_release.py"

    bash -n "$ROOT/build-macos.sh"
    bash -n "$ROOT/compare-release.sh"
    bash -n "$ROOT/verify-release.sh"

    if ! ROOT="$ROOT" python3 - <<'PY3'
import os
from pathlib import Path

root = Path(os.environ["ROOT"])

patterns = (
    "/home/admin1",
    "/home/",
    "/tmp/card-studio",
    "release/paco/source/",
)

allowed_suffixes = {
    ".py",
    ".sh",
    ".spec",
    ".example",
    ".txt",
    ".json",
    ".md",
    ".lock",
}

leaks = []

for path in root.rglob("*"):
    if not path.is_file():
        continue

    if path.name in {"verify-release.sh", "paco_release.py"}:
        continue

    if path.suffix not in allowed_suffixes:
        continue

    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue

    for pattern in patterns:
        if pattern in text:
            leaks.append((path.relative_to(root), pattern))

if leaks:
    for path, pattern in leaks:
        print(f"PATH_LEAK: {path}: {pattern}")
    raise SystemExit(1)

print("SOURCE_PATH_SCAN: PASS")
PY3
    then
        echo "[FAIL] Local path leak detected"
        exit 1
    fi

    echo
    echo "CARD STUDIO PACO SOURCE-ONLY VERIFICATION"
    echo "Source structure: PASS"
    echo "JSON metadata: PASS"
    echo "Tooling syntax: PASS"
    echo "Local path leaks: PASS"
    exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[FAIL] Full release verification requires macOS"
    echo "Use: $0 --source-only"
    exit 2
fi

echo "[FAIL] $1" >&2; FAIL=1; }
pass() { echo "[PASS] $1"; }

[[ "$(uname -s)" == "Darwin" ]] || fail "Se requiere macOS"
[[ -f "$DMG" ]] || fail "DMG ausente"
[[ -f "$MANIFEST" ]] || fail "Manifest ausente"
DMG_HASH=FAIL
MANIFEST_HASH=FAIL
INTEGRITY=FAIL
if [[ -f "$DMG" && -f "$ROOT/SHA256SUMS" ]] && (cd "$ROOT" && grep 'Card-Studio-P4-PACO.dmg$' SHA256SUMS | shasum -a 256 -c - >/dev/null); then DMG_HASH=PASS; else fail "Hash DMG"; fi
if [[ -f "$MANIFEST" && -f "$ROOT/SHA256SUMS" ]] && (cd "$ROOT" && grep 'release-manifest.json$' SHA256SUMS | shasum -a 256 -c - >/dev/null); then MANIFEST_HASH=PASS; else fail "Hash manifest"; fi
if (cd "$ROOT" && shasum -a 256 -c SHA256SUMS >/dev/null); then INTEGRITY=PASS; else fail "Integridad general"; fi
if [[ -f "$MANIFEST" ]]; then
  [[ "$(/usr/bin/plutil -extract version raw -o - "$MANIFEST")" == "1.2.0-p4-rc1" ]] || fail "Versión manifest"
  [[ "$(/usr/bin/plutil -extract git_commit raw -o - "$MANIFEST")" == "296fa89457590903488e0055953cbd1d171f52f0" ]] || fail "Commit manifest"
fi

APP_STATUS=FAIL
DEPENDENCIES=FAIL
EXECUTABLE="$APP/Contents/MacOS/Card Studio"
if [[ -d "$APP" && -x "$EXECUTABLE" ]]; then
  APP_STATUS=PASS
  [[ "$(/usr/bin/plutil -extract CardStudioReleaseVersion raw -o - "$APP/Contents/Info.plist")" == "1.2.0-p4-rc1" ]] || fail "Versión APP"
  [[ "$(/usr/bin/plutil -extract CardStudioSourceCommit raw -o - "$APP/Contents/Info.plist")" == "296fa89457590903488e0055953cbd1d171f52f0" ]] || fail "Commit APP"
  [[ -f "$APP/Contents/Resources/release/SBOM.json" ]] || fail "SBOM APP"
  [[ -f "$APP/Contents/Resources/release/bundle-manifest.json" ]] || fail "Bundle manifest APP"
  [[ -f "$APP/Contents/Resources/config/config.example" ]] || fail "Configuración de ejemplo"
  [[ -f "$APP/Contents/Resources/config/.env.example" ]] || fail "Ejemplo .env"
  [[ ! -f "$APP/Contents/Resources/config/.env" ]] || fail "Se incluyó .env real"
  if HOME="$WORK/home" "$EXECUTABLE" --cli kernel readiness > "$WORK/kernel.json" && HOME="$WORK/home" "$EXECUTABLE" --cli hardware-validation --dry-run --evidence "$WORK/dry-run-evidence.json" > "$WORK/dry-run.json"; then
    DEPENDENCIES=PASS
    [[ "$(/usr/bin/plutil -extract kernels.0.ready raw -o - "$WORK/kernel.json")" == "true" ]] || fail "InternalEmvKernel"
    [[ "$(/usr/bin/plutil -extract mode raw -o - "$WORK/dry-run.json")" == "SIMULATED" ]] || fail "Modo dry-run"
  else
    fail "Runtime, ReaderAdapter o dry-run"
  fi
  if grep -R -a -E -l '/home/|/payment/card-studio|/tmp/card-studio|\.venv-paco' "$APP" > "$WORK/absolute-paths.txt"; then
    fail "Rutas absolutas inesperadas: $(tr '\n' ' ' < "$WORK/absolute-paths.txt")"
  fi
else
  fail "Application bundle o ejecutable ausente: $APP"
fi

PCSC=UNAVAILABLE
if [[ -x "$EXECUTABLE" ]]; then
  set +e
  HOME="$WORK/home" "$EXECUTABLE" --cli hardware-status > "$WORK/hardware-status.json" 2>/dev/null
  set -e
  if [[ -f "$WORK/hardware-status.json" ]] && [[ "$(/usr/bin/plutil -extract pcsc_available raw -o - "$WORK/hardware-status.json" 2>/dev/null || echo false)" == "true" ]]; then PCSC=READY; fi
fi

printf '\nCARD STUDIO PACO RELEASE VERIFICATION\n'
printf 'Release integrity: %s\n' "$INTEGRITY"
printf 'DMG hash: %s\n' "$DMG_HASH"
printf 'Manifest: %s\n' "$MANIFEST_HASH"
printf 'Application bundle: %s\n' "$APP_STATUS"
printf 'Dependencies: %s\n' "$DEPENDENCIES"
printf 'PCSC environment: %s\n' "$PCSC"
printf 'Hardware validation: NOT EXECUTED\n'
[[ "$FAIL" == "0" ]]
