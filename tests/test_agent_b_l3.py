import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from card_studio.application import CardStudioApplication, laboratory_profile
from card_studio.l3.runner import GATE_CASES, L3Runner
from card_studio.l3.scenarios import LabKernelAdapter, matrix_definitions


class L3MatrixTests(unittest.TestCase):
    def application(self, root):
        kernel = LabKernelAdapter()
        return CardStudioApplication(
            data_dir=root,
            kernels=kernel,
            profiles=[laboratory_profile(kernel.kernel_ref)],
        )

    def test_complete_matrix_evidence_gates_and_readiness_are_non_conformant_with_fake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = L3Runner(root).run()
            self.assertEqual(report.total, len(matrix_definitions()))
            self.assertEqual(report.total, 27)
            self.assertEqual(report.passed, 27, [
                (case.test_case_id, [item.to_dict() for item in case.assertions if item.status == "FAIL"])
                for case in report.cases if case.status != "PASS"
            ])
            self.assertEqual(report.status, "NON_CONFORMANT")
            self.assertFalse(report.conformant)
            self.assertEqual(report.identity["kernel_adapter"], "LabKernelAdapter")
            self.assertEqual(report.identity["host_adapter"], "MockAuthorizationHost")
            self.assertEqual({gate.gate_id for gate in report.gates}, {f"G{number}" for number in range(1, 11)})
            self.assertTrue(all(gate.status == "NON_CONFORMANT" for gate in report.gates))
            self.assertTrue(all(gate.identity["kernel_adapter"] == "LabKernelAdapter" for gate in report.gates))
            self.assertTrue(all(not gate.conformant for gate in report.gates))
            self.assertEqual(set(GATE_CASES), {f"G{number}" for number in range(1, 11)})
            for case in report.cases:
                evidence = root / "cases" / case.evidence_reference
                self.assertTrue(evidence.exists())
                self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)
                value = json.loads(evidence.read_text())
                self.assertEqual(value["test_case_id"], case.test_case_id)
                self.assertEqual(value["operation_id"], case.operation_id)
                self.assertEqual(value["profile_ref"], case.profile_ref)
                self.assertEqual(value["profile_digest"], case.profile_digest)
                self.assertEqual(value["profile_digest"], case.profile_ref.rsplit(":", 1)[1])
                self.assertEqual(value["kernel_adapter"], "LabKernelAdapter")
                self.assertEqual(value["kernel_ref"], "mock-kernel-v1")
                self.assertEqual(value["kernel_version"], "1")
                self.assertEqual(value["kernel_mode"], "LAB / SYNTHETIC")
                self.assertEqual(value["host_adapter"], "MockAuthorizationHost")
                self.assertEqual(value["host_version"], "2")
                self.assertEqual(value["terminal_version"], case.terminal_version)
                self.assertEqual(value["interface"], case.interface)
                self.assertEqual(value["status"], "PASS")
                self.assertFalse(value["conformant"])
                self.assertFalse(value["readiness_eligible"])
                self.assertEqual(value["physical_validation"], "NOT_EXECUTED")
                self.assertTrue(value["assertions"])
                serialized = json.dumps(value)
                for forbidden in ('"pan"', '"pin"', '"cvv"', '"track1"', '"track2"', '"private_key"', '"raw_apdu"', '"response_template"'):
                    self.assertNotIn(forbidden, serialized)
            readiness = json.loads((root / "readiness.json").read_text())
            self.assertEqual(readiness["status"], "NON_CONFORMANT")
            self.assertFalse(readiness["readiness_eligible"])
            self.assertEqual(readiness["passed"], 27)
            self.assertEqual(readiness["total"], 27)
            self.assertEqual(readiness["certification"], "NOT COMPLETED")
            self.assertEqual(readiness["physical_validation"], "PENDING")
            self.assertEqual(self.application(root).l3_readiness()["status"], "NON_CONFORMANT")

    def test_any_failed_gate_prevents_overall_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = L3Runner(root).run()
            report.cases[0].status = "FAIL"
            report.gates[0] = type(report.gates[0])("G1", "FAIL", report.gates[0].case_ids)
            self.assertEqual(report.status, "FAIL")
            self.assertNotEqual(report.status, "PASS")

    def test_fake_identity_cannot_be_forced_to_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = L3Runner(root).run()
            report.gates = [replace(gate, status="PASS") for gate in report.gates]
            report.conformant = True
            self.assertEqual(report.status, "NON_CONFORMANT")
            value = report.to_dict()
            (root / "readiness.json").write_text(json.dumps(value))
            readiness = self.application(root).l3_readiness()
            self.assertEqual(readiness["status"], "FAIL")
            self.assertFalse(readiness["readiness_eligible"])

    def test_mixed_adapter_identity_fails_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = L3Runner(Path(directory)).run()
            report.cases[0].kernel_adapter = "InternalEmvKernel"
            self.assertEqual(report.status, "FAIL")

    def test_tampered_adapter_identity_is_rejected_by_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = L3Runner(root).run()
            value = report.to_dict()
            value["conformant"] = True
            value["identity"]["kernel_adapter"] = "InternalEmvKernel"
            for gate in value["gates"]:
                gate["status"] = "PASS"
                gate["conformant"] = True
                gate["identity"]["kernel_adapter"] = "InternalEmvKernel"
            (root / "readiness.json").write_text(json.dumps(value))
            readiness = self.application(root).l3_readiness()
            self.assertEqual(readiness["status"], "FAIL")
            self.assertFalse(readiness["readiness_eligible"])

    def test_missing_gate_cannot_pass_report_or_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = L3Runner(root).run()
            value = report.to_dict()
            report.gates.pop()
            self.assertEqual(report.status, "FAIL")
            value["gates"].pop()
            (root / "readiness.json").write_text(json.dumps(value))
            self.assertEqual(self.application(root).l3_readiness()["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
