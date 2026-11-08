from __future__ import annotations

import logging
import logging.handlers
import platform as host_platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppSettings
from .ledger import LedgerClient
from .model import CardError
from .pcsc import create_pcsc_backend
from .system import config_directory, data_directory, log_directory, platform_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    detail: str


def configure_logging() -> Path:
    directory = log_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "card-studio.log"
    root = logging.getLogger("card_studio")
    root.setLevel(logging.INFO)
    if not any(isinstance(handler, logging.handlers.RotatingFileHandler) for handler in root.handlers):
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    return path


def _check(name: str, status: str, detail: str) -> DiagnosticCheck:
    return DiagnosticCheck(name=name, status=status, detail=detail)


def collect_diagnostics(
    settings: AppSettings | None = None,
    platform: str | None = None,
    check_api: bool = False,
) -> dict[str, Any]:
    selected = platform_name(platform)
    current = settings or AppSettings()
    checks: list[DiagnosticCheck] = []
    checks.append(_check("Modo virtual", "OK", "Disponible sin lector ni servicios externos"))
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        checks.append(_check("Interfaz GTK4/libadwaita", "OK", f"PyGObject {gi.__version__}"))
    except (ImportError, ValueError) as exc:
        checks.append(_check("Interfaz GTK4/libadwaita", "ERROR", str(exc)))

    try:
        readers = create_pcsc_backend(selected).list_readers()
    except CardError as error:
        checks.append(_check("Lectores PC/SC", "AVISO", f"{error.code}: {error.message}"))
    else:
        detail = ", ".join(readers) if readers else "Sin lectores detectados; el modo virtual sigue disponible"
        checks.append(_check("Lectores PC/SC", "OK" if readers else "AVISO", detail))

    cardpeek = shutil.which("cardpeek")
    checks.append(_check("Cardpeek alternativo", "OK" if cardpeek else "AVISO", cardpeek or "No instalado"))
    adb = shutil.which("adb")
    if adb:
        try:
            completed = subprocess.run(
                [adb, "devices"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            lines = [line for line in completed.stdout.splitlines()[1:] if line.strip()]
            checks.append(_check("SUNMI por ADB", "OK" if lines else "AVISO", "; ".join(lines) or "Sin dispositivos"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(_check("SUNMI por ADB", "ERROR", str(exc)))
    else:
        checks.append(_check("SUNMI por ADB", "AVISO", "adb no está instalado"))

    if check_api:
        try:
            health = LedgerClient(current.ledger_url).health()
        except Exception as exc:
            logger.warning("Diagnóstico de API fallido: %s", exc)
            checks.append(_check("Swift Ledger sandbox", "ERROR", str(exc)))
        else:
            checks.append(_check("Swift Ledger sandbox", "OK", str(health.get("serverTime", "Disponible"))))

    return {
        "application": "Swift Ledger Card Studio",
        "version": __version__,
        "platform": selected,
        "system": host_platform.system(),
        "architecture": host_platform.machine(),
        "python": sys.version.split()[0],
        "paths": {
            "data": str(data_directory(selected)),
            "config": str(config_directory(selected)),
            "logs": str(log_directory(selected)),
        },
        "checks": [asdict(check) for check in checks],
        "credentialsIncluded": False,
    }


def format_diagnostics(report: dict[str, Any]) -> str:
    lines = [
        f"{report['application']} {report['version']}",
        f"Sistema: {report['platform']} · {report['architecture']} · Python {report['python']}",
        "",
    ]
    lines.extend(f"{check['status']}: {check['name']} — {check['detail']}" for check in report["checks"])
    lines.extend(["", f"Registro: {report['paths']['logs']}"])
    return "\n".join(lines)
