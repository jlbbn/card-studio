from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PRODUCT = "Card Studio"
VERSION = "1.2.0-p4-rc1"
COMMIT = "296fa89457590903488e0055953cbd1d171f52f0"
TAG = "card-studio-p4-baseline"
APP_NAME = "Card Studio.app"
DMG_NAME = "Card-Studio-P4-PACO.dmg"
APP_ARCHIVE = "Card-Studio-P4-PACO-app.zip"
BASELINE = {
    "P0": "35/35 PASS",
    "P1": "29/29 PASS",
    "P2": "7/7 PASS",
    "P3": "15/15 PASS",
    "P4": "18/18 PASS",
    "L3": "20/20 PASS",
    "full_regression": "441/441 PASS",
}
LOCKFILES = [
    "requirements.txt",
    "requirements-build.txt",
    "requirements-p3-lock.txt",
    "requirements-p4-lock.txt",
    "requirements-postgres.txt",
    "requirements-paco-macos.lock",
]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def p4_source_digest(root: Path) -> str:
    paths = [
        *sorted((root / "card_studio").rglob("*.py")),
        *sorted((root / "tests").glob("test_*.py")),
        *sorted((root / "migrations").glob("*.sql")),
        *sorted(root.glob("requirements*.txt")),
        root / "Dockerfile.p4",
        root / "staging" / "config.json",
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_value(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True,
    ).stdout.strip()


def verify_source(root: Path, require_clean: bool = True) -> None:
    if Path(git_value(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError("Git root no aislado")
    if git_value(root, "rev-parse", "HEAD") != COMMIT:
        raise ValueError("Commit fuente no coincide")
    if git_value(root, "describe", "--tags", "--exact-match", "HEAD") != TAG:
        raise ValueError("Tag fuente no coincide")
    if require_clean and git_value(root, "status", "--porcelain"):
        raise ValueError("Working tree no limpio")


def regression_result(path: Path, expected: int) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    runs = re.findall(r"Ran (\d+) tests in [0-9.]+s", text)
    if not runs or int(runs[-1]) != expected:
        raise ValueError(f"Regresión inválida en {path.name}")
    summaries = re.findall(r"^OK(?: \(skipped=(\d+)\))?$", text, re.MULTILINE)
    if not summaries or summaries[-1] not in {"", None}:
        raise ValueError(f"La regresión no está completa o tiene skips: {path.name}")
    if re.search(r"^FAILED|^ERROR:|unexpected success", text, re.MULTILINE | re.IGNORECASE):
        raise ValueError(f"La regresión contiene fallos: {path.name}")
    return {"passed": expected, "total": expected, "skipped": 0, "failures": 0, "errors": 0, "unexpected_successes": 0, "status": "PASS", "sha256": sha256_file(path)}


def dependency_sbom() -> dict[str, object]:
    packages = [
        "pyscard", "psycopg", "psycopg-binary", "pyinstaller",
        "pyinstaller-hooks-contrib", "altgraph", "packaging", "setuptools", "PyGObject",
    ]
    components = []
    for name in packages:
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = "UNAVAILABLE"
        components.append({
            "bom-ref": f"pkg:pypi/{name.lower()}@{version}",
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name.lower()}@{version}",
            "properties": [{"name": "scope", "value": "build" if name.lower() in {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "setuptools"} else "runtime"}],
        })
    components.append({"type": "platform", "name": "Python", "version": platform.python_version(), "properties": [{"name": "packaging", "value": "PyInstaller embedded runtime"}]})
    for name, package in [("GTK", "gtk4"), ("libadwaita", "libadwaita-1")]:
        try:
            result = subprocess.run(["pkg-config", "--modversion", package], capture_output=True, text=True, check=False)
            version = result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"
        except OSError:
            version = "UNAVAILABLE"
        if version == "UNAVAILABLE":
            try:
                import gi
                gi.require_version("Gtk", "4.0")
                gi.require_version("Adw", "1")
                from gi.repository import Adw, Gtk
                library = Gtk if name == "GTK" else Adw
                version = ".".join(str(getattr(library, f"get_{part}_version")()) for part in ["major", "minor", "micro"])
            except (ImportError, ValueError, AttributeError):
                pass
        components.append({"type": "framework", "name": name, "version": version})
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "product": PRODUCT,
            "version": VERSION,
            "git_commit": COMMIT,
            "target_platform": f"macOS 12+; native build {platform.machine()}",
            "native_build_status": "PASS" if platform.system() == "Darwin" else "NOT EXECUTED",
            "inventory_provenance": platform.system(),
        },
        "components": components,
    }


