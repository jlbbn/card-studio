from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, TextIO

from card_studio.evidence.redaction import redact_sensitive

from .store import LedgerStore


_ALLOWED = frozenset({
    "service", "operation", "state", "terminal_id", "correlation_id", "transaction_id",
    "elapsed_ms", "outcome", "event", "authorization", "persistence", "treasury", "reconciliation",
})


class StructuredLogger:
    def __init__(self, stream: TextIO, service: str):
        self.stream = stream
        self.service = service
        self._lock = threading.Lock()

    def emit(self, operation: str, **fields: Any) -> dict[str, Any]:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "service": self.service,
            "operation": operation,
            **fields,
        }
        safe = redact_sensitive({key: value for key, value in event.items() if key in _ALLOWED or key == "timestamp"})
        with self._lock:
            self.stream.write(json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n")
            self.stream.flush()
        return safe

    def timed(self, operation: str, **fields: Any) -> "TimedOperation":
        return TimedOperation(self, operation, fields)


class TimedOperation:
    def __init__(self, logger: StructuredLogger, operation: str, fields: dict[str, Any]):
        self.logger = logger
        self.operation = operation
        self.fields = fields
        self.started = 0.0

    def __enter__(self) -> "TimedOperation":
        self.started = time.monotonic()
        return self

    def __exit__(self, exception_type: Any, exception: Any, _traceback: Any) -> None:
        self.fields["elapsed_ms"] = round((time.monotonic() - self.started) * 1000, 3)
        self.fields["outcome"] = "ERROR" if exception is not None else self.fields.get("outcome", "PASS")
        self.logger.emit(self.operation, **self.fields)


def operation_trace(store: LedgerStore, terminal_id: str, transaction_id: str) -> dict[str, Any]:
    operation = store.get(terminal_id, transaction_id)
    consumed = [
        item for item in store.consumed("tesoreria-p3")
        if item["terminal_id"] == terminal_id and item["transaction_id"] == transaction_id
    ]
    return {
        "terminal_id": terminal_id,
        "correlation_id": operation.correlation_id,
        "transaction_id": transaction_id,
        "authorization": operation.result.get("host_reference"),
        "persistence": operation.transaction_state,
        "treasury": consumed[0]["effect_id"] if consumed else None,
        "reconciliation": "PENDING" if not consumed else "CONSUMED",
        "audit": store.audit(terminal_id, transaction_id),
    }
