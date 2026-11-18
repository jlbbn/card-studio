from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from card_studio.emv import (
    AcEvidencePolicy,
    AipCapability,
    ApduError,
    ApplicationInterchangeProfile,
    CardActionError,
    CardActionInput,
    Cid,
    CryptogramResult,
    CryptogramType,
    DataRecord,
    DataSource,
    DolDataType,
    DolDocument,
    DolEntry,
    DolKind,
    EmvDataStore,
    GacError,
    OdaError,
    OdaMaterialStatus,
    OdaMethod,
    OdaProfile,
    OdaProviderResult,
    OdaStatus,
    OwnershipDomain,
    ResponseApdu,
    SyntheticOdaProvider,
    TerminalAction,
    TsiFlag,
    TvrFlag,
    build_final_generate_ac_command,
    build_generate_ac_command,
    evaluate_oda,
    generate_ac,
    install_synthetic_decision_data,
    parse_generate_ac_response,
    run_card_action_flow,
    run_data_foundation,
    run_decision_engine,
    select_oda_method,
    synthetic_decision_input,
    synthetic_decision_profile,
    synthetic_gac_responses,
    synthetic_generate_ac_response,
)
from card_studio.interfaces import ContactInterface, InterfaceKind
from card_studio.readers.base import TransportResult
from card_studio.readers.mock import MockReaderAdapter
from card_studio.tlv import TlvNode, encode_tlv
from card_studio.transactions.model import FunctionalResult as TransactionFunctionalResult

from tests.test_agent_a_p0_block3 import store_with_rules


AMOUNT = bytes.fromhex("000000001000")
CURRENCY = bytes.fromhex("0484")


def make_interface(responses):
    adapter = MockReaderAdapter(responses=responses)
    session = adapter.open(adapter.reader_info.reader_id, "p0-block4-reader")
    return ContactInterface(adapter, session).open(), adapter


def decision_result():
    decision = run_decision_engine(store_with_rules(), synthetic_decision_profile(), synthetic_decision_input())
    return replace(decision, store=cdol_store())


def cdol_store():
    store = store_with_rules()
    store.merge((
        DataRecord("9F02", AMOUNT, DataSource.TERMINAL, OwnershipDomain.TERMINAL),
        DataRecord("95", bytes(5), DataSource.TERMINAL, OwnershipDomain.TERMINAL),
        DataRecord("9A", b"\x26\x01\x01", DataSource.TERMINAL, OwnershipDomain.TERMINAL),
        DataRecord("9C", b"\x00", DataSource.TERMINAL, OwnershipDomain.TERMINAL),
    ))
    return store


def cdol1():
    return DolDocument(DolKind.CDOL1, (
        DolEntry("9F02", 6, DolDataType.NUMERIC),
        DolEntry("95", 5, DolDataType.BINARY),
        DolEntry("9A", 3, DolDataType.NUMERIC),
        DolEntry("9C", 1, DolDataType.BINARY),
    ))


def cdol2():
    return DolDocument(DolKind.CDOL2, (
        DolEntry("8A", 2, DolDataType.BINARY),
        DolEntry("95", 5, DolDataType.BINARY),
    ))


def aip(raw=b"\x00\x00"):
    return ApplicationInterchangeProfile.parse(raw)


class CountingProvider:
    def __init__(self, succeed=True):
        self.calls = 0
        self.succeed = succeed

    def authenticate(self, request):
        self.calls += 1
        return OdaProviderResult(self.succeed, "COUNTED", "00" * 32)


