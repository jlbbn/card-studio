from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.emv import (
    CommandApdu,
    DataRecord,
    DataSource,
    DirectoryEntry,
    DolDataType,
    EmvDataStore,
    OwnershipDomain,
    ResponseApdu,
    TerminalAction,
    build_final_generate_ac_command,
    build_generate_ac_command,
    build_gpo_command,
    install_synthetic_decision_data,
    parse_gpo_response,
    run_decision_engine,
    select_environment_command,
    synthetic_data_foundation_responses,
    synthetic_generate_ac_response,
)
from card_studio.emv.contactless import (
    C8OutcomeKind,
    C8Policy,
    ContactlessError,
    c8_restart,
    c8_start,
    validate_ctq,
)
from card_studio.emv.online import OnlineFlowConfig
from card_studio.emv.runtime import EmvFlowConfig
from card_studio.host.mock import MockAuthorizationHost
from card_studio.kernels import InternalEmvKernel
from card_studio.money import Money
from card_studio.readers.base import TransportResult
from card_studio.readers.mock import MockReaderAdapter
from card_studio.tlv import TlvNode, encode_tlv
from card_studio.transactions import ExecutionState, RecoveryClass

from tests.test_agent_a_kernel import AID_ONE, make_profile, make_request


class RecordingReader(MockReaderAdapter):
    def __init__(self, responses, contact_protocol="T=0"):
        super().__init__(responses=responses, contact_protocol=contact_protocol)
        self.transmitted = []

    def transmit(self, connection, command, sensitivity="REDACTED"):
        raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
        self.transmitted.append(raw)
        return super().transmit(connection, command, sensitivity)


def online_parameters():
    return {
        "cdol2": [{"tag": "8A", "length": 2, "data_type": "BINARY"}],
        "final_gac_policy": "PROFILE",
        "issuer_authentication": "DISABLED",
        "scripts_enabled": True,
    }


def runtime_parameters(interface, action, require_ctq=False, limit=0):
    pdol = [
        {"tag": "9F02", "length": 6, "data_type": "NUMERIC"},
        {"tag": "5F2A", "length": 2, "data_type": "NUMERIC"},
    ]
    if interface == "CONTACTLESS":
        pdol.append({"tag": "9F66", "length": 4, "data_type": "BINARY"})
    online_capable = action != "AAC"
    return {
        "pdol": pdol,
        "cdol1": [
            {"tag": "9F02", "length": 6, "data_type": "NUMERIC"},
            {"tag": "95", "length": 5, "data_type": "BINARY"},
            {"tag": "9A", "length": 3, "data_type": "NUMERIC"},
            {"tag": "9C", "length": 1, "data_type": "BINARY"},
            {"tag": "9F34", "length": 3, "data_type": "BINARY"},
        ],
        "terminal_date": "260101",
        "supported_kernel_ids": ["02"] if interface == "CONTACTLESS" else [],
        "ttq": "26000000" if interface == "CONTACTLESS" else None,
        "c8": {
            "require_ctq": require_ctq,
            "transaction_limit_minor": limit,
            "ctq_allowed_mask": "FFFF",
        },
        "decision": {
            "terminal_online_capable": online_capable,
            "taa": {
                "no_match_default_when_online_capable": action if online_capable else "ARQC",
                "no_match_default_when_offline_only": action if action in {"TC", "AAC"} else "AAC",
            },
            "risk": {"floor_limit_minor": 0, "random_selection_percent": 0},
        },
    }


