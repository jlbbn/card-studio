from __future__ import annotations

from pathlib import Path
from typing import Any

from card_studio.evidence.redaction import redact_sensitive
from card_studio.integration.sunmi import SunmiIntegration
from card_studio.store import write_json_atomic
from card_studio.transactions.model import TransactionRequest

from .contract import CardServiceContract, utc_now
from .http import BackendError, LedgerServiceClient
from .observability import StructuredLogger, operation_trace
from .recovery import recovery_decision
from .treasury import TreasuryService


class SunmiPosService:
    def __init__(
        self,
        terminal_id: str,
        integration: SunmiIntegration,
        ledger: LedgerServiceClient,
        treasury: TreasuryService,
        evidence_directory: Path,
        logger: StructuredLogger | None = None,
    ):
        self.terminal_id = terminal_id
        self.integration = integration
        self.ledger = ledger
        self.treasury = treasury
        self.evidence_directory = evidence_directory
        self.logger = logger

    def _contract(self, request: TransactionRequest, result: dict[str, Any]) -> CardServiceContract:
        stored = self.integration.app.get(request.operation_id)
        operation = dict(result["operation"])
        operation.update({
            "transactionState": result["state"],
            "originalOperationId": request.original_operation_id,
            "recoveryAction": stored["recovery_action"],
            "commandSent": bool(stored.get("details", {}).get("command_sent", False)),
            "errorCode": stored.get("error_code"),
        })
        return CardServiceContract.from_card_operation(self.terminal_id, operation)

    def _emit(self, operation: str, contract: CardServiceContract, outcome: str, started: float) -> None:
        if self.logger is None:
            return
        import time

        self.logger.emit(
            operation,
            terminal_id=contract.terminal_id,
            correlation_id=contract.correlation_id,
            transaction_id=contract.transaction_id,
            state=contract.transaction_state,
            outcome=outcome,
            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        )

    def _write_evidence(self, contract: CardServiceContract, value: dict[str, Any]) -> str:
        path = self.evidence_directory / f"p3-{contract.transaction_id}.json"
        write_json_atomic(path, redact_sensitive(value))
        return path.name

    def _deliver(self, contract: CardServiceContract, *, settle: bool) -> dict[str, Any]:
        import time

        started = time.monotonic()
        try:
            response = self.ledger.submit(contract)
        except BackendError as error:
            delivery = {
                "status": "UNCERTAIN" if error.sent else "SAFE_TO_RETRY",
                "error": error.code,
                "automatic_retry": False,
            }
            self._emit("ledger_submit", contract, error.code, started)
            return {"delivery": delivery, "treasury": None, "reconciliation": None}
        self._emit("ledger_submit", contract, "RECORDED", started)
        settlement = None
        if settle and contract.transaction_state in {"APPROVED", "REVERSED"}:
            settlement = self.treasury.settle()
        return {
            "delivery": {"status": "RECORDED", "created": response["created"], "error": None},
            "backend_operation": response["operation"],
            "treasury": settlement["effects"] if settlement else [],
            "reconciliation": settlement["reconciliation"] if settlement else None,
        }

    def execute(
        self,
        request: TransactionRequest,
        evidence_path: Path,
        *,
        settle: bool = True,
    ) -> dict[str, Any]:
        flow = self.integration.execute(request, evidence_path)
        contract = self._contract(request, flow)
        delivered = self._deliver(contract, settle=settle)
        result = {
            "schema_version": 1,
            "terminal_id": self.terminal_id,
            "correlation_id": contract.correlation_id,
            "transaction_id": contract.transaction_id,
            "operation_type": contract.operation_type,
            "state": contract.transaction_state,
            "contract": contract.public_dict(),
            "card_flow": flow["flow"],
            **delivered,
            "chain": {
                "synthetic_terminal": "PASS",
                "sunmi_pos": "PASS",
                "card_lane": "PASS",
                "swift_ledger": "PASS" if delivered["delivery"]["status"] == "RECORDED" else delivered["delivery"]["status"],
                "authorization_result": flow["state"],
                "persistence": "PASS",
                "tesoreria": "PASS" if delivered["treasury"] is not None else "PENDING",
                "reconciliation": delivered["reconciliation"]["status"] if delivered["reconciliation"] else "PENDING",
            },
            "recovery": recovery_decision({
                "transaction_state": contract.transaction_state,
                "recovery_action": contract.recovery["recovery_action"],
            }).__dict__,
            "simulation": True,
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
            "certification": "NOT COMPLETED",
        }
        result["evidence_reference"] = self._write_evidence(contract, result)
        return result

    def query(self, operation_id: str, *, settle: bool = True) -> dict[str, Any]:
        request = self.integration.app.service.get(operation_id).request
        flow = self.integration.query(operation_id)
        contract = self._contract(request, flow)
        try:
            if contract.transaction_state in {"APPROVED", "DECLINED"}:
                response = self.ledger.resolve(contract)
            else:
                response = self.ledger.query(contract)
            delivery = {"status": response["status"], "error": None}
            backend = response["operation"]
        except BackendError as error:
            delivery = {"status": "UNCERTAIN", "error": error.code}
            backend = None
        settlement = self.treasury.settle() if settle and contract.transaction_state in {"APPROVED", "REVERSED"} else None
        return {
            "terminal_id": self.terminal_id,
            "transaction_id": operation_id,
            "correlation_id": contract.correlation_id,
            "state": contract.transaction_state,
            "delivery": delivery,
            "backend_operation": backend,
            "treasury": settlement["effects"] if settlement else [],
            "reconciliation": settlement["reconciliation"] if settlement else None,
            "simulation": True,
            "physical_validation": "NOT_EXECUTED",
        }

    recover = query

    def sync_pending(self) -> list[dict[str, Any]]:
        synchronized = []
        for operation in self.integration.store.history():
            flow = {
                "state": "REVERSED" if operation.kind == "reversal" and operation.execution_state == "SUCCEEDED" else (
                    "APPROVED" if operation.functional_result == "APPROVED" else
                    "DECLINED" if operation.functional_result == "DECLINED" else
                    "UNCERTAIN" if operation.execution_state == "UNCERTAIN" else "FAILED"
                ),
                "operation": operation.to_dict(),
            }
            request = self.integration.app.service.get(operation.operation_id).request
            contract = self._contract(request, flow)
            delivered = self._deliver(contract, settle=True)
            synchronized.append({"transaction_id": operation.operation_id, **delivered})
        return synchronized

    def trace(self, transaction_id: str) -> dict[str, Any]:
        return operation_trace(self.treasury.store, self.terminal_id, transaction_id)


def synthetic_evidence_manifest(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "cases": len(results),
        "passed": sum(
            result.get("state") in {"APPROVED", "DECLINED", "UNCERTAIN", "REVERSED", "FAILED"}
            and result.get("delivery", {}).get("status") in {"RECORDED", "RECONCILED"}
            for result in results
        ),
        "simulation": True,
        "evidence_level": "SIMULATED",
        "physical_validation": "NOT_EXECUTED",
        "certification": "NOT COMPLETED",
    }
