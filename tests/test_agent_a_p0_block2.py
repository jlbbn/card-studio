from __future__ import annotations

import json
import unittest

from card_studio.emv import (
    AflEntry,
    AflError,
    AipCapability,
    AipError,
    ApduError,
    ApplicationFileLocator,
    ApplicationInterchangeProfile,
    CommandApdu,
    DataRecord,
    DataSensitivity,
    DataSource,
    DirectoryEntry,
    DolDataType,
    DolDocument,
    DolEntry,
    DolKind,
    EmvDataStore,
    GpoError,
    GpoResponseFormat,
    OwnershipDomain,
    ProvenanceConflictError,
    ReadRecordError,
    RecordLocator,
    ResponseApdu,
    StatusBitmapError,
    Tsi,
    TsiFlag,
    Tvr,
    TvrFlag,
    build_gpo_command,
    get_processing_options,
    parse_afl,
    parse_gpo_response,
    parse_record_response,
    read_afl_records,
    read_record_command,
    run_data_foundation,
    synthetic_data_foundation_responses,
)
from card_studio.interfaces import ContactInterface, ContactlessInterface, InterfaceKind
from card_studio.readers.base import TransportResult
from card_studio.readers.mock import MockReaderAdapter
from card_studio.tlv import TlvNode, encode_tlv, parse_tlv


AID_ONE = "A0000000041010"
KERNEL_ONE = "02"
AMOUNT = bytes.fromhex("000000001000")
CURRENCY = bytes.fromhex("0484")


def record(tag, value, source=DataSource.TERMINAL, ownership=OwnershipDomain.TERMINAL, sensitivity=DataSensitivity.PUBLIC):
    return DataRecord(tag, value, source, ownership, sensitivity)


def make_store():
    store = EmvDataStore()
    store.merge((record("9F02", AMOUNT), record("5F2A", CURRENCY)))
    return store


def make_pdol():
    return DolDocument(DolKind.PDOL, (
        DolEntry("9F02", 6, DolDataType.NUMERIC),
        DolEntry("5F2A", 2, DolDataType.NUMERIC),
    ))


def application():
    return DirectoryEntry(AID_ONE, priority=1, label="LAB", kernel_id=KERNEL_ONE)


def make_interface(interface, responses):
    adapter = MockReaderAdapter(responses=responses)
    session = adapter.open(adapter.reader_info.reader_id, "p0-block2-reader")
    interface_type = ContactInterface if interface == InterfaceKind.CONTACT else ContactlessInterface
    return interface_type(adapter, session).open(), adapter


def afl_bytes():
    return AflEntry(1, 1, 2, 1).encode()


def valid_records():
    return {
        (1, 1): (
            ("5F24", b"\x30\x01\x01"),
            ("8C", bytes.fromhex("9F0206")),
            ("5A", b"\xaa\xbb"),
        ),
        (1, 2): (("DF77", b"\x7e"),),
    }


def foundation_responses(interface=InterfaceKind.CONTACT, gpo_format=77, malformed=None, records=None, aip=b"\x00\x00", afl=None):
    return synthetic_data_foundation_responses(
        interface,
        (application(),),
        make_pdol(),
        make_store(),
        aip=aip,
        afl=afl or afl_bytes(),
        records=valid_records() if records is None else records,
        gpo_format=gpo_format,
        malformed=malformed,
        malformed_record=(1, 2),
    )


