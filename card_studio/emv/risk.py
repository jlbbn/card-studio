from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from card_studio.interfaces.base import InterfaceKind

from .status import Tsi, TsiFlag, Tvr, TvrFlag
from .velocity import VelocityDecision


class RiskError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _limit(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RiskError("INVALID_RISK_LIMIT", f"{name} debe ser un entero no negativo.")
    return value


@dataclass(frozen=True)
class RiskProfile:
    floor_limit_minor: int = 0
    transaction_type_floor_limits: Mapping[int, int] = field(default_factory=dict)
    interface_limits_minor: Mapping[InterfaceKind, int] = field(default_factory=dict)
    random_selection_percent: float = 0.0

    def __post_init__(self) -> None:
        _limit(self.floor_limit_minor, "floor_limit_minor")
        for transaction_type, limit in self.transaction_type_floor_limits.items():
            _limit(transaction_type, "transaction_type")
            _limit(limit, "transaction floor limit")
        interfaces = {}
        for interface, limit in self.interface_limits_minor.items():
            selected = InterfaceKind(interface)
            interfaces[selected] = _limit(limit, "interface limit")
        object.__setattr__(self, "interface_limits_minor", interfaces)
        if not isinstance(self.random_selection_percent, (int, float)) or isinstance(self.random_selection_percent, bool):
            raise RiskError("INVALID_RANDOM_PERCENT", "random_selection_percent debe estar entre 0 y 100.")
        if not 0 <= self.random_selection_percent <= 100:
            raise RiskError("INVALID_RANDOM_PERCENT", "random_selection_percent debe estar entre 0 y 100.")

    def floor_for(self, transaction_type: int, interface: InterfaceKind) -> tuple[int, int]:
        floor = self.transaction_type_floor_limits.get(transaction_type, self.floor_limit_minor)
        return floor, self.interface_limits_minor.get(interface, 0)


@dataclass(frozen=True)
class RiskInput:
    amount_minor: int
    transaction_type: int
    interface: InterfaceKind
    operation_id: str
    profile_ref: str
    currency: str
    profile: RiskProfile
    exception_file_match: bool = False
    merchant_forced_online: bool = False
    random_sample_basis_points: int | None = None
    velocity: VelocityDecision | None = None


@dataclass(frozen=True)
class RiskEvaluation:
    online_required: bool
    deterministic_sample: int
    reasons: tuple[str, ...]
    tvr: Tvr
    tsi: Tsi
    velocity: VelocityDecision | None = None

    def to_evidence(self) -> dict[str, Any]:
        return {
            "online_required": self.online_required,
            "deterministic_sample": self.deterministic_sample,
            "reasons": list(self.reasons),
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
            "velocity": self.velocity.to_evidence() if self.velocity is not None else None,
            "mode": "LAB_ONLY",
        }


def _sample(input: RiskInput) -> int:
    if input.random_sample_basis_points is not None:
        value = input.random_sample_basis_points
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 9999:
            raise RiskError("INVALID_RANDOM_SAMPLE", "random_sample_basis_points debe estar entre 0 y 9999.")
        return value
    seed = "|".join((
        "LAB_ONLY",
        input.operation_id,
        input.profile_ref,
        input.interface.value,
        input.currency,
        str(input.amount_minor),
        str(input.transaction_type),
    )).encode("utf-8")
    return int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") % 10000


def evaluate_risk(
    input: RiskInput,
    tvr: Tvr | None = None,
    tsi: Tsi | None = None,
) -> RiskEvaluation:
    _limit(input.amount_minor, "amount_minor")
    _limit(input.transaction_type, "transaction_type")
    if not input.operation_id or not input.profile_ref or len(input.currency) != 3:
        raise RiskError("INVALID_RISK_CONTEXT", "Contexto de riesgo incompleto.")
    interface = InterfaceKind(input.interface)
    floor, interface_limit = input.profile.floor_for(input.transaction_type, interface)
    sample = _sample(input)
    next_tvr = Tvr.parse(tvr.to_bytes()) if tvr is not None else Tvr.cleared()
    next_tsi = Tsi.parse(tsi.to_bytes()) if tsi is not None else Tsi.cleared()
    reasons: list[str] = []
    if input.exception_file_match:
        next_tvr.set(TvrFlag.CARD_ON_EXCEPTION_FILE)
        reasons.append("EXCEPTION_FILE")
    if floor > 0 and input.amount_minor > floor:
        next_tvr.set(TvrFlag.FLOOR_LIMIT_EXCEEDED)
        reasons.append("FLOOR_LIMIT")
    if interface_limit > 0 and input.amount_minor > interface_limit:
        next_tvr.set(TvrFlag.FLOOR_LIMIT_EXCEEDED)
        reasons.append("INTERFACE_LIMIT")
    threshold = round(input.profile.random_selection_percent * 100)
    if threshold > 0 and sample < threshold:
        next_tvr.set(TvrFlag.RANDOM_TRANSACTION_SELECTION_PERFORMED)
        reasons.append("DETERMINISTIC_SELECTION")
    if input.merchant_forced_online:
        next_tvr.set(TvrFlag.MERCHANT_FORCED_TRANSACTION_ONLINE)
        reasons.append("MERCHANT_FORCED_ONLINE")
    if input.velocity is not None:
        velocity_flags = {
            "LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED": TvrFlag.LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED,
            "UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED": TvrFlag.UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED,
            "VELOCITY_AMOUNT_LIMIT_EXCEEDED": TvrFlag.FLOOR_LIMIT_EXCEEDED,
            "MERCHANT_FORCED_ONLINE": TvrFlag.MERCHANT_FORCED_TRANSACTION_ONLINE,
        }
        for reason in input.velocity.reasons:
            if reason in velocity_flags:
                next_tvr.set(velocity_flags[reason])
            if reason == "LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED":
                next_tvr.set(TvrFlag.RANDOM_TRANSACTION_SELECTION_PERFORMED)
            reasons.append(reason)
    next_tsi.mark_processed(TsiFlag.TERMINAL_RISK_MANAGEMENT_PERFORMED)
    return RiskEvaluation(bool(reasons), sample, tuple(dict.fromkeys(reasons)), next_tvr, next_tsi, input.velocity)
