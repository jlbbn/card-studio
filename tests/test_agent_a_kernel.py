from __future__ import annotations

import json
import time
import unittest

from card_studio.emv.decision import CvmDecision, combine_actions, decide_cvm, decide_risk
from card_studio.emv.protocol import laboratory_commands, laboratory_responses
from card_studio.interfaces import (
    ContactInterface,
    ContactlessInterface,
    MagneticSimulatedInterface,
    VirtualInterface,
)
from card_studio.kernels import InternalEmvKernel, KernelError, MockKernelAdapter
from card_studio.kernels.state import KernelState, KernelStateMachine
from card_studio.money import Money
from card_studio.profiles import CardApplication, CardProfile, ProfileStatus
from card_studio.readers.mock import MockReaderAdapter
from card_studio.readers.virtual import VirtualReaderAdapter
from card_studio.tlv import (
    TlvLimits,
    TlvLimitError,
    TlvMalformedError,
    TlvNode,
    TlvNonCanonicalError,
    TlvTruncatedError,
    encode_tlv,
    find_tag,
    parse_tlv,
    safe_tlv_evidence,
)
from card_studio.transactions import ExecutionState, FunctionalResult, RecoveryClass, SimulationScenario, TransactionRequest


AID_ONE = "A0000000041010"
AID_TWO = "B0000000041010"


def profile_ref(profile):
    return f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"


def make_profile(
    kernel_ref="internal-emv-lab-v1",
    applications=None,
    interfaces=("CONTACT", "CONTACTLESS", "VIRTUAL"),
    cvm="NO_CVM",
    cvm_capabilities=("NO_CVM", "SIGNATURE", "ONLINE_PIN_REQUIRED", "OFFLINE_PIN_REQUIRED", "CDCVM"),
    risk=None,
    scheme_parameters=None,
    status=ProfileStatus.APPROVED_FOR_SIMULATION.value,
):
    parameters = {
        "cvm": {"method": cvm},
        "risk": risk or {"floor_limit_minor": 0, "random_selection_percent": 0},
    }
    if scheme_parameters:
        parameters.update(scheme_parameters)
    return CardProfile(
        schema_version=1,
        profile_id="lab-generic",
        profile_version=1,
        profile_digest="",
        scheme_family="laboratory",
        display_name="Perfil LAB_ONLY",
        applications=tuple(applications or (CardApplication(AID_ONE, "LAB application", 1),)),
        interfaces=tuple(interfaces),
        cvm_capabilities=tuple(cvm_capabilities),
        kernel_requirements={"simulation": True, "kernel_ref": kernel_ref},
        scheme_parameters=parameters,
        test_credential_ref="test://lab-only",
        synthetic_pan_profile={"synthetic": True},
        tokenization_policy={"enabled": False},
        status=status,
    )


def make_request(
    profile,
    kernel_ref="internal-emv-lab-v1",
    operation_id="op-a-1",
    scenario=SimulationScenario.APPROVED.value,
    interface="VIRTUAL",
    amount=1000,
    application_aid=None,
    idempotency_key=None,
):
    return TransactionRequest.create(
        money=Money(amount, "MXN", 2),
        profile_ref=profile_ref(profile),
        interface=interface,
        kernel_ref=kernel_ref,
        scenario=scenario,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        application_aid=application_aid,
    )


def scenario_action(scenario):
    return {
        SimulationScenario.DECLINED.value: "DECLINED",
        SimulationScenario.ONLINE_REQUIRED.value: "ONLINE_REQUIRED",
        SimulationScenario.ACTION_REQUIRED.value: "ACTION_REQUIRED",
    }.get(scenario, "APPROVED")


def make_interface(profile, interface="VIRTUAL", action="APPROVED", application_aid=None, malformed=None):
    responses = laboratory_responses(
        profile,
        action=action,
        application_aid=application_aid,
        malformed=malformed,
    )
    if interface == "VIRTUAL":
        adapter = VirtualReaderAdapter(responses=responses)
        interface_type = VirtualInterface
    else:
        adapter = MockReaderAdapter(responses=responses)
        interface_type = ContactInterface if interface == "CONTACT" else ContactlessInterface
    reader_session = adapter.open(adapter.reader_info.reader_id, "reader-op-a")
    card_interface = interface_type(adapter, reader_session).open()
    return card_interface, adapter