class AipTests(unittest.TestCase):
    def test_exact_length_capabilities_and_evidence(self):
        aip = ApplicationInterchangeProfile.parse(b"\xc0\x80")
        self.assertTrue(aip.supports(AipCapability.SDA_SUPPORTED))
        self.assertTrue(aip.supports(AipCapability.DDA_SUPPORTED))
        self.assertTrue(aip.supports(AipCapability.CDA_SUPPORTED))
        self.assertFalse(aip.supports(AipCapability.CARDHOLDER_VERIFICATION_SUPPORTED))
        self.assertFalse(aip.supports(AipCapability.ISSUER_AUTHENTICATION_SUPPORTED))
        self.assertTrue(aip.oda_required)
        self.assertEqual(aip.to_bytes(), b"\xc0\x80")
        evidence = json.dumps(aip.to_evidence())
        self.assertIn("SDA_SUPPORTED", evidence)
        self.assertNotIn("c080", evidence)

    def test_invalid_lengths_and_rfu(self):
        for raw in (b"", b"\x00", b"\x00\x00\x00"):
            with self.subTest(raw=raw):
                with self.assertRaises(AipError) as caught:
                    ApplicationInterchangeProfile.parse(raw)
                self.assertEqual(caught.exception.code, "INVALID_AIP_LENGTH")
        for raw in (b"\x00\x01", b"\x00\x02", b"\x00\x40", b"\x01\x00", b"\x02\x00"):
            with self.subTest(raw=raw):
                with self.assertRaises(AipError) as caught:
                    ApplicationInterchangeProfile.parse(raw)
                self.assertEqual(caught.exception.code, "AIP_RFU_SET")


class AflTests(unittest.TestCase):
    def test_parse_encode_record_plan_and_p2(self):
        afl = parse_afl(bytes.fromhex("0801020110020300"))
        self.assertEqual(afl.encode(), bytes.fromhex("0801020110020300"))
        plan = afl.record_plan()
        self.assertEqual([(item.sfi, item.record) for item in plan], [(1, 1), (1, 2), (2, 2), (2, 3)])
        self.assertEqual([item.contributes_to_offline_auth for item in plan], [True, False, False, False])
        self.assertEqual(read_record_command(plan[0], afl).p2, 0x0C)
        self.assertEqual(read_record_command(plan[-1], afl).p2, 0x14)
        high = read_record_command(RecordLocator(30, 255), ApplicationFileLocator((AflEntry(30, 255, 255),)))
        self.assertEqual(high.p1, 255)
        self.assertEqual(high.p2, 0xF4)
        self.assertEqual(high.ins, 0xB2)

    def test_malformed_afl_inputs(self):
        cases = {
            "empty": b"",
            "truncated": b"\x08\x01\x01",
            "low_bits": b"\x09\x01\x01\x00",
            "zero_sfi": b"\x00\x01\x01\x00",
            "zero_first": b"\x08\x00\x01\x00",
            "reversed": b"\x08\x02\x01\x00",
            "offline_overflow": b"\x08\x01\x02\x03",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AflError):
                    parse_afl(raw)

    def test_overlapping_and_excessive_record_plans(self):
        with self.assertRaises(AflError) as caught:
            ApplicationFileLocator((AflEntry(1, 1, 2), AflEntry(1, 2, 3)))
        self.assertEqual(caught.exception.code, "OVERLAPPING_AFL_RANGES")
        with self.assertRaises(AflError) as caught:
            ApplicationFileLocator((AflEntry(1, 1, 255), AflEntry(2, 1, 255)))
        self.assertEqual(caught.exception.code, "RECORD_PLAN_LIMIT_EXCEEDED")
        with self.assertRaises(AflError):
            parse_afl(bytes(256))

    def test_nonoverlapping_ranges_same_sfi_are_valid(self):
        afl = ApplicationFileLocator((AflEntry(1, 1, 2), AflEntry(1, 3, 4)))
        self.assertEqual(len(afl.record_plan()), 4)

    def test_read_record_requires_validated_afl_object(self):
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {})
        with self.assertRaises(ReadRecordError) as caught:
            read_afl_records(card_interface, afl_bytes(), EmvDataStore())
        self.assertEqual(caught.exception.code, "INVALID_AFL")
        with self.assertRaises(ReadRecordError) as caught:
            read_record_command(RecordLocator(1, 3), ApplicationFileLocator.parse(afl_bytes()))
        self.assertEqual(caught.exception.code, "RECORD_OUTSIDE_AFL")