class GenerateAcTests(unittest.TestCase):
    def test_p1_and_cdol1_command_for_each_action(self):
        expected = {
            TerminalAction.AAC: 0x00,
            TerminalAction.TC: 0x40,
            TerminalAction.ARQC: 0x80,
        }
        for action, p1 in expected.items():
            with self.subTest(action=action):
                command = build_generate_ac_command(action, cdol1(), cdol_store())
                self.assertEqual(command.ins, 0xAE)
                self.assertEqual(command.p1, p1)
                self.assertEqual(command.data, AMOUNT + bytes(5) + b"\x26\x01\x01\x00")
        cda = build_generate_ac_command(TerminalAction.ARQC, cdol1(), cdol_store(), cda_requested=True)
        self.assertEqual(cda.p1, 0x90)
        with self.assertRaises(GacError):
            build_generate_ac_command(TerminalAction.AAC, cdol1(), cdol_store(), cda_requested=True)

    def test_final_cdol2_host_data_and_kind_ownership(self):
        store = cdol_store()
        store.put(DataRecord("8A", b"00", DataSource.HOST, OwnershipDomain.HOST))
        command = build_final_generate_ac_command(TerminalAction.TC, cdol2(), store)
        self.assertEqual(command.p1, 0x40)
        self.assertEqual(command.data, b"00" + bytes(5))
        with self.assertRaises(GacError) as caught:
            build_final_generate_ac_command(TerminalAction.TC, cdol1(), store)
        self.assertEqual(caught.exception.code, "INVALID_CDOL_KIND")
        foreign = EmvDataStore()
        foreign.put(DataRecord("9F02", AMOUNT, DataSource.HOST, OwnershipDomain.HOST))
        with self.assertRaises(GacError) as caught:
            build_generate_ac_command(TerminalAction.ARQC, cdol1(), foreign)
        self.assertEqual(caught.exception.code, "CDOL_OWNERSHIP_CONFLICT")

    def test_parse_format_77_with_optional_objects_and_redaction(self):
        response = ResponseApdu(encode_tlv(TlvNode("77", children=(
            TlvNode("9F27", b"\x80"),
            TlvNode("9F36", b"\x00\x01"),
            TlvNode("9F26", b"12345678"),
            TlvNode("9F10", b"IAD"),
            TlvNode("9F4C", b"IDN"),
            TlvNode("9F4B", b"SIGNED"),
        ))), 0x90, 0)
        parsed = parse_generate_ac_response(response)
        self.assertEqual(parsed.response_format, 77)
        self.assertEqual(parsed.result.cryptogram_type, CryptogramType.ARQC)
        self.assertEqual(parsed.result.atc, b"\x00\x01")
        self.assertEqual(parsed.result.issuer_application_data, b"IAD")
        self.assertEqual(parsed.icc_dynamic_number, b"IDN")
        self.assertEqual(parsed.signed_dynamic_application_data, b"SIGNED")
        suppressed = json.dumps(parsed.to_evidence())
        self.assertNotIn("12345678", suppressed)
        self.assertNotIn("SIGNED", suppressed)
        digest = parsed.result.to_evidence(AcEvidencePolicy.DIGEST)
        self.assertEqual(digest["ac_sha256"], hashlib.sha256(b"12345678").hexdigest())
        self.assertNotIn("application_cryptogram", digest)

    def test_parse_format_80_and_functional_mapping(self):
        for cid, expected in ((0x00, TransactionFunctionalResult.DECLINED), (0x40, TransactionFunctionalResult.APPROVED), (0x80, TransactionFunctionalResult.ONLINE_REQUIRED)):
            with self.subTest(cid=cid):
                response = ResponseApdu(encode_tlv(TlvNode("80", bytes((cid,)) + b"\x00\x02" + b"12345678" + b"IAD")), 0x90, 0)
                parsed = parse_generate_ac_response(response)
                self.assertEqual(parsed.response_format, 80)
                self.assertEqual(parsed.result.functional_result, expected)
        cid = Cid(0x8F)
        self.assertTrue(cid.advice_required)
        self.assertEqual(cid.reason_advice_code, 7)

    def test_malformed_gac_responses(self):
        expected = {
            "GAC_TRUNCATED": "MALFORMED_GAC_RESPONSE",
            "GAC_WRONG_TEMPLATE": "UNEXPECTED_GAC_TEMPLATE",
            "GAC_DUPLICATE_CID": "DUPLICATE_GAC_OBJECT",
            "GAC_MISSING_AC": "MISSING_GAC_OBJECT",
            "GAC_SHORT_AC": "INVALID_GAC_LENGTH",
            "GAC_MISSING_ATC": "MISSING_GAC_OBJECT",
            "GAC_UNSUPPORTED_CID": "UNSUPPORTED_CRYPTOGRAM_TYPE",
            "GAC_IAD_TOO_LONG": "INVALID_IAD_LENGTH",
            "GAC_UNKNOWN_TAG": "UNEXPECTED_GAC_OBJECT",
        }
        for malformed, code in expected.items():
            with self.subTest(malformed=malformed):
                response = ResponseApdu.from_transport(synthetic_generate_ac_response(malformed=malformed))
                with self.assertRaises((GacError, ValueError)) as caught:
                    parse_generate_ac_response(response)
                self.assertEqual(caught.exception.code, code)

    def test_exchange_status_and_post_send_uncertainty(self):
        command = build_generate_ac_command(TerminalAction.ARQC, cdol1(), cdol_store())
        card_interface, _ = make_interface({command.to_bytes().hex().upper(): TransportResult(b"", "6985")})
        with self.assertRaises(GacError) as caught:
            generate_ac(card_interface, command)
        self.assertEqual(caught.exception.code, "GAC_STATUS_ERROR")
        card_interface, adapter = make_interface({})
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        with self.assertRaises(ApduError) as caught:
            generate_ac(card_interface, command)
        self.assertEqual(caught.exception.outcome, "UNCERTAIN")
        self.assertEqual(caught.exception.phase, "POST_SEND")

    def test_cryptogram_model_validates_lengths_and_aar(self):
        with self.assertRaises(ValueError):
            CryptogramResult(Cid(0x00), b"short", b"\x00\x01")
        with self.assertRaises(ValueError):
            CryptogramResult(Cid(0x00), b"12345678", b"\x01")
        with self.assertRaises(ValueError):
            CryptogramResult(Cid(0xC0), b"12345678", b"\x00\x01")


