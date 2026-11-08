from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

APP_DIRECTORY = "swift-ledger-card-studio"
MACOS_DIRECTORY = "Swift Ledger Card Studio"


def platform_name(value: str | None = None) -> str:
    selected = sys.platform if value is None else value
    if selected == "macos" or selected.startswith("darwin"):
        return "macos"
    if selected.startswith("linux"):
        return "linux"
    return "unsupported"


def _home(home: Path | None) -> Path:
    return home or Path.home()


def data_directory(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("CARD_STUDIO_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if platform_name(platform) == "macos":
        return _home(home) / "Library" / "Application Support" / MACOS_DIRECTORY
    xdg_data = values.get("XDG_DATA_HOME")
    base = Path(xdg_data).expanduser() if xdg_data else _home(home) / ".local" / "share"
    return base / APP_DIRECTORY


def config_directory(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("CARD_STUDIO_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    if platform_name(platform) == "macos":
        return _home(home) / "Library" / "Application Support" / MACOS_DIRECTORY / "Config"
    xdg_config = values.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else _home(home) / ".config"
    return base / APP_DIRECTORY


def log_directory(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("CARD_STUDIO_LOG_DIR")
    if configured:
        return Path(configured).expanduser()
    if platform_name(platform) == "macos":
        return _home(home) / "Library" / "Logs" / MACOS_DIRECTORY
    xdg_state = values.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else _home(home) / ".local" / "state"
    return base / APP_DIRECTORY / "logs"


def evidence_directory(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("CARD_STUDIO_EVIDENCE_DIR")
    if configured:
        return Path(configured).expanduser()
    return data_directory(platform, values, home) / "Evidence" / "applet-validation"
