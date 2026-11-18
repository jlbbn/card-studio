from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from card_studio.emv import (
    ApduContext,
    ApduError,
    CommandApdu,
    DataLimitError,
    DataRecord,
    DataSensitivity,
    DataSource,
    DirectoryEntry,
    DolDataType,
    DolDocument,
    DolEntry,
    DolError,
    DolKind,
    DuplicateDataError,
    EmvDataStore,
    MandatoryDataError,
    OwnershipConflictError,
    OwnershipDomain,
    PPSE_NAME,
    PSE_NAME,
    ProvenanceConflictError,
    ResponseApdu,
    SelectionError,
    StatusAction,
    TruncationPolicy,
    choose_candidate,
    classify_status,
    environment_supports_read_record,
    exchange_with_followups,
    parse_directory_entries,
    parse_dol,
    select_aid_command,
    select_application,
    select_direct_aid,
    select_environment_command,
    select_payment_environment,
    synthetic_selection_responses,
    validate_selected_adf,
)
from card_studio.interfaces import ContactInterface, ContactlessInterface, InterfaceKind
from card_studio.readers.base import TransportResult
from card_studio.readers.mock import MockReaderAdapter


AID_ONE = "A0000000041010"
AID_TWO = "A0000000042020"
AID_PREFIX = "A000000004"
KERNEL_ONE = "02"
KERNEL_TWO = "03"
AMOUNT = bytes.fromhex("000000001000")
CURRENCY = bytes.fromhex("0484")


def record(
    tag="9F02",
    value=AMOUNT,
    source=DataSource.TERMINAL,
    ownership=OwnershipDomain.TERMINAL,
    sensitivity=DataSensitivity.PUBLIC,
    timestamp=None,
    context=None,
):
    return DataRecord(tag, value, source, ownership, sensitivity, timestamp, context)


def store_with(*records):
    store = EmvDataStore()
    store.merge(records)
    return store


def entry(aid=AID_ONE, priority=1, kernel_id=KERNEL_ONE, label="LAB"):
    return DirectoryEntry(aid=aid, priority=priority, label=label, kernel_id=kernel_id)


def make_interface(interface, responses):
    adapter = MockReaderAdapter(responses=responses)
    reader_session = adapter.open(adapter.reader_info.reader_id, "p0-block1-reader")
    interface_type = ContactInterface if interface == InterfaceKind.CONTACT else ContactlessInterface
    card_interface = interface_type(adapter, reader_session).open()
    return card_interface, adapter


