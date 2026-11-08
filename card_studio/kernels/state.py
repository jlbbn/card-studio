from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .base import KernelError


class KernelState(str, Enum):
    CREATED = "CREATED"
    SESSION_OPEN = "SESSION_OPEN"
    APPLICATION_SELECTION = "APPLICATION_SELECTION"
    APPLICATION_INITIALIZATION = "APPLICATION_INITIALIZATION"
    CARD_DATA_PROCESSING = "CARD_DATA_PROCESSING"
    PROCESSING_RESTRICTIONS = "PROCESSING_RESTRICTIONS"
    CVM_PROCESSING = "CVM_PROCESSING"
    TERMINAL_RISK_MANAGEMENT = "TERMINAL_RISK_MANAGEMENT"
    TERMINAL_ACTION_ANALYSIS = "TERMINAL_ACTION_ANALYSIS"
    OFFLINE_DATA_AUTHENTICATION = "OFFLINE_DATA_AUTHENTICATION"
    CARD_ACTION_ANALYSIS = "CARD_ACTION_ANALYSIS"
    C8_OUTCOME = "C8_OUTCOME"
    ONLINE_PROCESSING = "ONLINE_PROCESSING"
    ISSUER_AUTHENTICATION = "ISSUER_AUTHENTICATION"
    ISSUER_SCRIPTS_BEFORE_FINAL_GAC = "ISSUER_SCRIPTS_BEFORE_FINAL_GAC"
    FINAL_CARD_ACTION_ANALYSIS = "FINAL_CARD_ACTION_ANALYSIS"
    ISSUER_SCRIPTS_AFTER_FINAL_GAC = "ISSUER_SCRIPTS_AFTER_FINAL_GAC"
    COMPLETION = "COMPLETION"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({
    KernelState.APPROVED,
    KernelState.DECLINED,
    KernelState.FAILED,
    KernelState.UNCERTAIN,
    KernelState.CANCELLED,
})

ALLOWED_TRANSITIONS = {
    KernelState.CREATED: {KernelState.SESSION_OPEN, KernelState.FAILED, KernelState.CANCELLED},
    KernelState.SESSION_OPEN: {KernelState.APPLICATION_SELECTION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.APPLICATION_SELECTION: {KernelState.APPLICATION_INITIALIZATION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.APPLICATION_INITIALIZATION: {KernelState.CARD_DATA_PROCESSING, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.CARD_DATA_PROCESSING: {KernelState.PROCESSING_RESTRICTIONS, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.PROCESSING_RESTRICTIONS: {KernelState.CVM_PROCESSING, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.CVM_PROCESSING: {KernelState.TERMINAL_RISK_MANAGEMENT, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.TERMINAL_RISK_MANAGEMENT: {KernelState.TERMINAL_ACTION_ANALYSIS, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.TERMINAL_ACTION_ANALYSIS: {KernelState.OFFLINE_DATA_AUTHENTICATION, KernelState.CARD_ACTION_ANALYSIS, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.OFFLINE_DATA_AUTHENTICATION: {KernelState.CARD_ACTION_ANALYSIS, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.CARD_ACTION_ANALYSIS: {KernelState.C8_OUTCOME, KernelState.ONLINE_PROCESSING, KernelState.COMPLETION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.C8_OUTCOME: {KernelState.ONLINE_PROCESSING, KernelState.COMPLETION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.ONLINE_PROCESSING: {KernelState.ISSUER_AUTHENTICATION, KernelState.ISSUER_SCRIPTS_BEFORE_FINAL_GAC, KernelState.FINAL_CARD_ACTION_ANALYSIS, KernelState.COMPLETION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.ISSUER_AUTHENTICATION: {KernelState.ISSUER_SCRIPTS_BEFORE_FINAL_GAC, KernelState.FINAL_CARD_ACTION_ANALYSIS, KernelState.COMPLETION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.ISSUER_SCRIPTS_BEFORE_FINAL_GAC: {KernelState.FINAL_CARD_ACTION_ANALYSIS, KernelState.COMPLETION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.FINAL_CARD_ACTION_ANALYSIS: {KernelState.ISSUER_SCRIPTS_AFTER_FINAL_GAC, KernelState.COMPLETION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.ISSUER_SCRIPTS_AFTER_FINAL_GAC: {KernelState.COMPLETION, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
    KernelState.COMPLETION: {KernelState.APPROVED, KernelState.DECLINED, KernelState.FAILED, KernelState.UNCERTAIN, KernelState.CANCELLED},
}


@dataclass(frozen=True)
class StateTransition:
    previous_state: str
    new_state: str
    reason: str
    timestamp: str
    operation_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "operation_id": self.operation_id,
        }


class KernelStateMachine:
    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        self.state = KernelState.CREATED
        self._history: list[StateTransition] = []

    @property
    def history(self) -> tuple[StateTransition, ...]:
        return tuple(self._history)

    def transition(self, new_state: KernelState | str, reason: str) -> StateTransition:
        selected = KernelState(new_state)
        allowed = ALLOWED_TRANSITIONS.get(self.state, set())
        if selected not in allowed:
            raise KernelError(
                "INVALID_STATE_TRANSITION",
                f"Transición no permitida: {self.state.value} -> {selected.value}.",
            )
        previous = self.state
        self.state = selected
        transition = StateTransition(
            previous_state=previous.value,
            new_state=selected.value,
            reason=reason,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            operation_id=self.operation_id,
        )
        self._history.append(transition)
        return transition

    def sanitized_history(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._history]
