from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.emv.c8_recovery import (
    RecoverAcError,
    TornTransactionJournal,
    build_recover_ac_command,
    recover_ac,
)
from card_studio.emv.card_action import synthetic_generate_ac_response
from card_studio.emv.data import DataRecord, DataSource, EmvDataStore, OwnershipDomain
from card_studio.emv.dol import DolDataType, DolDocument, DolEntry, DolKind
from card_studio.emv.selection import select_aid_command, select_environment_command
from card_studio.interfaces.contactless import ContactlessInterface
from card_studio.host.mock import MockAuthorizationHost
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.authority import (
    SpecificationAuthorityError,
    SpecificationAuthorityRegistry,
    SpecificationRequirement,
)
from card_studio.l3.internal import internal_profile
from card_studio.l3.p1 import P1L3Runner
from card_studio.l3.scenarios import LabKernelAdapter
from card_studio.money import Money
from card_studio.simulator.icc import SyntheticIccReader
from card_studio.simulator.scenarios import p1_scenarios
from card_studio.simulator.terminal import TerminalScenario, TerminalStep
from card_studio.transactions.model import TransactionRequest


class P1SimulatorTests(unittest.TestCase):
    def test_t18_t20_t21_internal_matrix_evidence_and_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = P1L3Runner(root).run()
            self.assertEqual((report["passed"], report["total"]), (20, 20))
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["core_baseline"], {"passed": 27, "total": 27, "status": "PASS"})
            self.assertEqual(report["identity"]["kernel_adapter"], "InternalEmvKernel")
            self.assertEqual(report["mode"], "SIMULATED/LAB_ONLY")
            self.assertEqual(report["authority"], "EXTERNAL_UNVERIFIED")
            self.assertEqual(report["physical_validation"], "NOT_EXECUTED")
            self.assertEqual(CardStudioApplication(data_dir=root).p1_l3_readiness()["status"], "PASS")
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertEqual(len(manifest["cases"]), 20)
            self.assertEqual(manifest["matrix_sha256"], hashlib.sha256((root / "matrix.json").read_bytes()).hexdigest())
            for case in manifest["cases"]:
                evidence = root / "cases" / case["evidence_reference"]
                self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)
                self.assertEqual(case["evidence_sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
                serialized = evidence.read_text()
                self.assertNotIn('"raw_apdu"', serialized)
                self.assertNotIn('"commands"', serialized)
                self.assertIn('"command_sha256"', serialized)

    def test_t18_fake_matrix_is_non_conformant_and_cannot_pass_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = P1L3Runner(root, kernel_factory=LabKernelAdapter).run()
            self.assertEqual((report["passed"], report["total"]), (0, 20))
            self.assertEqual(report["status"], "NON_CONFORMANT")
            self.assertTrue(all(case["status"] == "NON_CONFORMANT" for case in report["cases"]))
            readiness = CardStudioApplication(data_dir=root).p1_l3_readiness()
            self.assertEqual(readiness["status"], "NON_CONFORMANT")
            self.assertFalse(readiness["readiness_eligible"])

    def test_t13_t16_matrix_executes_multiprocess_linked_and_cancel_integrations(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = P1L3Runner(Path(temporary)).run()
            cases = {case["scenario_id"]: case for case in report["cases"]}
            required_assertions = {
                "p1-multiprocess-idempotency": {
                    "real_multiprocess_count", "distinct_process_ids", "process_exit_codes",
                    "one_durable_transaction", "one_kernel_execution", "idempotent_result_convergence",
                    "single_host_authorization", "winner_apdu_sequence", "duplicate_apdu_count",
                },
                "p1-concurrent-refund": {
                    "one_linked_winner", "linked_negative_result", "single_host_refund",
                    "winner_generate_ac_count", "loser_apdu_count",
                },
                "p1-concurrent-reversal": {
                    "one_linked_winner", "linked_negative_result", "single_host_reverse",
                    "winner_generate_ac_count", "loser_apdu_count",
                },
                "p1-cancel-exchange": {
                    "cooperative_cancel_latency", "no_post_cancellation_approval",
                    "single_kernel_execution", "completed_apdu_count", "host_authorization_count",
                },
            }
            for scenario_id, required in required_assertions.items():
                case = cases[scenario_id]
                self.assertEqual(case["status"], "PASS")
                names = {assertion["name"] for assertion in case["assertions"]}
                self.assertTrue(required <= names, (scenario_id, required - names))

            multiprocess = cases["p1-multiprocess-idempotency"]["result"]
            self.assertEqual(multiprocess["transaction_count"], 1)
            self.assertEqual(multiprocess["host_authorization_count"], 1)
            self.assertEqual(multiprocess["kernel_execution_counts"], [0, 1])
            self.assertEqual(multiprocess["processes"]["count"], 2)
            self.assertEqual(len(set(multiprocess["processes"]["process_ids"])), 2)
            self.assertEqual(multiprocess["processes"]["exit_codes"], [0, 0])
            self.assertEqual(len(multiprocess["convergence_signatures"]), 2)
            self.assertEqual(
                multiprocess["convergence_signatures"][0],
                multiprocess["convergence_signatures"][1],
            )

            refund = cases["p1-concurrent-refund"]["result"]
            self.assertEqual(refund["expected_negative_error"], "EXCESSIVE_REFUND")
            self.assertEqual(refund["host_linked_operation_count"], 1)
            self.assertEqual(refund["approved_amount_minor"], 600)
            self.assertEqual(
                {item["execution_state"] for item in refund["results"]},
                {"SUCCEEDED", "FAILED"},
            )

            reversal = cases["p1-concurrent-reversal"]["result"]
            self.assertEqual(reversal["expected_negative_error"], "DUPLICATE_REVERSAL")
            self.assertEqual(reversal["host_linked_operation_count"], 1)
            self.assertEqual(reversal["approved_amount_minor"], 1000)
            self.assertEqual(
                {item["execution_state"] for item in reversal["results"]},
                {"SUCCEEDED", "FAILED"},
            )

            cancelled = cases["p1-cancel-exchange"]["result"]
            self.assertEqual(cancelled["execution_state"], "UNCERTAIN")
            self.assertEqual(cancelled["kernel_execution_count"], 1)
            self.assertEqual(cancelled["apdu_count"], 0)
            self.assertEqual(cancelled["host_authorization_count"], 0)
            self.assertNotEqual(cancelled["final_result"]["functional_result"], "APPROVED")

    def test_k28_matrix_torn_recovery_and_atc_arqc_negatives(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = P1L3Runner(Path(temporary)).run()
            case = next(
                item for item in report["cases"]
                if item["scenario_id"] == "p1-contactless-torn-recovery"
            )
            self.assertEqual(case["status"], "PASS")
            names = {assertion["name"] for assertion in case["assertions"]}
            self.assertTrue({
                "durable_torn_record", "restart_torn_record", "recover_instruction_sequence",
                "exact_recover_command", "exact_drdol_payload", "recovered_expected_atc",
                "recovered_tc_not_arqc", "recovered_journal_status", "replay_blocked",
                "replay_no_additional_apdu", "no_generate_ac_during_recovery",
                "unsafe_retry_blocked", "no_second_host_authorization",
            } <= names)
            result = case["result"]
            self.assertEqual(result["recovery"]["command_count"], 1)
            self.assertEqual(result["recovery"]["cryptogram_result"]["atc"], "0001")
            self.assertEqual(result["recovery"]["cryptogram_result"]["cryptogram_type"], "TC")
            self.assertFalse(result["recovery"]["second_generate_ac"])
            self.assertEqual(result["persisted_record"]["status"], "RECOVERED")
            self.assertEqual(result["replay_error"], "RECOVER_AC_ALREADY_COMPLETED")
            self.assertEqual(result["retry_error"], "NON_RETRYABLE")
            self.assertEqual(result["host_authorization_count"], 0)
            self.assertEqual(case["icc"]["before_restart"]["stage"], "TORN")
            self.assertEqual(case["icc"]["after_restart"]["stage"], "RECOVERED")
            self.assertEqual(case["icc"]["after_restart"]["instructions"], [0xD0])

        for operation, cid, response_atc, expected_error in (
            ("recover-atc-mismatch", 0x40, b"\x00\x02", "RECOVER_AC_ATC_MISMATCH"),
            ("recover-arqc", 0x80, b"\x00\x01", "RECOVER_AC_SECOND_ONLINE_REQUEST"),
        ):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                journal = TornTransactionJournal(Path(temporary) / "torn.sqlite3")
                journal.mark_torn(operation, "lab-profile:1:" + "0" * 64, b"\x00\x01")
                drdol = DolDocument(DolKind.DRDOL, (DolEntry("9F37", 4, DolDataType.BINARY),))
                store = EmvDataStore()
                store.put(DataRecord(
                    "9F37", b"\x01\x02\x03\x04", DataSource.TERMINAL, OwnershipDomain.TERMINAL,
                ))
                command = build_recover_ac_command(drdol, store)
                reader = SyntheticIccReader()
                reader.prepare_recover_ac(
                    command,
                    synthetic_generate_ac_response(cid, 80, operation, atc=response_atc),
                    b"\x00\x01",
                )
                session = reader.open(reader.reader_info.reader_id, operation)
                interface = ContactlessInterface(reader, session).open()
                try:
                    with self.assertRaises(RecoverAcError) as caught:
                        recover_ac(interface, journal, operation, drdol, store)
                finally:
                    interface.close()
                self.assertEqual(caught.exception.code, expected_error)
                self.assertEqual(journal.get(operation).status, "TORN")
                self.assertEqual(reader.instructions, [0xD0])
                self.assertEqual(len(reader.command_digests), 1)

    def test_t21_tampered_p1_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            P1L3Runner(root).run()
            path = root / "cases" / "p1-contact-approved.json"
            value = json.loads(path.read_text())
            value["status"] = "FAIL"
            path.write_text(json.dumps(value))
            self.assertEqual(CardStudioApplication(data_dir=root).p1_l3_readiness()["status"], "FAIL")

    def test_t19_malformed_corpus_and_faults_are_real_kernel_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = P1L3Runner(Path(temporary)).run()
            cases = {case["scenario_id"]: case for case in report["cases"]}
            expected = {
                "p1-malformed-select": "MALFORMED_FCI",
                "p1-malformed-gpo": "MALFORMED_GPO_RESPONSE",
                "p1-malformed-record": "MALFORMED_RECORD",
                "p1-malformed-gac": "MALFORMED_GAC_RESPONSE",
            }
            for scenario_id, error in expected.items():
                self.assertEqual(cases[scenario_id]["status"], "PASS")
                self.assertEqual(cases[scenario_id]["result"]["execution_state"], "FAILED")
                self.assertEqual(cases[scenario_id]["result"]["error_code"], error)
            self.assertEqual(cases["p1-contactless-torn"]["icc"]["stage"], "TORN")
            self.assertTrue(cases["p1-contactless-torn"]["icc"]["torn"])
            self.assertEqual(cases["p1-contactless-torn"]["result"]["execution_state"], "UNCERTAIN")
            self.assertEqual(cases["p1-card-removed"]["result"]["error_code"], "CARD_REMOVED")
            self.assertEqual(cases["p1-reader-removed"]["result"]["error_code"], "READER_REMOVED")

    def test_t19_directories_afl_atc_reset_and_un_faults(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = P1L3Runner(Path(temporary)).run()
            cases = {case["scenario_id"]: case for case in report["cases"]}
            contact = cases["p1-contact-approved"]["icc"]
            contactless = cases["p1-contactless-online"]["icc"]
            self.assertEqual(contact["directory"], "PSE")
            self.assertEqual(contactless["directory"], "PPSE")
            self.assertEqual(contact["afl"], "08010100")
            self.assertEqual(contact["records"], ["1:1"])
            self.assertEqual(contact["atc"], 1)
            self.assertEqual(contactless["atc"], 1)
            reset = cases["p1-card-reset"]["icc"]
            self.assertEqual(reset["stage"], "RESET")
            self.assertEqual(reset["atc"], 0)
            self.assertEqual(cases["p1-un-valid"]["status"], "PASS")
            self.assertEqual(cases["p1-un-weak"]["result"]["error_code"], "WEAK_UN_OUTPUT")
            self.assertEqual(cases["p1-un-repeated"]["result"]["error_code"], "REPEATED_UN_OUTPUT")
            self.assertEqual(cases["p1-un-repeated"]["result"]["execution_state"], "FAILED")

    def test_t21_exact_apdu_bytes_host_reference_and_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel = InternalEmvKernel()
            profile = internal_profile(kernel, "exact-protocol", "CONTACT", "online_required")
            profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
            request = TransactionRequest.create(
                money=Money(1000, "MXN", 2),
                profile_ref=profile_ref,
                interface="CONTACT",
                kernel_ref=kernel.kernel_ref,
                scenario="online_required",
                operation_id="exact-protocol",
                idempotency_key="key-exact-protocol",
            )
            reader = SyntheticIccReader()
            reader.configure(profile, request)
            host = MockAuthorizationHost(root / "host.sqlite3")
            app = CardStudioApplication(data_dir=root, profiles=[profile], kernels=kernel, reader=reader, host=host)
            result = app.create_transaction(request)
            self.assertEqual(result["execution_state"], "SUCCEEDED")
            self.assertEqual(reader.commands[0], select_environment_command("CONTACT").to_bytes())
            self.assertEqual(reader.commands[1], select_aid_command(profile.applications[0].aid).to_bytes())
            self.assertEqual(reader.commands[3].hex().upper(), "00B2010C00")
            self.assertEqual([raw[1] for raw in reader.commands], [0xA4, 0xA4, 0xA8, 0xB2, 0xAE, 0xAE])
            response = host.query(request.operation_id)
            self.assertEqual(result["details"]["host_reference"], response["host_reference"])
            self.assertEqual(result["details"]["host_response_timestamp"], response["timestamp"])
            self.assertEqual(host.operation_count("authorize"), 1)

    def test_cancel_exchange_is_cooperative_and_never_approves_later(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel = InternalEmvKernel()
            profile = internal_profile(kernel, "cancel-exchange", "CONTACT", "approved")
            profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
            request = TransactionRequest.create(
                money=Money(1000, "MXN", 2),
                profile_ref=profile_ref,
                interface="CONTACT",
                kernel_ref=kernel.kernel_ref,
                operation_id="cancel-exchange",
                idempotency_key="key-cancel-exchange",
            )
            reader = SyntheticIccReader()
            reader.configure(profile, request)
            reader.block_exchange(0xA4)
            app = CardStudioApplication(data_dir=root, profiles=[profile], kernels=kernel, reader=reader)
            output = []
            thread = threading.Thread(target=lambda: output.append(app.create_transaction(request)))
            thread.start()
            self.assertTrue(reader.exchange_started.wait(2))
            started = time.monotonic()
            cancelled = app.cancel(request.operation_id)
            elapsed = time.monotonic() - started
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(elapsed, 1)
            self.assertEqual(cancelled["execution_state"], "UNCERTAIN")
            self.assertEqual(output[0]["execution_state"], "UNCERTAIN")
            self.assertNotEqual(app.get(request.operation_id)["functional_result"], "APPROVED")
            self.assertEqual(kernel.execution_count(request.operation_id), 1)
            self.assertEqual(app.host.operation_count("authorize"), 0)

    def test_t12_t23_authority_registry_spec_wins_without_auto_approval(self):
        requirement = SpecificationRequirement(
            "EMV-LAB-RECOVERY-001",
            "AUTHORIZED_SPECIFICATION_PLACEHOLDER",
            "UNVERIFIED",
            "Late host outcome and original reference",
        )
        registry = SpecificationAuthorityRegistry([requirement])
        resolved = registry.resolve(requirement.requirement_id, "RESEND", "QUERY_ONLY")
        self.assertEqual(resolved["resolution"], "SPEC_WINS")
        self.assertEqual(resolved["selected_value"], "QUERY_ONLY")
        self.assertTrue(resolved["conflict"])
        kernel = InternalEmvKernel()
        profile = internal_profile(kernel, "authority", "CONTACT", "approved")
        binding = registry.bind_profile(requirement.requirement_id, profile)
        self.assertEqual(binding["profile_digest"], profile.profile_digest)
        self.assertFalse(binding["auto_approved"])
        self.assertEqual(binding["authority"], "EXTERNAL_UNVERIFIED")
        self.assertEqual(binding["certification"], "NOT COMPLETED")
        with self.assertRaises(SpecificationAuthorityError) as duplicate:
            registry.register(requirement)
        self.assertEqual(duplicate.exception.code, "DUPLICATE_REQUIREMENT_ID")
        with self.assertRaises(SpecificationAuthorityError) as unverified:
            SpecificationRequirement(
                "EMV-LAB-INVALID-001", "DOC", "1", "clause", provenance="VERIFIED",
            )
        self.assertEqual(unverified.exception.code, "EXTERNAL_AUTHORITY_UNVERIFIED")
        with self.assertRaises(SpecificationAuthorityError):
            SpecificationAuthorityRegistry([requirement]).resolve("MISSING", "A", "B")

    def test_t20_rejects_unknown_schema_and_steps(self):
        scenario = p1_scenarios()[0]
        self.assertEqual(scenario.schema_version, 1)
        self.assertEqual(len(scenario.digest()), 64)
        with self.assertRaises(ValueError):
            TerminalScenario("invalid-version", "CONTACT", schema_version=2)
        with self.assertRaises(ValueError):
            TerminalStep("UNKNOWN_STEP")


if __name__ == "__main__":
    unittest.main()