def case_fixture(
    interface,
    cid=0x40,
    gpo_cryptogram=False,
    require_ctq=False,
    include_ctq=True,
    limit=0,
    omit_card_tag=None,
    contact_protocol="T=0",
    amount=1000,
):
    action = {0x00: "AAC", 0x40: "TC", 0x80: "ARQC"}[cid]
    scenario = {0x00: "declined", 0x40: "approved", 0x80: "online_required"}[cid]
    parameters = runtime_parameters(interface, action, require_ctq, limit)
    if interface == "CONTACT":
        parameters.pop("ttq")
    profile = make_profile(
        interfaces=(interface,),
        scheme_parameters={
            "emv_execution": parameters,
            "online_completion": online_parameters(),
        },
    )
    request = make_request(
        profile,
        operation_id=f"op-block6-{interface.lower()}-{cid:02x}",
        scenario=scenario,
        interface=interface,
        application_aid=AID_ONE,
        amount=amount,
    )
    config = EmvFlowConfig.from_profile(profile)
    store = config.new_store(request)
    card = EmvDataStore()
    install_synthetic_decision_data(card)
    card_records = tuple(card.snapshot().values())
    response_records = tuple(item for item in card_records if item.tag != omit_card_tag)
    entry = DirectoryEntry(AID_ONE, 1, "LAB", "02" if interface == "CONTACTLESS" else None)
    responses = synthetic_data_foundation_responses(
        interface,
        (entry,),
        config.pdol,
        store,
        records={(1, 1): response_records},
    )
    if gpo_cryptogram:
        children = [
            TlvNode("82", b"\x00\x00"),
            TlvNode("94", bytes.fromhex("08010100")),
            TlvNode("9F27", bytes((cid,))),
            TlvNode("9F36", b"\x00\x01"),
            TlvNode("9F26", b"GPOAC123"),
        ]
        if include_ctq:
            children.append(TlvNode("9F6C", b"\x00\x00"))
        responses[build_gpo_command(config.pdol, store).to_bytes().hex().upper()] = TransportResult(
            encode_tlv(TlvNode("77", children=tuple(children))),
            "9000",
        )
    store.merge(card_records)
    decision = run_decision_engine(store, config.decision_profile, config.decision_input(request, profile))
    store.merge((
        DataRecord("95", decision.risk.tvr.to_bytes(), DataSource.KERNEL, OwnershipDomain.KERNEL),
        DataRecord("9F34", decision.cvm.cvm_results, DataSource.KERNEL, OwnershipDomain.KERNEL),
    ))
    first = build_generate_ac_command(decision.proposed_action, config.cdol1, store)
    if not gpo_cryptogram:
        responses[first.to_bytes().hex().upper()] = synthetic_generate_ac_response(cid, 77, "block6-first")
    online = OnlineFlowConfig.from_profile(profile)
    store.put(DataRecord("8A", b"00", DataSource.HOST, OwnershipDomain.HOST))
    final = build_final_generate_ac_command(TerminalAction.TC, online.cdol2, store)
    responses[final.to_bytes().hex().upper()] = synthetic_generate_ac_response(0x40, 80, "block6-final")
    reader = RecordingReader(responses, contact_protocol=contact_protocol)
    return {
        "profile": profile,
        "request": request,
        "reader": reader,
        "responses": responses,
        "config": config,
        "first": first,
        "final": final,
    }


def application(root, fixture):
    kernel = InternalEmvKernel()
    host = MockAuthorizationHost(root / "host.sqlite3", evidence_dir=root / "evidence" / "host")
    return CardStudioApplication(
        data_dir=root,
        config_dir=root / "config",
        profiles=[fixture["profile"]],
        reader=fixture["reader"],
        kernels=kernel,
        host=host,
    )


