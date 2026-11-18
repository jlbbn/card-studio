from __future__ import annotations

import json
import unittest

from card_studio.emv import (
    ActionCodes,
    AflEntry,
    ApplicationFileLocator,
    ApplicationUsageControl,
    CvmCapabilities,
    CvmContext,
    CvmError,
    CvmList,
    CvmMethod,
    CvmOutcome,
    CvmRule,
    CvmTransactionType,
    DataRecord,
    DataSource,
    DecisionInput,
    DolDataType,
    DolDocument,
    DolEntry,
    DolKind,
    EmvDataStore,
    EmvDate,
    ExternalPinHandle,
    MandatoryDataError,
    OwnershipDomain,
    PinHandles,
    RestrictionError,
    RestrictionInput,
    RestrictionPolicy,
    RiskInput,
    RiskProfile,
    ServiceType,
    TaaError,
    TaaInput,
    TaaProfile,
    TerminalAction,
    TerminalChannel,
    TransactionRegion,
    Tsi,
    TsiFlag,
    Tvr,
    TvrFlag,
    decision_input_with,
    evaluate_cvm,
    evaluate_processing_restrictions,
    evaluate_risk,
    evaluate_taa,
    install_synthetic_decision_data,
    run_data_foundation,
    run_decision_engine,
    synthetic_data_foundation_responses,
    synthetic_decision_input,
    synthetic_decision_profile,
)
from card_studio.interfaces import ContactInterface, InterfaceKind
from card_studio.readers.mock import MockReaderAdapter


AMOUNT = bytes.fromhex("000000001000")
CURRENCY = bytes.fromhex("0484")
AID_ONE = "A0000000041010"


def store_with_rules(rules=(CvmRule(0x1F, 0x00),), amount_x=0, amount_y=0, **overrides):
    store = EmvDataStore()
    return install_synthetic_decision_data(
        store,
        cvm_rules=tuple(rules),
        amount_x=amount_x,
        amount_y=amount_y,
        **overrides,
    )


def restriction_input(**overrides):
    values = {
        "terminal_date": EmvDate(26, 1, 1),
        "terminal_application_version": b"\x00\x01",
        "interface": InterfaceKind.CONTACT,
        "supported_interfaces": (InterfaceKind.CONTACT, InterfaceKind.CONTACTLESS),
        "region": TransactionRegion.DOMESTIC,
        "service": ServiceType.GOODS,
        "channel": TerminalChannel.OTHER_THAN_ATM,
        "new_card": False,
    }
    values.update(overrides)
    return RestrictionInput(**values)


def cvm_context(**overrides):
    values = {
        "amount": 1000,
        "transaction_type": CvmTransactionType.NON_CASH,
        "capabilities": CvmCapabilities(offline_pin=True, online_pin=True, signature=True, cdcvm=True),
        "handles": PinHandles(),
        "transaction_currency_matches_application": True,
        "cdcvm_performed": False,
        "online_pin_entered": False,
    }
    values.update(overrides)
    return CvmContext(**values)


def risk_input(**overrides):
    values = {
        "amount_minor": 1000,
        "transaction_type": 0,
        "interface": InterfaceKind.CONTACT,
        "operation_id": "risk-op-1",
        "profile_ref": "lab-risk:1:" + "a" * 64,
        "currency": "MXN",
        "profile": RiskProfile(),
        "exception_file_match": False,
        "merchant_forced_online": False,
        "random_sample_basis_points": None,
    }
    values.update(overrides)
    return RiskInput(**values)


def action_codes(denial=None, online=None, default=None):
    return ActionCodes(
        bytes(5) if denial is None else denial,
        bytes(5) if online is None else online,
        bytes(5) if default is None else default,
    )


def taa_input(**overrides):
    values = {
        "tvr": Tvr.cleared(),
        "tsi": Tsi.cleared(),
        "tac": ActionCodes.zeroed(),
        "iac": ActionCodes.zeroed(),
        "terminal_online_capable": True,
        "profile": TaaProfile(),
        "cvm": None,
        "risk": None,
    }
    values.update(overrides)
    return TaaInput(**values)


