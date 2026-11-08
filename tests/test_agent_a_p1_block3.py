from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from card_studio.emv import (
    DataRecord,
    DataSource,
    DolDataType,
    DolDocument,
    DolEntry,
    DolKind,
    EmvDataStore,
    OwnershipDomain,
    synthetic_generate_ac_response,
)
from card_studio.emv.c8_recovery import (
    RecoverAcError,
    TornTransactionJournal,
    build_recover_ac_command,
    enforce_relay_time_limit,
    recover_ac,
)
from card_studio.emv.runtime import EmvFlowConfig
from card_studio.emv.transaction_semantics import TransactionSemanticsError
from card_studio.interfaces import ContactInterface, ContactlessInterface, InterfaceKind
from card_studio.kernels import InternalEmvKernel, KernelError
from card_studio.profiles import CardProfile
from card_studio.readers.base import ReaderError
from card_studio.transactions import ExecutionState, FunctionalResult, RecoveryClass, TransactionRequest, TransactionResult
from tests.test_agent_a_kernel import make_request
from tests.test_agent_a_p0_block1 import make_interface
from tests.test_agent_a_p0_block5 import host_response, kernel_setup
from tests.test_agent_a_p0_block6 import RecordingReader, case_fixture
from tests.test_agent_a_p1_block2 import open_fixture


def recover_interface(responses):
    adapter = RecordingReader(responses)
    session = adapter.open(adapter.reader_info.reader_id, "recover-ac-reader")
    return ContactlessInterface(adapter, session).open(), adapter


