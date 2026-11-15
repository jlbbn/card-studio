#!/usr/bin/env bash
set -euo pipefail

RELEASE="$(cd -- "$(dirname -- "$0")" && pwd)"
BUNDLE="$RELEASE/source/Card-Studio-P4-PACO.bundle"
EXPECTED_COMMIT="296fa89457590903488e0055953cbd1d171f52f0"
EXPECTED_TAG="card-studio-p4-baseline"
DMG_NAME="Card-Studio-P4-PACO.dmg"
APP_NAME="Card Studio.app"

[[ "$(uname -s)" == "Darwin" ]] || { echo "Este build requiere macOS; no genera binarios cruzados." >&2; exit 2; }
ARCH="$(uname -m)"
[[ "$ARCH" == "arm64" || "$ARCH" == "x86_64" ]] || { echo "Arquitectura no soportada: $ARCH" >&2; exit 1; }
for command in git brew hdiutil codesign swig pkg-config open osascript; do
  command -v "$command" >/dev/null || { echo "Falta la dependencia macOS: $command" >&2; exit 1; }
done
xcode-select -p >/dev/null 2>&1 || { echo "Instala Xcode Command Line Tools." >&2; exit 1; }

export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_INSTALL_CLEANUP=1
packages=(python pkg-config gobject-introspection pygobject3 gtk4 libadwaita swig postgresql@16)
missing=()
for package in "${packages[@]}"; do
  brew list --versions "$package" >/dev/null 2>&1 || missing+=("$package")