TVR_CASES = (
    (TvrFlag.OFFLINE_DATA_AUTH_NOT_PERFORMED, 0, 0x80),
    (TvrFlag.SDA_FAILED, 0, 0x40),
    (TvrFlag.ICC_DATA_MISSING, 0, 0x20),
    (TvrFlag.CARD_ON_EXCEPTION_FILE, 0, 0x10),
    (TvrFlag.DDA_FAILED, 0, 0x08),
    (TvrFlag.CDA_FAILED, 0, 0x04),
    (TvrFlag.DIFFERENT_APPLICATION_VERSIONS, 1, 0x80),
    (TvrFlag.EXPIRED_APPLICATION, 1, 0x40),
    (TvrFlag.APPLICATION_NOT_YET_EFFECTIVE, 1, 0x20),
    (TvrFlag.REQUESTED_SERVICE_NOT_ALLOWED, 1, 0x10),
    (TvrFlag.NEW_CARD, 1, 0x08),
    (TvrFlag.CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL, 2, 0x80),
    (TvrFlag.UNRECOGNIZED_CVM, 2, 0x40),
    (TvrFlag.PIN_TRY_LIMIT_EXCEEDED, 2, 0x20),
    (TvrFlag.PIN_PAD_NOT_PRESENT_OR_NOT_WORKING, 2, 0x10),
    (TvrFlag.PIN_NOT_ENTERED, 2, 0x08),
    (TvrFlag.ONLINE_PIN_ENTERED, 2, 0x04),
    (TvrFlag.FLOOR_LIMIT_EXCEEDED, 3, 0x80),
    (TvrFlag.LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED, 3, 0x40),
    (TvrFlag.UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED, 3, 0x20),
    (TvrFlag.RANDOM_TRANSACTION_SELECTION_PERFORMED, 3, 0x10),
    (TvrFlag.MERCHANT_FORCED_TRANSACTION_ONLINE, 3, 0x08),
    (TvrFlag.DEFAULT_TDOL_USED, 4, 0x80),
    (TvrFlag.ISSUER_AUTHENTICATION_FAILED, 4, 0x40),
    (TvrFlag.SCRIPT_PROCESSING_FAILED_BEFORE_FINAL_GAC, 4, 0x20),
    (TvrFlag.SCRIPT_PROCESSING_FAILED_AFTER_FINAL_GAC, 4, 0x10),
)
TSI_CASES = (
    (TsiFlag.OFFLINE_DATA_AUTHENTICATION_PERFORMED, 0x80),
    (TsiFlag.CARDHOLDER_VERIFICATION_PERFORMED, 0x40),
    (TsiFlag.CARD_RISK_MANAGEMENT_PERFORMED, 0x20),
    (TsiFlag.ISSUER_AUTHENTICATION_PERFORMED, 0x10),
    (TsiFlag.TERMINAL_RISK_MANAGEMENT_PERFORMED, 0x08),
    (TsiFlag.SCRIPT_PROCESSING_PERFORMED, 0x04),
)