class ContactlessPrimitiveTests(unittest.TestCase):
    def test_gpo_cryptogram_and_ctq_are_typed_and_redacted(self):
        response = ResponseApdu(encode_tlv(TlvNode("77", children=(
            TlvNode("82", b"\x00\x00"),
            TlvNode("94", bytes.fromhex("08010100")),
            TlvNode("9F27", b"\x80"),
            TlvNode("9F36", b"\x00\x01"),
            TlvNode("9F26", b"GPOAC123"),
            TlvNode("9F6C", b"\x00\x00"),
        ))), 0x90, 0)
        result = parse_gpo_response(response)
        self.assertEqual(result.cryptogram_result.cryptogram_type.value, "ARQC")
        self.assertEqual(result.ctq, b"\x00\x00")
        evidence = json.dumps(result.to_evidence())
        self.assertNotIn("GPOAC123", evidence)
        self.assertNotIn(b"GPOAC123".hex(), evidence)

    def test_c8_start_restart_limits_and_ctq_policy(self):
        ready = c8_start(1000, C8Policy(transaction_limit_minor=1000))
        self.assertEqual(ready.kind, C8OutcomeKind.READY)
        self.assertTrue(ready.restart_allowed)
        limited = c8_start(1001, C8Policy(transaction_limit_minor=1000))
        self.assertEqual(limited.kind, C8OutcomeKind.END_APPLICATION)
        self.assertFalse(limited.restart_allowed)
        self.assertEqual(c8_restart(command_sent=False, uncertain=False).kind, C8OutcomeKind.TRY_AGAIN)
        self.assertEqual(c8_restart(command_sent=True, uncertain=True).kind, C8OutcomeKind.END_APPLICATION)
        self.assertFalse(c8_restart(command_sent=True, uncertain=True).restart_allowed)
        validate_ctq(b"\x00\x00", C8Policy(require_ctq=True))
        with self.assertRaises(ContactlessError) as caught:
            validate_ctq(None, C8Policy(require_ctq=True))
        self.assertEqual(caught.exception.code, "MISSING_CTQ")
        with self.assertRaises(ContactlessError) as caught:
            validate_ctq(b"\x00\x01", C8Policy(ctq_allowed_mask=b"\xff\xfe"))
        self.assertEqual(caught.exception.code, "CTQ_RFU_SET")


