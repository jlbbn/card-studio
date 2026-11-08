from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import TransactionResult


class RecoveryClass(str, Enum):
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    QUERY_REQUIRED = "QUERY_REQUIRED"
    READ_BACK_REQUIRED = "READ_BACK_REQUIRED"
    REVERSAL_REQUIRED = "REVERSAL_REQUIRED"
    OPERATOR_REVIEW = "OPERATOR_REVIEW"
    NON_RETRYABLE = "NON_RETRYABLE"


class RecoveryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RecoveryPlan:
    operation_id: str
    recovery_class: RecoveryClass
    action: str
    automatic_retry: bool = False


def recovery_plan(result: "TransactionResult") -> RecoveryPlan:
    if result.execution_state.value == "UNCERTAIN":
        return RecoveryPlan(
            result.operation_id,
            RecoveryClass.QUERY_REQUIRED,
            f"query({result.operation_id})",
            automatic_retry=False,
        )
    recovery = result.recovery
    actions = {
        RecoveryClass.SAFE_TO_RETRY: "retry_same_request",
        RecoveryClass.QUERY_REQUIRED: f"query({result.operation_id})",
        RecoveryClass.READ_BACK_REQUIRED: "read_back_before_retry",
        RecoveryClass.REVERSAL_REQUIRED: "reverse_before_retry",
        RecoveryClass.OPERATOR_REVIEW: "operator_review",
        RecoveryClass.NON_RETRYABLE: "do_not_retry",
    }
    return RecoveryPlan(result.operation_id, recovery, actions[recovery], automatic_retry=False)


def assert_retry_allowed(result: "TransactionResult") -> None:
    if result.execution_state.value == "UNCERTAIN":
        raise RecoveryError(
            "QUERY_REQUIRED",
            "El resultado es incierto; ejecuta query(operation_id) antes de cualquier retry.",
        )
    if result.execution_state.value != "FAILED" or result.recovery != RecoveryClass.SAFE_TO_RETRY:
        raise RecoveryError("NON_RETRYABLE", "La operación no admite un nuevo envío.")
