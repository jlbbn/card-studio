from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from card_studio.evidence.redaction import redact_evidence
from card_studio.interfaces.base import InterfaceKind
from card_studio.money import Money

from .recovery import RecoveryClass


class ExecutionState(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    CANCELLED = "CANCELLED"


class TransactionType(str, Enum):
    PURCHASE = "purchase"
    REFUND = "refund"
    REVERSAL = "reversal"


class SimulationScenario(str, Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    ONLINE_REQUIRED = "online_required"
    ACTION_REQUIRED = "action_required"
    FAILURE_BEFORE_SEND = "failure_before_send"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    UNCERTAIN = "uncertain"
    RETRY = "retry"


class FunctionalResult(str, Enum):
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    ONLINE_REQUIRED = "ONLINE_REQUIRED"
    ACTION_REQUIRED = "ACTION_REQUIRED"


class TransactionError(Exception):
    def __init__(self, code: str, message: str, recovery: RecoveryClass = RecoveryClass.NON_RETRYABLE):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,127}", value):
        raise ValueError(f"{field_name} no tiene un formato válido.")
    return value


@dataclass(frozen=True)
class TransactionRequest:
    operation_id: str
    idempotency_key: str
    correlation_id: str
    profile_ref: str
    interface: str
    kernel_ref: str
    money: Money
    transaction_type: str = TransactionType.PURCHASE.value
    simulation: bool = True
    original_operation_id: str | None = None
    original_amount: Money | None = None
    scenario: str = SimulationScenario.APPROVED.value
    application_aid: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.idempotency_key, "idempotency_key")
        _identifier(self.correlation_id, "correlation_id")
        if not isinstance(self.profile_ref, str) or not self.profile_ref.strip():
            raise ValueError("profile_ref es obligatorio.")
        if not isinstance(self.kernel_ref, str) or not self.kernel_ref.strip():
            raise ValueError("kernel_ref es obligatorio.")
        object.__setattr__(self, "interface", self.interface.upper())
        InterfaceKind(self.interface)
        object.__setattr__(self, "transaction_type", self.transaction_type.lower())
        TransactionType(self.transaction_type)
        object.__setattr__(self, "scenario", self.scenario.lower())
        SimulationScenario(self.scenario)
        if self.application_aid is not None:
            normalized_aid = self.application_aid.strip().upper()
            if not re.fullmatch(r"[0-9A-F]{10,32}", normalized_aid) or len(normalized_aid) % 2:
                raise ValueError("application_aid debe ser un AID hexadecimal válido.")
            object.__setattr__(self, "application_aid", normalized_aid)
        if self.simulation is not True:
            raise ValueError("Toda operación de esta fase debe conservar simulation=true.")
        if not isinstance(self.money, Money) or self.money.amount_minor == 0:
            raise ValueError("La operación requiere un Money con importe positivo.")
        if self.original_operation_id is not None:
            _identifier(self.original_operation_id, "original_operation_id")
        if self.original_amount is not None and not isinstance(self.original_amount, Money):
            raise ValueError("original_amount debe ser Money.")
        if self.transaction_type == TransactionType.PURCHASE.value:
            if self.original_operation_id is not None or self.original_amount is not None:
                raise ValueError("Purchase no admite referencias a una operación original.")
        elif self.original_operation_id is None or self.original_amount is None:
            raise ValueError("Refund y reversal requieren original_operation_id y original_amount.")
        if self.original_amount is not None and (
            self.money.currency != self.original_amount.currency
            or self.money.exponent != self.original_amount.exponent
        ):
            raise ValueError("Money y original_amount deben usar la misma moneda y exponente.")

    @classmethod
    def create(
        cls,
        money: Money,
        profile_ref: str,
        interface: str,
        kernel_ref: str = "mock-kernel-v1",
        transaction_type: str = TransactionType.PURCHASE.value,
        scenario: str = SimulationScenario.APPROVED.value,
        original_operation_id: str | None = None,
        original_amount: Money | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        application_aid: str | None = None,
    ) -> "TransactionRequest":
        selected_id = operation_id or f"op-{uuid.uuid4()}"
        return cls(
            operation_id=selected_id,
            idempotency_key=idempotency_key or f"tx-{selected_id}",
            correlation_id=correlation_id or selected_id,
            profile_ref=profile_ref,
            interface=interface,
            kernel_ref=kernel_ref,
            money=money,
            transaction_type=transaction_type,
            simulation=True,
            original_operation_id=original_operation_id,
            original_amount=original_amount,
            scenario=scenario,
            application_aid=application_aid,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "correlation_id": self.correlation_id,
            "profile_ref": self.profile_ref,
            "interface": self.interface,
            "kernel_ref": self.kernel_ref,
            "money": self.money.to_dict(),
            "transaction_type": self.transaction_type,
            "simulation": self.simulation,
            "original_operation_id": self.original_operation_id,
            "original_amount": self.original_amount.to_dict() if self.original_amount else None,
            "scenario": self.scenario,
            "application_aid": self.application_aid,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransactionRequest":
        return cls(
            operation_id=value["operation_id"],
            idempotency_key=value["idempotency_key"],
            correlation_id=value["correlation_id"],
            profile_ref=value["profile_ref"],
            interface=value["interface"],
            kernel_ref=value["kernel_ref"],
            money=Money.from_dict(value["money"]),
            transaction_type=value.get("transaction_type", TransactionType.PURCHASE.value),
            simulation=value.get("simulation", True),
            original_operation_id=value.get("original_operation_id"),
            original_amount=Money.from_dict(value["original_amount"]) if value.get("original_amount") else None,
            scenario=value.get("scenario", SimulationScenario.APPROVED.value),
            application_aid=value.get("application_aid"),
        )

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TransactionResult:
    request: TransactionRequest
    execution_state: ExecutionState
    outcome: str | None
    recovery: RecoveryClass
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_state", ExecutionState(self.execution_state))
        object.__setattr__(self, "recovery", RecoveryClass(self.recovery))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        if self.execution_state == ExecutionState.UNCERTAIN and self.recovery not in {
            RecoveryClass.QUERY_REQUIRED,
            RecoveryClass.READ_BACK_REQUIRED,
            RecoveryClass.REVERSAL_REQUIRED,
        }:
            raise ValueError("Un resultado UNCERTAIN debe requerir query, read-back o reversal.")
        if self.recovery == RecoveryClass.QUERY_REQUIRED and self.execution_state != ExecutionState.UNCERTAIN:
            raise ValueError("QUERY_REQUIRED solo es válido para un resultado incierto.")

    @property
    def operation_id(self) -> str:
        return self.request.operation_id

    @property
    def idempotency_key(self) -> str:
        return self.request.idempotency_key

    @property
    def correlation_id(self) -> str:
        return self.request.correlation_id

    @property
    def profile_ref(self) -> str:
        return self.request.profile_ref

    @property
    def interface(self) -> str:
        return self.request.interface

    @property
    def kernel_ref(self) -> str:
        return self.request.kernel_ref

    @property
    def money(self) -> Money:
        return self.request.money

    @property
    def original_operation_id(self) -> str | None:
        return self.request.original_operation_id

    @property
    def original_amount(self) -> Money | None:
        return self.request.original_amount

    @property
    def simulation(self) -> bool:
        return self.request.simulation

    def to_dict(self) -> dict[str, Any]:
        value = self.request.to_dict()
        value.update({
            "execution_state": self.execution_state.value,
            "outcome": self.outcome,
            "recovery": self.recovery.value,
            "error_code": self.error_code,
            "details": dict(self.details),
        })
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransactionResult":
        return cls(
            request=TransactionRequest.from_dict(value),
            execution_state=ExecutionState(value["execution_state"]),
            outcome=value.get("outcome"),
            recovery=RecoveryClass(value["recovery"]),
            error_code=value.get("error_code"),
            details=value.get("details", {}),
        )

    def to_evidence(self) -> dict[str, Any]:
        value = self.to_dict()
        value.update({
            "mode": "SIMULATED",
            "result": self.execution_state.value,
            "physical_validation": "NOT_EXECUTED",
            "evidence_level": "SIMULATED",
        })
        return redact_evidence(value)
