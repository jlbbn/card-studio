from __future__ import annotations

import json
import unittest

from card_studio.emv import (
    ApplicationFileLocator,
    DataRecord,
    DataSource,
    EmvDataStore,
    OwnershipDomain,
    ReadRecordError,
    ResponseApdu,
    build_gpo_command,
    parse_gpo_response,
    read_afl_records,
    read_record_command,
)
from card_studio.emv.unpredictable import UnpredictableNumberBoundary, UnpredictableNumberError
from card_studio.interfaces import ContactInterface
from card_studio.kernels import InternalEmvKernel, KernelError
from card_studio.profiles import CardProfile
from card_studio.profiles.authority import (
    ConfigurationAuthorityError,
    configuration_provenance,
    expected_configuration_mappings,
)
from card_studio.readers.base import TransportResult
from card_studio.tlv import TlvNode, TlvNonCanonicalError, encode_tlv, parse_tlv, safe_tlv_evidence
from tests.test_agent_a_kernel import make_profile, make_request
from tests.test_agent_a_p0_block1 import make_interface
from tests.test_agent_a_p0_block6 import case_fixture


class K26BerTlvHardeningTests(unittest.TestCase):
    def test_canonical_high_tag_and_unknown_value_preservation(self):
        encoded = bytes.fromhex("9F810201AA")
        node = parse_tlv(encoded, context="MUTATION_CORPUS")[0]
        self.assertEqual(node.tag, "9F8102")
        self.assertEqual(node.value, b"\xaa")
        self.assertEqual(encode_tlv(node), encoded)
        self.assertEqual(node.context, "MUTATION_CORPUS/9F8102")
        evidence = json.dumps(safe_tlv_evidence((node,)))
        self.assertIn("MUTATION_CORPUS/9F8102", evidence)
        self.assertNotIn("aa", evidence.lower())

    def test_invalid_tag_mutations_are_rejected_at_parse_and_model_boundaries(self):
        cases = (
            bytes.fromhex("0000"),
            bytes.fromhex("FF00"),
            bytes.fromhex("1F0000"),
            bytes.fromhex("9F0000"),
            bytes.fromhex("9F800100"),
            bytes.fromhex("9F8102"),
        )
        for raw in cases:
            with self.subTest(raw=raw.hex()):
                with self.assertRaises(ValueError):
                    parse_tlv(raw)
        for tag in ("00", "FF", "1F00", "9F00", "9F80", "9F8102FF01"):
            with self.subTest(tag=tag):
                with self.assertRaises(ValueError):
                    TlvNode(tag, b"")
        with self.assertRaises(TlvNonCanonicalError):
            parse_tlv(bytes.fromhex("4F8101"))

    def test_contextual_duplicate_gpo_object_is_not_accepted(self):
        response = ResponseApdu(encode_tlv(TlvNode("77", children=(
            TlvNode("82", b"\x00\x00"),
            TlvNode("82", b"\x00\x00"),
            TlvNode("94", bytes.fromhex("08010100")),
        ))), 0x90, 0)
        with self.assertRaises(ValueError) as caught:
            parse_gpo_response(response)
        self.assertEqual(caught.exception.code, "DUPLICATE_GPO_OBJECT")

    def test_failed_second_record_does_not_partially_store_first_record(self):
        afl = ApplicationFileLocator.parse(bytes.fromhex("08010200"))
        store = EmvDataStore()
        original = DataRecord("9F02", bytes.fromhex("000000000001"), DataSource.TERMINAL, OwnershipDomain.TERMINAL)
        store.put(original)
        first = read_record_command(afl.record_plan()[0], afl).to_bytes().hex().upper()
        second = read_record_command(afl.record_plan()[1], afl).to_bytes().hex().upper()
        responses = {
            first: TransportResult(encode_tlv(TlvNode("70", children=(TlvNode("DF01", b"\x01"),))), "9000"),
            second: TransportResult(encode_tlv(TlvNode("70", children=(
                TlvNode("DF02", b"\x02"),
                TlvNode("DF02", b"\x02"),
            ))), "9000"),
        }
        card_interface, _ = make_interface("CONTACT", responses)
        with self.assertRaises(ReadRecordError) as caught:
            read_afl_records(card_interface, afl, store)
        self.assertEqual(caught.exception.code, "DUPLICATE_RECORD_TAG")
        self.assertIsNone(store.get("DF01"))
        self.assertIsNone(store.get("DF02"))
        self.assertEqual(store.snapshot(), {"9F02": original})


