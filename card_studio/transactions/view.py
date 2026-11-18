from __future__ import annotations

from typing import Any, Mapping


def transaction_view(transaction: Mapping[str, Any]) -> dict[str, Any]:
    state = str(transaction.get("execution_state", "UNKNOWN"))
    functional = transaction.get("functional_result")
    recovery = transaction.get("recovery_action")
    if state == "SUCCEEDED" and functional == "APPROVED":
        status = "LAB — APPROVED"
    elif state == "FAILED" and functional == "DECLINED":
        status = "LAB — DECLINED"
    else:
        status = f"LAB — {state}"
    if state == "UNCERTAIN":
        actions = ["query", "review"]
    elif state == "FAILED" and recovery == "SAFE_TO_RETRY":
        actions = ["retry"]
    else:
        actions = []
    return {
        "operation_id": transaction.get("operation_id"),
        "status": status,
        "execution_state": state,
        "functional_result": functional,
        "recovery_action": recovery,
        "result_reference": transaction.get("result_reference"),
        "simulation": True,
        "mode": "LAB / SYNTHETIC",
        "physical_validation": "NOT_EXECUTED",
        "actions": actions,
        "retry_visible": actions == ["retry"],
        "query_visible": "query" in actions,
        "review_visible": "review" in actions,
    }


def readiness_view(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lab_readiness": report.get("status", "NOT_RUN"),
        "gates": list(report.get("gates", [])),
        "certification": "NOT COMPLETED",
        "physical_validation": "PENDING",
        "mode": "LAB / SYNTHETIC",
    }


def card_flow_view(operation: Mapping[str, Any]) -> dict[str, Any]:
    state = str(operation.get("executionState", operation.get("execution_state", "UNKNOWN")))
    functional = operation.get("functionalResult", operation.get("functional_result"))
    if state == "UNCERTAIN":
        actions = ["query", "review"]
    else:
        actions = []
    return {
        "operation_id": operation.get("operationId", operation.get("operation_id")),
        "channel": "CARD",
        "execution_state": state,
        "functional_result": functional,
        "status": f"CARD — {functional or state}",
        "actions": actions,
        "query_visible": "query" in actions,
        "review_visible": "review" in actions,
        "cash_state": None,
        "simulation": True,
        "physical_validation": "NOT_EXECUTED",
    }
