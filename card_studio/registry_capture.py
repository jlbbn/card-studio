from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import CardError
from .store import write_json_atomic


SCHEMA = "globalplatform-registry-evidence/v2"

CARDLAB_PACKAGE_AID = "D4534C504D47"
CARDLAB_APPLET_AID = "D4534C504D4701"

CONFIRMATION = "LEER_REGISTRO_SIN_MODIFICAR"

FORBIDDEN_OPTIONS = {
    "--install",
    "--delete",
    "--delete-key",
    "--uninstall",
    "--force",
    "--default",
    "--lock",
    "--lock-dek",
    "--lock-enc",
    "--lock-kdf",
    "--lock-mac",
    "--put-key",
    "--replace-key",
    "--secure-card",
    "--store-data-raw",
}


def _normalized_aid(value: Any) -> str:
    aid = str(value).strip().upper()

    if not re.fullmatch(r"[0-9A-F]{10,32}", aid):
        raise CardError(
            "INVALID_REGISTRY_AID",
            "El registro contiene un AID inválido.",
        )

    if len(aid) % 2:
        raise CardError(
            "INVALID_REGISTRY_AID",
            "El registro contiene un AID de longitud inválida.",
        )

    return aid


def parse_registry_text(text: str) -> dict[str, Any]:
    entries: dict[tuple[str, str], dict[str, str]] = {}

    for line in text.splitlines():
        match = re.match(
            r"^\s*(PKG|APP):\s+([0-9A-Fa-f]{10,32})\b",
            line,
        )

        if not match:
            continue

        kind = "PACKAGE" if match.group(1) == "PKG" else "APPLET"
        aid = _normalized_aid(match.group(2))

        entries[(kind, aid)] = {
            "type": kind,
            "aid": aid,
        }

    if not entries:
        raise CardError(
            "EMPTY_REGISTRY",
            "La salida no contiene paquetes ni applets GlobalPlatform reconocibles.",
        )

    return {
        "schema": SCHEMA,
        "entries": list(entries.values()),
    }


def build_registry_command(
    gp_executable: str,
    reader: str = "",
) -> list[str]:
    command = [
        gp_executable,
        "--info",
        "--list",
    ]

    if reader.strip():
        command.extend(["--reader", reader.strip()])

    if FORBIDDEN_OPTIONS.intersection(command):
        raise CardError(
            "FORBIDDEN_REGISTRY_OPTION",
            "El comando contiene una opción que puede modificar la tarjeta.",
        )

    return command


def summarize_cardlab(
    entries: list[dict[str, str]],
) -> dict[str, Any]:
    pairs = {
        (item["type"], item["aid"])
        for item in entries
    }

    package_present = (
        "PACKAGE",
        CARDLAB_PACKAGE_AID,
    ) in pairs

    applet_present = (
        "APPLET",
        CARDLAB_APPLET_AID,
    ) in pairs

    return {
        "package_aid": CARDLAB_PACKAGE_AID,
        "applet_aid": CARDLAB_APPLET_AID,
        "package_present": package_present,
        "applet_present": applet_present,
        "cardlab_registry_confirmed": (
            package_present and applet_present
        ),
    }


def detect_authentication_signal(text: str) -> str:
    lowered = text.lower()

    if (
        "no keys given" in lowered
        and "defaulting to" in lowered
    ):
        return "GPPRO_DEFAULT_KEY_FALLBACK_OBSERVED"

    return "NOT_OBSERVED"


def capture_registry(
    evidence_path: Path,
    *,
    reader: str = "",
    gp_executable: str | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    executable = gp_executable or shutil.which("gp")

    if not executable:
        raise CardError(
            "GP_NOT_FOUND",
            "No se encontró GlobalPlatformPro en PATH.",
        )

    command = build_registry_command(
        executable,
        reader,
    )

    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    except subprocess.TimeoutExpired as error:
        raise CardError(
            "REGISTRY_TIMEOUT",
            "La lectura del registro excedió el tiempo permitido; no se reintentará.",
        ) from error

    except OSError as error:
        raise CardError(
            "REGISTRY_COMMAND_START_FAILED",
            "No se pudo iniciar GlobalPlatformPro.",
        ) from error

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    combined = stdout + "\n" + stderr

    if completed.returncode != 0:
        raise CardError(
            "REGISTRY_READ_FAILED",
            "GlobalPlatformPro no pudo completar la lectura del registro.",
        )

    try:
        parsed = parse_registry_text(combined)

    except CardError as error:
        raise CardError(
            "REGISTRY_PARSE_FAILED",
            "GlobalPlatformPro respondió, pero no se reconoció un registro válido.",
        ) from error

    cardlab = summarize_cardlab(parsed["entries"])

    report = {
        "schema": SCHEMA,
        "mode": "LIVE_READ_ONLY",
        "captured_at": (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "entries": parsed["entries"],
        "reader_supplied": bool(reader.strip()),
        "command": command,
        "authentication_signal": detect_authentication_signal(
            combined
        ),
        "authentication_secret_recorded": False,
        "secure_channel": "UNDETERMINED",
        "physical_modification": "NOT_EXECUTED",
        "cardlab": cardlab,
        "result": (
            "CARDLAB_REGISTRY_CONFIRMED"
            if cardlab["cardlab_registry_confirmed"]
            else "REGISTRY_READ_PASS"
        ),
    }

    write_json_atomic(
        evidence_path,
        report,
    )

    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Captura GlobalPlatform estrictamente de lectura"
        )
    )

    result.add_argument(
        "--execute",
        action="store_true",
    )

    result.add_argument(
        "--reader",
        default="",
    )

    result.add_argument(
        "--confirm-registry",
        default="",
    )

    result.add_argument(
        "--evidence",
        type=Path,
        required=True,
    )

    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    if (
        not args.execute
        or args.confirm_registry != CONFIRMATION
    ):
        print(
            json.dumps(
                {
                    "error": "REGISTRY_CONFIRMATION_REQUIRED",
                    "required_confirmation": CONFIRMATION,
                    "physical_modification": "NOT_EXECUTED",
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        return 2

    try:
        report = capture_registry(
            args.evidence,
            reader=args.reader,
        )

        print(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
            )
        )

        return 0

    except CardError as error:
        print(
            json.dumps(
                {
                    "error": error.code,
                    "message": error.message,
                    "physical_modification": "NOT_EXECUTED",
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        return 2


if __name__ == "__main__":
    raise SystemExit(main())
