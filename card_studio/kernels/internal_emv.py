from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from card_studio.emv.decision import combine_actions, decide_cvm, decide_risk, terminal_action
from card_studio.emv.online import OnlineCompletionResult, OnlineFlowConfig, process_online_completion
from card_studio.emv.runtime import EmvFlowConfig, execute_emv_flow
from card_studio.emv.unpredictable import UnpredictableNumberBoundary
from card_studio.emv.velocity import DurableVelocityCounters, VelocityPolicy
from card_studio.emv.protocol import (
    LaboratoryProtocolError,
    laboratory_commands,
    parse_card_action_response,
    parse_initialize_response,
    parse_read_data_response,
    parse_select_response,
)
from card_studio.interfaces.base import CardInterface, InterfaceKind
from card_studio.profiles.model import CardProfile
from card_studio.profiles.authority import configuration_provenance
from card_studio.emv.status import Tsi, Tvr
from card_studio.readers.base import CloseEvidence, ReaderError
from card_studio.tlv import TlvLimits
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
from .control import ControlledCardInterface, SessionExecutionControl
from .result import KernelResult
from .session import InternalSession
from .state import KernelState, KernelStateMachine


class _AfterSendTimeout(Exception):
    pass


class _BeforeSendTimeout(Exception):
    pass