class TlvTests(unittest.TestCase):
    def test_short_multibyte_and_unknown_tags_round_trip(self):
        original = (
            TlvNode("4F", bytes.fromhex(AID_ONE)),
            TlvNode("9F02", b"\x00\x00\x00\x00\x10\x00"),
            TlvNode("DF7F", b"unknown-preserved"),
        )
        encoded = encode_tlv(original)
        parsed = parse_tlv(encoded)
        self.assertEqual(parsed[0].tag, "4F")
        self.assertEqual(parsed[1].tag, "9F02")
        self.assertEqual(parsed[2].value, b"unknown-preserved")
        self.assertFalse(parsed[2].known)
        self.assertEqual(encode_tlv(parsed), encoded)

    def test_long_form_lengths_128_and_256_are_canonical(self):
        for size, length_encoding in ((128, b"\x81\x80"), (256, b"\x82\x01\x00")):
            with self.subTest(size=size):
                encoded = encode_tlv(TlvNode("DF10", bytes(size)))
                self.assertEqual(encoded[2:2 + len(length_encoding)], length_encoding)
                self.assertEqual(parse_tlv(encoded)[0].value, bytes(size))

    def test_nested_templates_and_find_tag(self):
        encoded = encode_tlv(TlvNode("70", children=(
            TlvNode("77", children=(TlvNode("DF01", b"LAB_ONLY"),)),
            TlvNode("DF7F", b"preserved"),
        )))
        root = parse_tlv(encoded)[0]
        self.assertTrue(root.constructed)
        self.assertEqual(find_tag(root.children, "77").children[0].value, b"LAB_ONLY")
        self.assertEqual(find_tag(root.children, "DF7F").value, b"preserved")

    def test_safe_evidence_contains_metadata_not_values(self):
        secret_like_value = b"VALUE-MUST-NOT-APPEAR"
        nodes = parse_tlv(encode_tlv(TlvNode("70", children=(TlvNode("DF01", secret_like_value),))))
        evidence = safe_tlv_evidence(nodes)
        serialized = json.dumps(evidence)
        self.assertNotIn(secret_like_value.decode(), serialized)
        self.assertNotIn(secret_like_value.hex(), serialized)
        self.assertEqual(evidence[0]["children"][0]["value_length"], len(secret_like_value))

    def test_malformed_and_truncated_inputs(self):
        cases = {
            "truncated_multibyte_tag": (b"\x1f", TlvTruncatedError),
            "missing_length": (b"\x4f", TlvTruncatedError),
            "indefinite_length": (b"\x70\x80\x00\x00", TlvMalformedError),
            "truncated_long_length": (b"\x4f\x82\x01", TlvTruncatedError),
            "truncated_value": (b"\x4f\x05\xaa", TlvTruncatedError),
            "noncanonical_long_length": (b"\x4f\x81\x01\xaa", TlvNonCanonicalError),
            "noncanonical_multibyte_tag": (b"\x1f\x80\x01\x00", TlvNonCanonicalError),
            "zero_prefixed_length": (b"\x4f\x82\x00\x80" + bytes(128), TlvNonCanonicalError),
        }
        for name, (raw, error_type) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(error_type):
                    parse_tlv(raw)

    def test_byte_node_and_depth_limits(self):
        with self.assertRaises(TlvLimitError):
            parse_tlv(b"\x4f\x00", TlvLimits(max_bytes=1))
        with self.assertRaises(TlvLimitError):
            parse_tlv(b"\x4f\x00\x50\x00", TlvLimits(max_nodes=1))
        nested = encode_tlv(TlvNode("70", children=(TlvNode("70", children=(TlvNode("DF01", b"x"),)),)))
        with self.assertRaises(TlvLimitError):
            parse_tlv(nested, TlvLimits(max_depth=1))
        self.assertEqual(len(parse_tlv(nested, TlvLimits(max_depth=3))), 1)