class OdaTests(unittest.TestCase):
    def test_not_required_does_not_invoke_provider(self):
        provider = CountingProvider()
        result = evaluate_oda(EmvDataStore(), aip(), OdaProfile(), provider, "op-oda", "profile-1")
        self.assertEqual(result.status, OdaStatus.NOT_REQUIRED)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(result.tsi.to_bytes(), b"\x00\x00")

    def test_sda_dda_cda_selection_precedence_and_success(self):
        for raw, method in ((b"\x80\x00", OdaMethod.SDA), (b"\x40\x00", OdaMethod.DDA), (b"\x00\x80", OdaMethod.CDA)):
            with self.subTest(method=method):
                provider = CountingProvider()
                result = evaluate_oda(EmvDataStore(), aip(raw), OdaProfile(), provider, "op-oda", "profile-1")
                self.assertEqual(result.status, OdaStatus.SUCCESS)
                self.assertEqual(result.selection.method, method)
                self.assertEqual(provider.calls, 1)
                self.assertTrue(result.tsi.is_set(TsiFlag.OFFLINE_DATA_AUTHENTICATION_PERFORMED))
        self.assertEqual(select_oda_method(aip(b"\xc0\x80"), OdaProfile()).method, OdaMethod.CDA)

    def test_dynamic_unsupported_can_use_sda_or_blocks_required(self):
        profile = OdaProfile(terminal_dynamic_authentication_supported=False)
        selected = select_oda_method(aip(b"\xc0\x00"), profile)
        self.assertEqual(selected.method, OdaMethod.SDA)
        blocked = evaluate_oda(EmvDataStore(), aip(b"\x40\x00"), profile, CountingProvider(), "op", "profile")
        self.assertEqual(blocked.status, OdaStatus.BLOCKED)
        self.assertEqual(blocked.reason, "ODA_NOT_PERFORMED_REQUIRED")
        self.assertTrue(blocked.tvr.is_set(TvrFlag.OFFLINE_DATA_AUTH_NOT_PERFORMED))
        self.assertFalse(blocked.tsi.is_set(TsiFlag.OFFLINE_DATA_AUTHENTICATION_PERFORMED))

    def test_provider_failure_sets_method_specific_tvr_and_tsi(self):
        cases = (
            (b"\x80\x00", OdaMethod.SDA, TvrFlag.SDA_FAILED),
            (b"\x40\x00", OdaMethod.DDA, TvrFlag.DDA_FAILED),
            (b"\x00\x80", OdaMethod.CDA, TvrFlag.CDA_FAILED),
        )
        for raw, method, flag in cases:
            with self.subTest(method=method):
                provider = SyntheticOdaProvider(fail_methods=frozenset({method}))
                result = evaluate_oda(EmvDataStore(), aip(raw), OdaProfile(), provider, "op", "profile")
                self.assertEqual(result.status, OdaStatus.FAILED)
                self.assertFalse(result.technical_failure)
                self.assertTrue(result.tvr.is_set(flag))
                self.assertTrue(result.tsi.is_set(TsiFlag.OFFLINE_DATA_AUTHENTICATION_PERFORMED))

    def test_provider_error_unknown_and_missing_material_block_closed(self):
        provider = SyntheticOdaProvider(error_methods=frozenset({OdaMethod.SDA}))
        failed = evaluate_oda(EmvDataStore(), aip(b"\x80\x00"), OdaProfile(), provider, "op", "profile")
        self.assertEqual(failed.status, OdaStatus.BLOCKED)
        self.assertEqual(failed.reason, "ODA_PROVIDER_ERROR")

        unknown = evaluate_oda(
            EmvDataStore(),
            aip(b"\x80\x00"),
            OdaProfile(material_status=OdaMaterialStatus.UNKNOWN),
            CountingProvider(),
            "op",
            "profile",
        )
        self.assertEqual(unknown.status, OdaStatus.BLOCKED)
        self.assertEqual(unknown.reason, "UNKNOWN_ODA_MATERIAL")

        profile = OdaProfile(
            material_status=OdaMaterialStatus.TEST_AUTHORIZED,
            required_material_tags={OdaMethod.SDA: ("90",)},
        )
        missing = evaluate_oda(EmvDataStore(), aip(b"\x80\x00"), profile, CountingProvider(), "op", "profile")
        self.assertEqual(missing.status, OdaStatus.BLOCKED)
        self.assertEqual(missing.reason, "MISSING_ODA_MATERIAL")

        store = EmvDataStore()
        store.put(DataRecord("90", b"test-cert", DataSource.CARD, OwnershipDomain.CARD))
        present = evaluate_oda(store, aip(b"\x80\x00"), profile, CountingProvider(), "op", "profile")
        self.assertEqual(present.status, OdaStatus.SUCCESS)

    def test_invalid_profile_and_token_redaction(self):
        with self.assertRaises(OdaError):
            OdaProfile(required_material_tags={OdaMethod.SDA: ("90",)})
        result = evaluate_oda(EmvDataStore(), aip(b"\x80\x00"), OdaProfile(), SyntheticOdaProvider(), "op", "profile")
        self.assertNotIn("synthetic_evidence_digest", result.to_evidence())
        self.assertIn("synthetic_evidence_digest", result.to_evidence(AcEvidencePolicy.DIGEST))
        self.assertEqual(result.to_evidence()["provider"], "SYNTHETIC_NON_CRYPTOGRAPHIC")


