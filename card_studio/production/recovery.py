from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RecoveryDecision:
    state: str
    action: str
    automatic_retry: bool
    operator_review: bool


def recovery_decision(result: Mapping[str, object]) -> RecoveryDecision:
    state = str(result.get("transaction_state", "UNCERTAIN"))
    action = str(result.get("recovery_action", "QUERY_REQUIRED"))
    if state == "UNCERTAIN":
        if action not in {"QUERY_REQUIRED", "READ_BACK_REQUIRED", "OPERATOR_REVIEW"}:
            action = "QUERY_REQUIRED"
        return RecoveryDecision("UNCERTAIN", action, False, action == "OPERATOR_REVIEW")
    if state == "FAILED" and action == "SAFE_TO_RETRY":
        return RecoveryDecision("FAILED", "SAFE_TO_RETRY", False, False)
    if state in {"APPROVED", "DECLINED", "REVERSED"}:
        return RecoveryDecision(state, "NON_RETRYABLE", False, False)
    if state == "FAILED":
        return RecoveryDecision("FAILED", "OPERATOR_REVIEW" if action == "OPERATOR_REVIEW" else "NON_RETRYABLE", False, action == "OPERATOR_REVIEW")
    return RecoveryDecision("UNCERTAIN", "QUERY_REQUIRED", False, False)


def assert_no_automatic_decline(previous: str, resolved: str, *, definitive_host_result: bool) -> None:
    if previous == "UNCERTAIN" and resolved == "DECLINED" and not definitive_host_result:
        raise ValueError("UNCERTAIN no puede transformarse en DECLINED sin resultado definitivo.")
