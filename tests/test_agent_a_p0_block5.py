from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.emv import (
    ApduError,
    DataRecord,
    DataSource,
    DolDataType,
    DolDocument,
    DolEntry,
    DolKind,
    EmvDataStore,
    OwnershipDomain,
    TerminalAction,
    Tsi,
    TsiFlag,
    Tvr,
    TvrFlag,
    build_final_generate_ac_command,
    synthetic_generate_ac_response,
)
from card_studio.emv.issuer import (
    IssuerError,
    IssuerScript,
    ScriptPhase,
    execute_external_authenticate,
    external_authenticate_command,
    parse_host_envelope,
    synthetic_script_apdu,
)
from card_studio.emv.online import (
    FinalGacPolicy,
    IssuerAuthenticationPolicy,
    OnlineFlowConfig,
    UnablePolicy,
    process_online_completion,
    synthetic_host_response,
)
from card_studio.emv.protocol import laboratory_responses
from card_studio.host.mock import MockAuthorizationHost
from card_studio.interfaces import ContactInterface
from card_studio.kernels import InternalEmvKernel, KernelError
from card_studio.money import Money
from card_studio.readers.base import TransportResult
from card_studio.readers.mock import MockReaderAdapter
from card_studio.tlv import TlvNode, encode_tlv
from card_studio.transactions import ExecutionState, FunctionalResult, RecoveryClass

from tests.test_agent_a_kernel import AID_ONE, make_profile, make_request


ISSUER = bytes.fromhex("A1A2A3A4A5A6A7A8")
BEFORE = synthetic_script_apdu(0xDA, b"\x5A")
AFTER = synthetic_script_apdu(0xDB, b"\x6B")
BEFORE_SCRIPT = IssuerScript(ScriptPhase.BEFORE_FINAL_GAC, b"\x01\x02\x03\x04", (BEFORE,))
AFTER_SCRIPT = IssuerScript(ScriptPhase.AFTER_FINAL_GAC, b"\x05\x06\x07\x08", (AFTER,))


class RecordingReader(MockReaderAdapter):
    def __init__(self, responses):
        super().__init__(responses=responses)
        self.transmitted = []

    def transmit(self, connection, command, sensitivity="REDACTED"):
        raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
        self.transmitted.append(raw)
        return super().transmit(connection, command, sensitivity)


def cdol2():
    return DolDocument(DolKind.CDOL2, (
        DolEntry("8A", 2, DolDataType.BINARY),
    ))


def flow_config(**overrides):
    values = {
        "cdol2": cdol2(),
        "final_gac_policy": FinalGacPolicy.REQUIRED,
        "issuer_authentication": IssuerAuthenticationPolicy.REQUIRED,
    }
    values.update(overrides)
    return OnlineFlowConfig(**values)


def online_profile(**overrides):
    config = flow_config(**overrides)
    return make_profile(
        interfaces=("CONTACT",),
        scheme_parameters={
            "online_completion": {
                "cdol2": [{"tag": "8A", "length": 2, "data_type": "BINARY"}],
                "final_gac_policy": config.final_gac_policy.value,
                "unable_policy": config.unable_policy.value,
                "issuer_authentication": config.issuer_authentication.value,
                "scripts_enabled": config.scripts_enabled,
            },
        },
    )


def host_response(arc="00", **overrides):
    values = {
        "arc": arc,
        "issuer_authentication_data": ISSUER,
        "scripts_before": (BEFORE_SCRIPT,),
        "scripts_after": (AFTER_SCRIPT,),
    }
    values.update(overrides)
    return synthetic_host_response(**values)


def final_command(arc="00"):
    store = EmvDataStore()
    store.put(DataRecord("8A", arc.encode("ascii"), DataSource.HOST, OwnershipDomain.HOST))
    action = TerminalAction.TC if arc in {"00", "Y3"} else TerminalAction.AAC
    return build_final_generate_ac_command(action, cdol2(), store)


def online_responses(arc="00", final_cid=None):
    if final_cid is None:
        final_cid = 0x40 if arc in {"00", "Y3"} else 0x00
    return {
        external_authenticate_command(ISSUER).to_bytes().hex().upper(): TransportResult(b"", "9000"),
        BEFORE.to_bytes().hex().upper(): TransportResult(b"", "9000"),
        final_command(arc).to_bytes().hex().upper(): synthetic_generate_ac_response(final_cid, 80, "block5-final"),
        AFTER.to_bytes().hex().upper(): TransportResult(b"", "9000"),
    }


def contact(responses):
    adapter = RecordingReader(responses)
    session = adapter.open(adapter.reader_info.reader_id, "op-block5-reader")
    return ContactInterface(adapter, session).open(), adapter


