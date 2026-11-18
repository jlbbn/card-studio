from __future__ import annotations

from card_studio.kernels.base import KernelAdapter, KernelError
from card_studio.transactions.recovery import RecoveryClass, RecoveryError

from .model import (
    ExecutionState,
    TransactionError,
    TransactionRequest,
    TransactionResult,
    TransactionType,
)


class TransactionSimulator:
    def __init__(self, kernel: KernelAdapter):
        if not kernel.capabilities().simulation:
            raise ValueError("TransactionSimulator requiere un kernel de simulación.")
        self.kernel = kernel
        self._requests_by_operation: dict[str, TransactionRequest] = {}
        self._requests_by_key: dict[str, TransactionRequest] = {}
        self._results: dict[str, TransactionResult] = {}

    def _existing(self, request: TransactionRequest) -> TransactionResult | None:
        by_operation = self._requests_by_operation.get(request.operation_id)
        if by_operation is not None:
            if by_operation.fingerprint() != request.fingerprint():
                raise TransactionError(
                    "DUPLICATE_OPERATION",
                    "operation_id ya existe con otro request.",
                )
            return self._results[request.operation_id]
        by_key = self._requests_by_key.get(request.idempotency_key)
        if by_key is not None:
            if by_key.fingerprint() != request.fingerprint():
                raise TransactionError(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency_key ya existe con otro request.",
                )
            return self._results[request.operation_id]
        return None

    def _store(self, request: TransactionRequest, result: TransactionResult) -> TransactionResult:
        self._requests_by_operation[request.operation_id] = request
        self._requests_by_key[request.idempotency_key] = request
        self._results[request.operation_id] = result
        return result

    @staticmethod
    def _failure(
        request: TransactionRequest,
        code: str,
        recovery: RecoveryClass = RecoveryClass.NON_RETRYABLE,
    ) -> TransactionResult:
        return TransactionResult(
            request=request,
            execution_state=ExecutionState.FAILED,
            outcome="DECLINED",
            recovery=recovery,
            error_code=code,
            details={"scenario": request.scenario, "command_sent": False},
        )

    def _validate_original(self, request: TransactionRequest) -> TransactionResult | None:
        original = self._results.get(request.original_operation_id or "")
        if original is None:
            return self._failure(request, "ORIGINAL_OPERATION_NOT_FOUND", RecoveryClass.OPERATOR_REVIEW)
        if (
            original.request.transaction_type != TransactionType.PURCHASE.value
            or original.execution_state != ExecutionState.SUCCEEDED
            or original.outcome != "APPROVED"
        ):
            return self._failure(request, "INVALID_ORIGINAL_OPERATION")
        if request.original_amount != original.money:
            return self._failure(request, "ORIGINAL_AMOUNT_MISMATCH", RecoveryClass.OPERATOR_REVIEW)

        reversals = [
            result for result in self._results.values()
            if result.request.transaction_type == TransactionType.REVERSAL.value
            and result.original_operation_id == original.operation_id
            and result.execution_state == ExecutionState.SUCCEEDED
        ]
        if reversals:
            return self._failure(request, "DUPLICATE_REVERSAL")

        if request.transaction_type == TransactionType.REVERSAL.value:
            if request.money != original.money:
                return self._failure(request, "REVERSAL_AMOUNT_MISMATCH")
            refunds = [
                result for result in self._results.values()
                if result.request.transaction_type == TransactionType.REFUND.value
                and result.original_operation_id == original.operation_id
                and result.execution_state == ExecutionState.SUCCEEDED
            ]
            if refunds:
                return self._failure(request, "REFUND_ALREADY_EXISTS", RecoveryClass.OPERATOR_REVIEW)
            return None

        refunded = sum(
            result.money.amount_minor
            for result in self._results.values()
            if result.request.transaction_type == TransactionType.REFUND.value
            and result.original_operation_id == original.operation_id
            and result.execution_state == ExecutionState.SUCCEEDED
        )
        if (
            request.money.amount_minor > original.money.amount_minor
            or refunded + request.money.amount_minor > original.money.amount_minor
        ):
            return self._failure(request, "EXCESSIVE_REFUND")
        return None

    def execute(self, request: TransactionRequest, deadline: float | None = None) -> TransactionResult:
        existing = self._existing(request)
        if existing is not None:
            return existing
        if request.transaction_type != TransactionType.PURCHASE.value:
            failure = self._validate_original(request)
            if failure is not None:
                return self._store(request, failure)

        session = self.kernel.open_session(request)
        try:
            result = self.kernel.execute(session, request, deadline)
        finally:
            self.kernel.close(session)
        return self._store(request, result)

    def query(self, operation_id: str) -> TransactionResult:
        if operation_id in self._results:
            return self._results[operation_id]
        try:
            return self.kernel.query(operation_id)
        except KernelError as error:
            raise TransactionError(error.code, error.message, error.recovery) from error

    def cancel(self, request: TransactionRequest) -> TransactionResult:
        existing = self._existing(request)
        if existing is not None:
            return existing
        session = self.kernel.open_session(request)
        try:
            result = self.kernel.cancel(session)
        finally:
            self.kernel.close(session)
        return self._store(request, result)

    def retry(self, request: TransactionRequest) -> TransactionResult:
        existing = self._existing(request)
        if existing is None:
            return self.execute(request)
        if existing.execution_state == ExecutionState.UNCERTAIN:
            raise RecoveryError(
                "QUERY_REQUIRED",
                "El resultado es incierto; ejecuta query(operation_id), no otro envío.",
            )
        return existing

    def evidence(self, operation_id: str) -> dict:
        return self.query(operation_id).to_evidence()
