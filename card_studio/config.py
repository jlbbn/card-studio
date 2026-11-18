from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ledger import normalize_base_url
from .store import write_json_atomic
from .system import config_directory


@dataclass
class AppSettings:
    ledger_url: str = "http://127.0.0.1:5300"
    terminal_code: str = ""
    sunmi_url: str = "http://127.0.0.1:5380"
    mode: str = "virtual"
    reader: str = ""
    hardware_validation_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledgerUrl": self.ledger_url,
            "terminalCode": self.terminal_code,
            "sunmiUrl": self.sunmi_url,
            "mode": self.mode,
            "reader": self.reader,
            "hardwareValidationEnabled": self.hardware_validation_enabled,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AppSettings":
        mode = str(value.get("mode", "virtual"))
        if mode not in {"virtual", "pcsc", "cardpeek", "sunmi"}:
            mode = "virtual"
        return cls(
            ledger_url=normalize_base_url(str(value.get("ledgerUrl", cls.ledger_url))),
            terminal_code=str(value.get("terminalCode", "")).strip(),
            sunmi_url=normalize_base_url(str(value.get("sunmiUrl", cls.sunmi_url))),
            mode=mode,
            reader=str(value.get("reader", "")).strip(),
            hardware_validation_enabled=value.get("hardwareValidationEnabled", True) is not False,
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
        return settings

    def save(self, settings: AppSettings) -> None:
        normalized = AppSettings.from_dict(settings.to_dict())
        write_json_atomic(self.path, {"version": 2, **normalized.to_dict()})