class StatusBitmapTests(unittest.TestCase):
    def test_every_tvr_flag_bit_level_round_trip_reset_and_clear(self):
        for flag, index, mask in TVR_CASES:
            with self.subTest(flag=flag):
                tvr = Tvr.cleared()
                self.assertEqual(tvr.to_bytes(), bytes(5))
                tvr.set(flag)
                expected = bytearray(5)
                expected[index] = mask
                self.assertEqual(tvr.to_bytes(), bytes(expected))
                self.assertTrue(tvr.is_set(flag))
                self.assertEqual(tvr.to_evidence()["active_flags"], [flag.value])
                restored = Tvr.parse(tvr.to_bytes())
                self.assertTrue(restored.is_set(flag))
                tvr.clear(flag)
                self.assertEqual(tvr.to_bytes(), bytes(5))
                tvr.set(flag)
                tvr.reset()
                self.assertEqual(tvr.to_bytes(), bytes(5))

    def test_every_tsi_flag_only_marks_processing_when_called(self):
        for flag, mask in TSI_CASES:
            with self.subTest(flag=flag):
                tsi = Tsi.cleared()
                self.assertEqual(tsi.to_evidence()["processed"], [])
                tsi.mark_processed(flag)
                self.assertEqual(tsi.to_bytes(), bytes((mask, 0)))
                self.assertEqual(tsi.to_evidence()["processed"], [flag.value])
                restored = Tsi.parse(tsi.to_bytes())
                self.assertTrue(restored.is_set(flag))
                tsi.reset()
                self.assertEqual(tsi.to_bytes(), b"\x00\x00")

    def test_bitmap_length_and_rfu_are_strict(self):
        with self.assertRaises(StatusBitmapError):
            Tvr.parse(bytes(4))
        with self.assertRaises(StatusBitmapError):
            Tvr.parse(b"\x00\x00\x00\x00\x01")
        with self.assertRaises(StatusBitmapError):
            Tsi.parse(b"\x00")
        with self.assertRaises(StatusBitmapError):
            Tsi.parse(b"\x00\x01")


