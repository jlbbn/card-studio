from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .cvm import CvmEvaluation
from .risk import RiskEvaluation
from .status import Tsi, Tvr


class TaaError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TerminalAction(str, Enum):
    AAC = "AAC"
    TC = "TC"
    ARQC = "ARQC"


@dataclass(frozen=True)
class ActionCodes:
    denial: bytes
    online: bytes
    default: bytes

    def __post_init__(self) -> None:
        for name in ("denial", "online", "default"):
            value = bytes(getattr(self, name))
            object.__setattr__(self, name, value)
            if len(value) != 5:
                raise TaaError("INVALID_ACTION_CODE_LENGTH", f"{name} debe ocupar cinco bytes.")
            allowed = (0xFC, 0xF8, 0xFC, 0xF8, 0xF0)
            if any(byte & ~mask for byte, mask in zip(value, allowed)):
                raise TaaError("ACTION_CODE_RFU_SET", f"{name} contiene bits RFU fuera del alcance LAB_ONLY.")

    @classmethod
    def zeroed(cls) -> "ActionCodes":
        return cls(bytes(5), bytes(5), bytes(5))

    def to_evidence(self) -> dict[str, Any]:
        return {
            "denial_length": len(self.denial),
            "online_length": len(self.online),
            "default_length": len(self.default),
            "rfu_valid": True,
        }


@dataclass(frozen=True)
class TaaProfile:
    fallback_when_offline_unable_online: TerminalAction = TerminalAction.AAC
    no_match_default_when_online_capable: TerminalAction = TerminalAction.ARQC
    no_match_default_when_offline_only: TerminalAction = TerminalAction.AAC

    def __post_init__(self) -> None:
        object.__setattr__(self, "fallback_when_offline_unable_online", TerminalAction(self.fallback_when_offline_unable_online))
        object.__setattr__(self, "no_match_default_when_online_capable", TerminalAction(self.no_match_default_when_online_capable))
        object.__setattr__(self, "no_match_default_when_offline_only", TerminalAction(self.no_match_default_when_offline_only))
        if self.fallback_when_offline_unable_online == TerminalAction.ARQC:
            raise TaaError("INVALID_TAA_PROFILE", "Fallback offline no puede ser ARQC.")
        if self.no_match_default_when_online_capable == TerminalAction.AAC:
            raise TaaError("INVALID_TAA_PROFILE", "Default online-capable debe ser TC o ARQC.")
        if self.no_match_default_when_offline_only == TerminalAction.ARQC:
            raise TaaError("INVALID_TAA_PROFILE", "Default offline-only no puede ser ARQC.")


@dataclass(frozen=True)
class TaaInput:
    tvr: Tvr
    tsi: Tsi
    tac: ActionCodes
    iac: ActionCodes
    terminal_online_capable: bool
    profile: TaaProfile = TaaProfile()
    cvm: CvmEvaluation | None = None
    risk: RiskEvaluation | None = None


@dataclass(frozen=True)
class TaaEvaluation:
    proposed_action: TerminalAction
    reason: str
    tvr: Tvr
    tsi: Tsi
    cvm: CvmEvaluation | None
    risk: RiskEvaluation | None

    def to_evidence(self) -> dict[str, Any]:
        return {
            "proposed_action": self.proposed_action.value,
            "reason": self.reason,
            "cvm_outcome": self.cvm.outcome.value if self.cvm else None,
            "risk_online_required": self.risk.online_required if self.risk else None,
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
        }


def _or5(left: bytes, right: bytes) -> bytes:
    return bytes(a | b for a, b in zip(left, right))


def _intersects(left: bytes, right: bytes) -> bool:
    return any(a & b for a, b in zip(left, right))


def evaluate_taa(input: TaaInput) -> TaaEvaluation:
    tvr = input.tvr.to_bytes()
    if _intersects(tvr, _or5(input.tac.denial, input.iac.denial)):
        action, reason = TerminalAction.AAC, "DENIAL_ACTION_CODE_MATCHED"
    elif input.terminal_online_capable and _intersects(tvr, _or5(input.tac.online, input.iac.online)):
        action, reason = TerminalAction.ARQC, "ONLINE_ACTION_CODE_MATCHED"
    elif not input.terminal_online_capable and _intersects(tvr, _or5(input.tac.default, input.iac.default)):
        action = input.profile.fallback_when_offline_unable_online
        reason = "DEFAULT_ACTION_CODE_MATCHED_OFFLINE"
    elif input.terminal_online_capable:
        action = input.profile.no_match_default_when_online_capable
        reason = "NO_MATCH_ONLINE_CAPABLE"
    else:
        action = input.profile.no_match_default_when_offline_only
        reason = "NO_MATCH_OFFLINE_ONLY"
    return TaaEvaluation(action, reason, input.tvr, input.tsi, input.cvm, input.risk)
