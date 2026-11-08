from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class UnpredictableNumberError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UnpredictableNumberEvidence:
    value_length: int
    value_sha256: str
    repeated_output_rejected: bool = True
    mode: str = "LAB_ONLY"

    def to_evidence(self) -> dict[str, Any]:
        return {
            "value_length": self.value_length,
            "value_sha256": self.value_sha256,
            "repeated_output_rejected": self.repeated_output_rejected,
            "mode": self.mode,
        }


class UnpredictableNumberBoundary:
    def __init__(self, source: Callable[[], bytes]):
        if not callable(source):
            raise UnpredictableNumberError("INVALID_UN_SOURCE", "La fuente UN debe ser callable e inyectada.")
        self._source = source
        self._previous_digest: str | None = None
        self._lock = threading.Lock()

    def generate(self) -> tuple[bytes, UnpredictableNumberEvidence]:
        with self._lock:
            try:
                value = bytes(self._source())
            except Exception as exc:
                raise UnpredictableNumberError("UN_SOURCE_FAILED", "La fuente UN inyectada no produjo datos.") from exc
            if len(value) != 4:
                raise UnpredictableNumberError("INVALID_UN_LENGTH", "UN debe ocupar exactamente cuatro bytes.")
            if value in {bytes(4), b"\xff" * 4} or len(set(value)) < 3:
                raise UnpredictableNumberError("WEAK_UN_OUTPUT", "La fuente UN produjo una salida trivial o de baja diversidad.")
            digest = hashlib.sha256(value).hexdigest()
            if digest == self._previous_digest:
                raise UnpredictableNumberError("REPEATED_UN_OUTPUT", "La fuente UN repitió la salida anterior.")
            self._previous_digest = digest
            return value, UnpredictableNumberEvidence(len(value), digest)
