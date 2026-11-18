from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from card_studio.interfaces.base import CardInterface, InterfaceKind
from card_studio.profiles.model import CardProfile
from card_studio.readers.base import CloseEvidence
from card_studio.transactions.model import (
    ExecutionState,
    FunctionalResult,
    SimulationScenario,
    TransactionRequest,
    TransactionResult,
    TransactionType,
)
from card_studio.transactions.recovery import RecoveryClass

from .base import KernelAdapter, KernelCapabilities, KernelError, KernelSession


class MockKernelAdapter(KernelAdapter):
    kernel_ref = "mock-kernel-v1"
    kernel_version = "1"
    kernel_mode = "SIMULATED"

    def __init__(
        self,
        result_loader: Callable[[str], TransactionResult | Mapping[str, Any] | None] | None = None,
    ):
        self._result_loader = result_loader
        self._sessions: dict[str, TransactionRequest] = {}
        self._closed: set[str] = set()
        self._requests_by_operation: dict[str, TransactionRequest] = {}
        self._requests_by_key: dict[str, TransactionRequest] = {}
        self._results: dict[str, TransactionResult] = {}
        self._execution_counts: dict[str, int] = {}

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities(
            interfaces=tuple(item.value for item in InterfaceKind),
            transaction_types=tuple(item.value for item in TransactionType),
            simulation=True,
            online_processing=True,
        )

    def open_session(
        self,
        request: TransactionRequest,
        card_interface: CardInterface | None = None,
        profile: CardProfile | None = None,
    ) -> KernelSession:
        if request.kernel_ref != self.kernel_ref:
            raise KernelError("KERNEL_NOT_FOUND", "El kernel_ref no corresponde al mock.")
        if request.interface not in self.capabilities().interfaces:
            raise KernelError("UNSUPPORTED_INTERFACE", "El mock no admite la interfaz solicitada.")
        if card_interface is not None and card_interface.kind.value != request.interface:
            raise KernelError("INTERFACE_MISMATCH", "La CardInterface no coincide con el request.")
        if profile is not None:
            canonical = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
            if request.profile_ref != canonical:
                raise KernelError("PROFILE_REF_MISMATCH", "profile_ref no coincide con el perfil.")
            if not profile.supports_interface(request.interface):
                raise KernelError("PROFILE_INTERFACE_MISMATCH", "El perfil no admite la interfaz solicitada.")
            required_kernel = profile.kernel_requirements.get("kernel_ref")
            if required_kernel is not None and required_kernel != self.kernel_ref:
                raise KernelError("PROFILE_KERNEL_MISMATCH", "El perfil exige otro kernel.")
        session = KernelSession(
            session_id=f"ks-{uuid.uuid4()}",
            operation_id=request.operation_id,
            profile_ref=request.profile_ref,
            interface=request.interface,
            kernel_ref=request.kernel_ref,
            simulation=True,
        )
        self._sessions[session.session_id] = request
        return session

    def _require_open(self, session: KernelSession) -> TransactionRequest:
        if session.session_id not in self._sessions or session.session_id in self._closed:
            raise KernelError("SESSION_CLOSED", "La sesión del kernel mock no está abierta.")
        return self._sessions[session.session_id]

    def _existing(self, request: TransactionRequest) -> TransactionResult | None:
        by_operation = self._requests_by_operation.get(request.operation_id)
        if by_operation is not None:
            if by_operation.fingerprint() != request.fingerprint():
                raise KernelError("DUPLICATE_OPERATION", "operation_id ya existe con otro request.")
            return self._results[request.operation_id]
        by_key = self._requests_by_key.get(request.idempotency_key)
        if by_key is not None:
            if by_key.fingerprint() != request.fingerprint():
                raise KernelError("IDEMPOTENCY_CONFLICT", "idempotency_key ya existe con otro request.")
            return self._results[by_key.operation_id]
        return None

    def execute(
        self,
        session: KernelSession,
        request: TransactionRequest,
        deadline: float | None = None,
    ) -> TransactionResult:
        original = self._require_open(session)
        if original.fingerprint() != request.fingerprint():
            raise KernelError("SESSION_REQUEST_MISMATCH", "El request no corresponde a la sesión.")
        existing = self._existing(request)
        if existing is not None:
            return existing

        self._execution_counts[request.operation_id] = self._execution_counts.get(request.operation_id, 0) + 1
        expired = deadline is not None and deadline <= time.monotonic()
        if request.scenario == SimulationScenario.FAILURE_BEFORE_SEND.value or expired:
            result = TransactionResult(
                request=request,
                execution_state=ExecutionState.FAILED,
                outcome=None,
                recovery=RecoveryClass.SAFE_TO_RETRY,
                error_code="TIMEOUT_BEFORE_SEND" if expired else "SIMULATED_FAILURE_BEFORE_SEND",
                details={"scenario": request.scenario, "command_sent": False, "simulation": True},
            )
        elif request.scenario in {SimulationScenario.TIMEOUT.value, SimulationScenario.UNCERTAIN.value}:
            result = TransactionResult(
                request=request,
                execution_state=ExecutionState.UNCERTAIN,
                outcome=None,
                recovery=RecoveryClass.QUERY_REQUIRED,
                error_code="TIMEOUT_AFTER_SEND" if request.scenario == SimulationScenario.TIMEOUT.value else "UNCERTAIN_RESULT",
                details={"scenario": request.scenario, "command_sent": True, "simulation": True},
            )
        elif request.scenario == SimulationScenario.ONLINE_REQUIRED.value:
            result = TransactionResult(
                request=request,
                execution_state=ExecutionState.SUCCEEDED,
                outcome=FunctionalResult.ONLINE_REQUIRED.value,
                recovery=RecoveryClass.NON_RETRYABLE,
                details={"scenario": request.scenario, "command_sent": True, "simulation": True},
            )
        elif request.scenario == SimulationScenario.ACTION_REQUIRED.value:
            result = TransactionResult(
                request=request,
                execution_state=ExecutionState.SUCCEEDED,
                outcome=FunctionalResult.ACTION_REQUIRED.value,
                recovery=RecoveryClass.OPERATOR_REVIEW,
                details={"scenario": request.scenario, "command_sent": True, "simulation": True},
            )
        elif request.scenario == SimulationScenario.DECLINED.value:
            result = TransactionResult(
                request=request,
                execution_state=ExecutionState.FAILED,
                outcome=FunctionalResult.DECLINED.value,
                recovery=RecoveryClass.NON_RETRYABLE,
                error_code="SIMULATED_DECLINE",
                details={"scenario": request.scenario, "command_sent": True, "simulation": True},
            )
        elif request.scenario == SimulationScenario.CANCELLATION.value:
            result = TransactionResult(
                request=request,
                execution_state=ExecutionState.CANCELLED,
                outcome=None,
                recovery=RecoveryClass.NON_RETRYABLE,
                error_code="OPERATOR_CANCELLED",
                details={"scenario": request.scenario, "command_sent": False, "simulation": True},
            )
        else:
            result = TransactionResult(
                request=request,
                execution_state=ExecutionState.SUCCEEDED,
                outcome=FunctionalResult.APPROVED.value,
                recovery=RecoveryClass.NON_RETRYABLE,
                details={"scenario": request.scenario, "command_sent": True, "simulation": True},
            )
        self._store(request, result)
        return result

    def complete_online(
        self,
        session: KernelSession,
        request: TransactionRequest,
        host_response: Mapping[str, Any],
    ) -> TransactionResult:
        original = self._require_open(session)
        if original.fingerprint() != request.fingerprint():
            raise KernelError("SESSION_REQUEST_MISMATCH", "El request no corresponde a la sesión.")
        current = self._results.get(request.operation_id)
        if current is None or current.outcome != FunctionalResult.ONLINE_REQUIRED.value:
            if current is not None:
                return current
            raise KernelError("INVALID_ONLINE_COMPLETION", "No hay una operación ONLINE_REQUIRED pendiente.")
        if not isinstance(host_response, Mapping):
            raise KernelError("INVALID_HOST_RESPONSE", "host_response debe ser un mapping SIMULATED.")
        if host_response.get("simulation") is False or host_response.get("synthetic") is False:
            raise KernelError("INVALID_HOST_RESPONSE", "La respuesta del host debe ser SIMULATED.")
        raw_decision = host_response.get("decision", host_response.get("outcome", host_response.get("status")))
        if raw_decision is None and isinstance(host_response.get("approved"), bool):
            raw_decision = "APPROVED" if host_response["approved"] else "DECLINED"
        decision = str(raw_decision or "").upper()
        if decision not in {"APPROVED", "DECLINED"}:
            raise KernelError("INVALID_HOST_RESPONSE", "La decisión SIMULATED debe ser APPROVED o DECLINED.")
        result = TransactionResult(
            request=request,
            execution_state=ExecutionState.SUCCEEDED if decision == "APPROVED" else ExecutionState.FAILED,
            outcome=decision,
            recovery=RecoveryClass.NON_RETRYABLE,
            error_code=None if decision == "APPROVED" else "SIMULATED_HOST_DECLINE",
            details={
                "scenario": request.scenario,
                "command_sent": True,
                "simulation": True,
                "host_response": "SIMULATED",
                "host_decision": decision,
            },
        )
        self._store(request, result)
        return result

    def _store(self, request: TransactionRequest, result: TransactionResult) -> TransactionResult:
        self._requests_by_operation[request.operation_id] = request
        self._requests_by_key[request.idempotency_key] = request
        self._results[request.operation_id] = result
        return result

    def query(self, operation_id: str) -> TransactionResult:
        known = self._results.get(operation_id)
        if known is not None:
            return known
        if self._result_loader is not None:
            try:
                loaded = self._result_loader(operation_id)
            except Exception as exc:
                raise KernelError(
                    "OPERATION_QUERY_FAILED",
                    "El almacenamiento externo no pudo resolver la operación.",
                    RecoveryClass.OPERATOR_REVIEW,
                ) from exc
            if loaded is not None:
                result = loaded if isinstance(loaded, TransactionResult) else TransactionResult.from_dict(loaded)
                if result.operation_id != operation_id:
                    raise KernelError(
                        "OPERATION_ID_MISMATCH",
                        "El resultado externo pertenece a otra operación.",
                        RecoveryClass.OPERATOR_REVIEW,
                    )
                return self._store(result.request, result)
        raise KernelError(
            "OPERATION_NOT_FOUND",
            "No existe una operación mock con ese operation_id.",
            recovery=RecoveryClass.OPERATOR_REVIEW,
        )

    def cancel(self, session: KernelSession) -> TransactionResult:
        if session.session_id not in self._sessions:
            raise KernelError("SESSION_NOT_FOUND", "La sesión del kernel mock no existe.")
        request = self._sessions[session.session_id]
        existing = self._results.get(request.operation_id)
        if existing is not None:
            return existing
        if session.session_id in self._closed:
            raise KernelError("SESSION_CLOSED", "La sesión del kernel mock no está abierta.")
        result = TransactionResult(
            request=request,
            execution_state=ExecutionState.CANCELLED,
            outcome=None,
            recovery=RecoveryClass.NON_RETRYABLE,
            error_code="OPERATOR_CANCELLED",
            details={"scenario": SimulationScenario.CANCELLATION.value, "command_sent": False, "simulation": True},
        )
        self._store(request, result)
        return result

    def close(self, session: KernelSession) -> CloseEvidence:
        if session.session_id not in self._sessions:
            raise KernelError("SESSION_NOT_FOUND", "La sesión del kernel mock no existe.")
        self._closed.add(session.session_id)
        return CloseEvidence(session.session_id, closed=True, simulated=True)

    def evidence(self, operation_id: str) -> dict[str, Any]:
        return self.query(operation_id).to_evidence()

    def execution_count(self, operation_id: str) -> int:
        return self._execution_counts.get(operation_id, 0)
