from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Any


_ALLOWED_EVENTS = frozenset({
    "STAGE",
    "APDU_SENT",
    "APDU_RESPONSE",
    "APDU_ERROR",
    "CANCEL_REQUESTED",
    "RECOVERY",
})


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    stage: str
    event: str
    cla: int | None = None
    ins: int | None = None
    p1: int | None = None
    p2: int | None = None
    command_data_length: int | None = None
    le: int | None = None
    response_data_length: int | None = None
    sw1sw2: str | None = None
    error_code: str | None = None

    def to_evidence(self) -> dict[str, Any]:
        values = {
            "sequence": self.sequence,
            "stage": self.stage,
            "event": self.event,
            "cla": self.cla,
            "ins": self.ins,
            "p1": self.p1,
            "p2": self.p2,
            "command_data_length": self.command_data_length,
            "le": self.le,
            "response_data_length": self.response_data_length,
            "sw1sw2": self.sw1sw2,
            "error_code": self.error_code,
        }
        return {key: value for key, value in values.items() if value is not None}


class SanitizedTrace:
    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        self._sequence = 0
        self._events: list[TraceEvent] = []
        self._lock = threading.Lock()

    @staticmethod
    def _stage(stage: str) -> str:
        if not isinstance(stage, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", stage):
            raise ValueError("Stage de traza inválido.")
        return stage

    @staticmethod
    def _error_code(error_code: str | None) -> str | None:
        if error_code is None:
            return None
        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", error_code):
            return "CARD_EXCHANGE_FAILED"
        return error_code

    def _append(self, stage: str, event: str, **metadata: Any) -> TraceEvent:
        if event not in _ALLOWED_EVENTS:
            raise ValueError("Evento no permitido en la traza sanitizada.")
        with self._lock:
            self._sequence += 1
            item = TraceEvent(self._sequence, self._stage(stage), event, **metadata)
            self._events.append(item)
            return item

    def stage(self, stage: str) -> None:
        self._append(stage, "STAGE")

    def control(self, stage: str, event: str, error_code: str | None = None) -> None:
        self._append(stage, event, error_code=self._error_code(error_code))

    @staticmethod
    def _command_metadata(command: bytes | str) -> dict[str, Any]:
        raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
        metadata: dict[str, Any] = {"command_data_length": max(0, len(raw) - 4)}
        if len(raw) >= 4:
            metadata.update({"cla": raw[0], "ins": raw[1], "p1": raw[2], "p2": raw[3]})
        if len(raw) >= 5:
            declared = raw[4]
            if declared and len(raw) >= 5 + declared:
                metadata["command_data_length"] = declared
                if len(raw) == 6 + declared:
                    metadata["le"] = raw[-1]
            elif len(raw) == 5:
                metadata["command_data_length"] = 0
                metadata["le"] = declared
        return metadata

    def sent(self, stage: str, command: bytes | str) -> None:
        self._append(stage, "APDU_SENT", **self._command_metadata(command))

    def response(self, stage: str, command: bytes | str, response: Any) -> None:
        metadata = self._command_metadata(command)
        data = bytes(getattr(response, "data", b""))
        status = getattr(response, "sw1sw2", None)
        if status is not None and (not isinstance(status, str) or not re.fullmatch(r"[0-9A-Fa-f]{4}", status)):
            status = None
        self._append(
            stage,
            "APDU_RESPONSE",
            **metadata,
            response_data_length=len(data),
            sw1sw2=status,
        )

    def error(self, stage: str, command: bytes | str, error_code: str) -> None:
        self._append(stage, "APDU_ERROR", **self._command_metadata(command), error_code=self._error_code(error_code))

    def to_evidence(self) -> dict[str, Any]:
        with self._lock:
            events = [item.to_evidence() for item in self._events]
        return {
            "operation_sha256": hashlib.sha256(self.operation_id.encode("utf-8")).hexdigest(),
            "mode": "LAB_ONLY",
            "redaction": "ALLOWLIST_ONLY",
            "replay_policy": "NO_AUTOMATIC_REPLAY",
            "events": events,
        }