class DataStoreTests(unittest.TestCase):
    def test_record_metadata_and_evidence_preserve_unknown_tag_without_value(self):
        timestamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        sensitive = record(
            tag="DF77",
            value=b"\xaa\xbb\xcc\xdd",
            source=DataSource.HOST,
            ownership=OwnershipDomain.HOST,
            sensitivity=DataSensitivity.SECRET,
            timestamp=timestamp,
            context="host-response-1",
        )
        store = store_with(sensitive)
        evidence = store.to_evidence()
        self.assertEqual(evidence[0]["tag"], "DF77")
        self.assertEqual(evidence[0]["source"], "HOST")
        self.assertEqual(evidence[0]["ownership"], "HOST")
        self.assertEqual(evidence[0]["sensitivity"], "SECRET")
        self.assertEqual(evidence[0]["value_length"], 4)
        self.assertEqual(evidence[0]["timestamp"], "2026-01-02T03:04:05Z")
        self.assertEqual(evidence[0]["context"], "host-response-1")
        serialized = json.dumps(evidence)
        self.assertNotIn("aabbccdd", serialized.lower())
        self.assertNotIn("value\"", serialized)

    def test_idempotent_put_and_all_provenance_conflicts_are_atomic(self):
        original = record(timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), context="initial")
        store = store_with(original)
        store.put(original)
        self.assertEqual(len(store), 1)
        cases = (
            replace(original, ownership=OwnershipDomain.CARD, source=DataSource.CARD),
            replace(original, source=DataSource.PROFILE),
            replace(original, value=bytes.fromhex("000000000001")),
            replace(original, sensitivity=DataSensitivity.SENSITIVE),
            replace(original, context="different"),
            replace(original, timestamp=datetime(2027, 1, 1, tzinfo=timezone.utc)),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises((OwnershipConflictError, ProvenanceConflictError)):
                    store.put(candidate)
                self.assertEqual(store.get(original.tag), original)
                self.assertEqual(len(store), 1)

    def test_merge_rejects_batch_duplicate_without_partial_mutation(self):
        store = store_with(record())
        with self.assertRaises(DuplicateDataError) as caught:
            store.merge((record(tag="DF01", value=b"\x01"), record(tag="DF02", value=b"\x02"), record(tag="DF02", value=b"\x02")))
        self.assertEqual(caught.exception.code, "DUPLICATE_TAG")
        self.assertIsNone(store.get("DF01"))
        self.assertIsNone(store.get("DF02"))
        self.assertEqual(len(store), 1)

    def test_merge_ownership_failure_rolls_back_every_incoming_record(self):
        store = store_with(record())
        conflicting = record(source=DataSource.CARD, ownership=OwnershipDomain.CARD)
        with self.assertRaises(ProvenanceConflictError):
            store.merge((record(tag="DF02", value=b"\x02"), record(value=b"\x00")))
        self.assertIsNone(store.get("DF02"))
        with self.assertRaises((OwnershipConflictError, ProvenanceConflictError)):
            store.merge((conflicting, record(tag="DF03", value=b"\x03")))
        self.assertIsNone(store.get("DF03"))

    def test_record_and_value_limits_do_not_mutate(self):
        store = EmvDataStore(max_records=1, max_value_length=2)
        store.put(record(value=b"\x01"))
        with self.assertRaises(DataLimitError) as caught:
            store.put(record(tag="DF01", value=b"\x02"))
        self.assertEqual(caught.exception.code, "RECORD_LIMIT_EXCEEDED")
        self.assertIsNone(store.get("DF01"))
        with self.assertRaises(DataLimitError) as caught:
            store.put(record(tag="DF02", value=b"\x01\x02\x03"))
        self.assertEqual(caught.exception.code, "VALUE_LIMIT_EXCEEDED")
        self.assertIsNone(store.get("DF02"))

    def test_require_and_invalid_metadata(self):
        store = EmvDataStore()
        with self.assertRaises(MandatoryDataError) as caught:
            store.require("9F02")
        self.assertEqual(caught.exception.code, "MISSING_MANDATORY_DATA")
        with self.assertRaises(ValueError):
            record(tag="00")
        with self.assertRaises(ValueError):
            record(context="bad\ncontext")
        with self.assertRaises(ValueError):
            record(timestamp=datetime(2026, 1, 1))


class DolTests(unittest.TestCase):
    def test_all_dol_kinds_parse_encode_build_and_evidence(self):
        raw = bytes.fromhex("9F02065F2A02DF0101")
        store = store_with(
            record(),
            record(tag="5F2A", value=CURRENCY),
            record(tag="DF01", value=b"\x7f"),
        )
        for kind in DolKind:
            with self.subTest(kind=kind):
                document = parse_dol(raw, kind)
                self.assertEqual(document.kind, kind)
                self.assertEqual(document.encode(), raw)
                self.assertEqual(document.build(store), AMOUNT + CURRENCY + b"\x7f")
                evidence = document.to_evidence()
                self.assertEqual(evidence["mode"], "LAB_ONLY")
                self.assertEqual(evidence["declared_output_length"], 9)
                self.assertEqual(evidence["entries"][2]["tag"], "DF01")
                self.assertNotIn("7f", json.dumps(evidence["entries"][2]))

    def test_short_value_padding_is_explicit_by_data_type(self):
        store = store_with(
            record(tag="9F02", value=b"\x12\x34"),
            record(tag="DF01", value=b"\xaa\xbb"),
            record(tag="50", value=b"A"),
        )
        document = DolDocument(DolKind.CDOL1, (
            DolEntry("9F02", 4, DolDataType.NUMERIC),
            DolEntry("DF01", 4, DolDataType.BINARY),
            DolEntry("50", 3, DolDataType.TEXT),
        ))
        self.assertEqual(document.build(store), bytes.fromhex("00001234AABB0000") + b"A  ")

    def test_long_value_rejected_by_default_and_opt_in_truncation(self):
        store = store_with(
            record(tag="9F02", value=bytes.fromhex("00123456")),
            record(tag="DF01", value=b"\xaa\xbb\xcc"),
            record(tag="50", value=b"ABC"),
        )
        rejecting = DolDocument(DolKind.CDOL2, (
            DolEntry("9F02", 2, DolDataType.NUMERIC),
            DolEntry("DF01", 2, DolDataType.BINARY),
            DolEntry("50", 2, DolDataType.TEXT),
        ))
        with self.assertRaises(DolError) as caught:
            rejecting.build(store)
        self.assertEqual(caught.exception.code, "DOL_VALUE_TOO_LONG")
        allowing = DolDocument(DolKind.CDOL2, (
            DolEntry("9F02", 2, DolDataType.NUMERIC, TruncationPolicy.ALLOW),
            DolEntry("DF01", 2, DolDataType.BINARY, TruncationPolicy.ALLOW),
            DolEntry("50", 2, DolDataType.TEXT, TruncationPolicy.ALLOW),
        ))
        self.assertEqual(allowing.build(store), bytes.fromhex("3456AABB") + b"AB")

    def test_zero_length_still_requires_present_mandatory_data(self):
        document = DolDocument(DolKind.TDOL, (DolEntry("DF01", 0),))
        with self.assertRaises(DolError) as caught:
            document.build(EmvDataStore())
        self.assertEqual(caught.exception.code, "MISSING_MANDATORY_DATA")
        store = store_with(record(tag="DF01", value=b""))
        self.assertEqual(document.build(store), b"")

    def test_missing_mandatory_data_fails_for_every_dol_kind(self):
        document = DolDocument(DolKind.PDOL, (DolEntry("9F02", 6),))
        with self.assertRaises(DolError) as caught:
            document.build(EmvDataStore())
        self.assertEqual(caught.exception.code, "MISSING_MANDATORY_DATA")
        for kind in DolKind:
            with self.subTest(kind=kind):
                parsed = parse_dol(bytes.fromhex("9F0206"), kind)
                with self.assertRaises(DolError):
                    parsed.build(EmvDataStore())

    def test_malformed_dol_and_duplicate_tags(self):
        cases = {
            "multibyte_truncated": bytes.fromhex("9F"),
            "missing_length": bytes.fromhex("9F02"),
            "constructed": bytes.fromhex("7001"),
            "duplicate": bytes.fromhex("9F02069F0206"),
            "invalid_tag": bytes.fromhex("0001"),
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(DolError):
                    parse_dol(raw, DolKind.PDOL)
        with self.assertRaises(DolError):
            DolEntry("70", 1)
        with self.assertRaises(DolError):
            DolDocument(DolKind.PDOL, (DolEntry("DF01", 1), DolEntry("DF01", 1)))

    def test_entry_output_and_value_limits(self):
        with self.assertRaises(DolError) as caught:
            DolDocument(DolKind.PDOL, (DolEntry("DF01", 200), DolEntry("DF02", 53)))
        self.assertEqual(caught.exception.code, "DOL_LIMIT_EXCEEDED")
        with self.assertRaises(DolError):
            parse_dol(bytes.fromhex("DF0101DF0201"), DolKind.PDOL, max_entries=1)
        with self.assertRaises(DolError):
            DolDocument(DolKind.PDOL, (DolEntry("DF01", 1),), max_output=0)

    def test_invalid_numeric_and_text_values(self):
        numeric = DolDocument(DolKind.CDOL1, (DolEntry("9F02", 1, DolDataType.NUMERIC),))
        with self.assertRaises(DolError) as caught:
            numeric.build(store_with(record(value=b"\xfa")))
        self.assertEqual(caught.exception.code, "INVALID_NUMERIC_VALUE")
        text = DolDocument(DolKind.CDOL1, (DolEntry("50", 1, DolDataType.TEXT),))
        with self.assertRaises(DolError) as caught:
            text.build(store_with(record(tag="50", value=b"\xff")))
        self.assertEqual(caught.exception.code, "INVALID_TEXT_VALUE")


class ApduTests(unittest.TestCase):
    def test_command_round_trip_all_short_apdu_cases(self):
        commands = (
            CommandApdu(0x00, 0x84, 0, 0),
            CommandApdu(0x00, 0xC0, 0, 0, le=0x20),
            CommandApdu(0x80, 0xAE, 0x80, 0, b"\x01\x02"),
            CommandApdu(0x00, 0xA4, 4, 0, bytes.fromhex(AID_ONE), 0),
        )
        for command in commands:
            with self.subTest(command=command):
                raw = command.to_bytes()
                self.assertEqual(CommandApdu.parse(raw), command)
                self.assertEqual(command.lc, len(command.data) if command.data else None)
                if command.data:
                    self.assertNotIn(command.data.hex(), json.dumps(command.to_evidence()))

    def test_command_rejects_truncation_length_mismatch_and_limits(self):
        cases = (b"", b"\x00\xa4\x04", b"\x00\xa4\x04\x00\x02\xaa", b"\x00\xa4\x04\x00\x01\xaa\xbb\x00", b"\x00\xa4\x04\x00\x00\xaa")
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ApduError):
                    CommandApdu.parse(raw)
        with self.assertRaises(ApduError) as caught:
            CommandApdu(0, 0, 0, 0, bytes(256))
        self.assertEqual(caught.exception.code, "APDU_DATA_LIMIT_EXCEEDED")
        self.assertFalse(caught.exception.command_sent)
        self.assertEqual(caught.exception.phase, "PRE_SEND")
        with self.assertRaises(ApduError):
            CommandApdu(256, 0, 0, 0)

    def test_response_parse_transport_and_evidence(self):
        response = ResponseApdu.parse(b"\x01\x02\x90\x00")
        self.assertTrue(response.is_success)
        self.assertEqual(response.data, b"\x01\x02")
        self.assertEqual(response.sw1sw2, "9000")
        self.assertEqual(ResponseApdu.from_transport(response.to_transport()), response)
        self.assertEqual(ResponseApdu.parse(response.to_bytes()), response)
        self.assertNotIn("0102", json.dumps(response.to_evidence()))
        for raw in (b"", b"\x90"):
            with self.subTest(raw=raw):
                with self.assertRaises(ApduError):
                    ResponseApdu.parse(raw)
        with self.assertRaises(ApduError):
            ResponseApdu.from_transport(TransportResult(b"", None))
        with self.assertRaises(ApduError):
            ResponseApdu.from_transport(TransportResult(b"", "900000"))

    def test_required_status_classification(self):
        expected = {
            "9000": StatusAction.SUCCESS,
            "6102": StatusAction.GET_RESPONSE,
            "6C20": StatusAction.RETRY_WITH_LE,
            "6A82": StatusAction.FILE_NOT_FOUND,
            "6A86": StatusAction.INCORRECT_P1P2,
            "6700": StatusAction.WRONG_LENGTH,
            "6985": StatusAction.CONDITIONS_NOT_SATISFIED,
            "6D00": StatusAction.INSTRUCTION_NOT_SUPPORTED,
            "6E00": StatusAction.CLASS_NOT_SUPPORTED,
        }
        for status, action in expected.items():
            with self.subTest(status=status):
                response = ResponseApdu.from_transport(TransportResult(b"", status))
                classification = classify_status(response)
                self.assertEqual(classification.action, action)
        self.assertEqual(classify_status(ResponseApdu(b"", 0x61, 0), ApduContext.AID_SELECTION).expected_length, 256)
        self.assertEqual(classify_status(ResponseApdu(b"", 0x6A, 0x82), ApduContext.AID_SELECTION).action, StatusAction.TRY_NEXT_AID)
        self.assertEqual(classify_status(ResponseApdu(b"", 0x6A, 0x82), ApduContext.ENVIRONMENT_SELECTION).action, StatusAction.ENVIRONMENT_NOT_FOUND)
        with self.assertRaises(ApduError) as caught:
            classify_status(ResponseApdu(b"", 0x6F, 0x00))
        self.assertEqual(caught.exception.code, "UNEXPECTED_STATUS")

    def test_61xx_followup_accumulates_data(self):
        command = select_aid_command(AID_ONE)
        get_response = CommandApdu(0, 0xC0, 0, 0, le=2).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {
            command.to_bytes().hex().upper(): TransportResult(b"\x01", "6102"),
            get_response: TransportResult(b"\x02\x03", "9000"),
        })
        result = exchange_with_followups(card_interface, command)
        self.assertEqual(result.response.data, b"\x01\x02\x03")
        self.assertEqual(result.command_count, 2)
        self.assertEqual(result.action, StatusAction.SUCCESS)

    def test_6100_uses_zero_le_and_preserves_post_send_phase(self):
        command = select_aid_command(AID_ONE)
        get_response = CommandApdu(0, 0xC0, 0, 0, le=0).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {
            command.to_bytes().hex().upper(): TransportResult(b"\x01", "6100"),
            get_response: TransportResult(b"\x02", "9000"),
        })
        result = exchange_with_followups(card_interface, command)
        self.assertEqual(result.response.data, b"\x01\x02")
        self.assertEqual(result.command_count, 2)
        self.assertEqual(result.action, StatusAction.SUCCESS)

    def test_6cxx_retries_original_command_with_expected_le(self):
        command = select_aid_command(AID_ONE)
        corrected = command.with_le(7).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {
            command.to_bytes().hex().upper(): TransportResult(b"", "6C07"),
            corrected: TransportResult(b"\x04", "9000"),
        })
        result = exchange_with_followups(card_interface, command)
        self.assertEqual(result.response.data, b"\x04")
        self.assertEqual(result.command_count, 2)

    def test_apdu_chains_are_bounded(self):
        for status in ("6101", "6C01"):
            with self.subTest(status=status):
                command = select_aid_command(AID_ONE)
                get_response = CommandApdu(0, 0xC0, 0, 0, le=1).to_bytes().hex().upper()
                responses = {
                    command.to_bytes().hex().upper(): TransportResult(b"", status),
                    command.with_le(1).to_bytes().hex().upper(): TransportResult(b"", status),
                    get_response: TransportResult(b"", status),
                }
                card_interface, _ = make_interface(InterfaceKind.CONTACT, responses)
                with self.assertRaises(ApduError) as caught:
                    exchange_with_followups(card_interface, command, max_followups=2)
                self.assertEqual(caught.exception.code, "APDU_CHAIN_LIMIT_EXCEEDED")
                self.assertTrue(caught.exception.command_sent)

    def test_transport_uncertainty_and_pre_send_errors_are_distinct(self):
        command = select_aid_command(AID_ONE)
        card_interface, adapter = make_interface(InterfaceKind.CONTACT, {})
        adapter.fail_next_transmit = "TRANSMIT_TIMEOUT"
        with self.assertRaises(ApduError) as caught:
            exchange_with_followups(card_interface, command)
        self.assertEqual(caught.exception.code, "TRANSMIT_TIMEOUT")
        self.assertEqual(caught.exception.outcome, "UNCERTAIN")
        self.assertEqual(caught.exception.phase, "POST_SEND")
        closed = ContactInterface(adapter, adapter.open(adapter.reader_info.reader_id, "closed-session"))
        with self.assertRaises(ApduError) as caught:
            exchange_with_followups(closed, command)
        self.assertEqual(caught.exception.code, "CARD_INTERFACE_NOT_OPEN")
        self.assertEqual(caught.exception.phase, "PRE_SEND")


