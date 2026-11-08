from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,127}")
_CURRENCIES = frozenset({"MXN", "USD", "EUR", "GBP", "CAD", "JPY"})
_OPERATION_TYPES = frozenset({"purchase", "refund", "reversal"})
_TRANSACTION_STATES = frozenset({
    "CARD_DETECTED", "APPLICATION_SELECTED", "PROCESSING", "ONLINE_REQUIRED",
    "APPROVED", "DECLINED", "UNCERTAIN", "REVERSED", "FAILED",
})
_RECOVERY_ACTIONS = frozenset({
    "SAFE_TO_RETRY", "QUERY_REQUIRED", "READ_BACK_REQUIRED", "OPERATOR_REVIEW", "NON_RETRYABLE",
})


class ContractError(ValueError):
    def __init__(self, code: str, message: str = "Contrato CARD inválido."):
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ContractError("INVALID_IDENTIFIER", f"{name} no tiene un formato válido.")
    return value


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ContractError("INVALID_TIMESTAMP", f"{name} debe ser una fecha UTC ISO 8601.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError("INVALID_TIMESTAMP", f"{name} debe ser una fecha UTC ISO 8601.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError("INVALID_TIMESTAMP", f"{name} debe incluir zona UTC.")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class CardServiceContract:
    terminal_id: str
    correlation_id: str
    transaction_id: str
    idempotency_key: str
    operation_type: str
    amount_minor: int
    currency: str
    transaction_state: str
    created_at: str
    updated_at: str
    result: Mapping[str, Any] = field(default_factory=dict)
    recovery: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("terminal_id", "correlation_id", "transaction_id", "idempotency_key"):
            _identifier(getattr(self, name), name)
        if self.operation_type not in _OPERATION_TYPES:
            raise ContractError("INVALID_OPERATION_TYPE")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int) or not 0 < self.amount_minor <= 999_999_999_999:
            raise ContractError("INVALID_AMOUNT")
        if self.currency not in _CURRENCIES:
            raise ContractError("INVALID_CURRENCY")
        if self.transaction_state not in _TRANSACTION_STATES:
            raise ContractError("INVALID_TRANSACTION_STATE")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.updated_at, "updated_at")
        if not isinstance(self.result, Mapping) or not isinstance(self.recovery, Mapping):
            raise ContractError("INVALID_RESULT_METADATA")
        execution_state = self.result.get("execution_state")
        functional_result = self.result.get("functional_result")
        recovery_action = self.recovery.get("recovery_action")
        if recovery_action not in _RECOVERY_ACTIONS:
            raise ContractError("INVALID_RECOVERY_ACTION")
        if not isinstance(self.recovery.get("command_sent"), bool):
            raise ContractError("INVALID_RECOVERY_METADATA")
        if self.transaction_state == "UNCERTAIN" and (
            execution_state != "UNCERTAIN" or recovery_action == "SAFE_TO_RETRY"
        ):
            raise ContractError("INVALID_UNCERTAIN_SEMANTICS")
        if self.transaction_state == "DECLINED" and functional_result != "DECLINED":
            raise ContractError("INVALID_DECLINED_SEMANTICS")
        if self.transaction_state == "FAILED" and functional_result == "DECLINED":
            raise ContractError("FAILED_IS_NOT_DECLINED")
        if self.transaction_state == "APPROVED" and (
            execution_state != "SUCCEEDED" or functional_result != "APPROVED"
        ):
            raise ContractError("INVALID_APPROVED_SEMANTICS")
        if self.transaction_state == "REVERSED" and self.operation_type != "reversal":
            raise ContractError("INVALID_REVERSAL_SEMANTICS")
        original = self.recovery.get("original_transaction_id")
        if self.operation_type == "purchase" and original is not None:
            raise ContractError("INVALID_ORIGINAL_REFERENCE")
        if self.operation_type != "purchase":
            _identifier(original, "original_transaction_id")
        allowed_result = {
            "execution_state", "functional_result", "error_code", "host_reference",
            "host_operation", "result_reference",
        }
        allowed_recovery = {
            "recovery_action", "command_sent", "attempt", "original_transaction_id",
            "query_required", "read_back_required", "late_result",
        }
        if set(self.result) - allowed_result or set(self.recovery) - allowed_recovery:
            raise ContractError("UNEXPECTED_CONTRACT_FIELD")

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "correlation_id": self.correlation_id,
            "transaction_id": self.transaction_id,
            "idempotency_key": self.idempotency_key,
            "operation_type": self.operation_type,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "transaction_state": self.transaction_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": dict(self.result),
            "recovery": dict(self.recovery),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CardServiceContract":
        if not isinstance(value, Mapping):
            raise ContractError("INVALID_PAYLOAD")
        required = {
            "terminal_id", "correlation_id", "transaction_id", "idempotency_key",
            "operation_type", "amount_minor", "currency", "transaction_state",
            "created_at", "updated_at", "result", "recovery",
        }
        if set(value) != required:
            raise ContractError("INVALID_PAYLOAD_FIELDS")
        try:
            return cls(**{name: value[name] for name in required})
        except TypeError as error:
            raise ContractError("INVALID_PAYLOAD") from error

    @classmethod
    def from_card_operation(cls, terminal_id: str, operation: Mapping[str, Any]) -> "CardServiceContract":
        state = operation["transactionState"] if "transactionState" in operation else operation["executionState"]
        if state == "SUCCEEDED":
            state = "REVERSED" if operation["kind"] == "reversal" else "APPROVED"
        elif state == "FAILED" and operation.get("functionalResult") == "DECLINED":
            state = "DECLINED"
        now = utc_now()
        return cls(
            terminal_id=terminal_id,
            correlation_id=operation["correlationId"],
            transaction_id=operation["operationId"],
            idempotency_key=operation["idempotencyKey"],
            operation_type=operation["kind"],
            amount_minor=operation["amountMinor"],
            currency=operation["currency"],
            transaction_state=state,
            created_at=operation.get("occurredAt") or now,
            updated_at=now,
            result={
                "execution_state": operation["executionState"],
                "functional_result": operation.get("functionalResult"),
                "error_code": operation.get("errorCode"),
                "host_reference": operation.get("hostReference"),
                "host_operation": operation.get("hostOperation"),
                "result_reference": operation.get("resultReference"),
            },
            recovery={
                "recovery_action": operation.get("recoveryAction") or (
                    "QUERY_REQUIRED" if operation["executionState"] == "UNCERTAIN" else "NON_RETRYABLE"
                ),
                "command_sent": operation.get("commandSent", operation["executionState"] == "UNCERTAIN"),
                "attempt": 1,
                "original_transaction_id": operation.get("originalOperationId"),
            },
        )

    def request_digest(self) -> str:
        value = self.to_dict()
        for name in ("correlation_id", "transaction_id", "transaction_state", "created_at", "updated_at", "result"):
            value.pop(name)
        recovery = value["recovery"]
        for name in ("recovery_action", "command_sent", "attempt", "query_required", "read_back_required", "late_result"):
            recovery.pop(name, None)
        return hashlib.sha256(canonical_json(value)).hexdigest()

    def public_dict(self) -> dict[str, Any]:
        return self.to_dict()
