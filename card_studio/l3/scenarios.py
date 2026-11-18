from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from card_studio.interfaces.base import InterfaceKind
from card_studio.kernels.base import KernelAdapter, KernelCapabilities, KernelError, KernelSession
from card_studio.readers.base import CloseEvidence
from card_studio.transactions.model import (
    ExecutionState,
    SimulationScenario,
    TransactionRequest,
    TransactionResult,
    TransactionType,
)
from card_studio.transactions.recovery import RecoveryClass
from card_studio.transactions.store import utc_now


@dataclass
class _LabSession:
    session: KernelSession
    request: TransactionRequest
    card_interface: Any
    profile: Any
    closed: bool = False


class LabKernelAdapter(KernelAdapter):
    kernel_ref = "mock-kernel-v1"
    kernel_version = "1"
    kernel_mode = "LAB / SYNTHETIC"

    def __init__(self):
        self._sessions: dict[str, _LabSession] = {}
        self._results: dict[str, TransactionResult] = {}
        self._execution_counts: dict[str, int] = {}

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities(
            interfaces=tuple(item.value for item in InterfaceKind),
            transaction_types=tuple(item.value for item in TransactionType),
            simulation=True,
            online_processing=True,
        )

    def open_session(self, request, card_interface=None, profile=None) -> KernelSession:
        if request.kernel_ref != self.kernel_ref:
            raise KernelError("KERNEL_NOT_FOUND", "kernel_ref no corresponde al kernel LAB.")
        session = KernelSession(
            session_id=f"lab-{uuid.uuid4()}",
            operation_id=request.operation_id,
            profile_ref=request.profile_ref,
            interface=request.interface,
            kernel_ref=request.kernel_ref,
            simulation=True,
        )
        self._sessions[session.session_id] = _LabSession(session, request, card_interface, profile)
        return session

    def _open(self, session: KernelSession) -> _LabSession:
        value = self._sessions.get(session.session_id)
        if value is None or value.closed:
            raise KernelError("INVALID_STATE", "La sesión del kernel no está abierta.")
        return value

    @staticmethod
    def _transitions(request: TransactionRequest, state: ExecutionState, reason: str) -> list[dict[str, Any]]:
        return [{
            "previous_state": "IN_PROGRESS",
            "new_state": state.value,
            "reason": reason,
            "timestamp": utc_now(),
            "operation_id": request.operation_id,
        }]

    def _result(
        self,
        request: TransactionRequest,
        state: ExecutionState,
        outcome: str | None,
        recovery: RecoveryClass,
        error: str | None,
        selected_aid: str | None,
        command_sent: bool,
    ) -> TransactionResult:
        details = {
            "scenario": request.scenario,
            "command_sent": command_sent,
            "state_transitions": self._transitions(request, state, error or outcome or state.value),
        }
        if selected_aid:
            details["selected_aid"] = selected_aid
        if state == ExecutionState.SUCCEEDED:
            details.update({
                "cvm_result": "NO_CVM_SIMULATED",
                "risk_result": "LAB_ACCEPTED" if outcome == "APPROVED" else "LAB_REVIEW",
                "action_result": outcome,
            })
        return TransactionResult(request, state, outcome, recovery, error, details)

    def execute(self, session: KernelSession, request: TransactionRequest, deadline: float | None = None):
        opened = self._open(session)
        if opened.request.fingerprint() != request.fingerprint():
            raise KernelError("SESSION_REQUEST_MISMATCH", "El request no corresponde a la sesión.")
        requirements = opened.profile.kernel_requirements
        if requirements.get("malformed_tlv") is True:
            raise KernelError("MALFORMED_TLV", "Vector TLV mal formado simulado.")
        if requirements.get("invalid_state") is True:
            raise KernelError("INVALID_STATE", "Transición de estado inválida simulada.")
        applications = sorted(opened.profile.applications, key=lambda item: (item.priority, item.aid))
        selected = None
        if request.application_aid:
            selected = next((item.aid for item in applications if item.aid == request.application_aid), None)
            if selected is None:
                raise KernelError("APPLICATION_NOT_SUPPORTED", "La aplicación solicitada no está en el perfil.")
        elif applications:
            selected = applications[0].aid
        if opened.card_interface is not None and requirements.get("exchange_before_processing") is True:
            opened.card_interface.exchange("00000000")
        self._execution_counts[request.operation_id] = self._execution_counts.get(request.operation_id, 0) + 1
        attempt = self._execution_counts[request.operation_id]
        expired = deadline is not None and deadline <= time.monotonic()
        scenario = request.scenario
        if scenario == SimulationScenario.FAILURE_BEFORE_SEND.value and attempt == 1:
            result = self._result(
                request, ExecutionState.FAILED, None, RecoveryClass.SAFE_TO_RETRY,
                "FAILURE_BEFORE_SEND", selected, False,
            )
        elif scenario == SimulationScenario.RETRY.value and attempt == 1:
            result = self._result(
                request, ExecutionState.FAILED, None, RecoveryClass.SAFE_TO_RETRY,
                "SAFE_RETRY_REQUIRED", selected, False,
            )
        elif scenario in {SimulationScenario.TIMEOUT.value, SimulationScenario.UNCERTAIN.value} or expired:
            result = self._result(
                request, ExecutionState.UNCERTAIN, None, RecoveryClass.QUERY_REQUIRED,
                "TIMEOUT_AFTER_SEND" if scenario == SimulationScenario.TIMEOUT.value or expired else "UNCERTAIN_RESULT",
                selected, True,
            )
        elif scenario == SimulationScenario.DECLINED.value:
            result = self._result(
                request, ExecutionState.FAILED, "DECLINED", RecoveryClass.NON_RETRYABLE,
                "SIMULATED_DECLINE", selected, True,
            )
        elif scenario == SimulationScenario.CANCELLATION.value:
            result = self._result(
                request, ExecutionState.CANCELLED, None, RecoveryClass.NON_RETRYABLE,
                "OPERATOR_CANCELLED", selected, False,
            )
        elif scenario == SimulationScenario.ONLINE_REQUIRED.value:
            result = self._result(
                request, ExecutionState.SUCCEEDED, "ONLINE_REQUIRED", RecoveryClass.NON_RETRYABLE,
                None, selected, True,
            )
        elif scenario == SimulationScenario.ACTION_REQUIRED.value:
            result = self._result(
                request, ExecutionState.SUCCEEDED, "ACTION_REQUIRED", RecoveryClass.NON_RETRYABLE,
                None, selected, True,
            )
        else:
            result = self._result(
                request, ExecutionState.SUCCEEDED, "APPROVED", RecoveryClass.NON_RETRYABLE,
                None, selected, True,
            )
        self._results[request.operation_id] = result
        return result

    def complete_online(self, session: KernelSession, request: TransactionRequest, host_response):
        self._open(session)
        state = ExecutionState(str(host_response["execution_state"]))
        outcome = host_response.get("functional_result")
        result = self._result(
            request,
            state,
            str(outcome) if outcome else None,
            RecoveryClass(str(host_response.get("recovery_action", "NON_RETRYABLE"))),
            host_response.get("error_code"),
            self._results.get(request.operation_id).details.get("selected_aid") if request.operation_id in self._results else None,
            True,
        )
        details = dict(result.details)
        details.update({
            "host_ref": host_response.get("host_ref"),
            "host_version": host_response.get("host_version"),
        })
        result = TransactionResult(request, state, result.outcome, result.recovery, result.error_code, details)
        self._results[request.operation_id] = result
        return result

    def query(self, operation_id: str) -> TransactionResult:
        try:
            result = self._results[operation_id]
        except KeyError as exc:
            raise KernelError("OPERATION_NOT_FOUND", "Operación desconocida para el kernel.", RecoveryClass.QUERY_REQUIRED) from exc
        if result.outcome == "ONLINE_REQUIRED":
            return self._result(
                result.request, ExecutionState.UNCERTAIN, None, RecoveryClass.QUERY_REQUIRED,
                "ONLINE_HOST_QUERY_REQUIRED", result.details.get("selected_aid"), True,
            )
        if result.execution_state == ExecutionState.UNCERTAIN:
            resolved = self._result(
                result.request, ExecutionState.SUCCEEDED, "APPROVED", RecoveryClass.NON_RETRYABLE,
                None, result.details.get("selected_aid"), True,
            )
            details = dict(resolved.details)
            details["action_result"] = "RECOVERED_BY_EXPLICIT_QUERY"
            resolved = TransactionResult(
                result.request, resolved.execution_state, resolved.outcome,
                resolved.recovery, resolved.error_code, details,
            )
            self._results[operation_id] = resolved
            return resolved
        return result

    def cancel(self, session: KernelSession) -> TransactionResult:
        opened = self._open(session)
        existing = self._results.get(opened.request.operation_id)
        if existing is not None:
            return existing
        result = self._result(
            opened.request, ExecutionState.CANCELLED, None, RecoveryClass.NON_RETRYABLE,
            "OPERATOR_CANCELLED", None, False,
        )
        self._results[opened.request.operation_id] = result
        return result

    def close(self, session: KernelSession) -> CloseEvidence:
        opened = self._open(session)
        opened.closed = True
        return CloseEvidence(session.session_id, closed=True, simulated=True)

    def execution_count(self, operation_id: str) -> int:
        return self._execution_counts.get(operation_id, 0)


