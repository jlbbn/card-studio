from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from card_studio import __version__
from card_studio.application import CardStudioApplication, laboratory_profile
from card_studio.host.base import HostError
from card_studio.host.mock import MockAuthorizationHost
from card_studio.kernels.base import KernelAdapter
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.money import Money
from card_studio.profiles.model import CardProfile
from card_studio.readers.base import ReaderError
from card_studio.readers.mock import MockReaderAdapter
from card_studio.transactions.errors import SimulatedCrash
from card_studio.transactions.model import TransactionError, TransactionRequest, TransactionType
from card_studio.transactions.recovery import RecoveryError
from card_studio.transactions.store import utc_now

from .assertions import AssertionRecorder
from .evidence import L3EvidenceStore
from .internal import internal_profile, prepare_internal_reader
from .model import GateResult, L3CaseResult, L3Report, P0_GATE_IDS, build_run_identity, identity_is_conformant
from .scenarios import LabKernelAdapter, ScenarioDefinition, matrix_definitions


class _RemovedReader(MockReaderAdapter):
    def open(self, reader_id, operation_id, deadline=None):
        raise ReaderError("READER_REMOVED", "SIMULATED: lector retirado.")


class _TracingReader(MockReaderAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instructions = []

    def transmit(self, connection, command, sensitivity="REDACTED"):
        raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
        if len(raw) >= 4:
            self.instructions.append(raw[1])
        return super().transmit(connection, command, sensitivity)


GATE_CASES = {
    "G1": ("contact-approved", "contactless-approved"),
    "G2": ("contact-declined", "contactless-declined", "unsupported-application", "multiple-application"),
    "G3": ("contact-online_required", "contactless-online_required", "host-duplicate"),
    "G4": ("contact-timeout", "contactless-timeout", "host-timeout"),
    "G5": ("recovery-query", "safe-retry", "unsafe-retry-blocked"),
    "G6": ("idempotency-conflict", "host-duplicate"),
    "G7": ("contact-reversal", "contactless-reversal", "partial-refund", "excessive-refund", "duplicate-reversal"),
    "G8": ("card-removed", "reader-removed", "malformed-tlv", "invalid-state"),
    "G9": tuple(item.case_id for item in matrix_definitions()),
    "G10": ("restart-in-progress", "restart-uncertain", "recovery-query"),
}


class L3Runner:
    def __init__(
        self,
        directory: Path,
        application_factory: Callable[..., CardStudioApplication] = CardStudioApplication,
        kernel_factory: Callable[[], KernelAdapter] = LabKernelAdapter,
        p0_evidence: Mapping[str, str] | None = None,
    ):
        self.directory = directory
        self.application_factory = application_factory
        self.kernel_factory = kernel_factory
        self.p0_evidence = {
            gate_id: str(p0_evidence.get(gate_id, "MISSING"))
            for gate_id in sorted(P0_GATE_IDS)
        } if p0_evidence is not None else {}
        self.evidence = L3EvidenceStore(directory)

    def _profile(self, kernel: KernelAdapter, definition: ScenarioDefinition) -> CardProfile:
        if isinstance(kernel, InternalEmvKernel):
            profile = internal_profile(kernel, definition.case_id, definition.interface, definition.scenario)
        else:
            profile = laboratory_profile(kernel.kernel_ref)
        value = profile.to_dict()
        requirements = dict(value["kernel_requirements"])
        if definition.case_id == "card-removed":
            requirements["exchange_before_processing"] = True
        if definition.case_id == "malformed-tlv":
            requirements["malformed_tlv"] = True
        if definition.case_id == "invalid-state":
            requirements["invalid_state"] = True
        value["kernel_requirements"] = requirements
        value["profile_digest"] = ""
        return CardProfile.from_dict(value)

    def _application(
        self,
        data_dir: Path,
        kernel: LabKernelAdapter,
        profile: CardProfile,
        reader: MockReaderAdapter,
        host: MockAuthorizationHost,
        hooks=None,
    ) -> CardStudioApplication:
        return self.application_factory(
            data_dir=data_dir,
            config_dir=data_dir / "config",
            profiles=[profile],
            kernels=kernel,
            reader=reader,
            host=host,
            hooks=hooks,
        )

    @staticmethod
    def _request(
        definition: ScenarioDefinition,
        operation_id: str,
        profile_ref: str,
        kernel_ref: str,
        original: dict[str, Any] | None = None,
    ) -> TransactionRequest:
        amount = 1000
        if definition.case_id == "partial-refund":
            amount = 400
        elif definition.case_id == "excessive-refund":
            amount = 1001
        elif definition.case_id == "idempotency-conflict":
            amount = 2000
        original_id = original["operation_id"] if original else None
        original_amount = Money(1000, "MXN", 2) if original else None
        application_aid = None
        if definition.case_id == "unsupported-application":
            application_aid = "A0000000099999"
        elif definition.case_id == "multiple-application":
            application_aid = "A0000000041010"
        return TransactionRequest.create(
            money=Money(amount, "MXN", 2),
            profile_ref=profile_ref,
            interface=definition.interface,
            kernel_ref=kernel_ref,
            transaction_type=definition.transaction_type,
            scenario=definition.scenario,
            original_operation_id=original_id,
            original_amount=original_amount,
            operation_id=operation_id,
            idempotency_key=f"key-{operation_id}",
            correlation_id=f"correlation-{operation_id}",
            application_aid=application_aid,
        )

    def _create(
        self,
        app: CardStudioApplication,
        kernel: KernelAdapter,
        profile: CardProfile,
        reader: MockReaderAdapter,
        request: TransactionRequest,
    ) -> dict[str, Any]:
        if isinstance(kernel, InternalEmvKernel):
            prepare_internal_reader(
                reader,
                profile,
                request,
                malformed=request.operation_id == "l3-malformed-tlv",
            )
        return app.create_transaction(request)

    def _original(
        self,
        app: CardStudioApplication,
        kernel: KernelAdapter,
        profile: CardProfile,
        reader: MockReaderAdapter,
        definition: ScenarioDefinition,
    ) -> dict[str, Any]:
        purchase = ScenarioDefinition(
            f"{definition.case_id}-original", definition.interface, "approved",
        )
        profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
        request = self._request(
            purchase, f"l3-{definition.case_id}-original", profile_ref, kernel.kernel_ref,
        )
        return self._create(app, kernel, profile, reader, request)

    def _run_case(self, definition: ScenarioDefinition) -> L3CaseResult:
        started_at = utc_now()
        operation_id = f"l3-{definition.case_id}"
        data_dir = self.directory / "work" / definition.case_id
        kernel = self.kernel_factory()
        profile = self._profile(kernel, definition)
        profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
        reader = _RemovedReader() if definition.case_id == "reader-removed" else _TracingReader()
        if definition.case_id == "card-removed":
            reader.fail_next_transmit = "CARD_REMOVED"
        host = MockAuthorizationHost(
            data_dir / "host-v1.sqlite3",
            timeout_operations={operation_id} if definition.case_id == "host-timeout" else set(),
        )
        hooks = None
        if definition.case_id == "restart-in-progress":
            def crash(_operation_id: str) -> None:
                raise SimulatedCrash("after_in_progress")
            hooks = {"after_in_progress": crash}
        app = self._application(data_dir, kernel, profile, reader, host, hooks)
        assertions = AssertionRecorder()
        result: dict[str, Any] | None = None
        try:
            original = None
            if definition.transaction_type != TransactionType.PURCHASE.value:
                original = self._original(app, kernel, profile, reader, definition)
                assertions.equal("original_approved", original.get("functional_result"), "APPROVED")
            if definition.case_id == "duplicate-reversal":
                first_definition = replace(definition, case_id="duplicate-reversal-first")
                first_request = self._request(
                    first_definition, "l3-duplicate-reversal-first", profile_ref, kernel.kernel_ref, original,
                )
                first = self._create(app, kernel, profile, reader, first_request)
                assertions.equal("first_reversal", first.get("execution_state"), "SUCCEEDED")
            request = self._request(definition, operation_id, profile_ref, kernel.kernel_ref, original)
            if definition.case_id == "idempotency-conflict":
                first_request = replace(request, operation_id="l3-idempotency-conflict-first", money=Money(1000, "MXN", 2))
                first = self._create(app, kernel, profile, reader, first_request)
                assertions.equal("first_request", first.get("execution_state"), "SUCCEEDED")
                try:
                    self._create(app, kernel, profile, reader, request)
                    assertions.check("idempotency_conflict", False, "conflict not raised")
                except TransactionError as error:
                    assertions.equal("idempotency_conflict", error.code, "IDEMPOTENCY_CONFLICT")
                result = app.get(first_request.operation_id)
            elif definition.case_id == "restart-in-progress":
                try:
                    self._create(app, kernel, profile, reader, request)
                    assertions.check("crash_injected", False, "crash not raised")
                except SimulatedCrash:
                    assertions.check("crash_injected", True, "after_in_progress")
                kernel = self.kernel_factory()
                host = MockAuthorizationHost(data_dir / "host-v1.sqlite3")
                reader = _TracingReader()
                app = self._application(data_dir, kernel, profile, reader, host)
                before = app.get(operation_id)
                assertions.equal("restart_state", before.get("execution_state"), "IN_PROGRESS")
                result = app.recover(operation_id)
                assertions.equal("read_back_required", result.get("recovery_action"), "READ_BACK_REQUIRED")
            else:
                result = self._create(app, kernel, profile, reader, request)
                if definition.case_id == "host-duplicate":
                    duplicate = host.authorize(request)
                    assertions.equal("duplicate_host_operation", duplicate.get("operation_id"), operation_id)
                    assertions.equal("duplicate_host_state", duplicate.get("execution_state"), "SUCCEEDED")
                    try:
                        host.authorize(replace(request, money=Money(2000, "MXN", 2)))
                        assertions.check("host_digest_conflict", False, "conflict not raised")
                    except HostError as error:
                        assertions.equal("host_digest_conflict", error.code, "IDEMPOTENCY_CONFLICT")
                elif definition.case_id == "host-timeout":
                    events = [entry["event_type"] for entry in app.audit(operation_id)]
                    assertions.equal("single_host_authorization", events.count("HOST_AUTHORIZATION_STARTED"), 1)
                    assertions.check("no_online_completion", "ONLINE_COMPLETED" not in events, "post-send timeout has no completion")
                    assertions.check("no_reversal_event", "REVERSAL_STARTED" not in events, "post-send timeout has no reversal")
                    kernel = self.kernel_factory()
                    host = MockAuthorizationHost(data_dir / "host-v1.sqlite3")
                    reader = _TracingReader()
                    app = self._application(data_dir, kernel, profile, reader, host)
                    result = app.query(operation_id)
                    restarted_events = [entry["event_type"] for entry in app.audit(operation_id)]
                    assertions.equal("single_authorization_after_restart", restarted_events.count("HOST_AUTHORIZATION_STARTED"), 1)
                    assertions.equal("restart_conserves_uncertain", result.get("execution_state"), "UNCERTAIN")
                elif definition.case_id == "restart-uncertain":
                    kernel = self.kernel_factory()
                    host = MockAuthorizationHost(data_dir / "host-v1.sqlite3")
                    reader = _TracingReader()
                    app = self._application(data_dir, kernel, profile, reader, host)
                    result = app.query(operation_id)
                    assertions.equal("unknown_query_conserves_uncertain", result.get("execution_state"), "UNCERTAIN")
                elif definition.case_id == "recovery-query":
                    result = app.query(operation_id)
                    expected = "UNCERTAIN" if isinstance(kernel, InternalEmvKernel) else "SUCCEEDED"
                    assertions.equal("explicit_query_conserves_known_state", result.get("execution_state"), expected)
                elif definition.case_id == "safe-retry":
                    assertions.equal("safe_failure", result.get("recovery_action"), "SAFE_TO_RETRY")
                    result = app.retry_transaction(request)
                    assertions.equal("retry_succeeded", result.get("execution_state"), "SUCCEEDED")
                    assertions.equal("single_explicit_retry", kernel.execution_count(operation_id), 2)
                elif definition.case_id == "unsafe-retry-blocked":
                    try:
                        app.retry_transaction(request)
                        assertions.check("unsafe_retry_blocked", False, "retry not blocked")
                    except RecoveryError as error:
                        assertions.equal("unsafe_retry_blocked", error.code, "NON_RETRYABLE")

            case_id = definition.case_id
            if isinstance(kernel, InternalEmvKernel) and case_id in {
                "contact-approved", "contact-declined", "contact-online_required",
                "contactless-approved", "contactless-declined", "contactless-online_required",
            }:
                contact = case_id.startswith("contact-")
                expected = [0xA4, 0xA4, 0xA8, 0xB2]
                if contact:
                    expected.append(0xAE)
                if contact and case_id == "contact-online_required":
                    expected.append(0xAE)
                assertions.equal("interface_apdu_instruction_sequence", reader.instructions, expected)
                if not contact:
                    assertions.check("contactless_no_generate_ac", 0xAE not in reader.instructions, "cryptogram-in-GPO has no GENERATE AC")
            if case_id.endswith("-approved") and case_id.startswith(("contact-", "contactless-")):
                assertions.equal("approved_state", result.get("execution_state"), "SUCCEEDED")
                assertions.equal("approved_functional", result.get("functional_result"), "APPROVED")
            elif case_id.endswith("-declined"):
                assertions.equal("declined_state", result.get("execution_state"), "FAILED")
                assertions.equal("declined_functional", result.get("functional_result"), "DECLINED")
            elif case_id.endswith("-online_required"):
                assertions.equal("online_completed", result.get("functional_result"), "APPROVED")
            elif case_id.endswith("-timeout") and case_id != "host-timeout":
                assertions.equal("timeout_uncertain", result.get("execution_state"), "UNCERTAIN")
                assertions.equal("timeout_query", result.get("recovery_action"), "QUERY_REQUIRED")
            elif case_id.endswith("-reversal") and case_id != "duplicate-reversal":
                assertions.equal("reversal_succeeded", result.get("execution_state"), "SUCCEEDED")
            elif case_id == "unsupported-application":
                expected = "APPLICATION_NOT_FOUND" if isinstance(kernel, InternalEmvKernel) else "APPLICATION_NOT_SUPPORTED"
                assertions.equal("unsupported_application", result.get("error_code"), expected)
            elif case_id == "multiple-application":
                assertions.equal("selected_second_application", result.get("details", {}).get("selected_aid"), "A0000000041010")
            elif case_id == "card-removed":
                assertions.equal("card_removed", result.get("error_code"), "CARD_REMOVED")
            elif case_id == "reader-removed":
                assertions.equal("reader_removed", result.get("error_code"), "READER_REMOVED")
            elif case_id == "malformed-tlv":
                expected = "MALFORMED_FCI" if isinstance(kernel, InternalEmvKernel) else "MALFORMED_TLV"
                assertions.equal("malformed_tlv", result.get("error_code"), expected)
            elif case_id == "invalid-state":
                assertions.equal("invalid_state", result.get("error_code"), "INVALID_STATE")
            elif case_id == "host-timeout":
                assertions.equal("host_timeout_uncertain", result.get("execution_state"), "UNCERTAIN")
                assertions.equal("host_timeout_query", result.get("recovery_action"), "QUERY_REQUIRED")
                assertions.check("no_blind_reversal", result.get("functional_result") != "DECLINED", "timeout is not decline")
            elif case_id == "restart-in-progress":
                assertions.equal("recovered_uncertain", result.get("execution_state"), "UNCERTAIN")
            elif case_id == "restart-uncertain":
                assertions.equal("uncertain_conserved", result.get("recovery_action"), "QUERY_REQUIRED")
            elif case_id == "partial-refund":
                assertions.equal("partial_refund", result.get("execution_state"), "SUCCEEDED")
                assertions.equal("partial_amount", result.get("amount_minor"), 400)
            elif case_id == "excessive-refund":
                assertions.equal("excessive_refund", result.get("error_code"), "EXCESSIVE_REFUND")
            elif case_id == "duplicate-reversal":
                assertions.equal("duplicate_reversal", result.get("error_code"), "DUPLICATE_REVERSAL")
            assertions.equal("simulation", result.get("simulation"), True)
            assertions.equal("physical_validation", result.get("physical_validation"), "NOT_EXECUTED")
            serialized = json.dumps(result, ensure_ascii=False)
            assertions.check(
                "evidence_sanitized",
                not any(key in serialized for key in ('"pan"', '"pin"', '"cvv"', '"track2"', '"raw_apdu"')),
                "sensitive field names absent",
            )
        except Exception as error:
            assertions.check("unexpected_exception", False, type(error).__name__)
            if result is None:
                try:
                    result = app.get(operation_id)
                except Exception:
                    result = {}
        transitions = list((result or {}).get("state_transitions", []))
        case_result = L3CaseResult(
            test_case_id=definition.case_id,
            operation_id=operation_id,
            profile_ref=profile_ref,
            profile_digest=profile.profile_digest,
            kernel_adapter=type(kernel).__name__,
            kernel_ref=kernel.kernel_ref,
            kernel_version=kernel.kernel_version,
            kernel_mode=kernel.kernel_mode,
            terminal_version=__version__,
            interface=definition.interface,
            host=host.host_ref,
            host_adapter=type(host).__name__,
            host_version=host.host_version,
            state_transitions=transitions,
            functional_result=(result or {}).get("functional_result"),
            execution_state=(result or {}).get("execution_state"),
            recovery_action=(result or {}).get("recovery_action"),
            started_at=started_at,
            completed_at=utc_now(),
            assertions=assertions.items,
            status="PASS" if assertions.passed else "FAIL",
        )
        case_result.conformant = identity_is_conformant(case_result.identity())
        case_result.evidence_reference = f"{definition.case_id}.json"
        self.evidence.write_case(case_result)
        return case_result

    def run(self) -> L3Report:
        started_at = utc_now()
        cases = [self._run_case(definition) for definition in matrix_definitions()]
        by_id = {case.test_case_id: case for case in cases}
        gates = []
        for gate_id, case_ids in GATE_CASES.items():
            gate_cases = [by_id[case_id] for case_id in case_ids]
            identity = build_run_identity(gate_cases)
            passed = all(case.status == "PASS" for case in gate_cases)
            if gate_id == "G9":
                passed = passed and all(
                    self.evidence.private_file(self.directory / "cases" / f"{case_id}.json")
                    for case_id in case_ids
                )
            if not passed:
                status, reason = "FAIL", "ASSERTIONS_OR_EVIDENCE_FAILED"
            elif identity.get("mixed"):
                status, reason = "FAIL", "MIXED_ADAPTER_IDENTITY"
            elif not identity_is_conformant(identity):
                status, reason = "NON_CONFORMANT", "NON_CONFORMANT_KERNEL_ADAPTER"
            else:
                status, reason = "PASS", "REAL_ADAPTER_IDENTITY_VERIFIED"
            gates.append(GateResult(gate_id, status, tuple(case_ids), identity, reason))
        identity = build_run_identity(cases)
        report = L3Report(
            cases,
            gates,
            started_at,
            utc_now(),
            identity,
            identity_is_conformant(identity),
            dict(self.p0_evidence),
        )
        report.conformant = report.conformant and report.p0_complete
        self.evidence.write_report(report)
        return report
