from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Mapping

from .manifests import validate_rc1_manifest


GATE_SEQUENCE = (
    "PRECHECK",
    "DEPLOY",
    "HEALTH",
    "READINESS",
    "MIGRATION_VERIFY",
    "E2E_SYNTHETIC",
    "OBSERVABILITY_VERIFY",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DeploymentFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DeploymentGate:
    def __init__(self, manifest: Mapping[str, Any], correlation_id: str | None = None):
        self.manifest = manifest
        self.correlation_id = correlation_id or f"p4-{uuid.uuid4()}"
        self.started_at = utc_now()
        self.gates: list[dict[str, Any]] = []

    def run(self, actions: Mapping[str, Callable[[], Mapping[str, Any] | None]]) -> dict[str, Any]:
        if not validate_rc1_manifest(self.manifest):
            raise DeploymentFailure("INVALID_DEPLOYMENT_MANIFEST")
        for name in GATE_SEQUENCE:
            started = time.monotonic()
            timestamp = utc_now()
            try:
                action = actions[name]
                detail = action()
            except Exception as error:
                self.gates.append({
                    "gate": name, "status": "FAIL", "timestamp": timestamp,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "error": getattr(error, "code", type(error).__name__),
                })
                return self.report("FAIL")
            self.gates.append({
                "gate": name, "status": "PASS", "timestamp": timestamp,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "detail": dict(detail or {}),
            })
        return self.report("PASS")

    def report(self, status: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "release_candidate": self.manifest["release_candidate"],
            "correlation_id": self.correlation_id,
            "image_digest": self.manifest["image_digest"],
            "task_revisions": self.manifest["task_revisions"],
            "started_at": self.started_at,
            "completed_at": utc_now(),
            "status": status,
            "deployment": status if status == "FAIL" else "PASS",
            "gates": self.gates,
            "physical_validation": "NOT_EXECUTED",
        }