def kernel_setup(profile=None, operation_id="op-block5-internal"):
    selected = profile or online_profile()
    request = make_request(
        selected,
        operation_id=operation_id,
        scenario="online_required",
        interface="CONTACT",
        application_aid=AID_ONE,
    )
    responses = laboratory_responses(selected, action="ONLINE_REQUIRED", application_aid=AID_ONE)
    responses.update(online_responses())
    card_interface, adapter = contact(responses)
    kernel = InternalEmvKernel()
    session = kernel.open_session(request, card_interface, selected)
    return kernel, session, request, adapter


class IssuerPrimitiveTests(unittest.TestCase):
    def test_host_envelope_parses_arc_authentication_and_script_phases_with_redaction(self):
        envelope = parse_host_envelope(host_response())
        self.assertEqual(envelope.arc, "00")
        self.assertEqual(envelope.issuer_authentication_data, ISSUER)
        self.assertEqual(envelope.scripts_before, (BEFORE_SCRIPT,))
        self.assertEqual(envelope.scripts_after, (AFTER_SCRIPT,))
        evidence = json.dumps(envelope.to_evidence())
        self.assertNotIn(ISSUER.hex().upper(), evidence)
        self.assertNotIn(BEFORE.to_bytes().hex().upper(), evidence)
        self.assertNotIn(AFTER.to_bytes().hex().upper(), evidence)

    def test_host_envelope_rejects_malformed_duplicate_unknown_and_contradictory_data(self):
        malformed = host_response()
        malformed["response_template"] = "8A0300"
        duplicate = host_response()
        duplicate["response_template"] = "8A0200308A020030"
        unknown = host_response()
        unknown["response_template"] = encode_tlv((
            TlvNode("8A", b"00"),
            TlvNode("5A", b"unknown"),
        )).hex().upper()
        contradictory = host_response()
        contradictory["functional_result"] = "DECLINED"
        cases = (
            (malformed, "MALFORMED_RESPONSE_TEMPLATE"),
            (duplicate, "DUPLICATE_RESPONSE_OBJECT"),
            (unknown, "UNSUPPORTED_RESPONSE_OBJECT"),
            (contradictory, "HOST_TRANSPORT_MISMATCH"),
        )
        for response, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(IssuerError) as caught:
                    parse_host_envelope(response)
                self.assertEqual(caught.exception.code, code)

    def test_external_authenticate_success_failure_and_invalid_response(self):
        command = external_authenticate_command(ISSUER)
        for status, success in (("9000", True), ("6985", False)):
            with self.subTest(status=status):
                card_interface, _ = contact({command.to_bytes().hex().upper(): TransportResult(b"", status)})
                result = execute_external_authenticate(card_interface, ISSUER, Tvr(), Tsi())
                self.assertEqual(result.success, success)
                self.assertTrue(result.tsi.is_set(TsiFlag.ISSUER_AUTHENTICATION_PERFORMED))
                self.assertEqual(result.tvr.is_set(TvrFlag.ISSUER_AUTHENTICATION_FAILED), not success)
        card_interface, _ = contact({command.to_bytes().hex().upper(): TransportResult(b"unexpected", "9000")})
        with self.assertRaises(IssuerError) as caught:
            execute_external_authenticate(card_interface, ISSUER, Tvr(), Tsi())
        self.assertEqual(caught.exception.code, "INVALID_EXTERNAL_AUTHENTICATE_RESPONSE")

    def test_external_authenticate_post_send_timeout_is_uncertain(self):
        card_interface, adapter = contact({})
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        with self.assertRaises(ApduError) as caught:
            execute_external_authenticate(card_interface, ISSUER, Tvr(), Tsi())
        self.assertEqual(caught.exception.outcome, "UNCERTAIN")
        self.assertEqual(caught.exception.phase, "POST_SEND")


