from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from hashlib import sha256
from typing import Any

from card_studio.evidence.redaction import redact_sensitive
from card_studio.emv.transaction_semantics import KernelTransactionSemanticsPolicy
from card_studio.emv.unpredictable import UnpredictableNumberError
from card_studio.host.base import AuthorizationHostAdapter, HostError
from card_studio.host.response import validate_response_template
from card_studio.interfaces.base import CardInterface
from card_studio.interfaces.contact import ContactInterface
from card_studio.interfaces.contactless import ContactlessInterface
from card_studio.interfaces.magnetic_simulated import MagneticSimulatedInterface
from card_studio.interfaces.virtual import VirtualInterface
from card_studio.kernels.base import KernelAdapter, KernelError, KernelSession
from card_studio.profiles.model import CardProfile, ProfileStatus
from card_studio.profiles.registry import CardProfileRegistry
from card_studio.profiles.validation import ProfileValidationError
from card_studio.readers.base import ReaderAdapter, ReaderError, ReaderSession

from .evidence import HOST_METADATA_FIELDS, TransactionEvidenceStore
from .model import ExecutionState, TransactionError, TransactionRequest, TransactionResult, TransactionType
from .recovery import RecoveryClass, RecoveryError
from .store import AuditEntry, PersistentState, TransactionRecord, TransactionStore, profile_parts


InterfaceFactory = Callable[[ReaderAdapter, ReaderSession, str], CardInterface]


def default_interface_factory(adapter: ReaderAdapter, session: ReaderSession, interface: str) -> CardInterface:
    classes = {
        "CONTACT": ContactInterface,
        "CONTACTLESS": ContactlessInterface,
        "VIRTUAL": VirtualInterface,
        "MAGNETIC_SIMULATED": MagneticSimulatedInterface,
    }
    return classes[interface](adapter, session)


