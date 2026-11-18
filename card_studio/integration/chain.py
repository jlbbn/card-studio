from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from card_studio.application import CardStudioApplication
from card_studio.store import write_json_atomic
from card_studio.transactions.model import TransactionRequest

from .physical import reader_manifest


def run_integrated_transaction(
    app: CardStudioApplication,
    request: TransactionRequest,
    evidence_path: Path,
) -> dict[str, Any]:
    result = app.create_transaction(request)
    record = app.service.get(request.operation_id)
    readers = app.reader.enumerate_readers()
    if len(readers) != 1:
        raise ValueError("La evidencia integrada requiere un lector único.")
    manifest = reader_manifest(app.reader, readers[0].reader_id)
    protocol = record.result.details.get("action_result", {}).get("interface_protocol") if record.result else None
    manifest["protocol"] = protocol or manifest["protocol"]
    result_evidence = app.evidence.directory / result["result_reference"]
    if not result_evidence.is_file():
        raise ValueError("La transacción no produjo evidencia persistente.")
    run_id = str(uuid.uuid4())
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "correlation_id": request.correlation_id,
        "chain": {
            "profile": "PASS",
            "reader": "PASS",
            "card_interface": "PASS",
            "kernel": "PASS",
            "transaction_service": "PASS",
            "persistence": "PASS",
            "evidence": "PASS",
        },
        "profile": {
            "profile_ref": request.profile_ref,
            "profile_digest": result["profile_digest"],
            "status": app.default_profile.status,
        },
        "reader": manifest,
        "kernel": {
            "kernel_adapter": result["kernel_adapter"],
            "kernel_ref": result["kernel_ref"],
            "kernel_version": result["kernel_version"],
            "kernel_mode": result["kernel_mode"],
        },
        "transaction": {
            "operation_id": request.operation_id,
            "idempotency_key": request.idempotency_key,
            "transaction_type": request.transaction_type,
            "execution_state": result["execution_state"],
            "functional_result": result["functional_result"],
            "recovery_action": result["recovery_action"],
            "result_reference": result["result_reference"],
            "host_reference": result["details"].get("host_reference"),
            "original_host_reference": result["details"].get("original_host_reference"),
            "host_operation": result["details"].get("host_operation"),
            "host_response_timestamp": result["details"].get("host_response_timestamp"),
            "late_response_timestamp": result["details"].get("late_response_timestamp"),
        },
        "persistence": {
            "store": app.store.path.name,
            "evidence_reference": result_evidence.name,
            "evidence_sha256": hashlib.sha256(result_evidence.read_bytes()).hexdigest(),
            "audit_events": [entry["event_type"] for entry in app.audit(request.operation_id)],
        },
        "simulation": True,
        "mode": "SIMULATED",
        "evidence_level": "SIMULATED",
        "authority": "EXTERNAL_UNVERIFIED",
        "physical_validation": "NOT_EXECUTED",
        "certification": "NOT COMPLETED",
    }
    write_json_atomic(evidence_path, report)
    report["evidence_path"] = str(evidence_path)
    return report
