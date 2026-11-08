from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from card_studio.store import write_json_atomic

from .redaction import EvidenceRedactionError, redact_evidence


class EvidenceStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def write(
        self,
        name: str,
        report: Mapping[str, Any],
        allowed_fields: set[str] | frozenset[str] | None = None,
    ) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.json", name) or name.startswith("."):
            raise EvidenceRedactionError("El nombre de evidencia debe ser un archivo JSON simple y visible.")
        redacted = redact_evidence(report, allowed_fields)
        path = self.directory / name
        write_json_atomic(path, redacted)
        return path