class TransactionService:
    def __init__(
        self,
        store: TransactionStore,
        profiles: CardProfileRegistry,
        reader: ReaderAdapter,
        kernels: KernelAdapter | Iterable[KernelAdapter],
        host: AuthorizationHostAdapter,
        evidence: TransactionEvidenceStore,
        reader_id: str | None = None,
        interface_factory: InterfaceFactory = default_interface_factory,
        hooks: Mapping[str, Callable[[str], None]] | None = None,
    ):
        adapters = [kernels] if isinstance(kernels, KernelAdapter) else list(kernels)
        self.kernels = {adapter.kernel_ref: adapter for adapter in adapters}
        if len(self.kernels) != len(adapters):
            raise ValueError("kernel_ref duplicado.")
        self.store = store
        self.profiles = profiles
        self.reader = reader
        self.host = host
        self.evidence = evidence
        self.reader_id = reader_id
        self.interface_factory = interface_factory
        self.hooks = dict(hooks or {})
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._sessions: dict[str, tuple[KernelAdapter, KernelSession]] = {}
        self._sessions_guard = threading.RLock()

    def _lock_for(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    def _hook(self, name: str, operation_id: str) -> None:
        callback = self.hooks.get(name)
        if callback is not None:
            callback(operation_id)

    def _selected_reader_id(self) -> str:
        if self.reader_id:
            return self.reader_id
        readers = self.reader.enumerate_readers()
        if len(readers) != 1:
            raise TransactionError(
                "READER_SELECTION_REQUIRED",
                "Debe existir un lector simulado único o reader_id explícito.",
            )
        return readers[0].reader_id

    def _validate(self, request: TransactionRequest) -> tuple[CardProfile, KernelAdapter, str]:
        if request.simulation is not True:
            raise TransactionError("SIMULATION_REQUIRED", "Toda transacción debe ser simulada.")
        if self.reader.simulated is not True:
            raise TransactionError("SIMULATED_READER_REQUIRED", "L3 LAB no admite hardware físico.")
        profile_id, version, digest = profile_parts(request.profile_ref)
        try:
            profile = self.profiles.get(profile_id, version)
        except ProfileValidationError as error:
            raise TransactionError(error.code, error.message) from error
        if profile.profile_digest != digest:
            raise TransactionError("PROFILE_DIGEST_MISMATCH", "El digest del perfil no coincide exactamente.")
        try:
            profile.assert_simulation_enabled()
        except ProfileValidationError as error:
            raise TransactionError(error.code, error.message) from error
        if profile.status != ProfileStatus.APPROVED_FOR_SIMULATION.value:
            raise TransactionError("PROFILE_NOT_EXECUTABLE", "El perfil no está aprobado para simulación.")
        if not profile.supports_interface(request.interface):
            raise TransactionError("UNSUPPORTED_INTERFACE", "El perfil no admite la interfaz solicitada.")
        try:
            kernel = self.kernels[request.kernel_ref]
        except KeyError as error:
            raise TransactionError("KERNEL_NOT_FOUND", "No existe el kernel_ref solicitado.") from error
        capabilities = kernel.capabilities()
        if not capabilities.simulation:
            raise TransactionError("SIMULATED_KERNEL_REQUIRED", "El kernel debe ser exclusivamente simulado.")
        if request.interface not in capabilities.interfaces:
            raise TransactionError("UNSUPPORTED_INTERFACE", "El kernel no admite la interfaz solicitada.")
        if request.transaction_type not in capabilities.transaction_types:
            raise TransactionError("UNSUPPORTED_TRANSACTION_TYPE", "El kernel no admite este tipo de operación.")
        reader_id = self._selected_reader_id()
        try:
            self.reader.require_capability(reader_id, request.interface)
        except ReaderError as error:
            raise TransactionError(error.code, error.message) from error
        return profile, kernel, reader_id

    def _record_event(
        self,
        operation_id: str,
        event_type: str,
        reason: str,
        previous_state: str | None = None,
        new_state: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        audit = self.store.record_event(operation_id, event_type, reason, previous_state, new_state)
        self.evidence.write_audit(audit, self.store.get(operation_id), dict(metadata or {}))
        return audit

    def _persist(self, result: TransactionResult, reason: str | None = None) -> TransactionRecord:
        result = self._result(
            result.request,
            result.execution_state,
            result.recovery,
            result.error_code,
            result.outcome,
            result.details,
        )
        record, audit = self.store.complete(result, reason)
        if audit is not None:
            self.evidence.write_audit(audit, record)
            path = self.evidence.write_result(record, result)
            record = self.store.set_result_reference(record.operation_id, path.name)
            self.evidence.write_result(record, result)
        return record

    @staticmethod
    def _safe_details(value: Any) -> Any:
        forbidden = {
            "response_template",
            "issuer_authentication_data",
            "issuer_authentication",
            "issuer_script",
            "issuer_scripts",
            "issuer_script_template",
            "issuer_script_templates",
            "script_template",
            "script_templates",
            "script_commands",
            "application_cryptogram",
            "arqc",
            "ac",
            "91",
            "71",
            "72",
        }
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                normalized = str(key).strip().casefold().replace("-", "_").replace(" ", "_")
                if normalized not in forbidden:
                    result[str(key)] = TransactionService._safe_details(item)
            return result
        if isinstance(value, (list, tuple)):
            return [TransactionService._safe_details(item) for item in value]
        return value

    @staticmethod
    def _host_metadata(response: Mapping[str, Any]) -> dict[str, Any]:
        metadata = {
            key: response.get(key)
            for key in ("host_ref", "host_version", *HOST_METADATA_FIELDS)
            if key in response
        }
        if "timestamp" in response:
            metadata["host_response_timestamp"] = response["timestamp"]
        return metadata

    @staticmethod
    def _result(
        request: TransactionRequest,
        state: ExecutionState,
        recovery: RecoveryClass,
        error: str | None,
        outcome: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> TransactionResult:
        safe = TransactionService._safe_details(dict(details or {}))
        return TransactionResult(
            request=request,
            execution_state=state,
            outcome=outcome,
            recovery=recovery,
            error_code=error,
            details=redact_sensitive(safe),
        )

    @staticmethod
    def _with_host_metadata(
        result: TransactionResult,
        response: Mapping[str, Any],
    ) -> TransactionResult:
        details = dict(result.details)
        details.update(TransactionService._host_metadata(response))
        return TransactionService._result(
            result.request,
            result.execution_state,
            result.recovery,
            result.error_code,
            result.outcome,
            details,
        )

    @staticmethod
    def _canonical_host_request(request: TransactionRequest) -> TransactionRequest:
        return TransactionRequest.create(
            money=request.money,
            profile_ref=request.profile_ref,
            interface=request.interface,
            kernel_ref=request.kernel_ref,
            transaction_type=request.transaction_type,
            scenario=request.scenario,
            original_operation_id=request.original_operation_id,
            original_amount=request.original_amount,
            operation_id=request.operation_id,
            idempotency_key=request.idempotency_key,
            correlation_id=request.correlation_id,
            application_aid=request.application_aid,
        )

    def _validation_failure(self, request: TransactionRequest) -> TransactionResult | None:
        if request.transaction_type == TransactionType.PURCHASE.value:
            return None
        try:
            original = self.store.get(request.original_operation_id or "")
        except TransactionError:
            return self._result(
                request, ExecutionState.FAILED, RecoveryClass.OPERATOR_REVIEW,
                "ORIGINAL_OPERATION_NOT_FOUND", "DECLINED",
            )
        original_approved = (
            original.execution_state == PersistentState.SUCCEEDED
            and original.functional_result == "APPROVED"
        )
        reversible_uncertain = (
            request.transaction_type == TransactionType.REVERSAL.value
            and original.execution_state == PersistentState.UNCERTAIN
        )
        if original.request.transaction_type != TransactionType.PURCHASE.value or not (original_approved or reversible_uncertain):
            return self._result(
                request, ExecutionState.FAILED, RecoveryClass.NON_RETRYABLE,
                "INVALID_ORIGINAL_OPERATION", "DECLINED",
            )
        if request.original_amount != original.request.money:
            return self._result(
                request, ExecutionState.FAILED, RecoveryClass.OPERATOR_REVIEW,
                "ORIGINAL_AMOUNT_MISMATCH", "DECLINED",
            )
        related = self.store.related(original.operation_id)
        succeeded = [
            item for item in related
            if item.execution_state == PersistentState.SUCCEEDED and item.result is not None
        ]
        reversals = [item for item in succeeded if item.request.transaction_type == TransactionType.REVERSAL.value]
        refunds = [item for item in succeeded if item.request.transaction_type == TransactionType.REFUND.value]
        if request.transaction_type == TransactionType.REVERSAL.value:
            if reversals:
                return self._result(
                    request, ExecutionState.FAILED, RecoveryClass.NON_RETRYABLE,
                    "DUPLICATE_REVERSAL", "DECLINED",
                )
            if refunds:
                return self._result(
                    request, ExecutionState.FAILED, RecoveryClass.OPERATOR_REVIEW,
                    "REFUND_ALREADY_EXISTS", "DECLINED",
                )
            if request.money != original.request.money:
                return self._result(
                    request, ExecutionState.FAILED, RecoveryClass.NON_RETRYABLE,
                    "REVERSAL_AMOUNT_MISMATCH", "DECLINED",
                )
            return None
        refunded = sum(item.request.money.amount_minor for item in refunds)
        if refunded + request.money.amount_minor > original.request.money.amount_minor:
            return self._result(
                request, ExecutionState.FAILED, RecoveryClass.NON_RETRYABLE,
                "EXCESSIVE_REFUND", "DECLINED",
                details={"refunded_amount_minor": refunded},
            )
        return None

    @staticmethod
    def _reader_failure(request: TransactionRequest, error: ReaderError) -> TransactionResult:
        if error.outcome == "UNCERTAIN":
            return TransactionService._result(
                request, ExecutionState.UNCERTAIN, RecoveryClass.QUERY_REQUIRED, error.code,
                details={"command_sent": True},
            )
        retryable = error.code in {"READER_TIMEOUT", "READER_UNAVAILABLE"}
        return TransactionService._result(
            request,
            ExecutionState.FAILED,
            RecoveryClass.SAFE_TO_RETRY if retryable else RecoveryClass.NON_RETRYABLE,
            error.code,
            details={"command_sent": False},
        )

    @staticmethod
    def _kernel_failure(request: TransactionRequest, error: KernelError) -> TransactionResult:
        if error.recovery in {
            RecoveryClass.QUERY_REQUIRED,
            RecoveryClass.READ_BACK_REQUIRED,
            RecoveryClass.REVERSAL_REQUIRED,
        }:
            state = ExecutionState.UNCERTAIN
        else:
            state = ExecutionState.FAILED
        return TransactionService._result(
            request, state, error.recovery, error.code,
            details={"command_sent": state == ExecutionState.UNCERTAIN},
        )

    @staticmethod
    def _host_result(request: TransactionRequest, response: Mapping[str, Any]) -> TransactionResult:
        if response.get("operation_id") not in {None, request.operation_id}:
            return TransactionService._result(
                request,
                ExecutionState.UNCERTAIN,
                RecoveryClass.QUERY_REQUIRED,
                "HOST_OPERATION_ID_MISMATCH",
                details={"command_sent": True},
            )
        state = ExecutionState(str(response.get("execution_state", "UNCERTAIN")))
        recovery = RecoveryClass(str(response.get("recovery_action", "QUERY_REQUIRED")))
        details = TransactionService._host_metadata(response)
        details.update({
            "command_sent": bool(response.get("command_sent")),
            "host_mode": response.get("mode"),
        })
        return TransactionService._result(
            request,
            state,
            recovery,
            str(response.get("error_code")) if response.get("error_code") else None,
            str(response.get("functional_result")) if response.get("functional_result") else None,
            details,
        )

    @staticmethod
    def _awaiting_publication(record: TransactionRecord) -> bool:
        return (
            record.execution_state in {PersistentState.CREATED, PersistentState.IN_PROGRESS}
            or (record.result is not None and record.result_reference is None)
        )

    def _await_duplicate(
        self,
        record: TransactionRecord,
        deadline: float | None,
    ) -> TransactionRecord:
        timeout = 30.0 if deadline is None else max(0.0, min(30.0, deadline - time.monotonic()))
        limit = time.monotonic() + timeout
        current = record
        while self._awaiting_publication(current):
            if time.monotonic() >= limit:
                break
            time.sleep(0.01)
            current = self.store.get(record.operation_id)
        return current

    def execute(self, request: TransactionRequest, deadline: float | None = None) -> TransactionRecord:
        with self._lock_for(request.idempotency_key):
            _, kernel, _ = self._validate(request)
            record, created, audit = self.store.create(
                request, type(kernel).__name__, kernel.kernel_version, kernel.kernel_mode,
            )
            if audit is not None:
                self.evidence.write_audit(audit, record)
            if not created:
                return self._await_duplicate(record, deadline)
            if record.execution_state != PersistentState.CREATED:
                if record.result is not None:
                    path = self.evidence.write_result(record, record.result)
                    record = self.store.set_result_reference(record.operation_id, path.name)
                    self.evidence.write_result(record, record.result)
                return record
            self._hook("after_created", request.operation_id)
            return self._run(record, kernel, deadline, retry=False)

    create = execute

    def retry(self, request: TransactionRequest, deadline: float | None = None) -> TransactionRecord:
        with self._lock_for(request.idempotency_key):
            _, kernel, _ = self._validate(request)
            record = self.store.get(request.operation_id)
            if record.request.fingerprint() != request.fingerprint():
                raise TransactionError("DUPLICATE_OPERATION", "operation_id ya existe con otro request.")
            if record.execution_state != PersistentState.FAILED or record.recovery_action != RecoveryClass.SAFE_TO_RETRY:
                raise RecoveryError("NON_RETRYABLE", "La operación no admite un nuevo envío.")
            return self._run(record, kernel, deadline, retry=True)

    def _run(
        self,
        record: TransactionRecord,
        kernel: KernelAdapter,
        deadline: float | None,
        retry: bool,
    ) -> TransactionRecord:
        request = record.request
        validation_failure = self._validation_failure(request)
        if validation_failure is not None:
            return self._persist(validation_failure)
        if retry:
            record, audit = self.store.mark_retry(request.operation_id)
            self.evidence.write_audit(audit, record)
        reader_session: ReaderSession | None = None
        card_interface: CardInterface | None = None
        kernel_session: KernelSession | None = None
        result: TransactionResult | None = None
        try:
            try:
                profile_id, version, _ = profile_parts(request.profile_ref)
                profile = self.profiles.get(profile_id, version)
                semantics_policy = KernelTransactionSemanticsPolicy.from_profile(profile)
                semantics = semantics_policy.resolve(request) if semantics_policy is not None else None
                if semantics is not None and semantics.card_kernel_mode == "HOST_ONLY":
                    if not retry:
                        record, changed, audit = self.store.mark_in_progress(request.operation_id)
                        if not changed:
                            return record
                        self.evidence.write_audit(audit, record)
                    self._hook("after_in_progress", request.operation_id)
                    approved = self._result(
                        request,
                        ExecutionState.SUCCEEDED,
                        RecoveryClass.NON_RETRYABLE,
                        None,
                        "APPROVED",
                        {"transaction_semantics": semantics.to_evidence()},
                    )
                    result = self._complete_reversal(request, approved, deadline)
                    return self._persist(result)
                reader_session = self.reader.open(reader_id=self._selected_reader_id(), operation_id=request.operation_id, deadline=deadline)
                observation = self.reader.detect_card(reader_session)
                if not observation.card_present:
                    raise ReaderError("CARD_NOT_PRESENT", "SIMULATED: tarjeta ausente.")
                card_interface = self.interface_factory(self.reader, reader_session, request.interface)
                card_interface.open()
                self._record_event(request.operation_id, "SESSION_OPENED", "SIMULATED_READER_AND_INTERFACE_OPENED")
                kernel_session = kernel.open_session(request, card_interface=card_interface, profile=profile)
                with self._sessions_guard:
                    self._sessions[request.operation_id] = (kernel, kernel_session)
                if not retry:
                    record, changed, audit = self.store.mark_in_progress(request.operation_id)
                    if not changed:
                        return record
                    self.evidence.write_audit(audit, record)
                self._hook("after_in_progress", request.operation_id)
                if request.application_aid:
                    self._record_event(request.operation_id, "APPLICATION_SELECTED", "EXPLICIT_APPLICATION_SELECTION")
                result = kernel.execute(kernel_session, request, deadline)
                self._record_event(request.operation_id, "KERNEL_COMPLETED", result.error_code or result.execution_state.value)
                if result.details.get("selected_aid") and not request.application_aid:
                    self._record_event(request.operation_id, "APPLICATION_SELECTED", "KERNEL_SELECTED_APPLICATION")
                if result.outcome == "ONLINE_REQUIRED":
                    result = self._complete_online(kernel, kernel_session, request, deadline)
                if (
                    result.execution_state == ExecutionState.SUCCEEDED
                    and result.outcome == "APPROVED"
                    and request.transaction_type == TransactionType.REVERSAL.value
                ):
                    result = self._complete_reversal(request, result, deadline)
                elif (
                    result.execution_state == ExecutionState.SUCCEEDED
                    and result.outcome == "APPROVED"
                    and request.transaction_type == TransactionType.REFUND.value
                    and self._original_host_reference(request) is not None
                ):
                    result = self._complete_refund(request, result, deadline)
            except ReaderError as error:
                result = self._reader_failure(request, error)
            except KernelError as error:
                result = self._kernel_failure(request, error)
            except UnpredictableNumberError as error:
                result = self._result(
                    request, ExecutionState.FAILED, RecoveryClass.OPERATOR_REVIEW,
                    error.code, details={"command_sent": False},
                )
            except HostError as error:
                result = self._result(
                    request, ExecutionState.FAILED, RecoveryClass.OPERATOR_REVIEW,
                    error.code, details={"command_sent": False},
                )
            if result is None:
                result = self._result(
                    request, ExecutionState.UNCERTAIN, RecoveryClass.READ_BACK_REQUIRED,
                    "MISSING_KERNEL_RESULT", details={"command_sent": True},
                )
            return self._persist(result)
        finally:
            with self._sessions_guard:
                self._sessions.pop(request.operation_id, None)
            if kernel_session is not None:
                try:
                    kernel.close(kernel_session)
                    self._record_event(request.operation_id, "KERNEL_CLOSED", "BEST_EFFORT_CLOSE_OK")
                except Exception as error:
                    self._best_effort_close_event(request.operation_id, "KERNEL_CLOSE_FAILED", error)
            if card_interface is not None:
                try:
                    card_interface.close()
                    self._record_event(request.operation_id, "INTERFACE_CLOSED", "BEST_EFFORT_CLOSE_OK")
                except Exception as error:
                    self._best_effort_close_event(request.operation_id, "INTERFACE_CLOSE_FAILED", error)
            elif reader_session is not None:
                try:
                    self.reader.close(reader_session)
                    self._record_event(request.operation_id, "READER_CLOSED", "BEST_EFFORT_CLOSE_OK")
                except Exception as error:
                    self._best_effort_close_event(request.operation_id, "READER_CLOSE_FAILED", error)

    def _best_effort_close_event(self, operation_id: str, event: str, error: Exception) -> None:
        try:
            code = getattr(error, "code", type(error).__name__.upper())
            self._record_event(operation_id, event, str(code))
        except Exception:
            pass

    @staticmethod
    def _definitive_online_response(response: Mapping[str, Any]) -> bool:
        if response.get("execution_state") not in {"SUCCEEDED", "FAILED"}:
            return False
        template = response.get("response_template")
        if template is None:
            if response.get("authorization_response_code") is not None:
                raise HostError("HOST_RESPONSE_TEMPLATE_REQUIRED", "La respuesta ARC requiere response_template.")
            decision = response.get("functional_result", response.get("decision"))
            return decision in {"APPROVED", "DECLINED"}
        metadata = validate_response_template(template)
        if response.get("authorization_response_code") != metadata["authorization_response_code"]:
            raise HostError("HOST_ARC_MISMATCH", "ARC no coincide con response_template.")
        digest = sha256(bytes.fromhex(template)).hexdigest()
        if response.get("response_template_sha256") != digest:
            raise HostError("HOST_RESPONSE_DIGEST_MISMATCH", "Digest de response_template inválido.")
        return True

    @staticmethod
    def _invalid_host_response(request: TransactionRequest, error: HostError) -> TransactionResult:
        return TransactionService._result(
            request,
            ExecutionState.FAILED,
            RecoveryClass.NON_RETRYABLE,
            error.code,
            details={"command_sent": True},
        )

    def _complete_online(
        self,
        kernel: KernelAdapter,
        session: KernelSession,
        request: TransactionRequest,
        deadline: float | None,
    ) -> TransactionResult:
        self._record_event(request.operation_id, "ONLINE_REQUESTED", "CANONICAL_HOST_REQUEST")
        self._record_event(request.operation_id, "HOST_AUTHORIZATION_STARTED", "MOCK_HOST_ONLY")
        response = self.host.authorize(self._canonical_host_request(request), deadline)
        if response.get("operation_id") not in {None, request.operation_id}:
            self._record_event(request.operation_id, "HOST_AUTHORIZATION_UNCERTAIN", "HOST_OPERATION_ID_MISMATCH")
            return self._host_result(request, response)
        try:
            definitive = self._definitive_online_response(response)
        except HostError as error:
            self._record_event(request.operation_id, "HOST_AUTHORIZATION_UNCERTAIN", error.code)
            return self._invalid_host_response(request, error)
        if response.get("execution_state") == "UNCERTAIN":
            reason = str(response.get("error_code") or response.get("host_reference") or "HOST_UNCERTAIN")
            self._record_event(request.operation_id, "HOST_AUTHORIZATION_UNCERTAIN", reason)
            return self._host_result(request, response)
        reason = str(
            response.get("authorization_response_code")
            or response.get("error_code")
            or response.get("host_reference")
            or "HOST_RESPONSE"
        )
        self._record_event(request.operation_id, "HOST_AUTHORIZATION_COMPLETED", reason)
        if not definitive:
            return self._host_result(request, response)
        self._record_event(
            request.operation_id,
            "ISSUER_PROCESSING",
            str(response.get("authorization_response_code") or "ISSUER_RESPONSE_PRESENT"),
            metadata=self._host_metadata(response),
        )
        result = kernel.complete_online(session, request, response)
        self._record_event(
            request.operation_id,
            "ONLINE_COMPLETED",
            str(result.error_code or result.outcome or result.execution_state.value),
            metadata=self._host_metadata(response),
        )
        return self._with_host_metadata(result, response)

    def _original_host_reference(self, request: TransactionRequest) -> str | None:
        if request.original_operation_id is None:
            return None
        original = self.store.get(request.original_operation_id)
        if original.result is None:
            return None
        reference = original.result.details.get("host_reference")
        return str(reference) if reference else None

    def _complete_refund(
        self,
        request: TransactionRequest,
        approved: TransactionResult,
        deadline: float | None,
    ) -> TransactionResult:
        expected_original = self._original_host_reference(request)
        self._record_event(request.operation_id, "REFUND_STARTED", "ORIGINAL_HOST_REFERENCE")
        response = self.host.refund(request, deadline)
        self._record_event(
            request.operation_id,
            "REFUND_COMPLETED",
            str(response.get("error_code") or response.get("execution_state") or "HOST_RESPONSE"),
        )
        if (
            response.get("execution_state") == "SUCCEEDED"
            and response.get("operation_id") in {None, request.operation_id}
            and response.get("original_host_reference") == expected_original
        ):
            details = dict(approved.details)
            details.update(self._host_metadata(response))
            return self._result(
                request, ExecutionState.SUCCEEDED, RecoveryClass.NON_RETRYABLE, None, "APPROVED", details,
            )
        if response.get("original_host_reference") != expected_original:
            return self._result(
                request, ExecutionState.UNCERTAIN, RecoveryClass.QUERY_REQUIRED,
                "HOST_ORIGINAL_REFERENCE_MISMATCH", details={"command_sent": True, **self._host_metadata(response)},
            )
        return self._host_result(request, response)

    def _complete_reversal(
        self,
        request: TransactionRequest,
        approved: TransactionResult,
        deadline: float | None,
    ) -> TransactionResult:
        self._record_event(request.operation_id, "REVERSAL_STARTED", "ORIGINAL_OPERATION_REFERENCE")
        response = self.host.reverse(request, deadline)
        self._record_event(
            request.operation_id,
            "REVERSAL_COMPLETED",
            str(response.get("error_code") or response.get("execution_state") or "HOST_RESPONSE"),
        )
        if response.get("execution_state") == "SUCCEEDED" and response.get("operation_id") in {None, request.operation_id}:
            details = dict(approved.details)
            details.update(self._host_metadata(response))
            return self._result(
                request, ExecutionState.SUCCEEDED, RecoveryClass.NON_RETRYABLE, None, "APPROVED", details,
            )
        return self._host_result(request, response)

    def get(self, operation_id: str) -> TransactionRecord:
        return self.store.get(operation_id)

    def list_pending(self) -> list[TransactionRecord]:
        return self.store.list_pending()

    def list_transactions(self, limit: int = 100) -> list[TransactionRecord]:
        return self.store.list(limit)

    def query(self, operation_id: str) -> TransactionRecord:
        record = self.store.get(operation_id)
        if record.execution_state not in {PersistentState.IN_PROGRESS, PersistentState.UNCERTAIN}:
            return record
        response = self.host.query(operation_id)
        self._record_event(
            operation_id, "HOST_QUERY_COMPLETED", str(response.get("error_code") or response.get("execution_state")),
        )
        if response.get("execution_state") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return self._persist(self._host_result(record.request, response), "EXPLICIT_HOST_QUERY")
        kernel = self.kernels.get(record.kernel_ref)
        kernel_result: TransactionResult | None = None
        if kernel is not None:
            try:
                kernel_result = kernel.query(operation_id)
                self._record_event(operation_id, "RECOVERY_QUERY", "KERNEL_QUERY_RESULT")
            except KernelError as error:
                self._record_event(operation_id, "RECOVERY_QUERY_UNKNOWN", error.code)
        if kernel_result is not None and kernel_result.outcome == "ONLINE_REQUIRED":
            kernel_result = self._result(
                record.request,
                ExecutionState.UNCERTAIN,
                RecoveryClass.QUERY_REQUIRED,
                "ONLINE_HOST_QUERY_REQUIRED",
                details={"command_sent": True},
            )
        if kernel_result is not None and kernel_result.execution_state != ExecutionState.UNCERTAIN:
            return self._persist(kernel_result, "EXPLICIT_QUERY")
        if kernel_result is not None and record.result is None:
            return self._persist(kernel_result, "EXPLICIT_QUERY_UNCERTAIN")
        return self.store.get(operation_id)

    def recover(self, operation_id: str) -> TransactionRecord:
        record = self.store.get(operation_id)
        self._record_event(operation_id, "RECOVERY_STARTED", "EXPLICIT_RECOVERY_NO_AUTO_RESEND")
        if record.execution_state in {PersistentState.IN_PROGRESS, PersistentState.UNCERTAIN}:
            record = self.query(operation_id)
        if record.execution_state == PersistentState.IN_PROGRESS:
            result = self._result(
                record.request,
                ExecutionState.UNCERTAIN,
                RecoveryClass.READ_BACK_REQUIRED,
                "RESTART_IN_PROGRESS_READ_BACK_REQUIRED",
                details={"command_sent": True},
            )
            record = self._persist(result, "EXPLICIT_RECOVERY_AFTER_RESTART")
        self._record_event(operation_id, "RECOVERY_COMPLETED", record.execution_state.value)
        return self.store.get(operation_id)

    def cancel(self, operation_id: str) -> TransactionRecord:
        record = self.store.get(operation_id)
        if record.execution_state in {
            PersistentState.SUCCEEDED, PersistentState.FAILED,
            PersistentState.UNCERTAIN, PersistentState.CANCELLED,
        }:
            return record
        if record.execution_state == PersistentState.CREATED:
            result = self._result(
                record.request, ExecutionState.CANCELLED, RecoveryClass.NON_RETRYABLE,
                "CANCELLED_BEFORE_SEND", details={"command_sent": False},
            )
            return self._persist(result)
        with self._sessions_guard:
            active = self._sessions.get(operation_id)
        if active is not None:
            kernel, session = active
            try:
                cancel_exchange = getattr(self.reader, "cancel_exchange", None)
                if cancel_exchange is not None:
                    cancel_exchange()
                result = kernel.cancel(session)
                if result.outcome == "ONLINE_REQUIRED" or (
                    result.execution_state == ExecutionState.CANCELLED
                    and result.details.get("command_sent") is True
                ):
                    result = self._result(
                        record.request,
                        ExecutionState.UNCERTAIN,
                        RecoveryClass.QUERY_REQUIRED,
                        "CANCELLATION_AFTER_SEND_UNKNOWN",
                        details={"command_sent": True},
                    )
                return self._persist(result, "EXPLICIT_CANCEL")
            except KernelError as error:
                return self._persist(self._kernel_failure(record.request, error), "EXPLICIT_CANCEL")
        record = self.query(operation_id)
        if record.execution_state == PersistentState.IN_PROGRESS:
            result = self._result(
                record.request, ExecutionState.UNCERTAIN, RecoveryClass.READ_BACK_REQUIRED,
                "CANCELLATION_AFTER_SEND_UNKNOWN", details={"command_sent": True},
            )
            return self._persist(result)
        return record

    def readiness(self) -> dict[str, Any]:
        return {
            "simulation": True,
            "mode": "LAB / SYNTHETIC",
            "physical_validation": "NOT_EXECUTED",
            "kernels": [
                {
                    "kernel_ref": kernel.kernel_ref,
                    "kernel_version": kernel.kernel_version,
                    "kernel_mode": kernel.kernel_mode,
                    "simulation": kernel.capabilities().simulation,
                    "interfaces": list(kernel.capabilities().interfaces),
                    "online_processing": kernel.capabilities().online_processing,
                    "ready": kernel.capabilities().simulation,
                }
                for kernel in sorted(self.kernels.values(), key=lambda item: item.kernel_ref)
            ],
        }