class K28RelayAndTornRecoveryTests(unittest.TestCase):
    def test_recover_ac_uses_drdol_expected_atc_and_never_replays_final_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "torn.sqlite3"
            journal = TornTransactionJournal(path)
            journal.mark_torn("op-torn", "lab-profile:1:" + "a" * 64, b"\x00\x07")
            drdol = DolDocument(DolKind.DRDOL, (DolEntry("9F37", 4, DolDataType.BINARY),))
            store = EmvDataStore()
            store.put(DataRecord("9F37", b"\x01\x02\x03\x04", DataSource.TERMINAL, OwnershipDomain.TERMINAL))
            command = build_recover_ac_command(drdol, store)
            card_interface, adapter = recover_interface({
                command.to_bytes().hex().upper(): synthetic_generate_ac_response(0x40, 80, "recover", atc=b"\x00\x07")
            })
            result = recover_ac(card_interface, journal, "op-torn", drdol, store)
            self.assertEqual(result.cryptogram.functional_result, FunctionalResult.APPROVED)
            self.assertEqual(result.torn.status, "RECOVERED")
            self.assertFalse(result.to_evidence()["second_generate_ac"])
            self.assertEqual(adapter.transmitted, [command.to_bytes()])
            self.assertNotIn("AC123", json.dumps(result.to_evidence()))

            restarted = TornTransactionJournal(path)
            self.assertEqual(restarted.get("op-torn").status, "RECOVERED")
            with self.assertRaises(RecoverAcError) as caught:
                recover_ac(card_interface, restarted, "op-torn", drdol, store)
            self.assertEqual(caught.exception.code, "RECOVER_AC_ALREADY_COMPLETED")
            self.assertEqual(len(adapter.transmitted), 1)

    def test_atc_mismatch_and_arqc_recovery_do_not_finish_or_create_second_online_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = TornTransactionJournal(Path(temporary) / "torn.sqlite3")
            drdol = DolDocument(DolKind.DRDOL, ())
            store = EmvDataStore()
            for operation, cid in (("op-atc-mismatch", 0x40), ("op-recover-arqc", 0x80)):
                with self.subTest(operation=operation):
                    journal.mark_torn(operation, "profile", b"\x00\x07")
                    command = build_recover_ac_command(drdol, store)
                    card_interface, adapter = recover_interface({
                        command.to_bytes().hex().upper(): synthetic_generate_ac_response(cid, 80, operation, atc=b"\x00\x08" if operation == "op-atc-mismatch" else b"\x00\x07")
                    })
                    with self.assertRaises(RecoverAcError):
                        recover_ac(card_interface, journal, operation, drdol, store)
                    self.assertEqual(journal.get(operation).status, "TORN")
                    self.assertEqual(len(adapter.transmitted), 1)

    def test_relay_limit_is_measured_at_real_contactless_gpo_without_retry(self):
        fixture = case_fixture("CONTACTLESS", gpo_cryptogram=True)
        raw = fixture["profile"].to_dict()
        parameters = dict(raw["scheme_parameters"])
        execution = dict(parameters["emv_execution"])
        c8 = dict(execution["c8"])
        c8["relay_time_limit_ms"] = 1
        execution["c8"] = c8
        parameters["emv_execution"] = execution
        raw["scheme_parameters"] = parameters
        raw["profile_digest"] = ""
        fixture["profile"] = CardProfile.from_dict(raw)
        fixture["request"] = make_request(
            fixture["profile"], operation_id="op-relay-limit", interface="CONTACTLESS", application_aid=fixture["request"].application_aid
        )
        original = fixture["reader"]

        class SlowGpoReader(RecordingReader):
            def transmit(self, connection, command, sensitivity="REDACTED"):
                raw_command = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
                if raw_command[1] == 0xA8:
                    time.sleep(0.005)
                return super().transmit(connection, command, sensitivity)

        fixture["reader"] = SlowGpoReader(original.responses)
        kernel = InternalEmvKernel()
        adapter = fixture["reader"]
        reader_session = adapter.open(adapter.reader_info.reader_id, "relay-reader")
        card_interface = ContactlessInterface(adapter, reader_session).open()
        session = kernel.open_session(fixture["request"], card_interface, fixture["profile"])
        result = kernel.execute(session, fixture["request"])
        self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(result.error_code, "RELAY_TIME_LIMIT_EXCEEDED")
        transmitted = list(adapter.transmitted)
        self.assertEqual(len(transmitted), 3)
        self.assertEqual(kernel.execute(session, fixture["request"]), result)
        self.assertEqual(adapter.transmitted, transmitted)

        now = [1.0]
        enforce_relay_time_limit(0.995, 10, lambda: now[0])
        with self.assertRaises(ValueError) as caught:
            enforce_relay_time_limit(0.99, 10, lambda: now[0])
        self.assertEqual(caught.exception.code, "RELAY_TIME_LIMIT_EXCEEDED")