class CardActionFlowTests(unittest.TestCase):
    def input_for(self, decision, **overrides):
        values = {
            "decision": decision,
            "aip": aip(),
            "cdol1": cdol1(),
            "operation_id": "card-action-op",
            "profile_ref": "card-action-profile",
            "oda_profile": OdaProfile(),
            "final_cdol2": None,
            "final_action": None,
        }
        values.update(overrides)
        return CardActionInput(**values)

    def test_arqc_flow_gate_and_functional_result(self):
        decision = decision_result()
        command = build_generate_ac_command(decision.proposed_action, cdol1(), cdol_store())
        responses = synthetic_gac_responses(command, first_cid=0x80)
        card_interface, _ = make_interface(responses)
        result = run_card_action_flow(card_interface, self.input_for(decision), SyntheticOdaProvider())
        self.assertEqual(result.gate_status, "PASS")
        self.assertEqual(result.functional_result, TransactionFunctionalResult.ONLINE_REQUIRED)
        self.assertEqual(result.command_count, 1)
        self.assertEqual(result.to_evidence()["status"], "PASS")

    def test_data_foundation_decision_and_card_action_end_to_end(self):
        from tests.test_agent_a_p0_block2 import foundation_responses, make_store

        store = make_store()
        responses = foundation_responses()
        adapter = MockReaderAdapter(responses=responses)
        session = adapter.open(adapter.reader_info.reader_id, "foundation-card-action")
        card_interface = ContactInterface(adapter, session).open()
        pdol = DolDocument(DolKind.PDOL, (
            DolEntry("9F02", 6, DolDataType.NUMERIC),
            DolEntry("5F2A", 2, DolDataType.NUMERIC),
        ))
        foundation = run_data_foundation(card_interface, pdol, store, required_tags=("5F24", "8C"))
        install_synthetic_decision_data(store, expiration_date=b"\x30\x01\x01")
        store.merge((
            DataRecord("95", bytes(5), DataSource.TERMINAL, OwnershipDomain.TERMINAL),
            DataRecord("9A", b"\x26\x01\x01", DataSource.TERMINAL, OwnershipDomain.TERMINAL),
            DataRecord("9C", b"\x00", DataSource.TERMINAL, OwnershipDomain.TERMINAL),
        ))
        decision = run_decision_engine(store, synthetic_decision_profile(), synthetic_decision_input())
        command = build_generate_ac_command(decision.proposed_action, cdol1(), store)
        adapter.responses.update(synthetic_gac_responses(command, first_cid=0x80))
        result = run_card_action_flow(
            card_interface,
            self.input_for(decision, aip=foundation.gpo.aip),
            SyntheticOdaProvider(),
        )
        self.assertEqual(foundation.gate_status, "PASS")
        self.assertEqual(decision.gate_status, "PASS")
        self.assertEqual(result.gate_status, "PASS")
        self.assertEqual(result.functional_result, TransactionFunctionalResult.ONLINE_REQUIRED)

    def test_aac_and_tc_cryptogram_outcomes(self):
        for action, cid, expected in (
            (TerminalAction.AAC, 0x00, TransactionFunctionalResult.DECLINED),
            (TerminalAction.TC, 0x40, TransactionFunctionalResult.APPROVED),
        ):
            with self.subTest(action=action):
                decision = decision_result()
                decision = replace(decision, taa=replace(decision.taa, proposed_action=action))
                command = build_generate_ac_command(action, cdol1(), cdol_store())
                card_interface, _ = make_interface(synthetic_gac_responses(command, first_cid=cid))
                result = run_card_action_flow(card_interface, self.input_for(decision), SyntheticOdaProvider())
                self.assertEqual(result.functional_result, expected)

    def test_final_gac_primitive_with_cdol2(self):
        decision = decision_result()
        store = cdol_store()
        store.put(DataRecord("8A", b"00", DataSource.HOST, OwnershipDomain.HOST))
        decision = replace(decision, store=store)
        first_command = build_generate_ac_command(decision.proposed_action, cdol1(), store)
        final_command = build_final_generate_ac_command(TerminalAction.TC, cdol2(), store)
        responses = synthetic_gac_responses(first_command, 0x80, 77, final_command, 0x40, 80)
        card_interface, _ = make_interface(responses)
        result = run_card_action_flow(
            card_interface,
            self.input_for(decision, final_cdol2=cdol2(), final_action=TerminalAction.TC),
            SyntheticOdaProvider(),
        )
        self.assertIsNotNone(result.final_gac)
        self.assertEqual(result.functional_result, TransactionFunctionalResult.APPROVED)
        self.assertEqual(result.command_count, 2)
        self.assertEqual(result.to_evidence()["history"][-1]["stage"], "FINAL_GENERATE_AC")

    def test_cda_success_sets_p1_low_bit_and_redacts_ac(self):
        decision = decision_result()
        input_value = self.input_for(decision, aip=aip(b"\x00\x80"))
        command = build_generate_ac_command(decision.proposed_action, cdol1(), cdol_store(), cda_requested=True)
        self.assertEqual(command.p1, 0x90)
        card_interface, _ = make_interface(synthetic_gac_responses(command, first_cid=0x80))
        result = run_card_action_flow(card_interface, input_value, SyntheticOdaProvider())
        self.assertEqual(result.oda.status, OdaStatus.SUCCESS)
        serialized = json.dumps(result.to_evidence())
        self.assertNotIn("application_cryptogram", serialized)
        self.assertNotIn("ac_sha256", serialized)

    def test_oda_blocked_prevents_gac(self):
        decision = decision_result()
        card_interface, _ = make_interface({})
        with self.assertRaises(CardActionError) as caught:
            run_card_action_flow(
                card_interface,
                self.input_for(decision, aip=aip(b"\x80\x00"), oda_profile=OdaProfile(material_status=OdaMaterialStatus.UNKNOWN)),
                SyntheticOdaProvider(),
            )
        self.assertEqual(caught.exception.code, "UNKNOWN_ODA_MATERIAL")

    def test_flow_post_send_uncertainty(self):
        decision = decision_result()
        card_interface, adapter = make_interface({})
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        with self.assertRaises(ApduError) as caught:
            run_card_action_flow(card_interface, self.input_for(decision), SyntheticOdaProvider())
        self.assertEqual(caught.exception.outcome, "UNCERTAIN")


if __name__ == "__main__":
    unittest.main()
