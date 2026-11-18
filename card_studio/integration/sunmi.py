from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from card_studio.application import CardStudioApplication
from card_studio.ledger import LedgerClient
from card_studio.transactions.model import TransactionRequest

from .backend import CardLedgerBridge, CardOperation, CardOperationStore, card_receipt
from .chain import run_integrated_transaction


class CardFlowState(str, Enum):
    CARD_DETECTED = "CARD_DETECTED"
    APPLICATION_SELECTED = "APPLICATION_SELECTED"
    PROCESSING = "PROCESSING"
    ONLINE_REQUIRED = "ONLINE_REQUIRED"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    UNCERTAIN = "UNCERTAIN"
    REVERSED = "REVERSED"
    FAILED = "FAILED"


_ALLOWED = {
    CardFlowState.CARD_DETECTED: {CardFlowState.APPLICATION_SELECTED},
    CardFlowState.APPLICATION_SELECTED: {CardFlowState.PROCESSING},
    CardFlowState.PROCESSING: {
        CardFlowState.ONLINE_REQUIRED,
        CardFlowState.APPROVED,
        CardFlowState.DECLINED,
        CardFlowState.UNCERTAIN,
        CardFlowState.FAILED,
    },
    CardFlowState.ONLINE_REQUIRED: {
        CardFlowState.APPROVED,
        CardFlowState.DECLINED,
        CardFlowState.UNCERTAIN,
        CardFlowState.FAILED,
    },
    CardFlowState.APPROVED: {CardFlowState.REVERSED},
    CardFlowState.DECLINED: set(),
    CardFlowState.UNCERTAIN: {
        CardFlowState.APPROVED,
        CardFlowState.DECLINED,
        CardFlowState.REVERSED,
        CardFlowState.UNCERTAIN,
    },
    CardFlowState.REVERSED: set(),
    CardFlowState.FAILED: set(),
}


class CardFlowError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SunmiCardFlow:
    def __init__(self):
        self.state = CardFlowState.CARD_DETECTED
        self.history = [self.state.value]

    def advance(self, target: CardFlowState) -> None:
        selected = CardFlowState(target)
        if selected == self.state:
            return
        if selected not in _ALLOWED[self.state]:
            raise CardFlowError(
                "INVALID_CARD_FLOW_TRANSITION",
                f"Transición CARD no permitida: {self.state.value} -> {selected.value}.",
            )
        self.state = selected
        self.history.append(selected.value)

    def apply(self, result: dict[str, Any], audit: list[dict[str, Any]]) -> None:
        self.advance(CardFlowState.APPLICATION_SELECTED)
        self.advance(CardFlowState.PROCESSING)
        events = {entry["event_type"] for entry in audit}
        if "ONLINE_REQUESTED" in events:
            self.advance(CardFlowState.ONLINE_REQUIRED)
        if result["transaction_type"] == "reversal" and result["execution_state"] == "SUCCEEDED":
            self.advance(CardFlowState.APPROVED)
            self.advance(CardFlowState.REVERSED)
            return
        if result["execution_state"] == "UNCERTAIN":
            self.advance(CardFlowState.UNCERTAIN)
        elif result.get("functional_result") == "APPROVED":
            self.advance(CardFlowState.APPROVED)
        elif result.get("functional_result") == "DECLINED":
            self.advance(CardFlowState.DECLINED)
        else:
            self.advance(CardFlowState.FAILED)


class SunmiIntegration:
    def __init__(
        self,
        app: CardStudioApplication,
        operation_store: CardOperationStore,
        ledger: LedgerClient | None = None,
        transport: str = "SUNMI_V3",
    ):
        if transport not in {"SUNMI_V3", "PCSC"}:
            raise CardFlowError("INVALID_CARD_TRANSPORT", "Transporte CARD no soportado.")
        self.app = app
        self.store = operation_store
        self.bridge = CardLedgerBridge(operation_store)
        self.ledger = ledger
        self.transport = transport

    def execute(self, request: TransactionRequest, evidence_path: Path) -> dict[str, Any]:
        chain = run_integrated_transaction(self.app, request, evidence_path)
        result = self.app.get(request.operation_id)
        flow = SunmiCardFlow()
        flow.apply(result, self.app.audit(request.operation_id))
        operation = CardOperation.from_transaction(result)
        if self.ledger is not None:
            operation = self.bridge.record(operation, self.ledger)
        else:
            self.store.add(operation)
        return {
            "transport": self.transport,
            "flow": flow.history,
            "state": flow.state.value,
            "operation": operation.to_dict(),
            "receipt": card_receipt(operation),
            "chain_evidence": chain["evidence_path"],
            "mode": "CARD",
            "simulation": True,
            "physical_validation": "NOT_EXECUTED",
            "certification": "NOT COMPLETED",
        }

    def query(self, operation_id: str) -> dict[str, Any]:
        result = self.app.query(operation_id)
        flow = SunmiCardFlow()
        flow.apply(result, self.app.audit(operation_id))
        operation = CardOperation.from_transaction(result)
        stored = self.store.get(operation_id)
        operation.api_status = stored.api_status
        operation.api_message = stored.api_message
        self.store.update(operation)
        return {
            "state": flow.state.value,
            "operation": operation.to_dict(),
            "simulation": True,
        }
