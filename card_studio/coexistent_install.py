from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hardware_validation import write_evidence
from .model import CardError
from .write_exercise import APPLET_AID, PRESERVED_APPLET_AID

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAP = ROOT / "javacard-demo/build-coexistent/payment-demo.cap"
DEFAULT_GP_JAR = ROOT / "vendor/GlobalPlatformPro-next/tool/target/gp.jar"
PACKAGE_AID = "D4534C504D45"
PRESERVED_PACKAGE_AID = "D4534C504D44"
CAP_SHA256 = "d2a0cedc47199977421cede7d441a96827742a3685547461f874380c819c8d93"
LFDB_SHA256 = "e6c4e1bf93637fa8426cea14315fd7207d7c7dca686ab22c2f6f6ca54be3dd52"
CONFIRMATION = "INSTALAR_CAP_COEXISTENTE_SIN_BORRAR"
FORBIDDEN_OPTIONS = {"--delete", "--uninstall", "--force", "--default", "--lock", "--put-key", "--replace-key"}


def _normalized_aid(value: Any) -> str:
    aid = str(value).strip().upper()
    if not re.fullmatch(r"[0-9A-F]{10,32}", aid) or len(aid) % 2:
        raise CardError("INVALID_REGISTRY_AID", "El registro contiene un AID inválido.")
    return aid


def parse_registry_text(text: str) -> dict[str, Any]:
    entries: dict[tuple[str, str], dict[str, str]] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(PKG|APP):\s+([0-9A-Fa-f]{10,32})\b", line)
        if match:
            kind = "PACKAGE" if match.group(1) == "PKG" else "APPLET"
            aid = _normalized_aid(match.group(2))
            entries[(kind, aid)] = {"type": kind, "aid": aid}
    if not entries:
        raise CardError("EMPTY_REGISTRY", "La salida no contiene paquetes ni applets GlobalPlatform reconocibles.")
    return {"schema": "globalplatform-registry-evidence/v1", "mode": "UNCLASSIFIED_TEXT", "entries": list(entries.values())}


