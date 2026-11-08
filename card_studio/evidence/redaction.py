from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class EvidenceRedactionError(ValueError):
    pass


SENSITIVE_FIELDS = frozenset({
    "pan",
    "pin",
    "cvv",
    "cvc",
    "track1",
    "track2",
    "track_data",
    "private_key",
    "issuer_key",
    "production_key",
    "master_key",
    "session_key",
    "terminal_key",
    "secret",
    "password",
    "api_key",
    "apdu",
    "raw_apdu",
    "raw_command",
    "raw_response",
    "command_payload",
    "response_payload",
    "authorization",
})
TRACK_PATTERN = re.compile(r"(?<!\d)\d{12,19}(?:\^[^\n\r]+|\=[0-9?]+)")


def _key(value: Any) -> str:
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            if _key(key) in SENSITIVE_FIELDS:
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_sensitive(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return TRACK_PATTERN.sub("[REDACTED]", value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise EvidenceRedactionError(f"Tipo de evidencia no serializable: {type(value).__name__}.")


def validate_evidence(report: Mapping[str, Any]) -> None:
    mode = report.get("mode")
    if mode not in {"SIMULATED", "PHYSICAL", "EXTERNAL_UNVERIFIED"}:
        raise EvidenceRedactionError("La evidencia debe declarar un modo válido.")
    if mode != "SIMULATED":
        return
    if report.get("physical_validation") == "PASS" or report.get("result") == "PHYSICALLY_CONFIRMED":
        raise EvidenceRedactionError("La evidencia SIMULATED no puede confirmar validación física.")
    if report.get("evidence_level") in {
        "DEVICE_VALIDATED",
        "CARD_VALIDATED",
        "PROVISIONING_VALIDATED",
        "BACKEND_VALIDATED",
        "END_TO_END_VALIDATED",
    }:
        raise EvidenceRedactionError("La evidencia SIMULATED no puede usar un nivel físico validado.")


def redact_evidence(
    report: Mapping[str, Any],
    allowed_fields: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        raise EvidenceRedactionError("La evidencia debe ser un objeto.")
    selected = report if allowed_fields is None else {
        key: value for key, value in report.items() if str(key) in allowed_fields
    }
    redacted = redact_sensitive(selected)
    validate_evidence(redacted)
    return redacted