class InternalKernelTests(unittest.TestCase):
    def setUp(self):
        self.profile = make_profile()
        self.kernel = InternalEmvKernel()

    def open(self, request, profile=None, interface=None, malformed=None):
        selected_profile = profile or self.profile
        card_interface, adapter = make_interface(
            selected_profile,
            interface=request.interface,
            action=scenario_action(request.scenario),
            application_aid=request.application_aid,
            malformed=malformed,
        )
        session = self.kernel.open_session(request, card_interface, selected_profile)
        return session, card_interface, adapter

    def test_contact_contactless_and_virtual_e2e(self):
        for index, interface in enumerate(("CONTACT", "CONTACTLESS", "VIRTUAL")):
            with self.subTest(interface=interface):
                request = make_request(self.profile, operation_id=f"op-e2e-{index}", interface=interface)
                session, card_interface, _ = self.open(request)
                result = self.kernel.execute(session, request)
                self.assertEqual(result.execution_state, ExecutionState.SUCCEEDED)
                self.assertEqual(result.outcome, "APPROVED")
                self.assertEqual(result.details["selected_aid"], AID_ONE)
                self.assertTrue(result.details["command_sent"])
                self.assertEqual(result.details["protocol"], "LAB_ONLY")
                states = [item["new_state"] for item in result.details["state_transitions"]]
                self.assertEqual(states[-1], "APPROVED")
                for expected in (
                    "APPLICATION_SELECTION",
                    "APPLICATION_INITIALIZATION",
                    "CARD_DATA_PROCESSING",
                    "PROCESSING_RESTRICTIONS",
                    "CVM_PROCESSING",
                    "TERMINAL_RISK_MANAGEMENT",
                    "TERMINAL_ACTION_ANALYSIS",
                    "CARD_ACTION_ANALYSIS",
                    "COMPLETION",
                ):
                    self.assertIn(expected, states)
                self.kernel.close(session)
                card_interface.close()

    def test_application_ambiguity_never_selects_silently(self):
        profile = make_profile(applications=(CardApplication(AID_ONE, "One", 1), CardApplication(AID_TWO, "Two", 0)))
        request = make_request(profile)
        card_interface, _ = make_interface(profile, application_aid=AID_TWO)
        session = self.kernel.open_session(request, card_interface, profile)
        result = self.kernel.execute(session, request)
        self.assertEqual(result.execution_state, ExecutionState.FAILED)
        self.assertIsNone(result.outcome)
        self.assertEqual(result.error_code, "APPLICATION_SELECTION_REQUIRED")
        self.assertFalse(result.details["command_sent"])
        self.assertNotIn("selected_aid", result.details)

    def test_explicit_aid_selects_requested_application(self):
        profile = make_profile(applications=(CardApplication(AID_ONE, "One", 1), CardApplication(AID_TWO, "Two", 0)))
        request = make_request(profile, application_aid=AID_TWO)
        card_interface, _ = make_interface(profile, application_aid=AID_TWO)
        session = self.kernel.open_session(request, card_interface, profile)
        result = self.kernel.execute(session, request)
        self.assertEqual(result.outcome, "APPROVED")
        self.assertEqual(result.details["selected_aid"], AID_TWO)

    def test_unknown_explicit_aid_is_technical_failure(self):
        request = make_request(self.profile, application_aid=AID_TWO)
        card_interface, _ = make_interface(self.profile)
        session = self.kernel.open_session(request, card_interface, self.profile)
        result = self.kernel.execute(session, request)
        self.assertEqual(result.execution_state, ExecutionState.FAILED)
        self.assertEqual(result.error_code, "APPLICATION_NOT_FOUND")
        self.assertIsNone(result.outcome)

    def test_malformed_laboratory_response_at_each_step(self):
        for step in ("SELECT", "INITIALIZE", "READ_DATA", "CARD_ACTION"):
            with self.subTest(step=step):
                kernel = InternalEmvKernel()
                request = make_request(self.profile, operation_id=f"op-malformed-{step}")
                card_interface, _ = make_interface(self.profile, malformed=step)
                session = kernel.open_session(request, card_interface, self.profile)
                result = kernel.execute(session, request)
                self.assertEqual(result.execution_state, ExecutionState.FAILED)
                self.assertEqual(result.error_code, "TLV_TRUNCATED")
                self.assertIsNone(result.outcome)
                self.assertNotEqual(result.outcome, "DECLINED")

    def test_magnetic_and_non_simulated_interfaces_are_blocked(self):
        profile = make_profile(interfaces=("MAGNETIC_SIMULATED",))
        request = make_request(profile, interface="MAGNETIC_SIMULATED")
        adapter = MockReaderAdapter()
        reader_session = adapter.open(adapter.reader_info.reader_id, request.operation_id)
        magnetic = MagneticSimulatedInterface(adapter, reader_session).open()
        with self.assertRaises(KernelError) as caught:
            self.kernel.open_session(request, magnetic, profile)
        self.assertEqual(caught.exception.code, "UNSUPPORTED_INTERFACE")

        virtual_request = make_request(self.profile)
        card_interface, _ = make_interface(self.profile)
        object.__setattr__(card_interface.session, "simulated", False)
        with self.assertRaises(KernelError) as caught:
            self.kernel.open_session(virtual_request, card_interface, self.profile)
        self.assertEqual(caught.exception.code, "NON_SIMULATED_INTERFACE")

    def test_profile_kernel_interface_and_request_mismatches(self):
        request = make_request(self.profile)
        card_interface, _ = make_interface(self.profile)
        wrong_profile_ref = TransactionRequest.create(
            money=Money(1000, "MXN", 2),
            profile_ref="lab-generic:1:" + "0" * 64,
            interface="VIRTUAL",
            kernel_ref=self.kernel.kernel_ref,
        )
        with self.assertRaises(KernelError) as caught:
            self.kernel.open_session(wrong_profile_ref, card_interface, self.profile)
        self.assertEqual(caught.exception.code, "PROFILE_REF_MISMATCH")

        other_kernel_profile = make_profile(kernel_ref="mock-kernel-v1")
        other_request = make_request(other_kernel_profile)
        with self.assertRaises(KernelError) as caught:
            self.kernel.open_session(other_request, card_interface, other_kernel_profile)
        self.assertEqual(caught.exception.code, "PROFILE_KERNEL_MISMATCH")

        wrong_kernel_request = make_request(self.profile, kernel_ref="mock-kernel-v1")
        with self.assertRaises(KernelError) as caught:
            self.kernel.open_session(wrong_kernel_request, card_interface, self.profile)
        self.assertEqual(caught.exception.code, "KERNEL_NOT_FOUND")

        contact_request = make_request(self.profile, interface="CONTACT")
        with self.assertRaises(KernelError) as caught:
            self.kernel.open_session(contact_request, card_interface, self.profile)
        self.assertEqual(caught.exception.code, "INTERFACE_MISMATCH")

    def test_reader_uncertain_outcome_is_preserved(self):
        request = make_request(self.profile)
        session, _, adapter = self.open(request)
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        result = self.kernel.execute(session, request)
        self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(result.error_code, "TRANSMIT_TIMEOUT")
        self.assertEqual(result.recovery, RecoveryClass.QUERY_REQUIRED)
        self.assertIsNone(result.outcome)

    def test_reader_certain_failure_is_not_decline(self):
        request = make_request(self.profile)
        session, _, adapter = self.open(request)
        adapter.fail_next_transmit = "CARD_REMOVED"
        result = self.kernel.execute(session, request)
        self.assertEqual(result.execution_state, ExecutionState.FAILED)
        self.assertEqual(result.error_code, "CARD_REMOVED")
        self.assertIsNone(result.outcome)
        self.assertNotEqual(result.error_code, "SIMULATED_DECLINE")

    def test_timeout_before_send_is_safe_and_after_send_is_uncertain(self):
        before = make_request(self.profile, operation_id="op-before")
        before_session, _, _ = self.open(before)
        before_result = self.kernel.execute(before_session, before, deadline=time.monotonic() - 1)
        self.assertEqual(before_result.execution_state, ExecutionState.FAILED)
        self.assertEqual(before_result.recovery, RecoveryClass.SAFE_TO_RETRY)
        self.assertFalse(before_result.details["command_sent"])

        after = make_request(self.profile, operation_id="op-after", scenario=SimulationScenario.TIMEOUT.value)
        after_session, _, _ = self.open(after)
        after_result = self.kernel.execute(after_session, after)
        self.assertEqual(after_result.execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(after_result.recovery, RecoveryClass.QUERY_REQUIRED)
        self.assertTrue(after_result.details["command_sent"])
        self.assertIsNone(after_result.outcome)

    def test_cvm_models_and_pin_never_captured(self):
        methods = ("NO_CVM", "SIGNATURE", "ONLINE_PIN_REQUIRED", "OFFLINE_PIN_REQUIRED", "CDCVM")
        for method in methods:
            with self.subTest(method=method):
                profile = make_profile(cvm=method)
                decision = decide_cvm(profile)
                self.assertEqual(decision, CvmDecision(method))
                self.assertFalse(decision.pin_captured)
                request = make_request(profile, operation_id=f"op-cvm-{method}")
                card_interface, _ = make_interface(profile)
                session = self.kernel.open_session(request, card_interface, profile)
                result = self.kernel.execute(session, request)
                self.assertEqual(result.details["cvm_result"]["result"], method)
                self.assertFalse(result.details["cvm_result"]["pin_captured"])
                self.assertNotIn("pin_value", result.details)
        failed_profile = make_profile(cvm="CVM_FAILED")
        self.assertEqual(decide_cvm(failed_profile).result, "CVM_FAILED")
        unsupported = make_profile(cvm="SIGNATURE", cvm_capabilities=("NO_CVM",))
        self.assertEqual(decide_cvm(unsupported).result, "NOT_SUPPORTED")

    def test_risk_is_deterministic_and_profile_driven(self):
        profile = make_profile(risk={
            "floor_limit_minor": 500,
            "interface_limits_minor": {"CONTACTLESS": 400},
            "random_selection_percent": 100,
        })
        request = make_request(profile, interface="CONTACTLESS", amount=1000)
        first = decide_risk(request, profile)
        second = decide_risk(request, profile)
        self.assertEqual(first, second)
        self.assertTrue(first.floor_limit_exceeded)
        self.assertTrue(first.interface_limit_exceeded)
        self.assertTrue(first.randomly_selected)
        self.assertTrue(first.online_required)
        self.assertIn("DETERMINISTIC_SELECTION", first.reasons)
        offline = make_profile(risk={"floor_limit_minor": 1, "exceptions": ["FORCE_OFFLINE"]})
        self.assertFalse(decide_risk(request, offline).online_required)

    def test_action_combination_never_turns_failure_into_decline(self):
        self.assertEqual(combine_actions("APPROVED", "DECLINED"), FunctionalResult.DECLINED)
        self.assertEqual(combine_actions("ONLINE_REQUIRED", "APPROVED"), FunctionalResult.ONLINE_REQUIRED)
        self.assertEqual(combine_actions("ACTION_REQUIRED", "APPROVED"), FunctionalResult.ACTION_REQUIRED)
        self.assertEqual(combine_actions("APPROVED", "APPROVED"), FunctionalResult.APPROVED)

    def test_online_completion_approved_declined_and_invalid_response(self):
        for decision in ("APPROVED", "DECLINED"):
            with self.subTest(decision=decision):
                kernel = InternalEmvKernel()
                request = make_request(self.profile, operation_id=f"op-online-{decision}", scenario="online_required")
                card_interface, _ = make_interface(self.profile, action="ONLINE_REQUIRED")
                session = kernel.open_session(request, card_interface, self.profile)
                pending = kernel.execute(session, request)
                self.assertEqual(pending.outcome, "ONLINE_REQUIRED")
                with self.assertRaises(KernelError):
                    kernel.complete_online(session, request, {"decision": "UNKNOWN", "simulation": True})
                completed = kernel.complete_online(session, request, {
                    "decision": decision,
                    "simulation": True,
                    "authorization": "must-not-be-copied",
                })
                self.assertEqual(completed.outcome, decision)
                self.assertEqual(completed.details["host_response"], "SIMULATED")
                self.assertNotIn("authorization", completed.details)
                self.assertEqual(kernel.complete_online(session, request, {"decision": decision}), completed)

    def test_idempotency_query_duplicate_execution_cancel_and_close(self):
        request = make_request(self.profile, scenario=SimulationScenario.RETRY.value)
        session, card_interface, _ = self.open(request)
        first = self.kernel.execute(session, request)
        second = self.kernel.execute(session, request)
        self.assertEqual(first, second)
        self.assertEqual(self.kernel.execution_count(request.operation_id), 1)
        self.assertEqual(self.kernel.query(request.operation_id), first)
        self.assertTrue(self.kernel.close(session).closed)
        self.assertTrue(self.kernel.close(session).closed)
        with self.assertRaises(KernelError) as caught:
            self.kernel.execute(session, request)
        self.assertEqual(caught.exception.code, "SESSION_CLOSED")

        cancel_request = make_request(self.profile, operation_id="op-cancel")
        cancel_session, _, _ = self.open(cancel_request)
        cancelled = self.kernel.cancel(cancel_session)
        self.assertEqual(cancelled.execution_state, ExecutionState.CANCELLED)
        self.assertEqual(self.kernel.cancel(cancel_session), cancelled)
        self.assertEqual(self.kernel.execution_count(cancel_request.operation_id), 0)

    def test_duplicate_operation_and_idempotency_conflicts(self):
        first = make_request(self.profile, operation_id="op-duplicate", idempotency_key="key-one")
        first_session, _, _ = self.open(first)
        self.kernel.execute(first_session, first)
        conflicting = make_request(self.profile, operation_id="op-duplicate", idempotency_key="key-two", amount=2000)
        session, _, _ = self.open(conflicting)
        with self.assertRaises(KernelError) as caught:
            self.kernel.execute(session, conflicting)
        self.assertEqual(caught.exception.code, "DUPLICATE_OPERATION")

        keyed = make_request(self.profile, operation_id="op-key-two", idempotency_key="key-one", amount=3000)
        key_session, _, _ = self.open(keyed)
        with self.assertRaises(KernelError) as caught:
            self.kernel.execute(key_session, keyed)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_result_loader_recovers_known_result_after_restart(self):
        request = make_request(self.profile)
        session, _, _ = self.open(request)
        result = self.kernel.execute(session, request)
        external = {request.operation_id: result.to_dict()}
        restarted = InternalEmvKernel(result_loader=external.get)
        self.assertEqual(restarted.query(request.operation_id), result)
        with self.assertRaises(KernelError) as caught:
            restarted.query("op-unknown")
        self.assertEqual(caught.exception.code, "OPERATION_NOT_FOUND")

    def test_invalid_transition_and_sanitized_history(self):
        machine = KernelStateMachine("op-state")
        with self.assertRaises(KernelError) as caught:
            machine.transition(KernelState.APPROVED, "invalid")
        self.assertEqual(caught.exception.code, "INVALID_STATE_TRANSITION")
        machine.transition(KernelState.SESSION_OPEN, "open")
        with self.assertRaises(KernelError):
            machine.transition(KernelState.CVM_PROCESSING, "skip")
        history = machine.sanitized_history()[0]
        self.assertEqual(set(history), {"previous_state", "new_state", "reason", "timestamp", "operation_id"})
        self.assertTrue(history["timestamp"].endswith("Z"))
        self.assertEqual(history["operation_id"], "op-state")

    def test_result_details_are_simulation_marked_and_redacted(self):
        request = make_request(self.profile)
        session, _, _ = self.open(request)
        result = self.kernel.execute(session, request)
        serialized = json.dumps(dict(result.details))
        self.assertTrue(result.details["simulation"])
        self.assertEqual(result.details["kernel_mode"], "LAB / SYNTHETIC")
        self.assertNotIn("raw_apdu", result.details)
        self.assertNotIn("raw_response", result.details)
        self.assertNotIn(laboratory_commands(AID_ONE)["SELECT"], serialized)
        evidence = result.to_evidence()
        self.assertEqual(evidence["mode"], "SIMULATED")
        self.assertEqual(evidence["physical_validation"], "NOT_EXECUTED")


class ContractParityTests(unittest.TestCase):
    def kernels(self):
        return ("internal", InternalEmvKernel()), ("mock", MockKernelAdapter())

    def open(self, kernel, kind, request, profile):
        if kind == "internal":
            card_interface, _ = make_interface(
                profile,
                interface=request.interface,
                action=scenario_action(request.scenario),
            )
            return kernel.open_session(request, card_interface, profile)
        return kernel.open_session(request, profile=profile)

    def test_functional_and_failure_scenario_parity(self):
        expected = {
            "approved": (ExecutionState.SUCCEEDED, "APPROVED", RecoveryClass.NON_RETRYABLE),
            "declined": (ExecutionState.FAILED, "DECLINED", RecoveryClass.NON_RETRYABLE),
            "online_required": (ExecutionState.SUCCEEDED, "ONLINE_REQUIRED", RecoveryClass.NON_RETRYABLE),
            "action_required": (ExecutionState.SUCCEEDED, "ACTION_REQUIRED", RecoveryClass.OPERATOR_REVIEW),
            "failure_before_send": (ExecutionState.FAILED, None, RecoveryClass.SAFE_TO_RETRY),
            "timeout": (ExecutionState.UNCERTAIN, None, RecoveryClass.QUERY_REQUIRED),
            "uncertain": (ExecutionState.UNCERTAIN, None, RecoveryClass.QUERY_REQUIRED),
        }
        for scenario, expected_values in expected.items():
            for kind, kernel in self.kernels():
                with self.subTest(scenario=scenario, kernel=kind):
                    profile = make_profile(kernel_ref=kernel.kernel_ref)
                    request = make_request(profile, kernel_ref=kernel.kernel_ref, operation_id=f"op-{kind}-{scenario}", scenario=scenario)
                    session = self.open(kernel, kind, request, profile)
                    result = kernel.execute(session, request)
                    self.assertEqual(
                        (result.execution_state, result.outcome, result.recovery),
                        expected_values,
                    )
                    self.assertEqual(kernel.query(request.operation_id), result)
                    self.assertEqual(kernel.execute(session, request), result)
                    self.assertEqual(kernel.execution_count(request.operation_id), 1)
                    self.assertTrue(kernel.close(session).closed)
                    self.assertTrue(kernel.close(session).closed)

    def test_online_completion_parity(self):
        for kind, kernel in self.kernels():
            with self.subTest(kernel=kind):
                self.assertTrue(kernel.capabilities().online_processing)
                profile = make_profile(kernel_ref=kernel.kernel_ref)
                request = make_request(profile, kernel_ref=kernel.kernel_ref, operation_id=f"op-parity-online-{kind}", scenario="online_required")
                session = self.open(kernel, kind, request, profile)
                pending = kernel.execute(session, request)
                self.assertEqual(pending.outcome, "ONLINE_REQUIRED")
                result = kernel.complete_online(session, request, {"decision": "APPROVED", "simulation": True})
                self.assertEqual(result.execution_state, ExecutionState.SUCCEEDED)
                self.assertEqual(result.outcome, "APPROVED")
                self.assertEqual(result.details["host_response"], "SIMULATED")

    def test_before_send_cancel_and_not_found_parity(self):
        for kind, kernel in self.kernels():
            with self.subTest(kernel=kind):
                profile = make_profile(kernel_ref=kernel.kernel_ref)
                request = make_request(profile, kernel_ref=kernel.kernel_ref, operation_id=f"op-deadline-{kind}")
                session = self.open(kernel, kind, request, profile)
                result = kernel.execute(session, request, deadline=time.monotonic() - 1)
                self.assertEqual(result.execution_state, ExecutionState.FAILED)
                self.assertEqual(result.recovery, RecoveryClass.SAFE_TO_RETRY)
                self.assertFalse(result.details["command_sent"])

                cancel_request = make_request(profile, kernel_ref=kernel.kernel_ref, operation_id=f"op-cancel-parity-{kind}")
                cancel_session = self.open(kernel, kind, cancel_request, profile)
                cancelled = kernel.cancel(cancel_session)
                self.assertEqual(cancelled.execution_state, ExecutionState.CANCELLED)
                self.assertEqual(kernel.cancel(cancel_session), cancelled)
                self.assertEqual(kernel.execution_count(cancel_request.operation_id), 0)
                with self.assertRaises(KernelError) as caught:
                    kernel.query(f"unknown-{kind}")
                self.assertEqual(caught.exception.code, "OPERATION_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