def authoritative_profile(profile, **authority_overrides):
    mappings = expected_configuration_mappings(profile)
    raw = profile.to_dict()
    authority = {
        "schema_version": 1,
        "configuration_version": 7,
        "authority_ref": "lab://kernel/config-v7",
        "mode": "LAB_ONLY",
        "scheme_family": profile.scheme_family,
        "kernel_ref": profile.kernel_requirements["kernel_ref"],
        "mappings": mappings,
    }
    authority.update(authority_overrides)
    raw["scheme_parameters"] = dict(raw["scheme_parameters"])
    raw["scheme_parameters"]["configuration_authority"] = authority
    raw["profile_digest"] = ""
    return CardProfile.from_dict(raw)


class K37ConfigurationAuthorityTests(unittest.TestCase):
    def test_manifest_binds_effective_dol_action_oda_and_capk_configuration(self):
        fixture = case_fixture("CONTACT")
        profile = authoritative_profile(fixture["profile"])
        provenance = configuration_provenance(profile, "internal-emv-lab-v1")
        self.assertEqual(provenance["configuration_version"], 7)
        self.assertEqual(provenance["profile_digest"], profile.profile_digest)
        self.assertEqual(
            set(provenance["mapping_sha256"]),
            {"PDOL", "CDOL1", "CDOL2", "TAC", "IAC", "ODA", "CAPK"},
        )
        self.assertEqual(provenance["physical_validation"], "NOT_EXECUTED")

    def test_missing_mapping_tampered_digest_and_non_lab_authority_fail_closed(self):
        fixture = case_fixture("CONTACT")
        mappings = expected_configuration_mappings(fixture["profile"])
        incomplete = dict(mappings)
        incomplete.pop("PDOL")
        tampered = dict(mappings)
        tampered["TAC"] = "0" * 64
        cases = (
            {"mappings": incomplete},
            {"mappings": tampered},
            {"authority_ref": "https://unreviewed.example/config"},
            {"scheme_family": "other"},
            {"configuration_version": 0},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ConfigurationAuthorityError):
                    authoritative_profile(fixture["profile"], **overrides)

    def test_internal_execution_emits_provenance_and_legacy_stays_opt_out(self):
        fixture = case_fixture("CONTACT")
        fixture["profile"] = authoritative_profile(fixture["profile"])
        fixture["request"] = make_request(
            fixture["profile"],
            operation_id="op-p1-authority",
            interface="CONTACT",
            application_aid=fixture["request"].application_aid,
        )
        adapter = fixture["reader"]
        reader_session = adapter.open(adapter.reader_info.reader_id, "p1-authority-reader")
        card_interface = ContactInterface(adapter, reader_session).open()
        kernel = InternalEmvKernel()
        session = kernel.open_session(fixture["request"], card_interface, fixture["profile"])
        result = kernel.execute(session, fixture["request"])
        self.assertEqual(result.outcome, "APPROVED")
        self.assertEqual(result.details["configuration_provenance"]["mode"], "LAB_ONLY")

        legacy_profile = make_profile()
        self.assertIsNone(configuration_provenance(legacy_profile, "internal-emv-lab-v1"))