class GpoTests(unittest.TestCase):
    def test_build_real_pdol_tag83_gpo_command(self):
        command = build_gpo_command(make_pdol(), make_store())
        self.assertEqual(command.ins, 0xA8)
        self.assertEqual(command.le, 0)
        roots = parse_tlv(command.data)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].tag, "83")
        self.assertEqual(roots[0].value, AMOUNT + CURRENCY)
        self.assertEqual(CommandApdu.parse(command.to_bytes()), command)

    def test_pdol_kind_and_ownership_are_enforced_before_send(self):
        store = make_store()
        with self.assertRaises(GpoError) as caught:
            build_gpo_command(DolDocument(DolKind.CDOL1, ()), store)
        self.assertEqual(caught.exception.code, "INVALID_PDOL_KIND")
        card_store = EmvDataStore()
        card_store.put(record("9F02", AMOUNT, DataSource.CARD, OwnershipDomain.CARD))
        card_store.put(record("5F2A", CURRENCY))
        with self.assertRaises(GpoError) as caught:
            build_gpo_command(make_pdol(), card_store)
        self.assertEqual(caught.exception.code, "PDOL_OWNERSHIP_CONFLICT")
        self.assertFalse(caught.exception.command_sent)

    def test_parse_gpo_template_77_and_80(self):
        for response_format in (77, 80):
            with self.subTest(response_format=response_format):
                responses = foundation_responses(gpo_format=response_format)
                command = build_gpo_command(make_pdol(), make_store()).to_bytes().hex().upper()
                result = parse_gpo_response(ResponseApdu.from_transport(responses[command]))
                self.assertEqual(result.response_format, GpoResponseFormat(f"TEMPLATE_{response_format}"))
                self.assertEqual(result.aip.to_bytes(), b"\x00\x00")
                self.assertEqual(result.afl.encode(), afl_bytes())
                self.assertEqual(result.afl.record_plan()[0].contributes_to_offline_auth, True)

    def test_gpo_malformed_missing_duplicate_and_ambiguous_templates(self):
        expected = {
            "GPO_TRUNCATED": "MALFORMED_GPO_RESPONSE",
            "GPO_WRONG_TEMPLATE": "UNEXPECTED_GPO_TEMPLATE",
            "GPO_MISSING_AIP": "MISSING_GPO_OBJECT",
            "GPO_MISSING_AFL": "MISSING_GPO_OBJECT",
            "GPO_DUPLICATE_AIP": "DUPLICATE_GPO_OBJECT",
            "GPO_AMBIGUOUS": "AMBIGUOUS_GPO_RESPONSE",
        }
        for malformed, code in expected.items():
            with self.subTest(malformed=malformed):
                responses = foundation_responses(malformed=malformed)
                card_interface, _ = make_interface(InterfaceKind.CONTACT, responses)
                with self.assertRaises(GpoError) as caught:
                    get_processing_options(card_interface, make_pdol(), make_store())
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.command_sent)

    def test_gpo_rejects_aip_rfu(self):
        card_interface, _ = make_interface(InterfaceKind.CONTACT, foundation_responses(malformed="AIP_RFU"))
        with self.assertRaises(AipError) as caught:
            get_processing_options(card_interface, make_pdol(), make_store())
        self.assertEqual(caught.exception.code, "AIP_RFU_SET")

    def test_gpo_unknown_primitive_metadata_and_duplicate_rejection(self):
        afl = afl_bytes()
        response = ResponseApdu(encode_tlv(TlvNode("77", children=(
            TlvNode("82", b"\x00\x00"),
            TlvNode("94", afl),
            TlvNode("DF77", b"\xaa\xbb"),
        ))), 0x90, 0)
        result = parse_gpo_response(response)
        self.assertEqual(result.objects[-1]["tag"], "DF77")
        self.assertNotIn("aabb", json.dumps(result.to_evidence()))
        duplicate = ResponseApdu(encode_tlv(TlvNode("77", children=(
            TlvNode("82", b"\x00\x00"),
            TlvNode("94", afl),
            TlvNode("DF77", b"\x01"),
            TlvNode("DF77", b"\x02"),
        ))), 0x90, 0)
        with self.assertRaises(GpoError) as caught:
            parse_gpo_response(duplicate)
        self.assertEqual(caught.exception.code, "DUPLICATE_GPO_OBJECT")

    def test_gpo_status_and_61xx_followup(self):
        command = build_gpo_command(make_pdol(), make_store()).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {command: TransportResult(b"", "6985")})
        with self.assertRaises(GpoError) as caught:
            get_processing_options(card_interface, make_pdol(), make_store())
        self.assertEqual(caught.exception.code, "GPO_STATUS_ERROR")

        responses = foundation_responses()
        complete = responses[command]
        responses[command] = TransportResult(complete.data[:3], "6105")
        responses[CommandApdu(0, 0xC0, 0, 0, le=5).to_bytes().hex().upper()] = TransportResult(complete.data[3:], "9000")
        card_interface, _ = make_interface(InterfaceKind.CONTACT, responses)
        result = get_processing_options(card_interface, make_pdol(), make_store())
        self.assertEqual(result.command_count, 2)