class OnlineCompletionTests(unittest.TestCase):
    def test_full_order_cdol2_arc_decision_statuses_and_redaction(self):
        card_interface, adapter = contact(online_responses())
        result = process_online_completion(card_interface, host_response(), flow_config(), EmvDataStore())
        self.assertEqual(result.execution_state, ExecutionState.SUCCEEDED)
        self.assertEqual(result.functional_result, FunctionalResult.APPROVED)
        self.assertEqual(adapter.transmitted, [
            external_authenticate_command(ISSUER).to_bytes(),
            BEFORE.to_bytes(),
            final_command().to_bytes(),
            AFTER.to_bytes(),
        ])
        self.assertEqual(adapter.transmitted[2][4 + 1:-1], b"00")
        self.assertTrue(result.tsi.is_set(TsiFlag.ISSUER_AUTHENTICATION_PERFORMED))
        self.assertTrue(result.tsi.is_set(TsiFlag.SCRIPT_PROCESSING_PERFORMED))
        self.assertEqual(result.history[2]["statuses"][0]["sw1sw2"] if "sw1sw2" in result.history[2]["statuses"][0] else "9000", "9000")
        evidence = json.dumps(result.to_evidence())
        self.assertNotIn(ISSUER.hex().upper(), evidence)
        self.assertNotIn(BEFORE.to_bytes().hex().upper(), evidence)
        self.assertNotIn(AFTER.to_bytes().hex().upper(), evidence)
        self.assertNotIn("application_cryptogram", evidence)

    def test_decline_arc_overrides_text_decision_and_requires_final_aac(self):
        response = host_response("05")
        response["decision"] = "APPROVED"
        card_interface, adapter = contact(online_responses("05"))
        result = process_online_completion(card_interface, response, flow_config(), EmvDataStore())
        self.assertEqual(result.execution_state, ExecutionState.FAILED)
        self.assertEqual(result.functional_result, FunctionalResult.DECLINED)
        self.assertEqual(result.cryptogram_result.cryptogram_type.value, "AAC")
        self.assertEqual(adapter.transmitted[2], final_command("05").to_bytes())

    def test_authentication_and_script_failures_stop_at_their_phase(self):
        cases = (
            ("auth", "ISSUER_AUTHENTICATION_FAILED", TvrFlag.ISSUER_AUTHENTICATION_FAILED, 1),
            ("before", "ISSUER_SCRIPT_FAILED_BEFORE_FINAL_GAC", TvrFlag.SCRIPT_PROCESSING_FAILED_BEFORE_FINAL_GAC, 2),
            ("after", "ISSUER_SCRIPT_FAILED_AFTER_FINAL_GAC", TvrFlag.SCRIPT_PROCESSING_FAILED_AFTER_FINAL_GAC, 4),
        )
        for failed_stage, code, flag, command_count in cases:
            with self.subTest(stage=failed_stage):
                responses = online_responses()
                command = {
                    "auth": external_authenticate_command(ISSUER),
                    "before": BEFORE,
                    "after": AFTER,
                }[failed_stage]
                responses[command.to_bytes().hex().upper()] = TransportResult(b"", "6985")
                card_interface, adapter = contact(responses)
                result = process_online_completion(card_interface, host_response(), flow_config(), EmvDataStore())
                self.assertEqual(result.execution_state, ExecutionState.FAILED)
                self.assertIsNone(result.functional_result)
                self.assertEqual(result.error_code, code)
                self.assertTrue(result.tvr.is_set(flag))
                self.assertEqual(len(adapter.transmitted), command_count)

    def test_unable_uncertain_and_final_outcome_mismatch_never_approve(self):
        response = host_response(
            "Z3",
            execution_state=ExecutionState.UNCERTAIN,
            functional_result=None,
            issuer_authentication_data=None,
            scripts_before=(),
            scripts_after=(),
        )
        card_interface, adapter = contact({})
        result = process_online_completion(
            card_interface,
            response,
            flow_config(unable_policy=UnablePolicy.UNCERTAIN),
            EmvDataStore(),
        )
        self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(result.recovery, RecoveryClass.QUERY_REQUIRED)
        self.assertEqual(adapter.transmitted, [])

        card_interface, _ = contact(online_responses(final_cid=0x00))
        mismatch = process_online_completion(card_interface, host_response(), flow_config(), EmvDataStore())
        self.assertEqual(mismatch.execution_state, ExecutionState.FAILED)
        self.assertIsNone(mismatch.functional_result)
        self.assertEqual(mismatch.error_code, "FINAL_OUTCOME_MISMATCH")