class SelectionTests(unittest.TestCase):
    def test_builders_use_exact_pse_ppse_and_direct_aid(self):
        pse = select_environment_command(InterfaceKind.CONTACT)
        ppse = select_environment_command(InterfaceKind.CONTACTLESS)
        self.assertEqual(pse.data, PSE_NAME)
        self.assertEqual(ppse.data, PPSE_NAME)
        self.assertEqual(pse.ins, 0xA4)
        self.assertEqual(pse.le, 0)
        direct = select_aid_command(AID_ONE)
        self.assertEqual(direct.data, bytes.fromhex(AID_ONE))
        self.assertEqual(select_aid_command(AID_ONE, next_occurrence=True).p2, 2)
        self.assertTrue(environment_supports_read_record(InterfaceKind.CONTACT))
        self.assertFalse(environment_supports_read_record(InterfaceKind.CONTACTLESS))
        with self.assertRaises(SelectionError):
            select_environment_command(InterfaceKind.VIRTUAL)

    def test_direct_aid_select_and_final_fci_validation(self):
        responses = synthetic_selection_responses(InterfaceKind.CONTACT, (entry(),))
        card_interface, _ = make_interface(InterfaceKind.CONTACT, responses)
        self.assertEqual(select_direct_aid(card_interface, AID_ONE), 1)
        response = ResponseApdu.from_transport(responses[select_aid_command(AID_ONE).to_bytes().hex().upper()])
        self.assertEqual(validate_selected_adf(response, AID_ONE), AID_ONE)
        with self.assertRaises(SelectionError) as caught:
            validate_selected_adf(response, AID_TWO)
        self.assertEqual(caught.exception.code, "SELECTED_AID_MISMATCH")

    def test_pse_and_ppse_environment_parsing(self):
        for interface in (InterfaceKind.CONTACT, InterfaceKind.CONTACTLESS):
            with self.subTest(interface=interface):
                responses = synthetic_selection_responses(interface, (entry(),))
                card_interface, _ = make_interface(interface, responses)
                selected = select_payment_environment(card_interface)
                self.assertEqual(len(selected.entries), 1)
                self.assertEqual(selected.entries[0].aid, AID_ONE)
                self.assertEqual(selected.entries[0].priority, 1)
                self.assertEqual(selected.entries[0].kernel_id, KERNEL_ONE)
                self.assertEqual(selected.command_count, 1)

    def test_application_selection_end_to_end_contact_and_contactless(self):
        for interface in (InterfaceKind.CONTACT, InterfaceKind.CONTACTLESS):
            with self.subTest(interface=interface):
                responses = synthetic_selection_responses(interface, (entry(),))
                card_interface, _ = make_interface(interface, responses)
                selected = select_application(card_interface, supported_kernel_ids={KERNEL_ONE})
                self.assertEqual(selected.selected.candidate.aid, AID_ONE)
                self.assertEqual(selected.selected.selection_kind, "EXACT")
                self.assertEqual(selected.command_count, 2)
                evidence = json.dumps(selected.to_evidence())
                self.assertIn("LAB_ONLY", evidence)
                self.assertNotIn("6f", evidence.lower().replace("6f", ""))

    def test_multiple_candidates_require_explicit_aid_even_with_priorities(self):
        entries = (entry(AID_ONE, priority=0), entry(AID_TWO, priority=1, kernel_id=KERNEL_TWO))
        with self.assertRaises(SelectionError) as caught:
            choose_candidate(entries)
        self.assertEqual(caught.exception.code, "APPLICATION_SELECTION_REQUIRED")
        selected = choose_candidate(entries, application_aid=AID_TWO, supported_kernel_ids={KERNEL_ONE, KERNEL_TWO})
        self.assertEqual(selected.candidate.aid, AID_TWO)
        only_kernel_one = choose_candidate(entries, supported_kernel_ids={KERNEL_ONE})
        self.assertEqual(only_kernel_one.candidate.aid, AID_ONE)

    def test_partial_aid_requires_explicit_opt_in_and_full_final_select(self):
        entries = (entry(),)
        with self.assertRaises(SelectionError) as caught:
            choose_candidate(entries, application_aid=AID_PREFIX)
        self.assertEqual(caught.exception.code, "APPLICATION_NOT_FOUND")
        selected = choose_candidate(entries, application_aid=AID_PREFIX, allow_partial=True)
        self.assertEqual(selected.selection_kind, "PARTIAL")
        self.assertEqual(selected.candidate.aid, AID_ONE)
        responses = synthetic_selection_responses(InterfaceKind.CONTACTLESS, entries)
        card_interface, _ = make_interface(InterfaceKind.CONTACTLESS, responses)
        integrated = select_application(card_interface, application_aid=AID_PREFIX, allow_partial=True)
        self.assertEqual(integrated.selected.candidate.aid, AID_ONE)

    def test_partial_aid_with_multiple_matches_is_ambiguous(self):
        entries = (entry(AID_ONE), entry(AID_TWO, kernel_id=KERNEL_TWO))
        with self.assertRaises(SelectionError) as caught:
            choose_candidate(entries, application_aid=AID_PREFIX, allow_partial=True)
        self.assertEqual(caught.exception.code, "APPLICATION_SELECTION_REQUIRED")

    def test_unknown_or_unsupported_kernel_is_rejected(self):
        entries = (entry(),)
        with self.assertRaises(SelectionError) as caught:
            choose_candidate(entries, supported_kernel_ids={"7F"})
        self.assertEqual(caught.exception.code, "NO_COMMON_AID")
        with self.assertRaises(SelectionError) as caught:
            choose_candidate(entries, application_aid="A0000000099999")
        self.assertEqual(caught.exception.code, "APPLICATION_NOT_FOUND")

    def test_malformed_environment_fci_and_directory_entries(self):
        expected = {
            "FCI_TRUNCATED": "MALFORMED_FCI",
            "FCI_WRONG_ROOT": "UNEXPECTED_FCI_TEMPLATE",
            "DUPLICATE_AID": "DUPLICATE_AID",
            "DUPLICATE_TAG": "DUPLICATE_FCI_OBJECT",
            "MISSING_AID": "MISSING_FCI_OBJECT",
            "BAD_PRIORITY": "INVALID_APPLICATION_PRIORITY",
            "MISSING_KERNEL_ID": "MISSING_KERNEL_ID",
        }
        for malformed, code in expected.items():
            with self.subTest(malformed=malformed):
                interface = InterfaceKind.CONTACTLESS if malformed == "MISSING_KERNEL_ID" else InterfaceKind.CONTACT
                responses = synthetic_selection_responses(interface, (entry(),), malformed=malformed)
                card_interface, _ = make_interface(interface, responses)
                with self.assertRaises(SelectionError) as caught:
                    select_payment_environment(card_interface)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.command_sent)

    def test_duplicate_and_mismatched_final_fci_are_rejected(self):
        for malformed, code in (("DUPLICATE_DF_NAME", "DUPLICATE_FCI_OBJECT"), ("SELECTED_AID_MISMATCH", "SELECTED_AID_MISMATCH")):
            with self.subTest(malformed=malformed):
                responses = synthetic_selection_responses(InterfaceKind.CONTACT, (entry(),), malformed=malformed)
                card_interface, _ = make_interface(InterfaceKind.CONTACT, responses)
                with self.assertRaises(SelectionError) as caught:
                    select_direct_aid(card_interface, AID_ONE)
                self.assertEqual(caught.exception.code, code)

    def test_environment_not_found_is_typed(self):
        command = select_environment_command(InterfaceKind.CONTACT).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {command: TransportResult(b"", "6A82")})
        with self.assertRaises(SelectionError) as caught:
            select_payment_environment(card_interface)
        self.assertEqual(caught.exception.code, "ENVIRONMENT_NOT_FOUND")

    def test_selection_does_not_collapse_other_statuses_into_not_found(self):
        environment = select_environment_command(InterfaceKind.CONTACT).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {environment: TransportResult(b"", "6985")})
        with self.assertRaises(SelectionError) as caught:
            select_payment_environment(card_interface)
        self.assertEqual(caught.exception.code, "SELECTION_STATUS_ERROR")

        direct = select_aid_command(AID_ONE).to_bytes().hex().upper()
        card_interface, _ = make_interface(InterfaceKind.CONTACT, {direct: TransportResult(b"", "6A86")})
        with self.assertRaises(SelectionError) as caught:
            select_direct_aid(card_interface, AID_ONE)
        self.assertEqual(caught.exception.code, "SELECTION_STATUS_ERROR")

    def test_environment_select_uses_bounded_apdu_chain(self):
        entries = (entry(),)
        responses = synthetic_selection_responses(InterfaceKind.CONTACT, entries)
        command = select_environment_command(InterfaceKind.CONTACT).to_bytes().hex().upper()
        complete = responses[command]
        split = len(complete.data) // 2
        responses[command] = TransportResult(complete.data[:split], "6105")
        responses[CommandApdu(0, 0xC0, 0, 0, le=5).to_bytes().hex().upper()] = TransportResult(complete.data[split:], "9000")
        card_interface, _ = make_interface(InterfaceKind.CONTACT, responses)
        selected = select_payment_environment(card_interface)
        self.assertEqual(selected.command_count, 2)
        self.assertEqual(selected.entries[0].aid, AID_ONE)

    def test_duplicate_candidates_and_invalid_entry_models(self):
        with self.assertRaises(SelectionError) as caught:
            choose_candidate((entry(), entry()), application_aid=AID_ONE)
        self.assertEqual(caught.exception.code, "DUPLICATE_AID")
        with self.assertRaises(SelectionError):
            DirectoryEntry(AID_ONE, priority=256)
        with self.assertRaises(ValueError):
            DirectoryEntry("1234")
        with self.assertRaises(SelectionError):
            DirectoryEntry(AID_ONE, kernel_id="XYZ")


if __name__ == "__main__":
    unittest.main()