class K38UnpredictableNumberTests(unittest.TestCase):
    def test_injected_boundary_accepts_varied_values_and_only_exposes_digest(self):
        values = iter((b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"))
        boundary = UnpredictableNumberBoundary(lambda: next(values))
        value, evidence = boundary.generate()
        self.assertEqual(value, b"\x01\x02\x03\x04")
        boundary.generate()
        serialized = json.dumps(evidence.to_evidence())
        self.assertEqual(evidence.value_length, 4)
        self.assertNotIn(value.hex(), serialized.lower())
        self.assertEqual(evidence.mode, "LAB_ONLY")

    def test_zero_repeated_short_and_low_diversity_outputs_are_rejected(self):
        cases = (
            (b"\x00\x00\x00\x00", "WEAK_UN_OUTPUT"),
            (b"\xff\xff\xff\xff", "WEAK_UN_OUTPUT"),
            (b"\x01\x01\x01\x01", "WEAK_UN_OUTPUT"),
            (b"\x01\x02\x03", "INVALID_UN_LENGTH"),
        )
        for value, code in cases:
            with self.subTest(value=value.hex(), code=code):
                with self.assertRaises(UnpredictableNumberError) as caught:
                    UnpredictableNumberBoundary(lambda value=value: value).generate()
                self.assertEqual(caught.exception.code, code)
        repeated = UnpredictableNumberBoundary(lambda: b"\x01\x02\x03\x04")
        repeated.generate()
        with self.assertRaises(UnpredictableNumberError) as caught:
            repeated.generate()
        self.assertEqual(caught.exception.code, "REPEATED_UN_OUTPUT")

    def un_fixture(self):
        fixture = case_fixture("CONTACT")
        raw = fixture["profile"].to_dict()
        parameters = dict(raw["scheme_parameters"])
        execution = dict(parameters["emv_execution"])
        execution["pdol"] = list(execution["pdol"]) + [{"tag": "9F37", "length": 4, "data_type": "BINARY"}]
        execution["unpredictable_number"] = {"required": True}
        parameters["emv_execution"] = execution
        raw["scheme_parameters"] = parameters
        raw["profile_digest"] = ""
        fixture["profile"] = CardProfile.from_dict(raw)
        fixture["request"] = make_request(
            fixture["profile"],
            operation_id="op-p1-un",
            interface="CONTACT",
            application_aid=fixture["request"].application_aid,
        )
        from card_studio.emv.runtime import EmvFlowConfig

        config = EmvFlowConfig.from_profile(fixture["profile"])
        store = config.new_store(fixture["request"], b"\x01\x02\x03\x04")
        old_gpo = next(key for key in fixture["responses"] if key.startswith("80A8"))
        response = fixture["responses"].pop(old_gpo)
        new_gpo = build_gpo_command(config.pdol, store).to_bytes().hex().upper()
        fixture["responses"][new_gpo] = response
        fixture["reader"].responses = fixture["responses"]
        fixture["gpo"] = bytes.fromhex(new_gpo)
        return fixture

    def test_kernel_injects_un_into_real_gpo_and_weak_output_sends_no_apdu(self):
        fixture = self.un_fixture()
        adapter = fixture["reader"]
        reader_session = adapter.open(adapter.reader_info.reader_id, "p1-un-reader")
        card_interface = ContactInterface(adapter, reader_session).open()
        kernel = InternalEmvKernel(unpredictable_number_source=lambda: b"\x01\x02\x03\x04")
        session = kernel.open_session(fixture["request"], card_interface, fixture["profile"])
        result = kernel.execute(session, fixture["request"])
        self.assertEqual(result.outcome, "APPROVED")
        self.assertIn(fixture["gpo"], adapter.transmitted)
        self.assertEqual(result.details["unpredictable_number"]["value_length"], 4)
        self.assertNotIn("01020304", json.dumps(dict(result.details)).lower())

        weak_fixture = self.un_fixture()
        weak_adapter = weak_fixture["reader"]
        weak_session = weak_adapter.open(weak_adapter.reader_info.reader_id, "p1-weak-un-reader")
        weak_interface = ContactInterface(weak_adapter, weak_session).open()
        weak_kernel = InternalEmvKernel(unpredictable_number_source=lambda: bytes(4))
        opened = weak_kernel.open_session(weak_fixture["request"], weak_interface, weak_fixture["profile"])
        weak_result = weak_kernel.execute(opened, weak_fixture["request"])
        self.assertEqual(weak_result.error_code, "WEAK_UN_OUTPUT")
        self.assertEqual(weak_adapter.transmitted, [])

        no_source = InternalEmvKernel()
        with self.assertRaises(KernelError) as caught:
            no_source.open_session(weak_fixture["request"], weak_interface, weak_fixture["profile"])
        self.assertEqual(caught.exception.code, "UN_SOURCE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
