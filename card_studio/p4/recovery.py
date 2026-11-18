from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


class DisasterRecoveryClock:
    def __init__(self):
        self.started_wall = datetime.now(timezone.utc)
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def mark(self, event: str, **detail: Any) -> None:
        self.events.append({
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "elapsed_seconds": round(time.monotonic() - self.started, 6),
            **detail,
        })

    def report(self, data_loss_window_seconds: float) -> dict[str, Any]:
        names = {event["event"] for event in self.events}
        required = {"BACKUP_VERIFIED", "RESTORE_COMPLETE", "VERIFICATION_COMPLETE"}
        if not required <= names:
            raise ValueError("DR requiere backup, restore y verificación.")
        restore = next(event for event in self.events if event["event"] == "RESTORE_COMPLETE")
        verification = next(event for event in self.events if event["event"] == "VERIFICATION_COMPLETE")
        return {
            "started_at": self.started_wall.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "events": self.events,
            "rto_seconds": verification["elapsed_seconds"],
            "restore_seconds": restore["elapsed_seconds"],
            "rpo_seconds": round(max(0.0, data_loss_window_seconds), 6),
            "isolated": True,
            "production_restore": False,
            "status": "PASS",
        }