@dataclass(frozen=True)
class ScenarioDefinition:
    case_id: str
    interface: str
    scenario: str
    transaction_type: str = "purchase"
    expectation: str = "PASS"


def matrix_definitions() -> list[ScenarioDefinition]:
    definitions = [
        ScenarioDefinition(f"{interface.lower()}-{scenario}", interface, scenario)
        for interface in ("CONTACT", "CONTACTLESS")
        for scenario in ("approved", "declined", "online_required", "timeout")
    ]
    definitions.extend([
        ScenarioDefinition(f"{interface.lower()}-reversal", interface, "approved", "reversal")
        for interface in ("CONTACT", "CONTACTLESS")
    ])
    definitions.extend([
        ScenarioDefinition("unsupported-application", "CONTACT", "approved"),
        ScenarioDefinition("multiple-application", "CONTACTLESS", "approved"),
        ScenarioDefinition("card-removed", "CONTACT", "approved"),
        ScenarioDefinition("reader-removed", "CONTACTLESS", "approved"),
        ScenarioDefinition("malformed-tlv", "CONTACT", "approved"),
        ScenarioDefinition("invalid-state", "CONTACTLESS", "approved"),
        ScenarioDefinition("host-timeout", "CONTACT", "online_required"),
        ScenarioDefinition("host-duplicate", "CONTACTLESS", "online_required"),
        ScenarioDefinition("idempotency-conflict", "CONTACT", "approved"),
        ScenarioDefinition("restart-in-progress", "CONTACTLESS", "approved"),
        ScenarioDefinition("restart-uncertain", "CONTACT", "timeout"),
        ScenarioDefinition("recovery-query", "CONTACTLESS", "timeout"),
        ScenarioDefinition("safe-retry", "CONTACT", "failure_before_send"),
        ScenarioDefinition("unsafe-retry-blocked", "CONTACTLESS", "declined"),
        ScenarioDefinition("partial-refund", "CONTACT", "approved", "refund"),
        ScenarioDefinition("excessive-refund", "CONTACTLESS", "approved", "refund"),
        ScenarioDefinition("duplicate-reversal", "CONTACT", "approved", "reversal"),
    ])
    return definitions
