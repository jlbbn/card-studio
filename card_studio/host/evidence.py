from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from card_studio.evidence.store import EvidenceStore


HOST_EVIDENCE_FIELDS = frozenset({
    "operation_id",
    "correlation_id",
    "host_reference",
    "original_host_reference",
    "original_operation_id",
    "host_operation",
    "host_ref",
    "host_version",
    "execution_state",
    "functional_result",
    "recovery_action",
    "error_code",
    "authorization_response_code",
    "response_template_sha256",
    "response_template_length",
    "response_tag_count",
    "script_count",
    "response_origin",
    "policy",
    "fallback_policy",
    "timestamp",
    "simulation",
    "mode",
    "evidence_level",
    "physical_validation",
})


class HostEvidenceStore:
    def __init__(self, directory: Path):
        self.store = EvidenceStore(directory)

    def write(self, response: Mapping[str, Any]) -> Path:
        reference = response.get("host_reference")
        if not isinstance(reference, str) or not reference.startswith("hr-"):
            raise ValueError("La evidencia de host requiere host_reference.")
        value = {key: response.get(key) for key in HOST_EVIDENCE_FIELDS if key in response}
        value.update({
            "simulation": True,
            "mode": "SIMULATED",
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
        })
        return self.store.write(f"{reference}.json", value, HOST_EVIDENCE_FIELDS)