class ReadRecordTests(unittest.TestCase):
    def test_parse_valid_record_with_unknown_tag_and_sensitive_metadata(self):
        response = ResponseApdu(encode_tlv(TlvNode("70", children=(
            TlvNode("5A", b"\xaa\xbb"),
            TlvNode("DF77", b"\x7e"),
        ))), 0x90, 0)
        records = parse_record_response(response)
        self.assertEqual(records[0].ownership, OwnershipDomain.CARD)
        self.assertEqual(records[0].source, DataSource.CARD)
        self.assertEqual(records[0].sensitivity, DataSensitivity.SENSITIVE)
        self.assertEqual(records[1].sensitivity, DataSensitivity.PUBLIC)
        store = EmvDataStore()
        store.merge(records)
        self.assertEqual(store.to_evidence()[1]["tag"], "DF77")
        serialized = json.dumps(store.to_evidence())
        self.assertNotIn("aabb", serialized)
        self.assertNotIn("7e", serialized.replace("sensitive", ""))

    def test_record_rejects_malformed_nested_empty_duplicate_and_owned_tags(self):
        cases = {
            "wrong_template": (ResponseApdu(encode_tlv(TlvNode("77", children=())), 0x90, 0), "UNEXPECTED_RECORD_TEMPLATE"),
            "empty": (ResponseApdu(encode_tlv(TlvNode("70", children=())), 0x90, 0), "EMPTY_RECORD"),
            "truncated": (ResponseApdu(b"\x70\x05\x4f\x01", 0x90, 0), "MALFORMED_RECORD"),
            "duplicate": (ResponseApdu(encode_tlv(TlvNode("70", children=(TlvNode("DF01", b"\x01"), TlvNode("DF01", b"\x01")))), 0x90, 0), "DUPLICATE_RECORD_TAG"),
            "nested": (ResponseApdu(encode_tlv(TlvNode("70", children=(TlvNode("A5", children=()),))), 0x90, 0), "NESTED_RECORD_OBJECT"),
            "terminal": (ResponseApdu(encode_tlv(TlvNode("70", children=(TlvNode("9F02", b"\x00"),))), 0x90, 0), "OWNERSHIP_RECORD_INJECTION"),
            "host": (ResponseApdu(encode_tlv(TlvNode("70", children=(TlvNode("8A", b"00"),))), 0x90, 0), "OWNERSHIP_RECORD_INJECTION"),
            "gac": (ResponseApdu(encode_tlv(TlvNode("70", children=(TlvNode("9F26", b"\x00"),))), 0x90, 0), "OWNERSHIP_RECORD_INJECTION"),
        }
        for name, (response, code) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ReadRecordError) as caught:
                    parse_record_response(response)
                self.assertEqual(caught.exception.code, code)

    def test_unknown_tags_require_explicit_context_permission(self):
        response = ResponseApdu(encode_tlv(TlvNode("70", children=(TlvNode("DF77", b"\x01"),))), 0x90, 0)
        self.assertEqual(len(parse_record_response(response, allow_unknown_tags=True)), 1)
        with self.assertRaises(ReadRecordError) as caught:
            parse_record_response(response, allow_unknown_tags=False)
        self.assertEqual(caught.exception.code, "UNKNOWN_RECORD_TAG")

    def test_global_merge_success_and_identical_repeated_card_data(self):
        store = make_store()
        records = valid_records()
        records[(1, 2)] = (("5F24", b"\x30\x01\x01"),)
        card_interface, _ = make_interface(InterfaceKind.CONTACT, foundation_responses(records=records))
        result = read_afl_records(card_interface, ApplicationFileLocator.parse(afl_bytes()), store, required_tags=("5F24", "8C"))
        self.assertEqual(result.records_read, 2)
        self.assertEqual(store.get("5F24").ownership, OwnershipDomain.CARD)
        self.assertEqual(store.get("9F02").ownership, OwnershipDomain.TERMINAL)

    def test_conflicting_card_data_aborts_without_partial_merge(self):
        store = make_store()
        before = dict(store.snapshot())
        records = valid_records()
        records[(1, 2)] = (("5F24", b"\x30\x01\x02"),)
        card_interface, _ = make_interface(InterfaceKind.CONTACT, foundation_responses(records=records))
        with self.assertRaises(ProvenanceConflictError):
            read_afl_records(card_interface, ApplicationFileLocator.parse(afl_bytes()), store)
        self.assertEqual(dict(store.snapshot()), before)

    def test_second_malformed_record_rolls_back_entire_plan(self):
        store = make_store()
        before = dict(store.snapshot())
        card_interface, _ = make_interface(InterfaceKind.CONTACT, foundation_responses(malformed="RECORD_DUPLICATE_TAG"))
        with self.assertRaises(ReadRecordError) as caught:
            read_afl_records(card_interface, ApplicationFileLocator.parse(afl_bytes()), store)
        self.assertEqual(caught.exception.code, "DUPLICATE_RECORD_TAG")
        self.assertEqual(dict(store.snapshot()), before)

    def test_missing_mandatory_tag_after_plan_rolls_back(self):
        store = make_store()
        before = dict(store.snapshot())
        card_interface, _ = make_interface(InterfaceKind.CONTACT, foundation_responses())
        with self.assertRaises(Exception):
            read_afl_records(card_interface, ApplicationFileLocator.parse(afl_bytes()), store, required_tags=("9F07",))
        self.assertEqual(dict(store.snapshot()), before)

    def test_unknown_tag_rejection_and_status_abort_without_mutation(self):
        for malformed in ("RECORD_UNKNOWN_TAG", "RECORD_TERMINAL_TAG"):
            with self.subTest(malformed=malformed):
                store = make_store()
                before = dict(store.snapshot())
                card_interface, _ = make_interface(InterfaceKind.CONTACT, foundation_responses(malformed=malformed))
                with self.assertRaises(ReadRecordError):
                    read_afl_records(card_interface, ApplicationFileLocator.parse(afl_bytes()), store, allow_unknown_tags=False)
                self.assertEqual(dict(store.snapshot()), before)

        store = make_store()
        before = dict(store.snapshot())
        command = read_record_command(RecordLocator(1, 1), ApplicationFileLocator.parse(afl_bytes())).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {command: TransportResult(b"", "6A83")})
        with self.assertRaises(ReadRecordError):
            read_afl_records(card_interface, ApplicationFileLocator.parse(afl_bytes()), store)
        self.assertEqual(dict(store.snapshot()), before)

    def test_transport_uncertain_aborts_without_mutation(self):
        store = make_store()
        before = dict(store.snapshot())
        card_interface, adapter = make_interface(InterfaceKind.CONTACT, foundation_responses())
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        with self.assertRaises(ApduError) as caught:
            read_afl_records(card_interface, ApplicationFileLocator.parse(afl_bytes()), store)
        self.assertEqual(caught.exception.outcome, "UNCERTAIN")
        self.assertEqual(caught.exception.phase, "POST_SEND")
        self.assertEqual(dict(store.snapshot()), before)