def load_registry(path: Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise CardError("REGISTRY_NOT_READABLE", "No se pudo leer la evidencia del registro.") from error
    if not text.lstrip().startswith("{"):
        return parse_registry_text(text)
    try:
        document = json.loads(text)
        entries = document["entries"]
        mode = document["mode"]
        if not isinstance(entries, list) or not entries:
            raise ValueError
        normalized = {
            (str(item["type"]).upper(), _normalized_aid(item["aid"])): {
                "type": str(item["type"]).upper(),
                "aid": _normalized_aid(item["aid"]),
            }
            for item in entries
        }
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise CardError("INVALID_REGISTRY_EVIDENCE", "La evidencia JSON del registro es inválida.") from error
    if any(kind not in {"PACKAGE", "APPLET"} for kind, _ in normalized):
        raise CardError("INVALID_REGISTRY_EVIDENCE", "La evidencia contiene tipos de entrada no admitidos.")
    if mode not in {"SIMULATED", "LIVE_READ_ONLY"}:
        raise CardError("INVALID_REGISTRY_MODE", "La evidencia debe declarar SIMULATED o LIVE_READ_ONLY.")
    if mode == "LIVE_READ_ONLY" and not document.get("captured_at"):
        raise CardError("REGISTRY_TIMESTAMP_REQUIRED", "La evidencia física debe incluir captured_at.")
    return {"schema": "globalplatform-registry-evidence/v1", "mode": mode, "entries": list(normalized.values())}


def validate_registry(registry: dict[str, Any], *, require_live: bool = False) -> dict[str, Any]:
    if require_live and registry["mode"] != "LIVE_READ_ONLY":
        raise CardError("LIVE_REGISTRY_REQUIRED", "La instalación física requiere evidencia LIVE_READ_ONLY con fecha.")
    pairs = {(item["type"], item["aid"]) for item in registry["entries"]}
    if ("PACKAGE", PACKAGE_AID) in pairs or ("APPLET", APPLET_AID) in pairs:
        raise CardError("COEXISTENT_AID_COLLISION", "El paquete o applet coexistible ya aparece en el registro; no se instalará.")
    if ("PACKAGE", PRESERVED_PACKAGE_AID) not in pairs or ("APPLET", PRESERVED_APPLET_AID) not in pairs:
        raise CardError("PRESERVED_AID_MISSING", "El paquete o applet que debe preservarse no aparece completo en el registro.")
    return {
        "registry_mode": registry["mode"],
        "preserved_package_present": True,
        "preserved_applet_present": True,
        "coexistent_package_free": True,
        "coexistent_applet_free": True,
        "result": "PASS",
    }


def inspect_cap(cap_path: Path, gp_jar: Path) -> dict[str, Any]:
    try:
        raw = Path(cap_path).read_bytes()
    except OSError as error:
        raise CardError("CAP_NOT_READABLE", "No se pudo leer el CAP coexistible.") from error
    cap_sha256 = hashlib.sha256(raw).hexdigest()
    if cap_sha256 != CAP_SHA256:
        raise CardError("CAP_HASH_MISMATCH", "El SHA-256 del CAP no coincide con la variante aprobada.")
    command = ["java", "-jar", str(gp_jar), "--cap", str(cap_path)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CardError("CAP_PARSE_FAILED", "GlobalPlatformPro no pudo analizar el CAP.") from error
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise CardError("CAP_PARSE_FAILED", "GlobalPlatformPro rechazó el CAP coexistible.")
    package = re.search(r"^Package:\s+\S+\s+([0-9A-Fa-f]{10,32})\s+v", output, re.MULTILINE)
    applet = re.search(r"^Applet:\s+\S+\s+([0-9A-Fa-f]{10,32})\s*$", output, re.MULTILINE)
    lfdb = re.search(r"^SHA-256\s+([0-9a-f]{64})\s*$", output, re.MULTILINE)
    if not package or _normalized_aid(package.group(1)) != PACKAGE_AID:
        raise CardError("CAP_PACKAGE_AID_MISMATCH", "El paquete dentro del CAP no coincide con el aprobado.")
    if not applet or _normalized_aid(applet.group(1)) != APPLET_AID:
        raise CardError("CAP_APPLET_AID_MISMATCH", "El applet dentro del CAP no coincide con el aprobado.")
    if not lfdb or lfdb.group(1) != LFDB_SHA256:
        raise CardError("CAP_LFDB_MISMATCH", "El LFDB del CAP no coincide con la variante aprobada.")
    return {
        "cap_path": str(cap_path),
        "cap_bytes": len(raw),
        "cap_sha256": cap_sha256,
        "load_file_data_hash_sha256": lfdb.group(1).upper(),
        "package_aid": PACKAGE_AID,
        "applet_aid": APPLET_AID,
        "capfile_parse": "PASS",
    }


def prepare_install(cap_path: Path, gp_jar: Path, registry_path: Path, *, require_live: bool = False) -> dict[str, Any]:
    registry = load_registry(registry_path)
    validation = validate_registry(registry, require_live=require_live)
    cap = inspect_cap(cap_path, gp_jar)
    return {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "PHYSICAL" if require_live else "SIMULATED",
        "cap": cap,
        "registry": validation,
        "physical_install": "NOT_EXECUTED",
        "physical_validation": "PENDING" if require_live else "NOT_EXECUTED",
        "post_install_registry": "REQUIRED_AFTER_PHYSICAL_INSTALL",
        "result": "READY_FOR_AUTHORIZED_PHYSICAL_INSTALL" if require_live else "SIMULATED_READY",
        "errors": [],
    }


def build_install_command(gp_jar: Path, cap_path: Path, reader: str = "") -> list[str]:
    command = [
        "java", "-jar", str(gp_jar),
        "--key-ver", "0x01",
        "--mode", "MAC",
        "--mode", "ENC",
        "--install", str(cap_path),
    ]
    if reader.strip():
        command.extend(["--reader", reader.strip()])
    if FORBIDDEN_OPTIONS.intersection(command):
        raise CardError("FORBIDDEN_INSTALL_OPTION", "El comando contiene una opción prohibida.")
    return command


def redact_output(text: str | bytes, key: str) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    redacted = text
    variants = {key, key.upper(), key.lower()}
    compact = re.sub(r"[^0-9A-Fa-f]", "", key)
    if compact:
        variants.add(compact)
        variants.add(compact.upper())
        variants.add(compact.lower())
        variants.add(" ".join(compact[index:index + 2] for index in range(0, len(compact), 2)))
        variants.add(":".join(compact[index:index + 2] for index in range(0, len(compact), 2)))
    for variant in sorted((item for item in variants if item), key=len, reverse=True):
        redacted = redacted.replace(variant, "[REDACTED]")
    return re.sub(r"(?im)^(\s*(?:GP_KEY|key)\s*[:=]\s*).*$", r"\1[REDACTED]", redacted)


def execute_install(
    cap_path: Path,
    gp_jar: Path,
    registry_path: Path,
    *,
    reader: str = "",
    evidence_path: Path | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    report = prepare_install(cap_path, gp_jar, registry_path, require_live=True)
    key = os.environ.get("GP_KEY", "")
    if not key:
        raise CardError("GP_KEY_REQUIRED", "Define GP_KEY en el entorno sin incluirla en argumentos ni evidencias.")
    command = build_install_command(gp_jar, cap_path, reader)
    try:
        completed = runner(command, capture_output=True, text=True, timeout=120, check=False, env=os.environ.copy())
        stdout = redact_output(completed.stdout, key)
        stderr = redact_output(completed.stderr, key)
        report["command_result"] = {
            "returncode": completed.returncode,
            "stdout_redacted": stdout,
            "stderr_redacted": stderr,
            "reader_supplied": bool(reader.strip()),
        }
        if completed.returncode == 0:
            report["physical_install"] = "EXECUTED_PASS_POSTCHECK_REQUIRED"
            report["physical_validation"] = "INCONCLUSIVE"
            report["result"] = "INSTALL_COMMAND_PASS_POSTCHECK_REQUIRED"
        else:
            report["physical_install"] = "EXECUTED_FAIL"
            report["physical_validation"] = "FAIL"
            report["result"] = "FAIL"
            report["errors"].append("INSTALL_COMMAND_FAILED")
    except subprocess.TimeoutExpired as error:
        report["command_result"] = {
            "stdout_redacted": redact_output(error.stdout or "", key),
            "stderr_redacted": redact_output(error.stderr or "", key),
        }
        report["physical_install"] = "EXECUTED_TIMEOUT_OUTCOME_UNKNOWN"
        report["physical_validation"] = "INCONCLUSIVE"
        report["result"] = "FAIL"
        report["errors"].append("INSTALL_TIMEOUT_QUERY_REQUIRED")
    except OSError:
        report["physical_install"] = "NOT_EXECUTED_COMMAND_START_FAILED"
        report["physical_validation"] = "FAIL"
        report["result"] = "FAIL"
        report["errors"].append("INSTALL_COMMAND_START_FAILED")
    if evidence_path is not None:
        write_evidence(Path(evidence_path), report)
        report["evidence_path"] = str(evidence_path)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validación e instalación del CAP JavaCard coexistible sin borrar el anterior")
    result.add_argument("--cap", type=Path, default=DEFAULT_CAP)
    result.add_argument("--gp-jar", type=Path, default=DEFAULT_GP_JAR)
    result.add_argument("--registry", type=Path, required=True)
    result.add_argument("--execute", action="store_true")
    result.add_argument("--reader", default="")
    result.add_argument("--confirm-install", default="")
    result.add_argument("--evidence", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute and args.confirm_install != CONFIRMATION:
        print(json.dumps({
            "error": "INSTALL_CONFIRMATION_REQUIRED",
            "required_confirmation": CONFIRMATION,
            "physical_install": "NOT_EXECUTED",
        }, ensure_ascii=False, indent=2))
        return 2
    try:
        if args.execute:
            report = execute_install(args.cap, args.gp_jar, args.registry, reader=args.reader, evidence_path=args.evidence)
        else:
            report = prepare_install(args.cap, args.gp_jar, args.registry)
            if args.evidence:
                write_evidence(args.evidence, report)
                report["evidence_path"] = str(args.evidence)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["result"] in {"SIMULATED_READY", "INSTALL_COMMAND_PASS_POSTCHECK_REQUIRED"} else 2
    except CardError as error:
        print(json.dumps({"error": error.code, "message": error.message, "physical_install": "NOT_EXECUTED"}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