class RestrictionTests(unittest.TestCase):
    def test_valid_restrictions_and_tsi_unchanged(self):
        result = evaluate_processing_restrictions(store_with_rules(), restriction_input())
        self.assertFalse(result.functional_restriction)
        self.assertEqual(result.failed_checks, ())
        self.assertEqual(result.tvr.to_bytes(), bytes(5))
        self.assertEqual(result.tsi.to_bytes(), b"\x00\x00")

    def test_bcd_date_round_trip_and_leap_year(self):
        for raw, expected in ((b"\x24\x02\x29", EmvDate(24, 2, 29)), (b"\x26\x12\x31", EmvDate(26, 12, 31))):
            with self.subTest(raw=raw):
                parsed = EmvDate.parse(raw)
                self.assertEqual(parsed, expected)
                self.assertEqual(parsed.to_bytes(), raw)

    def test_invalid_bcd_dates(self):
        cases = (b"\x26\x01", b"\x26\x01\x01\x00", b"\x2a\x01\x01", b"\x26\x00\x01", b"\x26\x13\x01", b"\x26\x02\x30", b"\x25\x02\x29", b"\x26\x04\x31")
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(RestrictionError):
                    EmvDate.parse(raw)

    def test_version_expiry_effective_and_auc_flags(self):
        version = evaluate_processing_restrictions(store_with_rules(), restriction_input(terminal_application_version=b"\x00\x02"))
        self.assertIn("DIFFERENT_APPLICATION_VERSIONS", version.failed_checks)
        self.assertTrue(version.tvr.is_set(TvrFlag.DIFFERENT_APPLICATION_VERSIONS))

        expired = evaluate_processing_restrictions(store_with_rules(), restriction_input(terminal_date=EmvDate(31, 1, 1)))
        self.assertIn("EXPIRED_APPLICATION", expired.failed_checks)

        early = evaluate_processing_restrictions(store_with_rules(), restriction_input(terminal_date=EmvDate(24, 12, 31)))
        self.assertIn("APPLICATION_NOT_YET_EFFECTIVE", early.failed_checks)

        auc = evaluate_processing_restrictions(store_with_rules(auc=b"\x00\x00"), restriction_input())
        self.assertIn("REQUESTED_SERVICE_NOT_ALLOWED", auc.failed_checks)
        self.assertTrue(auc.functional_restriction)

    def test_new_card_policy_and_interface_mismatch(self):
        allowed = evaluate_processing_restrictions(store_with_rules(), restriction_input(new_card=True))
        self.assertTrue(allowed.tvr.is_set(TvrFlag.NEW_CARD))
        self.assertFalse(allowed.functional_restriction)
        rejected = evaluate_processing_restrictions(
            store_with_rules(),
            restriction_input(new_card=True),
            policy=RestrictionPolicy(allow_new_card=False),
        )
        self.assertTrue(rejected.functional_restriction)
        with self.assertRaises(RestrictionError) as caught:
            evaluate_processing_restrictions(store_with_rules(), restriction_input(interface=InterfaceKind.VIRTUAL))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_INTERFACE")

    def test_missing_malformed_objects_are_technical_and_tvr_is_not_mutated(self):
        initial = Tvr.cleared()
        initial.set(TvrFlag.FLOOR_LIMIT_EXCEEDED)
        with self.assertRaises(MandatoryDataError):
            evaluate_processing_restrictions(EmvDataStore(), restriction_input(), tvr=initial)
        self.assertEqual(initial.to_bytes(), bytes.fromhex("0000008000"))
        for overrides in (
            {"expiration_date": b"\x30\x02\x30"},
            {"effective_date": b"\x25\x13\x01"},
            {"application_version": b"\x01"},
            {"auc": b"\xff\xff"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(RestrictionError):
                    evaluate_processing_restrictions(store_with_rules(**overrides), restriction_input(), tvr=initial)
                self.assertEqual(initial.to_bytes(), bytes.fromhex("0000008000"))
        foreign = EmvDataStore()
        foreign.put(DataRecord("9F08", b"\x00\x01", DataSource.TERMINAL, OwnershipDomain.TERMINAL))
        with self.assertRaises(RestrictionError) as caught:
            evaluate_processing_restrictions(foreign, restriction_input(), tvr=initial)
        self.assertEqual(caught.exception.code, "INVALID_CARD_PROVENANCE")
        self.assertEqual(initial.to_bytes(), bytes.fromhex("0000008000"))

    def test_auc_service_channel_and_rfu(self):
        auc = ApplicationUsageControl(b"\x01\x40")
        self.assertTrue(auc.allows(TransactionRegion.INTERNATIONAL, ServiceType.CASHBACK, TerminalChannel.OTHER_THAN_ATM))
        self.assertFalse(auc.allows(TransactionRegion.DOMESTIC, ServiceType.CASHBACK, TerminalChannel.OTHER_THAN_ATM))
        with self.assertRaises(RestrictionError):
            ApplicationUsageControl(b"\xff\xc1")


class CvmTests(unittest.TestCase):
    def test_cvm_list_parse_round_trip_and_malformed(self):
        values = CvmList(1000, 5000, (CvmRule(0x42, 0x03), CvmRule(0x1F, 0x00)))
        encoded = values.encode()
        self.assertEqual(CvmList.parse(encoded), values)
        self.assertEqual(values.rules[0].method, CvmMethod.ONLINE_PIN)
        self.assertTrue(values.rules[0].continue_on_failure)
        for raw in (b"", bytes(7), bytes(8), bytes(9), bytes(10) + bytes(64 * 2 + 2)):
            with self.subTest(raw=raw):
                with self.assertRaises(CvmError):
                    CvmList.parse(raw)

    def test_no_cvm_success_results_and_tsi(self):
        result = evaluate_cvm(store_with_rules(), cvm_context())
        self.assertEqual(result.outcome, CvmOutcome.NO_CVM)
        self.assertEqual(result.cvm_results, bytes((0x1F, 0x00, 0x02)))
        self.assertTrue(result.tsi.is_set(TsiFlag.CARDHOLDER_VERIFICATION_PERFORMED))
        self.assertFalse(result.tvr.is_set(TvrFlag.CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL))

    def test_cvm_condition_codes_and_amount_boundaries(self):
        cases = (
            (0x01, {"transaction_type": CvmTransactionType.UNATTENDED_CASH}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x02, {"transaction_type": CvmTransactionType.NON_CASH}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x03, {}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x04, {"transaction_type": CvmTransactionType.MANUAL_CASH}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x05, {"transaction_type": CvmTransactionType.PURCHASE_WITH_CASHBACK}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x06, {"amount": 1000}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x07, {"amount": 1001}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x08, {"amount": 1000}, CvmOutcome.ONLINE_PIN_REQUIRED),
            (0x09, {"amount": 1001}, CvmOutcome.ONLINE_PIN_REQUIRED),
        )
        for condition, overrides, expected in cases:
            with self.subTest(condition=condition):
                store = store_with_rules((CvmRule(0x02, condition),), amount_x=1000, amount_y=1000)
                result = evaluate_cvm(store, cvm_context(**overrides))
                self.assertEqual(result.outcome, expected)
        for condition in (0x06, 0x07, 0x08, 0x09):
            with self.subTest(currency_condition=condition):
                store = store_with_rules((CvmRule(0x02, condition),), amount_x=1000, amount_y=1000)
                result = evaluate_cvm(store, cvm_context(transaction_currency_matches_application=False))
                self.assertEqual(result.outcome, CvmOutcome.NOT_SUPPORTED)

    def test_continue_on_failure_preserves_unrecognized_tvr_then_succeeds(self):
        store = store_with_rules((CvmRule(0x40, 0x00), CvmRule(0x1F, 0x00)))
        result = evaluate_cvm(store, cvm_context())
        self.assertEqual(result.outcome, CvmOutcome.NO_CVM)
        self.assertEqual(result.cvm_results, bytes((0x1F, 0x00, 0x02)))
        self.assertTrue(result.tvr.is_set(TvrFlag.UNRECOGNIZED_CVM))
        self.assertFalse(result.tvr.is_set(TvrFlag.CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL))

    def test_unknown_without_continue_fails_closed_and_explicit_fail_cvm(self):
        unknown = evaluate_cvm(store_with_rules((CvmRule(0x00, 0x00),)), cvm_context())
        self.assertEqual(unknown.outcome, CvmOutcome.NOT_SUPPORTED)
        self.assertEqual(unknown.cvm_results, bytes((0x00, 0x00, 0x01)))
        self.assertTrue(unknown.tvr.is_set(TvrFlag.CARDHOLDER_VERIFICATION_NOT_SUCCESSFUL))
        explicit = evaluate_cvm(store_with_rules((CvmRule(0x1E, 0x00),)), cvm_context())
        self.assertEqual(explicit.outcome, CvmOutcome.CVM_FAILED)
        self.assertEqual(explicit.cvm_results, bytes((0x1E, 0x00, 0x01)))

    def test_no_matching_rule_and_invalid_condition(self):
        store = store_with_rules((CvmRule(0x02, 0x01),))
        result = evaluate_cvm(store, cvm_context())
        self.assertEqual(result.outcome, CvmOutcome.NOT_SUPPORTED)
        self.assertEqual(result.cvm_results, bytes((0x3F, 0x00, 0x01)))
        with self.assertRaises(CvmError) as caught:
            evaluate_cvm(store_with_rules((CvmRule(0x1F, 0x0A),)), cvm_context())
        self.assertEqual(caught.exception.code, "INVALID_CVM_CONDITION")
        foreign = EmvDataStore()
        foreign.put(DataRecord("8E", CvmList(0, 0, (CvmRule(0x1F, 0),)).encode(), DataSource.KERNEL, OwnershipDomain.KERNEL))
        with self.assertRaises(CvmError) as caught:
            evaluate_cvm(foreign, cvm_context())
        self.assertEqual(caught.exception.code, "INVALID_CARD_PROVENANCE")

    def test_offline_pin_requires_external_handle_without_pin_capture(self):
        handles = PinHandles(offline_plaintext=ExternalPinHandle("test://offline-pin-handle"))
        result = evaluate_cvm(
            store_with_rules((CvmRule(0x01, 0x00),)),
            cvm_context(handles=handles),
        )
        self.assertEqual(result.outcome, CvmOutcome.OFFLINE_PIN_REQUIRED)
        self.assertFalse(result.to_evidence()["pin_captured"])
        self.assertNotIn("test://", json.dumps(result.to_evidence()))
        missing = evaluate_cvm(store_with_rules((CvmRule(0x01, 0x00),)), cvm_context())
        self.assertEqual(missing.outcome, CvmOutcome.NOT_SUPPORTED)
        self.assertTrue(missing.tvr.is_set(TvrFlag.PIN_NOT_ENTERED))
        with self.assertRaises(CvmError):
            ExternalPinHandle("1234")

    def test_combined_offline_pin_signature_online_pin_signature_and_cdcvm(self):
        combined = evaluate_cvm(
            store_with_rules((CvmRule(0x03, 0x00),)),
            cvm_context(handles=PinHandles(offline_plaintext=ExternalPinHandle("test://offline"))),
        )
        self.assertEqual(combined.outcome, CvmOutcome.OFFLINE_PIN_REQUIRED)
        self.assertTrue(combined.signature_required)
        online = evaluate_cvm(
            store_with_rules((CvmRule(0x02, 0x00),)),
            cvm_context(online_pin_entered=True),
        )
        self.assertEqual(online.outcome, CvmOutcome.ONLINE_PIN_REQUIRED)
        self.assertTrue(online.tvr.is_set(TvrFlag.ONLINE_PIN_ENTERED))
        signature = evaluate_cvm(store_with_rules((CvmRule(0x06, 0x00),)), cvm_context())
        self.assertEqual(signature.outcome, CvmOutcome.SIGNATURE)
        self.assertTrue(signature.signature_required)
        cdcvm = evaluate_cvm(store_with_rules((CvmRule(0x20, 0x00),)), cvm_context(cdcvm_performed=True))
        self.assertEqual(cdcvm.outcome, CvmOutcome.CDCVM)
        unsupported = evaluate_cvm(store_with_rules((CvmRule(0x20, 0x00),)), cvm_context())
        self.assertEqual(unsupported.outcome, CvmOutcome.NOT_SUPPORTED)


class RiskTests(unittest.TestCase):
    def test_floor_and_interface_boundaries(self):
        profile = RiskProfile(floor_limit_minor=1000, interface_limits_minor={InterfaceKind.CONTACT: 900})
        exact = evaluate_risk(risk_input(profile=profile, amount_minor=900))
        self.assertFalse(exact.online_required)
        above_interface = evaluate_risk(risk_input(profile=profile, amount_minor=901))
        self.assertTrue(above_interface.online_required)
        self.assertIn("INTERFACE_LIMIT", above_interface.reasons)
        above_floor = evaluate_risk(risk_input(profile=profile, amount_minor=1001))
        self.assertIn("FLOOR_LIMIT", above_floor.reasons)
        self.assertTrue(above_floor.tvr.is_set(TvrFlag.FLOOR_LIMIT_EXCEEDED))
        self.assertTrue(above_floor.tsi.is_set(TsiFlag.TERMINAL_RISK_MANAGEMENT_PERFORMED))

    def test_transaction_type_override_exception_and_merchant(self):
        profile = RiskProfile(floor_limit_minor=1000, transaction_type_floor_limits={9: 2000})
        self.assertFalse(evaluate_risk(risk_input(profile=profile, transaction_type=9, amount_minor=1500)).online_required)
        exception = evaluate_risk(risk_input(profile=profile, exception_file_match=True))
        self.assertIn("EXCEPTION_FILE", exception.reasons)
        self.assertTrue(exception.tvr.is_set(TvrFlag.CARD_ON_EXCEPTION_FILE))
        merchant = evaluate_risk(risk_input(profile=profile, merchant_forced_online=True))
        self.assertIn("MERCHANT_FORCED_ONLINE", merchant.reasons)
        self.assertTrue(merchant.tvr.is_set(TvrFlag.MERCHANT_FORCED_TRANSACTION_ONLINE))

    def test_deterministic_random_selection_boundaries(self):
        profile = RiskProfile(random_selection_percent=5)
        selected = evaluate_risk(risk_input(profile=profile, random_sample_basis_points=499))
        self.assertTrue(selected.online_required)
        self.assertIn("DETERMINISTIC_SELECTION", selected.reasons)
        boundary = evaluate_risk(risk_input(profile=profile, random_sample_basis_points=500))
        self.assertFalse(boundary.online_required)
        first = evaluate_risk(risk_input(profile=profile))
        second = evaluate_risk(risk_input(profile=profile))
        self.assertEqual(first.deterministic_sample, second.deterministic_sample)
        self.assertEqual(first.reasons, second.reasons)

    def test_invalid_risk_configuration_and_sample(self):
        with self.assertRaises(ValueError):
            RiskProfile(random_selection_percent=101)
        with self.assertRaises(ValueError):
            RiskProfile(floor_limit_minor=-1)
        with self.assertRaises(ValueError):
            evaluate_risk(risk_input(random_sample_basis_points=10000))
        with self.assertRaises(ValueError):
            evaluate_risk(risk_input(amount_minor=-1))


class TaaTests(unittest.TestCase):
    def test_denial_precedes_online(self):
        tvr = Tvr.cleared()
        tvr.set(TvrFlag.SDA_FAILED)
        tac = action_codes(denial=bytes.fromhex("4000000000"), online=bytes.fromhex("4000000000"))
        result = evaluate_taa(taa_input(tvr=tvr, tac=tac))
        self.assertEqual(result.proposed_action, TerminalAction.AAC)
        self.assertEqual(result.reason, "DENIAL_ACTION_CODE_MATCHED")

    def test_online_default_and_no_match_profiles(self):
        tvr = Tvr.cleared()
        tvr.set(TvrFlag.FLOOR_LIMIT_EXCEEDED)
        online = action_codes(online=bytes.fromhex("0000008000"))
        result = evaluate_taa(taa_input(tvr=tvr, iac=online))
        self.assertEqual(result.proposed_action, TerminalAction.ARQC)
        fallback_profile = TaaProfile(fallback_when_offline_unable_online=TerminalAction.TC)
        default = action_codes(default=bytes.fromhex("0000008000"))
        offline = evaluate_taa(taa_input(tvr=tvr, iac=default, terminal_online_capable=False, profile=fallback_profile))
        self.assertEqual(offline.proposed_action, TerminalAction.TC)
        no_match_online = evaluate_taa(taa_input(profile=TaaProfile(no_match_default_when_online_capable=TerminalAction.TC)))
        self.assertEqual(no_match_online.proposed_action, TerminalAction.TC)
        no_match_offline = evaluate_taa(taa_input(terminal_online_capable=False))
        self.assertEqual(no_match_offline.proposed_action, TerminalAction.AAC)

    def test_action_code_and_profile_validation(self):
        with self.assertRaises(TaaError):
            ActionCodes(bytes(4), bytes(5), bytes(5))
        with self.assertRaises(TaaError):
            ActionCodes(bytes.fromhex("0000000001"), bytes(5), bytes(5))
        with self.assertRaises(TaaError):
            TaaProfile(fallback_when_offline_unable_online=TerminalAction.ARQC)
        with self.assertRaises(TaaError):
            TaaProfile(no_match_default_when_online_capable=TerminalAction.AAC)
        with self.assertRaises(TaaError):
            TaaProfile(no_match_default_when_offline_only=TerminalAction.ARQC)


class DecisionEngineTests(unittest.TestCase):
    def test_default_decision_engine_gate_and_history(self):
        store = store_with_rules()
        result = run_decision_engine(store, synthetic_decision_profile(), synthetic_decision_input())
        self.assertEqual(result.gate_status, "PASS")
        self.assertEqual(result.proposed_action, TerminalAction.ARQC)
        self.assertEqual(result.to_evidence()["status"], "PASS")
        self.assertEqual([step["stage"] for step in result.to_evidence()["history"]], [
            "PROCESSING_RESTRICTIONS", "CVM_PROCESSING", "TERMINAL_RISK_MANAGEMENT", "TERMINAL_ACTION_ANALYSIS",
        ])
        self.assertNotIn("execution_state", result.to_evidence())

    def test_restriction_denial_iac_drives_aac_without_technical_failure(self):
        iac = action_codes(denial=bytes.fromhex("0040000000"))
        store = store_with_rules(iac=iac)
        result = run_decision_engine(
            store,
            synthetic_decision_profile(),
            decision_input_with(synthetic_decision_input(), terminal_date=EmvDate(31, 1, 1)),
        )
        self.assertTrue(result.restrictions.functional_restriction)
        self.assertEqual(result.proposed_action, TerminalAction.AAC)
        self.assertEqual(result.gate_status, "PASS")

    def test_cvm_denial_and_risk_online_paths(self):
        denial = action_codes(denial=bytes.fromhex("0000800000"))
        store = store_with_rules((CvmRule(0x1E, 0x00),), iac=denial)
        declined = run_decision_engine(store, synthetic_decision_profile(), synthetic_decision_input())
        self.assertEqual(declined.cvm.outcome, CvmOutcome.CVM_FAILED)
        self.assertEqual(declined.proposed_action, TerminalAction.AAC)

        online = action_codes(online=bytes.fromhex("0000008000"))
        profile = synthetic_decision_profile(
            tac=online,
            risk=RiskProfile(floor_limit_minor=500),
        )
        store = store_with_rules()
        approved = run_decision_engine(store, profile, synthetic_decision_input())
        self.assertTrue(approved.risk.online_required)
        self.assertEqual(approved.proposed_action, TerminalAction.ARQC)

    def test_missing_decision_data_is_technical_not_declined(self):
        store = store_with_rules()
        store._records.pop("8E")
        with self.assertRaises(MandatoryDataError):
            run_decision_engine(store, synthetic_decision_profile(), synthetic_decision_input())

    def test_decision_evidence_redacts_handles_and_values(self):
        handles = PinHandles(offline_plaintext=ExternalPinHandle("test://sensitive-handle"))
        store = store_with_rules((CvmRule(0x01, 0x00),))
        result = run_decision_engine(
            store,
            synthetic_decision_profile(),
            decision_input_with(synthetic_decision_input(), pin_handles=handles),
        )
        evidence = json.dumps(result.to_evidence())
        self.assertNotIn("test://", evidence)
        self.assertNotIn("raw_apdu", evidence)
        self.assertNotIn("pin_value", evidence)

    def test_data_foundation_to_decision_engine_integration(self):
        from tests.test_agent_a_p0_block2 import foundation_responses, make_store

        store = make_store()
        adapter = MockReaderAdapter(responses=foundation_responses())
        session = adapter.open(adapter.reader_info.reader_id, "decision-integration")
        card_interface = ContactInterface(adapter, session).open()
        pdol = DolDocument(DolKind.PDOL, (
            DolEntry("9F02", 6, DolDataType.NUMERIC),
            DolEntry("5F2A", 2, DolDataType.NUMERIC),
        ))
        foundation = run_data_foundation(card_interface, pdol, store, required_tags=("5F24", "8C"))
        install_synthetic_decision_data(store, expiration_date=b"\x30\x01\x01")
        decision = run_decision_engine(store, synthetic_decision_profile(), synthetic_decision_input())
        self.assertEqual(foundation.gate_status, "PASS")
        self.assertEqual(decision.gate_status, "PASS")
        self.assertEqual(decision.restrictions.tvr.to_bytes(), bytes(5))


if __name__ == "__main__":
    unittest.main()
