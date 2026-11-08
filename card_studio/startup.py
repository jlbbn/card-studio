from __future__ import annotations

import platform as host_platform
import tempfile
from pathlib import Path
from typing import Any

from .applet import APPLET_AID_CANDIDATES, CANONICAL_AID, PHYSICAL_VALIDATION
from .config import SettingsStore
from .hardware_validation import HardwareValidator
from .model import CardError
from .pcsc import PcscBackend, create_pcsc_backend
from .system import evidence_directory, platform_name


def initialize_application(
    settings_store: SettingsStore | None = None,
    evidence_path: Path | None = None,
    reader_provider: Any | None = None,
) -> dict[str, Any]:
    settings = (settings_store or SettingsStore()).load()
    candidates_valid = len(APPLET_AID_CANDIDATES) == 2 and len({item.aid for item in APPLET_AID_CANDIDATES}) == 2
    subsystem_available = all(
        hasattr(PcscBackend, method)
        for method in ["listReaders", "connect", "waitForCard", "getATR", "transmit", "selectApplet", "disconnect"]
    ) and hasattr(HardwareValidator, "run")
    directory = evidence_path or evidence_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(prefix=".write-check-", dir=directory, delete=True):
        pass
    readers: list[str] = []
    pcsc_available = False
    pcsc_error = None
    try:
        readers = create_pcsc_backend(reader_provider=reader_provider).listReaders()
        pcsc_available = True
    except CardError as error:
        pcsc_error = error.code
    return {
        "settings": settings,
        "platform": platform_name(),
        "architecture": host_platform.machine(),
        "configuration_valid": candidates_valid,
        "aid_candidates": [{"aid": item.aid, "source": item.source} for item in APPLET_AID_CANDIDATES],
        "canonical_aid": CANONICAL_AID,
        "physical_validation": PHYSICAL_VALIDATION,
        "hardware_subsystem_available": subsystem_available,
        "hardware_validation_available": subsystem_available,
        "evidence_directory": str(directory),
        "evidence_writable": True,
        "pcsc_available": pcsc_available,
        "pcsc_error": pcsc_error,
        "reader_names": readers,
        "software_ready": candidates_valid and subsystem_available and pcsc_available,
    }
