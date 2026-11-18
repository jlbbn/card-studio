from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from card_studio.transactions.model import (
    ExecutionState,
    FunctionalResult,
    TransactionRequest,
    TransactionResult,
)
from card_studio.transactions.recovery import RecoveryClass

RecoveryAction = RecoveryClass


@dataclass(frozen=True)
class KernelResult:
    functional_result: FunctionalResult | None
    execution_state: ExecutionState
    recovery_action: RecoveryAction
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "functional_result", FunctionalResult(self.functional_result) if self.functional_result else None)
        object.__setattr__(self, "execution_state", ExecutionState(self.execution_state))
        object.__setattr__(self, "recovery_action", RecoveryClass(self.recovery_action))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        if self.execution_state == ExecutionState.FAILED and self.functional_result is not None:
            if self.functional_result != FunctionalResult.DECLINED:
                raise ValueError("Un fallo técnico no puede presentarse como aprobación ni acción funcional.")

    def to_transaction_result(self, request: TransactionRequest) -> TransactionResult:
        details = dict(self.details)
        if self.functional_result is not None:
            details["functional_result"] = self.functional_result.value
        return TransactionResult(
            request=request,
            execution_state=self.execution_state,
            outcome=self.functional_result.value if self.functional_result else None,
            recovery=self.recovery_action,
            error_code=self.error_code,
            details=details,
        )

    @classmethod
    def from_transaction_result(cls, result: TransactionResult) -> "KernelResult":
        functional = result.details.get("functional_result", result.outcome)
        return cls(
            functional_result=FunctionalResult(functional) if functional else None,
            execution_state=result.execution_state,
            recovery_action=result.recovery,
            error_code=result.error_code,
            details={key: value for key, value in result.details.items() if key != "functional_result"},
        )