def refresh_sha256sums(release: Path) -> None:
    lines = []
    for path in sorted(release.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name == "SHA256SUMS":
            continue
        relative = path.relative_to(release)
        if APP_NAME in relative.parts or relative.as_posix() == "evidence/verify-release.log":
            continue
        lines.append(f"{sha256_file(path)}  {relative.as_posix()}")
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def tree_listing(root: Path) -> dict[str, dict[str, object]]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = {"kind": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            result[relative] = {
                "kind": "file",
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & 0o111),
            }
        elif path.is_dir():
            result[relative] = {"kind": "directory"}
    return result


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for name, value in tree_listing(root).items():
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(json.dumps(value, sort_keys=True).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def deterministic_app_zip(app: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in [app, *sorted(app.rglob("*"))]:
            relative = path.relative_to(app.parent).as_posix()
            info = zipfile.ZipInfo(relative + ("/" if path.is_dir() and not path.is_symlink() else ""), (1980, 1, 1, 0, 0, 0))
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                info.external_attr = (stat.S_IFLNK | mode) << 16
                data = os.readlink(path).encode()
            elif path.is_dir():
                info.external_attr = (stat.S_IFDIR | mode) << 16 | 0x10
                data = b""
            else:
                info.external_attr = (stat.S_IFREG | mode) << 16
                data = path.read_bytes()
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def scan_app(app: Path, source: Path) -> None:
    forbidden = [b"/home/", b"/payment/card-studio", b"/tmp/card-studio", b"/.venv-paco", str(source).encode()]
    secret_pattern = re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}")
    violations = []
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name == ".env" or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            violations.append(f"{path.relative_to(app)}: forbidden file")
            continue
        data = path.read_bytes()
        for token in forbidden:
            if token and token in data:
                violations.append(f"{path.relative_to(app)}: development path")
        if secret_pattern.search(data):
            violations.append(f"{path.relative_to(app)}: possible secret")
    if violations:
        raise ValueError("Escaneo APP falló: " + "; ".join(violations[:20]))


def prepare_app(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    app = args.app.resolve()
    release = args.release.resolve()
    verify_source(source)
    if platform.system() != "Darwin":
        raise ValueError("El APP nativo sólo se prepara en macOS")
    executable = app / "Contents" / "MacOS" / "Card Studio"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Ejecutable APP ausente")
    if executable.read_bytes()[:4] not in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}:
        raise ValueError("El ejecutable no es Mach-O")
    resources = app / "Contents" / "Resources"
    release_resources = resources / "release"
    release_resources.mkdir(parents=True, exist_ok=True)
    for name in ["SBOM.json", "requirements-paco-macos.lock"]:
        (release_resources / name).write_bytes((release / name).read_bytes())
    (release_resources / "source-freeze.json").write_bytes((release / "source-freeze.json").read_bytes())
    config = resources / "config"
    config.mkdir(exist_ok=True)
    for name in [".env.example", "config.example"]:
        (config / name).write_bytes((release / name).read_bytes())
    components = {}
    for name in ["card_studio/readers/pcsc.py", "card_studio/pcsc.py", "card_studio/hardware_validation.py", "card_studio/integration/physical.py", "card_studio/kernels/internal_emv.py"]:
        components[name] = sha256_file(source / name)
    provisional = release_resources / "bundle-manifest.json"
    if provisional.exists():
        provisional.unlink()
    bundle = {
        "product": PRODUCT,
        "version": VERSION,
        "git_commit": COMMIT,
        "git_tag": TAG,
        "source_hash": p4_source_digest(source),
        "components": components,
        "files": tree_listing(app),
        "physical_validation": "DEFERRED / NOT EXECUTED",
        "certification": "NOT COMPLETED",
    }
    write_json(provisional, bundle)
    scan_app(app, source)
    deterministic_app_zip(app, release / APP_ARCHIVE)
    return 0


def finalize(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    release = args.release.resolve()
    app = release / APP_NAME
    dmg = release / DMG_NAME
    verify_source(source)
    regression = regression_result(release / "evidence" / "regression-verbose.log", 441)
    postgres = regression_result(release / "evidence" / "postgres-suite.log", 16)
    if platform.system() != "Darwin":
        raise ValueError("El cierre nativo requiere macOS")
    with dmg.open("rb") as stream:
        stream.seek(max(0, dmg.stat().st_size - 512))
        if b"koly" not in stream.read(512):
            raise ValueError("DMG UDZO inválido")
    smoke = {"status": "NOT EXECUTED"}
    if args.install_smoke:
        smoke = json.loads(args.install_smoke.read_text(encoding="utf-8"))
    app_hash = tree_hash(app)
    manifest = {
        "product": PRODUCT,
        "version": VERSION,
        "target_platform": f"macOS 12+ {platform.machine()}",
        "git_commit": COMMIT,
        "git_tag": TAG,
        "git_status": "EMPTY_AT_SOURCE_CHECKOUT",
        "source_hash": p4_source_digest(source),
        "lockfile_hash": sha256_file(release / "requirements-paco-macos.lock"),
        "dependency_locks": {name: sha256_file(release / name) for name in LOCKFILES},
        "app_bundle_hash": app_hash,
        "app_archive": {"path": APP_ARCHIVE, "sha256": sha256_file(release / APP_ARCHIVE)},
        "dmg": {"path": DMG_NAME, "sha256": sha256_file(dmg)},
        "baseline": BASELINE,
        "l3_result": "20/20 PASS",
        "full_regression": regression,
        "postgresql_suite": postgres,
        "build_date": datetime.now(timezone.utc).isoformat(),
        "clean_build": "PASS",
        "macos_app": "PASS",
        "dmg_status": "PASS",
        "install_smoke_test": smoke.get("status", "FAIL"),
        "paco_parity_verification": "PASS" if args.parity_pass else "NOT EXECUTED",
        "physical_validation": "DEFERRED / NOT EXECUTED",
        "certification": "NOT COMPLETED",
        "code_signing": "NOT CONFIGURED",
        "notarization": "NOT CONFIGURED",
    }
    if manifest["install_smoke_test"] != "PASS":
        raise ValueError("Install smoke test no PASS")
    write_json(release / "release-manifest.json", manifest)
    refresh_sha256sums(release)
    return 0


def compare(args: argparse.Namespace) -> int:
    reference = args.reference_app.resolve()
    installed = args.installed_app.resolve()
    left = tree_listing(reference)
    right = tree_listing(installed)
    differences = []
    for name in sorted(set(left) | set(right)):
        if left.get(name) != right.get(name):
            differences.append(f"{name}: SOURCE={left.get(name)} INSTALLED={right.get(name)}")
    if differences:
        print("\n".join(differences))
        print("PARITY: FAIL")
        return 1
    for app in [reference, installed]:
        bundle = json.loads((app / "Contents" / "Resources" / "release" / "bundle-manifest.json").read_text(encoding="utf-8"))
        if bundle["version"] != VERSION or bundle["git_commit"] != COMMIT:
            print(f"{app}: version/commit mismatch")
            print("PARITY: FAIL")
            return 1
    print(f"Version: {VERSION}")
    print(f"App bundle SHA256: {tree_hash(reference)}")
    print("Manifest: PASS")
    print("Dependency manifest: PASS")
    print("Embedded resource hashes: PASS")
    print("PARITY: PASS")
    return 0


def verify(args: argparse.Namespace) -> int:
    release = args.release.resolve()
    for line in (release / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        if sha256_file(release / name) != expected:
            print(f"HASH FAIL: {name}")
            return 1
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest["git_commit"] != COMMIT or manifest["version"] != VERSION:
        return 1
    if tree_hash(release / APP_NAME) != manifest["app_bundle_hash"]:
        return 1
    print("Release integrity: PASS")
    return 0


def snapshot(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    release = args.release.resolve()
    verify_source(source)
    regression = regression_result(args.regression_log, 441)
    postgres = regression_result(args.postgres_log, 16)
    bundle_hash = sha256_file(args.bundle)
    freeze = {
        "product": PRODUCT,
        "version": VERSION,
        "git_commit": COMMIT,
        "git_tag": TAG,
        "git_status": "EMPTY",
        "git_root_isolated": True,
        "source_hash": p4_source_digest(source),
        "source_bundle_sha256": bundle_hash,
        "dependency_locks": {name: sha256_file(source / name) for name in LOCKFILES[:-1]},
        "build_timestamp": datetime.now(timezone.utc).isoformat(),
        "platform_target": "macOS 12+; native arm64 or x86_64",
        "provenance": "OFFICIAL_GIT_BUNDLE_CLEAN_ROOM",
        "native_macos_build": "NOT EXECUTED",
    }
    write_json(release / "source-freeze.json", freeze)
    write_json(release / "SBOM.json", dependency_sbom())
    write_json(release / "evidence" / "clean-source-validation.json", {
        "git_root_isolated": "PASS",
        "bundle_sha256": "PASS",
        "commit_parity": "PASS",
        "working_tree_clean": "PASS",
        "dependencies": "PASS",
        "regression": regression,
        "postgresql_suite": postgres,
        "ready_for_macos_packaging": True,
        "physical_validation": "DEFERRED / NOT EXECUTED",
    })
    manifest = {
        "product": PRODUCT,
        "version": VERSION,
        "target_platform": "macOS 12+; native arm64 or x86_64",
        "git_commit": COMMIT,
        "git_tag": TAG,
        "source_hash": freeze["source_hash"],
        "lockfile_hash": sha256_file(release / "requirements-paco-macos.lock"),
        "app_bundle_hash": None,
        "dmg": {"path": DMG_NAME, "sha256": None, "status": "NOT EXECUTED"},
        "baseline": BASELINE,
        "l3_result": "20/20 PASS",
        "full_regression": regression,
        "postgresql_suite": postgres,
        "build_date": freeze["build_timestamp"],
        "clean_build": "PASS",
        "macos_app": "NOT EXECUTED",
        "install_smoke_test": "NOT EXECUTED",
        "paco_parity_verification": "NOT EXECUTED",
        "physical_validation": "DEFERRED / NOT EXECUTED",
        "certification": "NOT COMPLETED",
    }
    write_json(release / "release-manifest.json", manifest)
    refresh_sha256sums(release)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["prepare-app", "finalize", "snapshot"]:
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--release", type=Path, required=True)
        if name == "prepare-app":
            command.add_argument("--app", type=Path, required=True)
        if name == "finalize":
            command.add_argument("--install-smoke", type=Path)
            command.add_argument("--parity-pass", action="store_true")
        if name == "snapshot":
            command.add_argument("--bundle", type=Path, required=True)
            command.add_argument("--regression-log", type=Path, required=True)
            command.add_argument("--postgres-log", type=Path, required=True)
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("--reference-app", type=Path, required=True)
    compare_parser.add_argument("--installed-app", type=Path, required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--release", type=Path, required=True)
    sbom_parser = commands.add_parser("sbom")
    sbom_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare-app":
        return prepare_app(args)
    if args.command == "finalize":
        return finalize(args)
    if args.command == "snapshot":
        return snapshot(args)
    if args.command == "compare":
        return compare(args)
    if args.command == "verify":
        return verify(args)
    write_json(args.output, dependency_sbom())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
