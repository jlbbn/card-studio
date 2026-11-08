from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path
from typing import Any

from card_studio.evidence.store import EvidenceStore

from .model import TransactionResult
from .store import AuditEntry, TransactionRecord


EVIDENCE_FIELDS = frozenset({
    "operation_id",
    "correlation_id",
    "profile_ref",
    "profile_id",
    "profile_version",
    "profile_digest",
    "interface",
    "kernel_ref",
    "kernel_adapter",
    "kernel_version",
    "kernel_mode",
    "event_type",
    "sequence",
    "state_transition",
    "previous_state",
    "new_state",
    "reason",
    "recovery",
    "recovery_action",
    "functional_result",
    "execution_state",
    "timestamp",
    "simulation",
    "mode",
    "evidence_level",
    "physical_validation",
    "result_reference",
    "selected_aid",
    "cvm_result",
    "risk_result",
    "action_result",
    "host_ref",
    "host_version",
    "error_code",
    "transaction_type",
    "original_operation_id",
    "host_reference",
    "original_host_reference",
    "authorization_response_code",
    "response_template_sha256",
    "response_template_length",
    "response_tag_count",
    "script_count",
    "script_identifier_count",
    "host_operation",
    "response_origin",
    "policy",
    "fallback_policy",
    "host_response_timestamp",
    "late_response_timestamp",
})

HOST_METADATA_FIELDS = (
    "host_reference",
    "original_host_reference",
    "authorization_response_code",
    "response_template_sha256",
    "response_template_length",
    "response_tag_count",
    "script_count",
    "script_identifier_count",
    "host_operation",
    "response_origin",
    "policy",
    "fallback_policy",
    "host_response_timestamp",
    "late_response_timestamp",
)


class TransactionEvidenceStore:
    @staticmethod
    def _file_id(operation_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", operation_id)
        if safe != operation_id:
            safe = f"{safe}-{sha256(operation_id.encode('utf-8')).hexdigest()[:8]}"
        return safe

    def __init__(self, directory: Path):
        self.store = EvidenceStore(directory)

    @property
    def directory(self) -> Path:
        return self.store.directory

    @staticmethod
    def _base(record: TransactionRecord) -> dict[str, Any]:
        value = record.to_dict()
        return {
            key: value.get(key)
            for key in (
                "operation_id", "correlation_id", "profile_ref", "profile_id",
                "profile_version", "profile_digest", "interface", "kernel_ref",
                "kernel_adapter", "kernel_version", "kernel_mode", "simulation",
                "transaction_type", "original_operation_id", "execution_state",
                "functional_result", "recovery_action", "result_reference",
            )
        }

    def write_audit(
        self,
        audit: AuditEntry,
        record: TransactionRecord,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        value = self._base(record)
        if metadata:
            value.update({
                key: metadata[key]
                for key in ("host_ref", "host_version", *HOST_METADATA_FIELDS)
                if key in metadata
            })
        value.update({
            "event_type": audit.event_type,
            "sequence": audit.sequence,
            "previous_state": audit.previous_state,
            "new_state": audit.new_state,
            "state_transition": {
                "previous_state": audit.previous_state,
                "new_state": audit.new_state,
                "reason": audit.reason,
                "timestamp": audit.timestamp,
                "operation_id": audit.operation_id,
            },
            "reason": audit.reason,
            "timestamp": audit.timestamp,
            "mode": "SIMULATED",
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
        })
        if record.result is not None:
            for key in ("host_ref", "host_version", *HOST_METADATA_FIELDS):
                if key in record.result.details:
                    value[key] = record.result.details[key]
        return self.store.write(
            f"{self._file_id(audit.operation_id)}-event-{audit.sequence:06d}.json",
            value,
            EVIDENCE_FIELDS,
        )

    def write_result(self, record: TransactionRecord, result: TransactionResult) -> Path:
        value = self._base(record)
        value.update({
            "event_type": "TRANSACTION_RESULT",
            "execution_state": result.execution_state.value,
            "functional_result": result.details.get("functional_result", result.outcome),
            "recovery_action": result.recovery.value,
            "error_code": result.error_code,
            "timestamp": record.updated_at,
            "simulation": True,
            "mode": "SIMULATED",
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
        })
        for key in ("selected_aid", "cvm_result", "risk_result", "action_result", "host_ref", "host_version", *HOST_METADATA_FIELDS):
            if key in result.details:
                value[key] = result.details[key]
        reference = f"{self._file_id(record.operation_id)}-result.json"
        path = self.store.write(reference, value, EVIDENCE_FIELDS)
        return path