class DataFoundationTests(unittest.TestCase):
    def test_full_foundation_both_interfaces_and_gpo_formats(self):
        for interface in (InterfaceKind.CONTACT, InterfaceKind.CONTACTLESS):
            for response_format in (77, 80):
                with self.subTest(interface=interface, response_format=response_format):
                    store = make_store()
                    responses = foundation_responses(interface=interface, gpo_format=response_format)
                    card_interface, _ = make_interface(interface, responses)
                    result = run_data_foundation(
                        card_interface,
                        make_pdol(),
                        store,
                        supported_kernel_ids={KERNEL_ONE},
                        required_tags=("5F24", "8C"),
                    )
                    self.assertEqual(result.command_count, 5)
                    self.assertEqual(result.records.records_read, 2)
                    self.assertEqual(result.gate_status, "PASS")
                    self.assertEqual(result.to_evidence()["status"], "PASS")
                    self.assertEqual(result.selection.selected.candidate.aid, AID_ONE)
                    self.assertEqual(result.gpo.response_format, GpoResponseFormat(f"TEMPLATE_{response_format}"))
                    evidence = json.dumps(result.to_evidence())
                    self.assertIn("EMV_DATA_FOUNDATION", evidence)
                    self.assertIn("DF77", evidence)
                    self.assertNotIn("aabb", evidence)
                    self.assertNotIn("raw_apdu", evidence)

    def test_foundation_failure_on_second_record_preserves_input_store(self):
        store = make_store()
        before = dict(store.snapshot())
        card_interface, _ = make_interface(InterfaceKind.CONTACT, foundation_responses(malformed="RECORD_TERMINAL_TAG"))
        with self.assertRaises(ReadRecordError) as caught:
            run_data_foundation(card_interface, make_pdol(), store)
        self.assertEqual(caught.exception.code, "OWNERSHIP_RECORD_INJECTION")
        self.assertEqual(dict(store.snapshot()), before)


if __name__ == "__main__":
    unittest.main()
