from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ledger import normalize_base_url
from .store import write_json_atomic
from .system import config_directory

DEFAULT_REAL_LEDGER_URL = "http://admin1-venus-series.tail7dc5a9.ts.net:5200"
DEFAULT_REAL_BACKEND_ID = "venus-swift-ledger-real"
DEFAULT_REAL_TERMINAL = "VENUS-MAC-001"
LEGACY_SANDBOX_URL = "http://127.0.0.1:5300"


@dataclass
class AppSettings:
    ledger_url: str = DEFAULT_REAL_LEDGER_URL
    terminal_code: str = DEFAULT_REAL_TERMINAL
    sunmi_url: str = "http://127.0.0.1:5380"
    mode: str = "virtual"
    reader: str = ""
    hardware_validation_enabled: bool = True
    ledger_mode: str = "real"
    ledger_backend_id: str = DEFAULT_REAL_BACKEND_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledgerUrl": self.ledger_url,
            "terminalCode": self.terminal_code,
            "sunmiUrl": self.sunmi_url,
            "mode": self.mode,
            "reader": self.reader,
            "hardwareValidationEnabled": self.hardware_validation_enabled,
            "ledgerMode": self.ledger_mode,
            "ledgerBackendId": self.ledger_backend_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AppSettings":
        mode = str(value.get("mode", "virtual"))
        if mode not in {"virtual", "pcsc", "cardpeek", "sunmi"}:
            mode = "virtual"
        ledger_mode = str(value.get("ledgerMode", "real"))
        if ledger_mode not in {"real", "sandbox"}:
            ledger_mode = "real"
        ledger_url = str(value.get("ledgerUrl", cls.ledger_url))
        terminal_code = str(value.get("terminalCode", cls.terminal_code)).strip()
        if "ledgerMode" not in value and ledger_url.rstrip("/") == LEGACY_SANDBOX_URL:
            ledger_url = cls.ledger_url
            if not terminal_code:
                terminal_code = cls.terminal_code
        return cls(
            ledger_url=normalize_base_url(ledger_url),
            terminal_code=terminal_code,
            sunmi_url=normalize_base_url(str(value.get("sunmiUrl", cls.sunmi_url))),
            mode=mode,
            reader=str(value.get("reader", "")).strip(),
            hardware_validation_enabled=value.get("hardwareValidationEnabled", True) is not False,
            ledger_mode=ledger_mode,
            ledger_backend_id=str(value.get("ledgerBackendId", cls.ledger_backend_id)).strip(),
        )


class SettingsStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or config_directory()
        self.path = self.directory / "settings.json"

    def load(self, environ: Mapping[str, str] | None = None) -> AppSettings:
        import json

        values = os.environ if environ is None else environ
        settings = AppSettings()
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("La configuración no contiene un objeto JSON.")
                settings = AppSettings.from_dict(saved)
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("No se pudo leer la configuración local de Card Studio.") from exc
        if values.get("SWIFT_LEDGER_URL"):
            settings.ledger_url = normalize_base_url(values["SWIFT_LEDGER_URL"])
        if values.get("SWIFT_LEDGER_TERMINAL"):
            settings.terminal_code = values["SWIFT_LEDGER_TERMINAL"].strip()
        if values.get("SWIFT_LEDGER_BACKEND_ID"):
            settings.ledger_backend_id = values["SWIFT_LEDGER_BACKEND_ID"].strip()
        if values.get("SWIFT_LEDGER_MODE"):
            if values["SWIFT_LEDGER_MODE"] not in {"real", "sandbox"}:
                raise ValueError("SWIFT_LEDGER_MODE debe ser real o sandbox.")
            settings.ledger_mode = values["SWIFT_LEDGER_MODE"]
        return settings

    def save(self, settings: AppSettings) -> None:
        normalized = AppSettings.from_dict(settings.to_dict())
        write_json_atomic(self.path, {"version": 2, **normalized.to_dict()})