class EmvRuntimeIntegrationTests(unittest.TestCase):
    def test_contact_offline_uses_pse_records_oda_gac_and_t0_t1(self):
        for protocol in ("T=0", "T=1"):
            with self.subTest(protocol=protocol):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = case_fixture("CONTACT", contact_protocol=protocol)
                    app = application(Path(temporary), fixture)
                    result = app.create_transaction(fixture["request"])
                    self.assertEqual(result["execution_state"], "SUCCEEDED")
                    self.assertEqual(result["functional_result"], "APPROVED")
                    transmitted = fixture["reader"].transmitted
                    self.assertEqual([command[1] for command in transmitted], [0xA4, 0xA4, 0xA8, 0xB2, 0xAE])
                    self.assertEqual(transmitted[0], select_environment_command("CONTACT").to_bytes())
                    record = app.service.get(fixture["request"].operation_id)
                    states = [item["new_state"] for item in record.result.details["state_transitions"]]
                    self.assertIn("OFFLINE_DATA_AUTHENTICATION", states)
                    self.assertEqual(states[-2:], ["COMPLETION", "APPROVED"])
                    self.assertEqual(record.result.details["action_result"]["interface_protocol"], protocol)
                    self.assertEqual(record.result.details["action_result"]["cryptogram_source"], "GENERATE_AC")

    def test_contact_online_completes_with_second_gac_and_arc(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACT", cid=0x80)
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "SUCCEEDED")
            self.assertEqual(result["functional_result"], "APPROVED")
            self.assertEqual(result["details"]["action_result"]["final_decision_source"], "KERNEL_ARC")
            self.assertEqual([command[1] for command in fixture["reader"].transmitted], [0xA4, 0xA4, 0xA8, 0xB2, 0xAE, 0xAE])
            self.assertEqual(fixture["reader"].transmitted[-1], fixture["final"].to_bytes())
            record = app.service.get(fixture["request"].operation_id)
            states = [item["new_state"] for item in record.result.details["state_transitions"]]
            self.assertIn("FINAL_CARD_ACTION_ANALYSIS", states)
            self.assertEqual(states[-2:], ["COMPLETION", "APPROVED"])

    def test_contact_unknown_protocol_fails_before_apdu(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACT", contact_protocol="UNKNOWN")
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "FAILED")
            self.assertEqual(result["error_code"], "CONTACT_PROTOCOL_UNKNOWN")
            self.assertIsNone(result["functional_result"])
            self.assertEqual(fixture["reader"].transmitted, [])

    def test_contactless_gpo_arqc_uses_ppse_ttq_ctq_and_no_generate_ac(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACTLESS", cid=0x80, gpo_cryptogram=True, require_ctq=True)
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "SUCCEEDED")
            self.assertEqual(result["functional_result"], "APPROVED")
            transmitted = fixture["reader"].transmitted
            self.assertEqual([command[1] for command in transmitted], [0xA4, 0xA4, 0xA8, 0xB2])
            self.assertEqual(transmitted[0], select_environment_command("CONTACTLESS").to_bytes())
            self.assertFalse(any(command[1] == 0xAE for command in transmitted))
            self.assertEqual(result["details"]["action_result"]["cryptogram_source"], "GPO")
            self.assertEqual(result["details"]["action_result"]["c8"]["outcome"], "ONLINE_REQUIRED")
            self.assertEqual(result["details"]["action_result"]["final_decision_source"], "KERNEL_ARC")
            record = app.service.get(fixture["request"].operation_id)
            states = [item["new_state"] for item in record.result.details["state_transitions"]]
            self.assertIn("C8_OUTCOME", states)
            self.assertNotIn("FINAL_CARD_ACTION_ANALYSIS", states)

    def test_contactless_gpo_tc_finishes_offline_without_host_or_gac(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACTLESS", gpo_cryptogram=True)
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "SUCCEEDED")
            self.assertEqual(result["functional_result"], "APPROVED")
            self.assertFalse(any(command[1] == 0xAE for command in fixture["reader"].transmitted))
            self.assertEqual(result["details"]["action_result"]["c8"]["outcome"], "APPROVED")
            self.assertNotIn("host_reference", result["details"])

    def test_contactless_without_gpo_cryptogram_uses_only_first_gac(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACTLESS", cid=0x80)
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "SUCCEEDED")
            self.assertEqual([command[1] for command in fixture["reader"].transmitted].count(0xAE), 1)
            self.assertEqual(result["details"]["action_result"]["cryptogram_source"], "GENERATE_AC")

    def test_contactless_ctq_and_limit_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACTLESS", cid=0x80, gpo_cryptogram=True, require_ctq=True, include_ctq=False)
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "FAILED")
            self.assertEqual(result["error_code"], "MISSING_CTQ")
            self.assertIsNone(result["functional_result"])
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACTLESS", limit=999)
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "FAILED")
            self.assertEqual(result["error_code"], "CONTACTLESS_LIMIT_EXCEEDED")
            self.assertEqual(fixture["reader"].transmitted, [])

    def test_malformed_and_missing_mandatory_record_data_are_technical_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACT")
            record_key = next(key for key in fixture["responses"] if key.startswith("00B2"))
            fixture["reader"].responses[record_key] = TransportResult(b"\x70\x05\x4f\x01", "9000")
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "FAILED")
            self.assertIsNone(result["functional_result"])
            self.assertNotEqual(result["functional_result"], "DECLINED")
            self.assertTrue(result["error_code"])
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACT", omit_card_tag="8E")
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "FAILED")
            self.assertEqual(result["error_code"], "MISSING_MANDATORY_DATA")
            self.assertIsNone(result["functional_result"])

    def test_post_send_apdu_timeout_is_uncertain_and_replay_does_not_resend(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = case_fixture("CONTACT")
            fixture["reader"].fail_next_transmit = "TRANSMIT_TIMEOUT"
            app = application(Path(temporary), fixture)
            result = app.create_transaction(fixture["request"])
            self.assertEqual(result["execution_state"], "UNCERTAIN")
            self.assertEqual(result["recovery_action"], RecoveryClass.QUERY_REQUIRED.value)
            self.assertIsNone(result["functional_result"])
            transmitted = list(fixture["reader"].transmitted)
            replay = app.create_transaction(fixture["request"])
            self.assertEqual(replay["result_reference"], result["result_reference"])
            self.assertEqual(fixture["reader"].transmitted, transmitted)


if __name__ == "__main__":
    unittest.main()