class InternalOnlineIntegrationTests(unittest.TestCase):
    def test_internal_full_cycle_states_replay_conflict_and_redaction(self):
        kernel, session, request, adapter = kernel_setup()
        pending = kernel.execute(session, request)
        self.assertEqual(pending.outcome, "ONLINE_REQUIRED")
        response = host_response()
        result = kernel.complete_online(session, request, response)
        self.assertEqual(result.execution_state, ExecutionState.SUCCEEDED)
        self.assertEqual(result.outcome, "APPROVED")
        self.assertEqual(result.details["action_result"]["final_decision_source"], "KERNEL_ARC")
        self.assertEqual(result.details["authorization_response_code"], "00")
        states = [item["new_state"] for item in result.details["state_transitions"]]
        self.assertEqual(states[-7:], [
            "ONLINE_PROCESSING",
            "ISSUER_AUTHENTICATION",
            "ISSUER_SCRIPTS_BEFORE_FINAL_GAC",
            "FINAL_CARD_ACTION_ANALYSIS",
            "ISSUER_SCRIPTS_AFTER_FINAL_GAC",
            "COMPLETION",
            "APPROVED",
        ])
        transmitted = list(adapter.transmitted)
        self.assertEqual(kernel.complete_online(session, request, response), result)
        self.assertEqual(adapter.transmitted, transmitted)
        conflict = dict(response)
        conflict["host_reference"] = "hr-conflict"
        with self.assertRaises(KernelError) as caught:
            kernel.complete_online(session, request, conflict)
        self.assertEqual(caught.exception.code, "ONLINE_COMPLETION_CONFLICT")
        self.assertEqual(adapter.transmitted, transmitted)
        serialized = json.dumps(dict(result.details))
        self.assertNotIn(response["response_template"], serialized)
        self.assertNotIn(ISSUER.hex().upper(), serialized)
        self.assertNotIn(BEFORE.to_bytes().hex().upper(), serialized)

    def test_internal_post_send_timeout_is_uncertain_and_does_not_replay(self):
        kernel, session, request, adapter = kernel_setup(operation_id="op-block5-timeout")
        kernel.execute(session, request)
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        response = host_response()
        result = kernel.complete_online(session, request, response)
        self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(result.recovery, RecoveryClass.QUERY_REQUIRED)
        self.assertIsNone(result.outcome)
        transmitted = list(adapter.transmitted)
        self.assertEqual(kernel.complete_online(session, request, response), result)
        self.assertEqual(adapter.transmitted, transmitted)

    def test_facade_internal_host_online_completion_and_persistent_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = online_profile()
            responses = laboratory_responses(profile, action="ONLINE_REQUIRED", application_aid=AID_ONE)
            responses.update(online_responses())
            reader = RecordingReader(responses)
            kernel = InternalEmvKernel()
            host = MockAuthorizationHost(
                root / "host.sqlite3",
                evidence_dir=root / "evidence" / "host",
                issuer_authentication_data=ISSUER.hex(),
                issuer_script_commands={"71": [BEFORE.to_bytes().hex()], "72": [AFTER.to_bytes().hex()]},
                issuer_script_identifiers={"71": "01020304", "72": "05060708"},
            )
            app = CardStudioApplication(
                data_dir=root,
                config_dir=root / "config",
                profiles=[profile],
                reader=reader,
                kernels=kernel,
                host=host,
            )
            request = make_request(
                profile,
                operation_id="op-block5-facade",
                scenario="online_required",
                interface="CONTACT",
                application_aid=AID_ONE,
            )
            result = app.create_transaction(request)
            self.assertEqual(result["execution_state"], "SUCCEEDED")
            self.assertEqual(result["functional_result"], "APPROVED")
            self.assertEqual(result["details"]["action_result"]["final_decision_source"], "KERNEL_ARC")
            self.assertEqual(result["details"]["authorization_response_code"], "00")
            self.assertEqual(reader.transmitted[-4:], [
                external_authenticate_command(ISSUER).to_bytes(),
                BEFORE.to_bytes(),
                final_command().to_bytes(),
                AFTER.to_bytes(),
            ])
            record = app.service.get(request.operation_id)
            self.assertIn("online_completion", record.result.details)
            events = {entry["event_type"] for entry in app.audit(request.operation_id)}
            self.assertIn("HOST_AUTHORIZATION_COMPLETED", events)
            self.assertIn("ONLINE_COMPLETED", events)
            transmitted = list(reader.transmitted)
            replay = app.create_transaction(request)
            self.assertEqual(replay["result_reference"], result["result_reference"])
            self.assertEqual(reader.transmitted, transmitted)
            evidence = (root / "evidence" / "transactions" / result["result_reference"]).read_text()
            host_evidence = (root / "evidence" / "host" / f"{result['details']['host_reference']}.json").read_text()
            for content in (json.dumps(result), evidence, host_evidence):
                self.assertNotIn(ISSUER.hex(), content)
                self.assertNotIn(ISSUER.hex().upper(), content)
                self.assertNotIn(BEFORE.to_bytes().hex(), content)
                self.assertNotIn(BEFORE.to_bytes().hex().upper(), content)
            self.assertNotIn("response_template", json.loads(evidence))
            self.assertNotIn("response_template", json.loads(host_evidence))


if __name__ == "__main__":
    unittest.main()