class K29K33K34K35RecoveryTests(unittest.TestCase):
    def test_late_final_read_back_reconciles_and_restart_never_resends(self):
        fixture = case_fixture("CONTACT")
        fixture["reader"].fail_next_transmit = "TRANSMIT_TIMEOUT"
        external = {}
        kernel = InternalEmvKernel(result_loader=external.get)
        session = open_fixture(fixture, kernel, "late-result-reader")
        uncertain = kernel.execute(session, fixture["request"])
        self.assertEqual(uncertain.execution_state, ExecutionState.UNCERTAIN)
        transmitted = list(fixture["reader"].transmitted)
        final = TransactionResult(
            fixture["request"],
            ExecutionState.SUCCEEDED,
            "APPROVED",
            RecoveryClass.NON_RETRYABLE,
            details={"simulation": True, "protocol": "LAB_ONLY", "source": "EXTERNAL_READ_BACK"},
        )
        external[fixture["request"].operation_id] = final.to_dict()
        self.assertEqual(kernel.query(fixture["request"].operation_id), final)
        self.assertEqual(fixture["reader"].transmitted, transmitted)

        restarted = InternalEmvKernel(result_loader=external.get)
        new_fixture = case_fixture("CONTACT")
        new_fixture["request"] = fixture["request"]
        new_session = open_fixture(new_fixture, restarted, "restarted-read-back-reader")
        self.assertEqual(restarted.execute(new_session, fixture["request"]), final)
        self.assertEqual(restarted.query(fixture["request"].operation_id), final)
        self.assertEqual(new_fixture["reader"].transmitted, [])
        self.assertTrue(restarted.close(new_session).closed)
        self.assertTrue(restarted.close(new_session).closed)
        self.assertEqual(restarted.cancel(new_session), final)

    def test_unknown_or_contradictory_read_back_cannot_fabricate_approval(self):
        fixture = case_fixture("CONTACT")
        fixture["reader"].fail_next_transmit = "TRANSMIT_TIMEOUT"
        changed_request = TransactionRequest.create(
            money=fixture["request"].money,
            profile_ref=fixture["request"].profile_ref,
            interface="CONTACT",
            kernel_ref=fixture["request"].kernel_ref,
            operation_id=fixture["request"].operation_id,
            idempotency_key=fixture["request"].idempotency_key,
            correlation_id="changed-correlation",
            application_aid=fixture["request"].application_aid,
        )
        contradictory = TransactionResult(changed_request, ExecutionState.SUCCEEDED, "APPROVED", RecoveryClass.NON_RETRYABLE)
        external = {}
        kernel = InternalEmvKernel(result_loader=external.get)
        session = open_fixture(fixture, kernel, "bad-read-back-reader")
        uncertain = kernel.execute(session, fixture["request"])
        external[fixture["request"].operation_id] = contradictory
        transmitted = list(fixture["reader"].transmitted)
        with self.assertRaises(KernelError) as caught:
            kernel.query(fixture["request"].operation_id)
        self.assertEqual(caught.exception.code, "OPERATION_QUERY_CONFLICT")
        self.assertEqual(fixture["reader"].transmitted, transmitted)
        without_loader = InternalEmvKernel()
        other = case_fixture("CONTACT")
        other["reader"].fail_next_transmit = "TRANSMIT_TIMEOUT"
        other_session = open_fixture(other, without_loader, "unknown-read-back-reader")
        known = without_loader.execute(other_session, other["request"])
        self.assertEqual(without_loader.query(other["request"].operation_id), known)
        self.assertEqual(uncertain.execution_state, ExecutionState.UNCERTAIN)

    def test_online_auth_scripts_and_final_gac_timeout_are_uncertain_without_replay(self):
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at):
                kernel, session, request, adapter = kernel_setup(operation_id=f"op-online-stage-{fail_at}")
                pending = kernel.execute(session, request)
                self.assertEqual(pending.outcome, "ONLINE_REQUIRED")
                original = adapter.transmit
                calls = [0]

                def fail_selected(connection, command, sensitivity="REDACTED"):
                    calls[0] += 1
                    if calls[0] == fail_at:
                        raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
                        adapter.transmitted.append(raw)
                        raise ReaderError("TRANSMIT_TIMEOUT", "LAB online stage timeout", outcome="UNCERTAIN")
                    return original(connection, command, sensitivity)

                adapter.transmit = fail_selected
                result = kernel.complete_online(session, request, host_response())
                self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
                self.assertEqual(result.recovery, RecoveryClass.QUERY_REQUIRED)
                transmitted = list(adapter.transmitted)
                self.assertEqual(kernel.complete_online(session, request, host_response()), result)
                self.assertEqual(adapter.transmitted, transmitted)
                self.assertFalse(result.details["session_recovery"]["apdu_session_restorable"])


SEMANTICS = {
    "schema_version": 1,
    "purchase": {"card_kernel_mode": "REQUIRED", "transaction_code": "00", "original_link": "NONE"},
    "refund": {"card_kernel_mode": "REQUIRED", "transaction_code": "20", "original_link": "REQUEST_ORIGINAL"},
    "reversal": {"card_kernel_mode": "HOST_ONLY", "transaction_code": None, "original_link": "HOST_ORIGINAL"},
}


def semantic_fixture():
    fixture = case_fixture("CONTACT")
    raw = fixture["profile"].to_dict()
    parameters = dict(raw["scheme_parameters"])
    parameters["kernel_transaction_semantics"] = SEMANTICS
    raw["scheme_parameters"] = parameters
    raw["profile_digest"] = ""
    fixture["profile"] = CardProfile.from_dict(raw)
    return fixture


