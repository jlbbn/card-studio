from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from card_studio.transactions.recovery import RecoveryClass

from .cryptogram import CryptogramResult, CryptogramType


class ContactlessError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        recovery: RecoveryClass = RecoveryClass.OPERATOR_REVIEW,
        outcome: str = "CERTAIN_FAILURE",
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery
        self.outcome = outcome


class C8OutcomeKind(str, Enum):
    READY = "READY"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    ONLINE_REQUIRED = "ONLINE_REQUIRED"
    TRY_AGAIN = "TRY_AGAIN"
    END_APPLICATION = "END_APPLICATION"


@dataclass(frozen=True)
class C8Outcome:
    kind: C8OutcomeKind
    ui_status: str
    ui_message: str
    restart_allowed: bool

    def to_evidence(self) -> dict[str, Any]:
        return {
            "outcome": self.kind.value,
            "ui_status": self.ui_status,
            "ui_message": self.ui_message,
            "restart_allowed": self.restart_allowed,
            "mode": "LAB_ONLY",
        }


@dataclass(frozen=True)
class C8Policy:
    transaction_limit_minor: int = 0
    require_ctq: bool = False
    ctq_allowed_mask: bytes = b"\xff\xff"
    relay_time_limit_ms: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.transaction_limit_minor, int)
            or isinstance(self.transaction_limit_minor, bool)
            or self.transaction_limit_minor < 0
        ):
            raise ContactlessError("INVALID_C8_LIMIT", "El límite C-8 debe ser un entero no negativo.")
        if (
            not isinstance(self.relay_time_limit_ms, int)
            or isinstance(self.relay_time_limit_ms, bool)
            or not 0 <= self.relay_time_limit_ms <= 600000
        ):
            raise ContactlessError("INVALID_RELAY_LIMIT", "relay_time_limit_ms debe estar entre 0 y 600000.")
        object.__setattr__(self, "ctq_allowed_mask", bytes(self.ctq_allowed_mask))
        if len(self.ctq_allowed_mask) != 2:
            raise ContactlessError("INVALID_C8_CTQ_MASK", "La máscara CTQ debe ocupar dos bytes.")


def c8_start(amount_minor: int, policy: C8Policy) -> C8Outcome:
    if policy.transaction_limit_minor and amount_minor > policy.transaction_limit_minor:
        return C8Outcome(
            C8OutcomeKind.END_APPLICATION,
            "END_APPLICATION",
            "CONTACTLESS_LIMIT_EXCEEDED",
            False,
        )
    return C8Outcome(C8OutcomeKind.READY, "READY", "PRESENT_CARD", True)


def c8_restart(*, command_sent: bool, uncertain: bool) -> C8Outcome:
    if command_sent or uncertain:
        return C8Outcome(
            C8OutcomeKind.END_APPLICATION,
            "READ_BACK_REQUIRED" if uncertain else "END_APPLICATION",
            "NO_AUTOMATIC_RESTART_AFTER_SEND",
            False,
        )
    return C8Outcome(C8OutcomeKind.TRY_AGAIN, "TRY_AGAIN", "RESTART_ALLOWED_BEFORE_SEND", True)


def validate_ctq(ctq: bytes | None, policy: C8Policy) -> None:
    if ctq is None:
        if policy.require_ctq:
            raise ContactlessError("MISSING_CTQ", "La configuración C-8 exige CTQ en GPO.")
        return
    value = bytes(ctq)
    if len(value) != 2:
        raise ContactlessError("INVALID_CTQ_LENGTH", "CTQ debe ocupar dos bytes.")
    if any(byte & ~mask for byte, mask in zip(value, policy.ctq_allowed_mask)):
        raise ContactlessError("CTQ_RFU_SET", "CTQ contiene bits fuera de la política configurada.")


def c8_cryptogram_outcome(cryptogram: CryptogramResult) -> C8Outcome:
    mapping = {
        CryptogramType.TC: (C8OutcomeKind.APPROVED, "APPROVED", "TRANSACTION_APPROVED"),
        CryptogramType.AAC: (C8OutcomeKind.DECLINED, "DECLINED", "TRANSACTION_DECLINED"),
        CryptogramType.ARQC: (C8OutcomeKind.ONLINE_REQUIRED, "ONLINE_REQUIRED", "ONLINE_AUTHORIZATION_REQUIRED"),
    }
    kind, status, message = mapping[cryptogram.cryptogram_type]
    return C8Outcome(kind, status, message, False)
