from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .model import CardError
from .system import data_directory


def default_cardlab_evidence() -> Path:
    return data_directory() / "Evidence" / "cardlab" / "validate-all.json"


def legacy_cardlab_evidence() -> Path:
    return Path.home() / "chip-id" / "artifacts" / "validation" / "validate-all.json"


def resolve_cardlab_evidence(evidence_path: Path | None = None) -> Path:
    if evidence_path is not None:
        return Path(evidence_path)
    configured = os.environ.get("CARD_STUDIO_CARDLAB_EVIDENCE")
    if configured:
        return Path(configured).expanduser()
    primary = default_cardlab_evidence()
    legacy = legacy_cardlab_evidence()
    return legacy if not primary.is_file() and legacy.is_file() else primary


DEFAULT_EVIDENCE = default_cardlab_evidence()

EXPECTED_SCHEMA = "card-studio-validation/1.0"
EXPECTED_PACKAGE_AID = "D4534C504D47"
EXPECTED_APPLET_AID = "D4534C504D4701"


def load_cardlab_status(
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    path = resolve_cardlab_evidence(evidence_path)

    if not path.is_file():
        raise CardError(
            "CARDLAB_EVIDENCE_NOT_FOUND",
            f"No existe evidencia CardLab: {path}. No se ejecutó una validación nueva.",
        )

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CardError(
            "CARDLAB_EVIDENCE_INVALID",
            "La evidencia CardLab no contiene JSON válido.",
        ) from error

    if value.get("schema") != EXPECTED_SCHEMA:
        raise CardError(
            "CARDLAB_SCHEMA_MISMATCH",
            f"Schema inesperado: {value.get('schema')}",
        )

    if value.get("package_aid") != EXPECTED_PACKAGE_AID:
        raise CardError(
            "CARDLAB_PACKAGE_AID_MISMATCH",
            f"Package AID inesperado: {value.get('package_aid')}",
        )

    if value.get("applet_aid") != EXPECTED_APPLET_AID:
        raise CardError(
            "CARDLAB_APPLET_AID_MISMATCH",
            f"Applet AID inesperado: {value.get('applet_aid')}",
        )

    software_checks = {
        "regression": value.get("regression"),
        "cap": value.get("cap"),
        "verifycap": value.get("verifycap"),
        "jcardsim": value.get("jcardsim"),
        "container": value.get("container"),
    }

    physical = value.get("physical") or {}

    physical_checks = {
        "select": physical.get("select"),
        "get_info": physical.get("get_info"),
        "get_status": physical.get("get_status"),
        "read_container": physical.get("read_container"),
    }

    software_pass = all(
        status == "PASS"
        for status in software_checks.values()
    )

    physical_pass = all(
        status == "PASS"
        for status in physical_checks.values()
    )

    confirmed = (
        value.get("result") == "PASS"
        and value.get("state") == "PHYSICALLY_CONFIRMED"
        and software_pass
        and physical_pass
    )

    return {
        "schema": "card-studio-cardlab-status/v1",
        "status": "PASS" if confirmed else "FAIL",
        "state": value.get("state", "UNKNOWN"),
        "package_aid": value.get("package_aid"),
        "applet_aid": value.get("applet_aid"),
        "software": software_checks,
        "physical": physical_checks,
        "reader": physical.get("reader"),
        "cap_sha256": value.get("cap_sha256"),
        "source_evidence": str(path),
        "captured_at": value.get("timestamp"),
        "evidence_scope": "HISTORICAL_RECORD",
        "physically_confirmed": confirmed,
    }