class K39K40TransactionSemanticsTests(unittest.TestCase):
    def test_refund_uses_9c20_and_original_link_in_real_generate_ac_flow(self):
        fixture = semantic_fixture()
        request = TransactionRequest.create(
            money=fixture["request"].money,
            profile_ref=fixture["profile"].profile_id + ":" + str(fixture["profile"].profile_version) + ":" + fixture["profile"].profile_digest,
            interface="CONTACT",
            kernel_ref=fixture["request"].kernel_ref,
            transaction_type="refund",
            original_operation_id="op-original-purchase",
            original_amount=fixture["request"].money,
            operation_id="op-refund-p1",
            application_aid=fixture["request"].application_aid,
        )
        fixture["request"] = request
        config = EmvFlowConfig.from_profile(fixture["profile"])
        self.assertEqual(config.new_store(request).require("9C").value, b"\x20")
        original = fixture["reader"]

        class RefundReader(RecordingReader):
            def transmit(self, connection, command, sensitivity="REDACTED"):
                raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
                if raw[1] == 0xAE:
                    self.transmitted.append(raw)
                    return synthetic_generate_ac_response(0x40, 77, "refund-p1")
                return super().transmit(connection, command, sensitivity)

        fixture["reader"] = RefundReader(original.responses)
        kernel = InternalEmvKernel()
        session = open_fixture(fixture, kernel, "refund-reader")
        result = kernel.execute(session, request)
        self.assertEqual(result.outcome, "APPROVED")
        self.assertEqual(result.details["transaction_semantics"]["transaction_code"], "20")
        self.assertEqual(result.details["transaction_semantics"]["original_link"], "REQUEST_ORIGINAL")
        self.assertFalse(result.details["transaction_semantics"]["second_approval"])
        self.assertTrue(any(command[1] == 0xAE for command in fixture["reader"].transmitted))

    def test_reversal_is_host_only_and_sends_no_card_apdu(self):
        fixture = semantic_fixture()
        request = TransactionRequest.create(
            money=fixture["request"].money,
            profile_ref=fixture["profile"].profile_id + ":" + str(fixture["profile"].profile_version) + ":" + fixture["profile"].profile_digest,
            interface="CONTACT",
            kernel_ref=fixture["request"].kernel_ref,
            transaction_type="reversal",
            original_operation_id="op-original-purchase",
            original_amount=fixture["request"].money,
            operation_id="op-reversal-p1",
            application_aid=fixture["request"].application_aid,
        )
        kernel = InternalEmvKernel()
        adapter = fixture["reader"]
        reader_session = adapter.open(adapter.reader_info.reader_id, "reversal-reader")
        card_interface = ContactInterface(adapter, reader_session).open()
        with self.assertRaises(KernelError) as caught:
            kernel.open_session(request, card_interface, fixture["profile"])
        self.assertEqual(caught.exception.code, "REVERSAL_CARD_KERNEL_NOT_REQUIRED")
        self.assertEqual(adapter.transmitted, [])
        with self.assertRaises(KernelError):
            kernel.query(request.operation_id)

    def test_unreviewed_refund_mapping_is_rejected(self):
        fixture = semantic_fixture()
        raw = fixture["profile"].to_dict()
        parameters = dict(raw["scheme_parameters"])
        semantics = {key: dict(value) if isinstance(value, dict) else value for key, value in SEMANTICS.items()}
        semantics["refund"] = dict(semantics["refund"])
        semantics["refund"]["transaction_code"] = "00"
        parameters["kernel_transaction_semantics"] = semantics
        raw["scheme_parameters"] = parameters
        raw["profile_digest"] = ""
        profile = CardProfile.from_dict(raw)
        with self.assertRaises(TransactionSemanticsError):
            EmvFlowConfig.from_profile(profile)


if __name__ == "__main__":
    unittest.main()