class InternalEmvKernel(KernelAdapter):
    kernel_ref = "internal-emv-lab-v1"
    kernel_version = "1.0.0"
    kernel_mode = "LAB / SYNTHETIC"

    def __init__(
        self,
        result_loader: Callable[[str], TransactionResult | Mapping[str, Any] | None] | None = None,
        tlv_limits: TlvLimits | None = None,
        unpredictable_number_source: Callable[[], bytes] | None = None,
        velocity_store: DurableVelocityCounters | None = None,
    ):
        self._result_loader = result_loader
        self._tlv_limits = tlv_limits or TlvLimits()
        self._un_boundary = UnpredictableNumberBoundary(unpredictable_number_source) if unpredictable_number_source is not None else None
        self._velocity_store = velocity_store
        self._sessions: dict[str, InternalSession] = {}
        self._requests_by_operation: dict[str, TransactionRequest] = {}
        self._requests_by_key: dict[str, TransactionRequest] = {}
        self._results: dict[str, TransactionResult] = {}
        self._execution_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    def capabilities(self) -> KernelCapabilities:
        return KernelCapabilities(
            interfaces=(InterfaceKind.CONTACT.value, InterfaceKind.CONTACTLESS.value, InterfaceKind.VIRTUAL.value),
            transaction_types=tuple(item.value for item in TransactionType),
            simulation=True,
            online_processing=True,
        )

    @staticmethod
    def _canonical_profile_ref(profile: CardProfile) -> str:
        return f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"

    def _validate_profile(self, request: TransactionRequest, profile: CardProfile) -> None:
        canonical = self._canonical_profile_ref(profile)
        if request.profile_ref != canonical:
            raise KernelError("PROFILE_REF_MISMATCH", "profile_ref no coincide con profile_id:version:digest.")
        try:
            profile.assert_simulation_enabled()
        except ValueError as exc:
            code = getattr(exc, "code", "PROFILE_NOT_EXECUTABLE")
            raise KernelError(code, "El perfil no está habilitado para simulación.") from exc
        if not profile.supports_interface(request.interface):
            raise KernelError("PROFILE_INTERFACE_MISMATCH", "El perfil no admite la interfaz solicitada.")
        required_kernel = profile.kernel_requirements.get("kernel_ref")
        if required_kernel is not None and required_kernel != self.kernel_ref:
            raise KernelError("PROFILE_KERNEL_MISMATCH", "El perfil exige otro kernel.")
        if profile.kernel_requirements.get("simulation") is not True:
            raise KernelError("SIMULATION_REQUIRED", "El perfil debe exigir simulation=true.")

    def open_session(
        self,
        request: TransactionRequest,
        card_interface: CardInterface | None = None,
        profile: CardProfile | None = None,
    ) -> KernelSession:
        if request.kernel_ref != self.kernel_ref:
            raise KernelError("KERNEL_NOT_FOUND", "kernel_ref no corresponde al kernel interno LAB.")
        if request.interface not in self.capabilities().interfaces:
            raise KernelError("UNSUPPORTED_INTERFACE", "MAGNETIC_SIMULATED no es una interfaz EMV L2.")
        if profile is None or not isinstance(profile, CardProfile):
            raise KernelError("PROFILE_REQUIRED", "El kernel interno requiere un CardProfile validado.")
        self._validate_profile(request, profile)
        if card_interface is None:
            raise KernelError("CARD_INTERFACE_REQUIRED", "Se requiere una CardInterface sintética.")
        if card_interface.kind.value != request.interface:
            raise KernelError("INTERFACE_MISMATCH", "La CardInterface no coincide con el request.")
        connection = card_interface.connection
        if not card_interface.session.simulated or (connection is not None and not connection.simulated):
            raise KernelError("NON_SIMULATED_INTERFACE", "El kernel interno solo admite interfaces sintéticas.")
        session_id = f"iks-{uuid.uuid4()}"
        public = KernelSession(
            session_id=session_id,
            operation_id=request.operation_id,
            profile_ref=request.profile_ref,
            interface=request.interface,
            kernel_ref=self.kernel_ref,
            simulation=True,
        )
        online_flow = OnlineFlowConfig.from_profile(profile)
        emv_flow = EmvFlowConfig.from_profile(profile)
        semantics = None
        if emv_flow is not None and emv_flow.transaction_semantics is not None:
            try:
                semantics = emv_flow.transaction_semantics.assert_card_kernel(request)
            except ValueError as exc:
                raise KernelError(getattr(exc, "code", "INVALID_TRANSACTION_SEMANTICS"), str(exc)) from exc
        if emv_flow is not None and emv_flow.unpredictable_number_required and self._un_boundary is None:
            raise KernelError("UN_SOURCE_REQUIRED", "El perfil exige una fuente UN inyectada.")
        velocity_policy = VelocityPolicy.from_profile(profile)
        if velocity_policy is not None and self._velocity_store is None:
            raise KernelError("VELOCITY_STORE_REQUIRED", "El perfil velocity exige contadores durables inyectados.")
        previous = self._results.get(request.operation_id)
        if previous is None and self._result_loader is not None:
            try:
                previous = self.query(request.operation_id)
            except KernelError as exc:
                if exc.code != "OPERATION_NOT_FOUND":
                    raise
        retry_execution = previous is not None and previous.recovery == RecoveryClass.SAFE_TO_RETRY
        machine = KernelStateMachine(request.operation_id)
        machine.transition(KernelState.SESSION_OPEN, "Sesión LAB/SYNTHETIC abierta")
        internal = InternalSession(
            session_id=session_id,
            request=request,
            profile=profile,
            card_interface=card_interface,
            machine=machine,
            online_flow=online_flow,
            emv_flow=emv_flow,
            data_store=online_flow.new_store() if online_flow is not None else None,
            retry_execution=retry_execution,
            configuration_provenance=configuration_provenance(profile, self.kernel_ref),
            velocity_policy=velocity_policy,
            transaction_semantics=semantics,
        )
        internal.control = SessionExecutionControl.from_profile(
            profile,
            request.operation_id,
            internal.cancellation,
            None,
            on_sent=lambda: setattr(internal, "command_sent", True),
        )
        internal.card_interface = ControlledCardInterface(card_interface, internal.control)
        self._sessions[session_id] = internal
        return public

    def _require_session(self, session: KernelSession, allow_closed: bool = False) -> InternalSession:
        internal = self._sessions.get(session.session_id)
        if internal is None:
            raise KernelError("SESSION_NOT_FOUND", "La sesión interna no existe.")
        if internal.closed and not allow_closed:
            raise KernelError("SESSION_CLOSED", "La sesión interna está cerrada.")
        return internal

    @staticmethod
    def _check_request(internal: InternalSession, request: TransactionRequest) -> None:
        if internal.request.fingerprint() != request.fingerprint():
            raise KernelError("SESSION_REQUEST_MISMATCH", "El request no corresponde a la sesión.")

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

    def _store(self, request: TransactionRequest, result: TransactionResult) -> TransactionResult:
        self._requests_by_operation[request.operation_id] = request
        self._requests_by_key[request.idempotency_key] = request
        self._results[request.operation_id] = result
        return result

    @staticmethod
    def _details(internal: InternalSession, scenario: str | None = None) -> dict[str, Any]:
        details: dict[str, Any] = {
            "simulation": True,
            "kernel_mode": InternalEmvKernel.kernel_mode,
            "kernel_version": InternalEmvKernel.kernel_version,
            "protocol": "LAB_ONLY",
            "scenario": scenario or internal.request.scenario,
            "command_sent": internal.command_sent,
            "state_transitions": internal.machine.sanitized_history(),
            "sanitized_trace": internal.control.trace.to_evidence(),
            "session_recovery": {
                "apdu_session_restorable": False,
                "automatic_replay": False,
                "read_back_required": internal.machine.state == KernelState.UNCERTAIN,
                "mode": "LAB_ONLY",
            },
        }
        if internal.selected_aid is not None:
            details["selected_aid"] = internal.selected_aid
        if internal.cvm_result is not None:
            details["cvm_result"] = dict(internal.cvm_result)
        if internal.risk_result is not None:
            details["risk_result"] = dict(internal.risk_result)
        if internal.action_result is not None:
            details["action_result"] = dict(internal.action_result)
        if internal.un_evidence is not None:
            details["unpredictable_number"] = internal.un_evidence.to_evidence()
        if internal.configuration_provenance is not None:
            details["configuration_provenance"] = dict(internal.configuration_provenance)
        if internal.transaction_semantics is not None:
            details["transaction_semantics"] = internal.transaction_semantics.to_evidence()
        return details

    def _result(
        self,
        internal: InternalSession,
        execution_state: ExecutionState,
        functional_result: FunctionalResult | None,
        recovery: RecoveryClass,
        error_code: str | None = None,
        extra_details: Mapping[str, Any] | None = None,
    ) -> TransactionResult:
        details = self._details(internal)
        if extra_details:
            details.update(extra_details)
        return KernelResult(
            functional_result=functional_result,
            execution_state=execution_state,
            recovery_action=recovery,
            error_code=error_code,
            details=details,
        ).to_transaction_result(internal.request)

    def _terminal(self, internal: InternalSession, state: KernelState, reason: str) -> None:
        if internal.machine.state != state:
            internal.machine.transition(state, reason)

    def _finish_velocity(self, internal: InternalSession, outcome: str) -> None:
        if internal.velocity_reservation is None:
            return
        internal.velocity_reservation = self._velocity_store.complete(internal.request.operation_id, outcome)
        if internal.risk_result is not None and internal.risk_result.get("velocity") is not None:
            internal.risk_result["velocity"] = internal.velocity_reservation.to_evidence()

    def _failure(
        self,
        internal: InternalSession,
        code: str,
        recovery: RecoveryClass = RecoveryClass.OPERATOR_REVIEW,
    ) -> TransactionResult:
        if not internal.command_sent and internal.velocity_reservation is not None:
            self._finish_velocity(internal, "ABORTED_BEFORE_SEND")
        self._terminal(internal, KernelState.FAILED, code)
        return self._store(internal.request, self._result(
            internal,
            ExecutionState.FAILED,
            None,
            recovery,
            code,
        ))

    def _uncertain(self, internal: InternalSession, code: str) -> TransactionResult:
        self._terminal(internal, KernelState.UNCERTAIN, code)
        return self._store(internal.request, self._result(
            internal,
            ExecutionState.UNCERTAIN,
            None,
            RecoveryClass.QUERY_REQUIRED,
            code,
        ))

    def _select_application(self, internal: InternalSession) -> None:
        request = internal.request
        applications = sorted(internal.profile.applications, key=lambda item: (item.priority, item.aid))
        if request.application_aid is not None:
            selected = next((item for item in applications if item.aid == request.application_aid), None)
            if selected is None:
                raise LaboratoryProtocolError("APPLICATION_NOT_FOUND", "El AID explícito no pertenece al perfil.")
        elif len(applications) > 1:
            raise LaboratoryProtocolError(
                "APPLICATION_SELECTION_REQUIRED",
                "Hay varias aplicaciones compatibles; application_aid es obligatorio.",
            )
        else:
            selected = applications[0]
        application_interfaces = internal.profile.scheme_parameters.get("application_interfaces", {})
        if isinstance(application_interfaces, Mapping) and selected.aid in application_interfaces:
            if request.interface not in application_interfaces[selected.aid]:
                raise LaboratoryProtocolError("APPLICATION_INTERFACE_MISMATCH", "La aplicación no admite esta interfaz.")
        internal.selected_aid = selected.aid

    def _exchange(self, internal: InternalSession, name: str, command: str, deadline: float | None) -> Any:
        stage = internal.machine.state.value
        if internal.control.stage != stage:
            internal.control.enter_stage(stage)
        else:
            internal.control.check_point()
        if deadline is not None and deadline <= time.monotonic():
            if internal.command_sent:
                raise _AfterSendTimeout()
            raise _BeforeSendTimeout()
        internal.command_sent = True
        internal.commands.append(name)
        return internal.card_interface.exchange(command, sensitivity="REDACTED")

    def _execute_emv(self, internal: InternalSession, request: TransactionRequest) -> TransactionResult:
        def enter_stage(stage: str) -> None:
            internal.machine.transition(KernelState(stage), stage)
            internal.control.enter_stage(stage)

        if internal.velocity_policy is not None:
            scope = f"{internal.profile.profile_digest}:{request.money.currency}"
            sample = int.from_bytes(hashlib.sha256(f"LAB_ONLY_VELOCITY|{request.operation_id}".encode("utf-8")).digest()[:2], "big") % 10000
            internal.velocity_reservation = self._velocity_store.reserve(
                request.operation_id,
                scope,
                request.money.amount_minor,
                internal.velocity_policy,
                sample,
            )
        unpredictable_number = None
        if internal.emv_flow.unpredictable_number_required:
            unpredictable_number, internal.un_evidence = self._un_boundary.generate()
        execution = execute_emv_flow(
            internal.card_interface,
            request,
            internal.profile,
            internal.emv_flow,
            stage_handler=enter_stage,
            unpredictable_number=unpredictable_number,
            velocity=internal.velocity_reservation,
        )
        internal.selected_aid = execution.selected_aid
        internal.cvm_result = execution.cvm_result
        internal.risk_result = execution.risk_result
        internal.action_result = execution.action_result
        internal.data_store = execution.store
        functional = execution.functional_result
        if request.scenario in {SimulationScenario.TIMEOUT.value, SimulationScenario.UNCERTAIN.value}:
            return self._uncertain(internal, "TIMEOUT_AFTER_SEND" if request.scenario == SimulationScenario.TIMEOUT.value else "UNCERTAIN_RESULT")
        if functional == FunctionalResult.APPROVED:
            self._finish_velocity(internal, "OFFLINE_APPROVED")
        elif functional in {FunctionalResult.DECLINED, FunctionalResult.ACTION_REQUIRED}:
            self._finish_velocity(internal, "NOT_OFFLINE")
        execution.evidence["decision"]["risk"]["velocity"] = internal.velocity_reservation.to_evidence() if internal.velocity_reservation is not None else None
        extra = {"emv_execution": execution.evidence}
        if functional == FunctionalResult.ONLINE_REQUIRED:
            if internal.online_flow is None:
                return self._failure(internal, "ONLINE_COMPLETION_REQUIRED", RecoveryClass.OPERATOR_REVIEW)
            self._terminal(internal, KernelState.ONLINE_PROCESSING, "ARQC EMV; se requiere host SIMULATED")
            return self._store(request, self._result(
                internal,
                ExecutionState.SUCCEEDED,
                functional,
                RecoveryClass.NON_RETRYABLE,
                extra_details=extra,
            ))
        self._terminal(internal, KernelState.COMPLETION, "Completado funcional EMV")
        if functional == FunctionalResult.DECLINED:
            self._terminal(internal, KernelState.DECLINED, "AAC EMV")
            return self._store(request, self._result(
                internal,
                ExecutionState.FAILED,
                functional,
                RecoveryClass.NON_RETRYABLE,
                "SIMULATED_DECLINE",
                extra,
            ))
        self._terminal(internal, KernelState.APPROVED, "TC EMV")
        return self._store(request, self._result(
            internal,
            ExecutionState.SUCCEEDED,
            FunctionalResult.APPROVED,
            RecoveryClass.NON_RETRYABLE,
            extra_details=extra,
        ))

    def execute(
        self,
        session: KernelSession,
        request: TransactionRequest,
        deadline: float | None = None,
    ) -> TransactionResult:
        with self._lock:
            internal = self._require_session(session)
            self._check_request(internal, request)
            internal.control.deadline = deadline
            existing = self._existing(request)
            if existing is not None:
                if internal.retry_execution and existing.recovery == RecoveryClass.SAFE_TO_RETRY:
                    internal.retry_execution = False
                else:
                    return existing
            self._execution_counts[request.operation_id] = self._execution_counts.get(request.operation_id, 0) + 1
            if request.scenario == SimulationScenario.CANCELLATION.value:
                return self.cancel(session)
            if (
                request.scenario == SimulationScenario.FAILURE_BEFORE_SEND.value
                and self._execution_counts[request.operation_id] == 1
            ):
                return self._failure(internal, "SIMULATED_FAILURE_BEFORE_SEND", RecoveryClass.SAFE_TO_RETRY)
            if deadline is not None and deadline <= time.monotonic():
                return self._failure(internal, "TIMEOUT_BEFORE_SEND", RecoveryClass.SAFE_TO_RETRY)
            try:
                if internal.profile.kernel_requirements.get("invalid_state") is True:
                    return self._failure(internal, "INVALID_STATE", RecoveryClass.OPERATOR_REVIEW)
                if internal.emv_flow is not None:
                    return self._execute_emv(internal, request)
                internal.machine.transition(KernelState.APPLICATION_SELECTION, "Inicio de selección por perfil")
                self._select_application(internal)
                commands = laboratory_commands(internal.selected_aid or "")
                internal.machine.transition(KernelState.APPLICATION_INITIALIZATION, "AID seleccionado")
                select = self._exchange(internal, "SELECT", commands["SELECT"], deadline)
                parse_select_response(select, internal.selected_aid or "", self._tlv_limits)
                initialize = self._exchange(internal, "INITIALIZE", commands["INITIALIZE"], deadline)
                parse_initialize_response(initialize, self._tlv_limits)
                internal.machine.transition(KernelState.CARD_DATA_PROCESSING, "Inicialización LAB confirmada")
                read_data = self._exchange(internal, "READ_DATA", commands["READ_DATA"], deadline)
                parse_read_data_response(read_data, self._tlv_limits)
                internal.machine.transition(KernelState.PROCESSING_RESTRICTIONS, "Datos LAB leídos")
                if not internal.profile.supports_interface(request.interface):
                    raise LaboratoryProtocolError("PROCESSING_RESTRICTION_FAILED", "Restricción de interfaz.")
                internal.machine.transition(KernelState.CVM_PROCESSING, "Restricciones de perfil satisfechas")
                cvm = decide_cvm(internal.profile)
                internal.cvm_result = cvm.to_dict()
                internal.machine.transition(KernelState.TERMINAL_RISK_MANAGEMENT, "CVM sintético decidido")
                risk = decide_risk(request, internal.profile)
                internal.risk_result = risk.to_dict()
                internal.machine.transition(KernelState.TERMINAL_ACTION_ANALYSIS, "Riesgo determinista evaluado")
                terminal = terminal_action(request, internal.profile, risk)
                if cvm.result in {"CVM_FAILED", "NOT_SUPPORTED"}:
                    terminal = "ACTION_REQUIRED"
                internal.machine.transition(KernelState.CARD_ACTION_ANALYSIS, "Acción de terminal decidida")
                card_response = self._exchange(internal, "CARD_ACTION", commands["CARD_ACTION"], deadline)
                card = parse_card_action_response(card_response, self._tlv_limits)
                if request.scenario == SimulationScenario.TIMEOUT.value:
                    raise _AfterSendTimeout()
                if request.scenario == SimulationScenario.UNCERTAIN.value:
                    return self._uncertain(internal, "UNCERTAIN_RESULT")
                functional = combine_actions(terminal, card)
                internal.action_result = {
                    "terminal_action": terminal,
                    "card_action": card,
                    "functional_result": functional.value,
                    "synthetic": True,
                }
                if functional == FunctionalResult.ONLINE_REQUIRED:
                    internal.machine.transition(KernelState.ONLINE_PROCESSING, "Se requiere respuesta SIMULATED del host")
                    return self._store(request, self._result(
                        internal,
                        ExecutionState.SUCCEEDED,
                        functional,
                        RecoveryClass.NON_RETRYABLE,
                    ))
                internal.machine.transition(KernelState.COMPLETION, "Completado funcional sintético")
                if functional == FunctionalResult.ACTION_REQUIRED:
                    return self._store(request, self._result(
                        internal,
                        ExecutionState.SUCCEEDED,
                        functional,
                        RecoveryClass.OPERATOR_REVIEW,
                    ))
                if functional == FunctionalResult.DECLINED:
                    internal.machine.transition(KernelState.DECLINED, "Decisión DECLINED sintética")
                    return self._store(request, self._result(
                        internal,
                        ExecutionState.FAILED,
                        functional,
                        RecoveryClass.NON_RETRYABLE,
                        "SIMULATED_DECLINE",
                    ))
                internal.machine.transition(KernelState.APPROVED, "Decisión APPROVED sintética")
                return self._store(request, self._result(
                    internal,
                    ExecutionState.SUCCEEDED,
                    FunctionalResult.APPROVED,
                    RecoveryClass.NON_RETRYABLE,
                ))
            except _BeforeSendTimeout:
                return self._failure(internal, "TIMEOUT_BEFORE_SEND", RecoveryClass.SAFE_TO_RETRY)
            except _AfterSendTimeout:
                return self._uncertain(internal, "TIMEOUT_AFTER_SEND")
            except ReaderError as exc:
                if exc.outcome == "UNCERTAIN":
                    return self._uncertain(internal, exc.code)
                if not internal.command_sent:
                    return self._failure(internal, exc.code, RecoveryClass.SAFE_TO_RETRY)
                return self._failure(internal, exc.code, RecoveryClass.OPERATOR_REVIEW)
            except LaboratoryProtocolError as exc:
                recovery = RecoveryClass.SAFE_TO_RETRY if not internal.command_sent else RecoveryClass.OPERATOR_REVIEW
                return self._failure(internal, exc.code, recovery)
            except (ValueError, RuntimeError) as exc:
                code = getattr(exc, "code", "LAB_PROTOCOL_ERROR")
                if getattr(exc, "outcome", None) == "UNCERTAIN":
                    return self._uncertain(internal, code)
                if getattr(exc, "cancelled", False):
                    return self._cancel_result(internal)
                recovery = getattr(exc, "recovery", None)
                if recovery is None:
                    recovery = RecoveryClass.SAFE_TO_RETRY if code == "TIMEOUT_BEFORE_SEND" else RecoveryClass.OPERATOR_REVIEW
                return self._failure(internal, code, recovery)

    @staticmethod
    def _online_fingerprint(host_response: Mapping[str, Any]) -> str:
        try:
            canonical = json.dumps(host_response, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise KernelError("INVALID_HOST_RESPONSE", "La respuesta online no es canonicalizable.") from exc
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _complete_emv_online(
        self,
        internal: InternalSession,
        host_response: Mapping[str, Any],
    ) -> TransactionResult:
        request = internal.request
        def enter_stage(stage: str) -> None:
            internal.machine.transition(KernelState(stage), stage)
            internal.control.enter_stage(stage)

        try:
            internal.control.check_point()
            completion = process_online_completion(
                internal.card_interface,
                host_response,
                internal.online_flow,
                internal.data_store,
                stage_handler=enter_stage,
            )
            internal.data_store = completion.store
            arc = next(
                (
                    item["arc"]
                    for item in completion.history
                    if item.get("stage") == "HOST_RESPONSE_ACCEPTED"
                ),
                host_response.get("authorization_response_code"),
            )
            internal.action_result = {
                **(internal.action_result or {}),
                "final_decision_source": "KERNEL_ARC",
                "arc": arc,
                "online_history": list(completion.history),
                "tvr": completion.tvr.to_evidence(),
                "tsi": completion.tsi.to_evidence(),
            }
            extra = {
                "host_reference": completion.host_reference,
                "response_template_sha256": completion.response_template_sha256,
                "authorization_response_code": internal.action_result["arc"],
                "online_completion": completion.to_evidence(),
            }
            if completion.functional_result in {FunctionalResult.APPROVED, FunctionalResult.DECLINED}:
                self._finish_velocity(internal, "ONLINE_COMPLETED")
                self._terminal(internal, KernelState.COMPLETION, "Completado online EMV")
                self._terminal(
                    internal,
                    KernelState.APPROVED if completion.functional_result == FunctionalResult.APPROVED else KernelState.DECLINED,
                    f"ARC EMV: {internal.action_result['arc']}",
                )
            elif completion.execution_state == ExecutionState.UNCERTAIN:
                self._terminal(internal, KernelState.UNCERTAIN, completion.error_code or "ONLINE_UNCERTAIN")
            else:
                self._terminal(internal, KernelState.FAILED, completion.error_code or "ONLINE_FAILED")
            return self._store(request, self._result(
                internal,
                completion.execution_state,
                completion.functional_result,
                completion.recovery,
                completion.error_code,
                extra,
            ))
        except (ValueError, RuntimeError) as exc:
            code = getattr(exc, "code", "ONLINE_COMPLETION_FAILED")
            if getattr(exc, "outcome", None) == "UNCERTAIN":
                return self._uncertain(internal, code)
            if getattr(exc, "cancelled", False):
                return self._cancel_result(internal)
            recovery = RecoveryClass.SAFE_TO_RETRY if code == "TIMEOUT_BEFORE_SEND" else RecoveryClass.OPERATOR_REVIEW
            return self._failure(internal, code, recovery)

    def complete_online(
        self,
        session: KernelSession,
        request: TransactionRequest,
        host_response: Mapping[str, Any],
    ) -> TransactionResult:
        with self._lock:
            internal = self._require_session(session)
            self._check_request(internal, request)
            current = self._results.get(request.operation_id)
            if internal.online_flow is not None:
                if not isinstance(host_response, Mapping):
                    raise KernelError("INVALID_HOST_RESPONSE", "host_response debe ser un mapping SIMULATED.")
                fingerprint = self._online_fingerprint(host_response)
                if internal.online_fingerprint is not None and internal.online_fingerprint != fingerprint:
                    raise KernelError("ONLINE_COMPLETION_CONFLICT", "La operación ya usó otra respuesta online.")
                if current is not None and current.outcome != FunctionalResult.ONLINE_REQUIRED.value:
                    return current
                if current is None:
                    raise KernelError("INVALID_ONLINE_COMPLETION", "No hay una operación ONLINE_REQUIRED pendiente.")
                if internal.machine.state != KernelState.ONLINE_PROCESSING:
                    raise KernelError("INVALID_ONLINE_COMPLETION", "La sesión no está en ONLINE_PROCESSING.")
                internal.online_fingerprint = fingerprint
                return self._complete_emv_online(internal, host_response)
            if current is None or current.outcome != FunctionalResult.ONLINE_REQUIRED.value:
                if current is not None:
                    return current
                raise KernelError("INVALID_ONLINE_COMPLETION", "No hay una operación ONLINE_REQUIRED pendiente.")
            if internal.machine.state != KernelState.ONLINE_PROCESSING:
                raise KernelError("INVALID_ONLINE_COMPLETION", "La sesión no está en ONLINE_PROCESSING.")
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
            internal.machine.transition(KernelState.COMPLETION, "Respuesta SIMULATED del host recibida")
            internal.machine.transition(
                KernelState.APPROVED if decision == "APPROVED" else KernelState.DECLINED,
                f"Host SIMULATED: {decision}",
            )
            functional = FunctionalResult(decision)
            result = self._result(
                internal,
                ExecutionState.SUCCEEDED if decision == "APPROVED" else ExecutionState.FAILED,
                functional,
                RecoveryClass.NON_RETRYABLE,
                None if decision == "APPROVED" else "SIMULATED_HOST_DECLINE",
                {"host_response": "SIMULATED", "host_decision": decision},
            )
            return self._store(request, result)

    @staticmethod
    def _validate_loaded_result(result: TransactionResult, operation_id: str, known: TransactionResult | None) -> None:
        if result.operation_id != operation_id:
            raise KernelError("OPERATION_ID_MISMATCH", "El resultado externo pertenece a otra operación.", RecoveryClass.OPERATOR_REVIEW)
        if result.kernel_ref != InternalEmvKernel.kernel_ref or not result.simulation:
            raise KernelError("OPERATION_QUERY_INVALID", "El read-back no pertenece al kernel SIMULATED.", RecoveryClass.OPERATOR_REVIEW)
        if result.details.get("physical_validation") == "PHYSICAL_VALIDATED":
            raise KernelError("OPERATION_QUERY_INVALID", "Evidencia PHYSICAL_VALIDATED no es admisible en LAB.", RecoveryClass.OPERATOR_REVIEW)
        if known is not None and known.request.fingerprint() != result.request.fingerprint():
            raise KernelError("OPERATION_QUERY_CONFLICT", "El read-back cambió el request de la operación.", RecoveryClass.OPERATOR_REVIEW)
        valid = {
            ExecutionState.SUCCEEDED: {FunctionalResult.APPROVED.value, FunctionalResult.ONLINE_REQUIRED.value, None},
            ExecutionState.FAILED: {FunctionalResult.DECLINED.value, None},
            ExecutionState.UNCERTAIN: {None},
            ExecutionState.CANCELLED: {None},
        }
        if result.outcome not in valid[result.execution_state]:
            raise KernelError("OPERATION_QUERY_INVALID", "El read-back tiene estado y resultado funcional contradictorios.", RecoveryClass.OPERATOR_REVIEW)

    def query(self, operation_id: str) -> TransactionResult:
        with self._lock:
            known = self._results.get(operation_id)
            if known is not None and known.execution_state != ExecutionState.UNCERTAIN:
                return known
            if self._result_loader is not None:
                try:
                    loaded = self._result_loader(operation_id)
                    result = None
                    if loaded is not None:
                        result = loaded if isinstance(loaded, TransactionResult) else TransactionResult.from_dict(loaded)
                        self._validate_loaded_result(result, operation_id, known)
                except KernelError:
                    raise
                except Exception as exc:
                    raise KernelError("OPERATION_QUERY_FAILED", "El almacenamiento externo no pudo resolver la operación.", RecoveryClass.OPERATOR_REVIEW) from exc
                if result is not None:
                    if known is not None and result.execution_state == ExecutionState.UNCERTAIN:
                        return known
                    if known is not None and result.execution_state not in {ExecutionState.SUCCEEDED, ExecutionState.FAILED}:
                        raise KernelError("INVALID_LATE_RESULT", "Un UNCERTAIN sólo reconcilia con un resultado final conocido.", RecoveryClass.OPERATOR_REVIEW)
                    if known is not None and result.outcome == FunctionalResult.ONLINE_REQUIRED.value:
                        raise KernelError("INVALID_LATE_RESULT", "ONLINE_REQUIRED no es reconciliación final.", RecoveryClass.OPERATOR_REVIEW)
                    return self._store(result.request, result)
            if known is not None:
                return known
            raise KernelError("OPERATION_NOT_FOUND", "No existe un resultado conocido para operation_id.", RecoveryClass.OPERATOR_REVIEW)

    def _cancel_result(self, internal: InternalSession) -> TransactionResult:
        if not internal.command_sent and internal.velocity_reservation is not None:
            self._finish_velocity(internal, "ABORTED_BEFORE_SEND")
        internal.cancelled = True
        internal.machine.transition(KernelState.CANCELLED, "Cancelación del operador")
        result = self._result(
            internal,
            ExecutionState.CANCELLED,
            None,
            RecoveryClass.NON_RETRYABLE,
            "OPERATOR_CANCELLED",
            {"scenario": SimulationScenario.CANCELLATION.value, "command_sent": internal.command_sent},
        )
        return self._store(internal.request, result)

    def cancel(self, session: KernelSession) -> TransactionResult:
        pending = self._sessions.get(session.session_id)
        if pending is not None:
            pending.cancellation.cancel()
            pending.control.trace.control(pending.control.stage, "CANCEL_REQUESTED")
        with self._lock:
            internal = self._require_session(session, allow_closed=True)
            existing = self._results.get(internal.request.operation_id)
            if existing is not None:
                return existing
            if internal.closed:
                raise KernelError("SESSION_CLOSED", "La sesión interna está cerrada.")
            return self._cancel_result(internal)

    def close(self, session: KernelSession) -> CloseEvidence:
        with self._lock:
            internal = self._require_session(session, allow_closed=True)
            internal.closed = True
            return CloseEvidence(session.session_id, closed=True, simulated=True)

    def evidence(self, operation_id: str) -> dict[str, Any]:
        return self.query(operation_id).to_evidence()

    def execution_count(self, operation_id: str) -> int:
        return self._execution_counts.get(operation_id, 0)