done
if (( ${#missing[@]} )); then
  brew install "${missing[@]}"
fi

BREW_PYTHON="$(brew --prefix python)/bin/python3"
PG_PREFIX="$(brew --prefix postgresql@16)"
[[ -x "$BREW_PYTHON" ]] || { echo "Python Homebrew ausente." >&2; exit 1; }
[[ -x "$PG_PREFIX/bin/initdb" ]] || { echo "PostgreSQL 16 Homebrew ausente." >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/card-studio-paco-native.XXXXXXXX")"
PG_STARTED=0
MOUNTED=0
APP_OPEN=0
cleanup() {
  if [[ "$APP_OPEN" == "1" ]]; then osascript -e 'tell application id "us.swiftledger.CardStudio" to quit' >/dev/null 2>&1 || true; fi
  if [[ "$MOUNTED" == "1" ]]; then hdiutil detach "$WORK/mount" -quiet || true; fi
  if [[ "$PG_STARTED" == "1" ]]; then "$PG_PREFIX/bin/pg_ctl" -D "$WORK/pgdata" -m immediate stop >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$RELEASE/evidence"
(cd "$RELEASE" && shasum -a 256 -c source/Card-Studio-P4-PACO.bundle.sha256)
git clone "$BUNDLE" "$WORK/source"
cd "$WORK/source"
git checkout "$EXPECTED_TAG"
[[ "$(git rev-parse --show-toplevel)" == "$WORK/source" ]]
[[ "$(git rev-parse HEAD)" == "$EXPECTED_COMMIT" ]]
[[ -z "$(git status --porcelain)" ]]

"$BREW_PYTHON" -m venv --system-site-packages "$WORK/venv"
PYTHON="$WORK/venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt -r requirements-build.txt -r requirements-p4-lock.txt -r "$RELEASE/requirements-paco-macos.lock"
"$PYTHON" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); import smartcard, psycopg'

export PGPASSWORD='paco-clean-synthetic-only'
printf '%s\n' "$PGPASSWORD" > "$WORK/pg-password"
chmod 600 "$WORK/pg-password"
"$PG_PREFIX/bin/initdb" -D "$WORK/pgdata" -U paco_test --pwfile="$WORK/pg-password" --auth=scram-sha-256 --no-instructions >/dev/null
PGPORT="$("$BREW_PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
mkdir -p "$WORK/pgsocket"
"$PG_PREFIX/bin/pg_ctl" -D "$WORK/pgdata" -l "$WORK/postgres.log" -o "-h 127.0.0.1 -p $PGPORT -k $WORK/pgsocket" start >/dev/null
PG_STARTED=1
"$PG_PREFIX/bin/createdb" -h 127.0.0.1 -p "$PGPORT" -U paco_test paco_clean
"$PG_PREFIX/bin/psql" -h 127.0.0.1 -p "$PGPORT" -U paco_test -d paco_clean -tAc 'SHOW server_version;' > "$RELEASE/evidence/postgres-version.txt"
export P3_TEST_POSTGRES_DSN="postgresql://paco_test:${PGPASSWORD}@127.0.0.1:${PGPORT}/paco_clean"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m unittest discover -s tests -p 'test_p3_persistence.py' -v 2>&1 | tee "$RELEASE/evidence/postgres-suite.log"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m unittest discover -s tests -v 2>&1 | tee "$RELEASE/evidence/regression-verbose.log"
[[ -z "$(git status --porcelain)" ]]
"$PG_PREFIX/bin/pg_ctl" -D "$WORK/pgdata" -m immediate stop >/dev/null
PG_STARTED=0
unset P3_TEST_POSTGRES_DSN PGPASSWORD
rm -f "$WORK/pg-password"

export CARD_STUDIO_SOURCE="$WORK/source"
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath "$WORK/dist" --workpath "$WORK/pyinstaller" "$RELEASE/card-studio-paco.spec"
[[ -d "$WORK/dist/$APP_NAME" ]]
rm -rf "$RELEASE/$APP_NAME"
rm -f "$RELEASE/$DMG_NAME" "$RELEASE/Card-Studio-P4-PACO-app.zip"
ditto "$WORK/dist/$APP_NAME" "$RELEASE/$APP_NAME"
"$PYTHON" - "$RELEASE/source-freeze.json" "$ARCH" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1])
value = json.loads(path.read_text())
value["native_macos_build"] = "PASS"
value["native_build_timestamp"] = datetime.now(timezone.utc).isoformat()
value["platform_target"] = f"macOS 12+ {sys.argv[2]}"
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
"$PYTHON" "$RELEASE/paco_release.py" sbom --output "$RELEASE/SBOM.json"
"$PYTHON" "$RELEASE/paco_release.py" prepare-app --source "$WORK/source" --release "$RELEASE" --app "$RELEASE/$APP_NAME"

mkdir -p "$WORK/home"
HOME="$WORK/home" "$RELEASE/$APP_NAME/Contents/MacOS/Card Studio" --cli kernel readiness > "$RELEASE/evidence/kernel-readiness.json"
HOME="$WORK/home" "$RELEASE/$APP_NAME/Contents/MacOS/Card Studio" --cli hardware-validation --dry-run --evidence "$WORK/dry-run-evidence.json" > "$RELEASE/evidence/dry-run.json"

STAGE="$WORK/dmg-content"
mkdir -p "$STAGE"
ditto "$RELEASE/$APP_NAME" "$STAGE/$APP_NAME"
ln -s /Applications "$STAGE/Applications"
cp "$RELEASE/README-PACO.md" "$STAGE/README-PACO.md"
hdiutil create -volname "Card Studio P4 PACO" -srcfolder "$STAGE" -ov -format UDZO "$RELEASE/$DMG_NAME" >/dev/null

mkdir -p "$WORK/mount" "$WORK/Applications"
hdiutil attach "$RELEASE/$DMG_NAME" -nobrowse -readonly -mountpoint "$WORK/mount" >/dev/null
MOUNTED=1
ditto "$WORK/mount/$APP_NAME" "$WORK/Applications/$APP_NAME"
INSTALLED="$WORK/Applications/$APP_NAME"
(
  cd /
  HOME="$WORK/home" "$INSTALLED/Contents/MacOS/Card Studio" --cli kernel readiness >/dev/null
  HOME="$WORK/home" "$INSTALLED/Contents/MacOS/Card Studio" --cli hardware-validation --dry-run --evidence "$WORK/installed-dry-run.json" >/dev/null
)
open -n "$INSTALLED"
APP_OPEN=1
sleep 8
pgrep -f "$INSTALLED/Contents/MacOS/Card Studio" >/dev/null
osascript -e 'tell application id "us.swiftledger.CardStudio" to quit'
APP_OPEN=0
sleep 3
open -n "$INSTALLED"
APP_OPEN=1
sleep 5
pgrep -f "$INSTALLED/Contents/MacOS/Card Studio" >/dev/null
osascript -e 'tell application id "us.swiftledger.CardStudio" to quit'
APP_OPEN=0
"$PYTHON" - "$RELEASE/evidence/install-smoke.json" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
  "status": "PASS",
  "mount_dmg": "PASS",
  "install_copy": "PASS",
  "cli_smoke": "PASS",
  "launch_close_relaunch": "PASS",
  "source_tree_independent": True,
  "mode": "SIMULATED",
  "physical_validation": "NOT EXECUTED"
}, indent=2, sort_keys=True) + "\n")
PY
CARD_STUDIO_APP="$INSTALLED" "$RELEASE/compare-release.sh" | tee "$RELEASE/evidence/parity.log"
hdiutil detach "$WORK/mount" -quiet
MOUNTED=0
rm -f "$RELEASE/evidence/verify-release.log"
"$PYTHON" "$RELEASE/paco_release.py" finalize --source "$WORK/source" --release "$RELEASE" --install-smoke "$RELEASE/evidence/install-smoke.json" --parity-pass
CARD_STUDIO_APP="$RELEASE/$APP_NAME" "$RELEASE/verify-release.sh" | tee "$RELEASE/evidence/verify-release.log"
"$PYTHON" "$RELEASE/paco_release.py" verify --release "$RELEASE"

printf '\nBUILD COMPLETE\nAPP: %s\nDMG: %s\nSHA256: %s\n' "$RELEASE/$APP_NAME" "$RELEASE/$DMG_NAME" "$(shasum -a 256 "$RELEASE/$DMG_NAME" | awk '{print $1}')"
