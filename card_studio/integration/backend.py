from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from card_studio.evidence.redaction import redact_sensitive
from card_studio.ledger import LedgerClient, LedgerError
from card_studio.store import write_json_atomic


class BackendIntegrationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class CardOperation:
    operation_id: str
    idempotency_key: str
    correlation_id: str
    kind: str
    amount_minor: int
    currency: str
    execution_state: str
    functional_result: str | None
    host_reference: str | None = None
    original_host_reference: str | None = None
    host_operation: str | None = None
    occurred_at: str = ""
    result_reference: str | None = None
    api_status: str = "PENDING"
    api_message: str = "Pendiente de sincronizar"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_transaction(cls, result: Mapping[str, Any]) -> "CardOperation":
        details = result.get("details", {})
        return cls(
            operation_id=result["operation_id"],
            idempotency_key=result["idempotency_key"],
            correlation_id=result["correlation_id"],
            kind=result["transaction_type"],
            amount_minor=result["amount_minor"],
            currency=result["currency"],
            execution_state=result["execution_state"],
            functional_result=result.get("functional_result"),
            host_reference=details.get("host_reference"),
            original_host_reference=details.get("original_host_reference"),
            host_operation=details.get("host_operation"),
            occurred_at=result["updated_at"],
            result_reference=result.get("result_reference"),
            metadata={
                "kernel_adapter": result["kernel_adapter"],
                "kernel_ref": result["kernel_ref"],
                "profile_ref": result["profile_ref"],
                "simulation": True,
            },
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "operationId": self.operation_id,
            "idempotencyKey": self.idempotency_key,
            "correlationId": self.correlation_id,
            "mode": "CARD",
            "paymentInstrument": "CARD",
            "kind": self.kind,
            "amountMinor": self.amount_minor,
            "currency": self.currency,
            "executionState": self.execution_state,
            "functionalResult": self.functional_result,
            "hostReference": self.host_reference,
            "originalHostReference": self.original_host_reference,
            "hostOperation": self.host_operation,
            "occurredAt": self.occurred_at,
            "simulation": True,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.to_api()
        value.update({
            "resultReference": self.result_reference,
            "apiStatus": self.api_status,
            "apiMessage": self.api_message,
            "metadata": self.metadata,
        })
        return redact_sensitive(value)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CardOperation":
        return cls(
            operation_id=value["operationId"],
            idempotency_key=value["idempotencyKey"],
            correlation_id=value["correlationId"],
            kind=value["kind"],
            amount_minor=value["amountMinor"],
            currency=value["currency"],
            execution_state=value["executionState"],
            functional_result=value.get("functionalResult"),
            host_reference=value.get("hostReference"),
            original_host_reference=value.get("originalHostReference"),
            host_operation=value.get("hostOperation"),
            occurred_at=value["occurredAt"],
            result_reference=value.get("resultReference"),
            api_status=value.get("apiStatus", "PENDING"),
            api_message=value.get("apiMessage", "Pendiente de sincronizar"),
            metadata=dict(value.get("metadata", {})),
        )

    def digest(self) -> str:
        canonical = json.dumps(self.to_api(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CardOperationStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "card-operations.json"

    def history(self) -> list[CardOperation]:
        if not self.path.exists():
            return []
        values = json.loads(self.path.read_text(encoding="utf-8"))
        return [CardOperation.from_dict(value) for value in values[:100]]

    def get(self, operation_id: str) -> CardOperation:
        for operation in self.history():
            if operation.operation_id == operation_id:
                return operation
        raise BackendIntegrationError("CARD_OPERATION_NOT_FOUND", "No existe la operación CARD.")

    def add(self, operation: CardOperation) -> None:
        values = self.history()
        for existing in values:
            if existing.operation_id == operation.operation_id or existing.idempotency_key == operation.idempotency_key:
                if existing.digest() != operation.digest():
                    raise BackendIntegrationError("IDEMPOTENCY_CONFLICT", "La operación CARD existe con otro contenido.")
                return
        write_json_atomic(self.path, [operation.to_dict(), *(item.to_dict() for item in values)][:100])

    def update(self, operation: CardOperation) -> None:
        values = self.history()
        for index, existing in enumerate(values):
            if existing.operation_id == operation.operation_id:
                values[index] = operation
                write_json_atomic(self.path, [item.to_dict() for item in values[:100]])
                return
        raise BackendIntegrationError("CARD_OPERATION_NOT_FOUND", "No existe la operación CARD.")


class CardLedgerBridge:
    def __init__(self, store: CardOperationStore):
        self.store = store

    def record(self, operation: CardOperation, ledger: LedgerClient) -> CardOperation:
        self.store.add(operation)
        try:
            ledger.record(operation)
        except LedgerError as error:
            operation.api_message = str(error)
        else:
            operation.api_status = "RECORDED"
            operation.api_message = "Registrado en Swift Ledger sandbox"
        self.store.update(operation)
        return operation

    def sync_pending(self, ledger: LedgerClient) -> tuple[int, str]:
        recorded = 0
        last_error = ""
        for operation in self.store.history():
            if operation.api_status == "RECORDED":
                continue
            try:
                ledger.record(operation)
            except LedgerError as error:
                operation.api_message = str(error)
                self.store.update(operation)
                last_error = str(error)
                break
            operation.api_status = "RECORDED"
            operation.api_message = "Registrado en Swift Ledger sandbox"
            self.store.update(operation)
            recorded += 1
        return recorded, last_error

    def reconcile(self, operation_id: str, remote: Mapping[str, Any]) -> dict[str, Any]:
        operation = self.store.get(operation_id)
        if (
            remote.get("operationId") != operation.operation_id
            or remote.get("idempotencyKey") != operation.idempotency_key
            or remote.get("status") != "RECORDED"
            or remote.get("simulation") is not True
        ):
            raise BackendIntegrationError("RECONCILIATION_MISMATCH", "La respuesta remota no coincide con CARD local.")
        if operation.api_status != "RECORDED":
            raise BackendIntegrationError("RECONCILIATION_PENDING", "La operación CARD local sigue pendiente.")
        return {
            "operation_id": operation.operation_id,
            "status": "RECONCILED",
            "result_reference": operation.result_reference,
            "mode": "CARD",
            "simulation": True,
        }


def card_receipt(operation: CardOperation) -> dict[str, Any]:
    return {
        "title": "CARD / LAB_ONLY",
        "operation_id": operation.operation_id,
        "kind": operation.kind,
        "amount_minor": operation.amount_minor,
        "currency": operation.currency,
        "execution_state": operation.execution_state,
        "functional_result": operation.functional_result,
        "result_reference": operation.result_reference,
        "physical_validation": "NOT_EXECUTED",
    }
