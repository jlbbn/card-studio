from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from card_studio.store import write_json_atomic


class ConfigMigrationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


DEFAULT_V3_CONFIG: dict[str, Any] = {
    "version": 3,
    "profiles": [],
    "storage": {
        "transactions": "transactions-v1.sqlite3",
        "host": "host-v1.sqlite3",
        "evidence": "evidence/transactions",
    },
    "facade": {
        "transaction_api": "CardStudioApplication",
        "legacy_history": False,
    },
    "simulation": {
        "required": True,
        "mode": "LAB / SYNTHETIC",
        "physical_validation": "NOT_EXECUTED",
        "legacy_profiles_approved": False,
    },
}


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigMigrationError("CONFIG_READ_FAILED", f"No se pudo leer {path.name}.") from exc
    if not isinstance(value, dict):
        raise ConfigMigrationError("INVALID_CONFIG", "La configuración debe ser un objeto JSON.")
    return value


class ConfigMigration:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.legacy_path = config_dir / "settings.json"
        self.path = config_dir / "settings-v3.json"
        self.backup_path = config_dir / "settings-v3.backup.json"

    def migrate(self) -> dict[str, Any]:
        if self.path.exists():
            current = _read_object(self.path)
            if current.get("version") != 3:
                raise ConfigMigrationError("INVALID_CONFIG", "settings-v3.json debe tener version=3.")
            if current.get("profiles") != [] or current.get("simulation", {}).get("required") is not True:
                raise ConfigMigrationError("UNSAFE_CONFIG", "La configuración v3 no cumple el modo de laboratorio.")
            return current
        legacy: dict[str, Any] = {}
        if self.legacy_path.exists():
            legacy = _read_object(self.legacy_path)
            if legacy.get("version") != 2:
                raise ConfigMigrationError(
                    "UNSUPPORTED_CONFIG_VERSION",
                    "Solo se migra explícitamente configuración legacy version=2.",
                )
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json_atomic(self.backup_path, legacy)
        try:
            write_json_atomic(self.path, DEFAULT_V3_CONFIG)
            os.chmod(self.path, 0o600)
            os.chmod(self.backup_path, 0o600)
        except Exception as exc:
            raise ConfigMigrationError("CONFIG_WRITE_FAILED", "No se pudo escribir settings-v3.json.") from exc
        return json.loads(json.dumps(DEFAULT_V3_CONFIG))

    def rollback(self) -> bool:
        if not self.backup_path.exists():
            raise ConfigMigrationError("BACKUP_NOT_FOUND", "No existe backup para rollback.")
        backup = _read_object(self.backup_path)
        if backup.get("version") == 3:
            write_json_atomic(self.path, backup)
        elif self.path.exists():
            self.path.unlink()
        return True


def migrate_v2_to_v3(config_dir: Path) -> dict[str, Any]:
    return ConfigMigration(config_dir).migrate()


def rollback_v3(config_dir: Path) -> bool:
    return ConfigMigration(config_dir).rollback()
