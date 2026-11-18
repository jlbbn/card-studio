from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.model import P0_GATE_IDS
from card_studio.l3.runner import L3Runner
from card_studio.l3.scenarios import LabKernelAdapter


def p0_evidence():
    return {gate_id: "PASS" for gate_id in P0_GATE_IDS}


def internal_runner(root, with_p0=True):
    return L3Runner(
        root,
        kernel_factory=InternalEmvKernel,
        p0_evidence=p0_evidence() if with_p0 else None,
    )


class InternalL3MatrixTests(unittest.TestCase):
    def test_real_kernel_matrix_is_27_of_27_with_conformant_manifest_and_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = internal_runner(root).run()
            self.assertEqual((report.passed, report.total), (27, 27))
            self.assertEqual(report.status, "PASS")
            self.assertTrue(report.conformant)
            self.assertTrue(report.p0_complete)
            self.assertEqual(report.identity["kernel_adapter"], "InternalEmvKernel")
            self.assertTrue(all(case.status == "PASS" and case.conformant for case in report.cases))
            self.assertTrue(all(gate.status == "PASS" and gate.conformant for gate in report.gates))
            by_id = {case.test_case_id: case for case in report.cases}
            for case_id in (
                "contact-approved",
                "contact-online_required",
                "contactless-approved",
                "contactless-online_required",
            ):
                names = {assertion.name for assertion in by_id[case_id].assertions}
                self.assertIn("interface_apdu_instruction_sequence", names)
            self.assertIn(
                "contactless_no_generate_ac",
                {assertion.name for assertion in by_id["contactless-online_required"].assertions},
            )
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
            self.assertEqual(manifest["identity"], report.identity)
            self.assertEqual(len(manifest["cases"]), 27)
            self.assertEqual(manifest["matrix_sha256"], hashlib.sha256((root / "matrix.json").read_bytes()).hexdigest())
            self.assertEqual(manifest["readiness_sha256"], hashlib.sha256((root / "readiness.json").read_bytes()).hexdigest())
            for case in manifest["cases"]:
                evidence = root / "cases" / case["evidence_reference"]
                self.assertEqual(case["evidence_sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
                self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)
            readiness = CardStudioApplication(data_dir=root).l3_readiness()
            self.assertEqual(readiness["status"], "PASS")
            self.assertTrue(readiness["readiness_eligible"])
            self.assertEqual(readiness["certification"], "NOT COMPLETED")
            self.assertEqual(readiness["physical_validation"], "PENDING")

    def test_real_kernel_without_complete_p0_cannot_pass_and_fake_never_can(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = internal_runner(root, with_p0=False).run()
            self.assertEqual((report.passed, report.total), (27, 27))
            self.assertEqual(report.status, "NON_CONFORMANT")
            self.assertFalse(report.p0_complete)
            self.assertFalse(report.conformant)
            self.assertEqual(CardStudioApplication(data_dir=root).l3_readiness()["status"], "NON_CONFORMANT")
        with tempfile.TemporaryDirectory() as temporary:
            report = L3Runner(
                Path(temporary),
                kernel_factory=LabKernelAdapter,
                p0_evidence=p0_evidence(),
            ).run()
            self.assertEqual((report.passed, report.total), (27, 27))
            self.assertEqual(report.status, "NON_CONFORMANT")
            self.assertFalse(report.conformant)

    def test_tampered_case_evidence_is_rejected_by_facade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal_runner(root).run()
            evidence = root / "cases" / "contact-approved.json"
            value = json.loads(evidence.read_text())
            value["kernel_adapter"] = "LabKernelAdapter"
            evidence.write_text(json.dumps(value))
            readiness = CardStudioApplication(data_dir=root).l3_readiness()
            self.assertEqual(readiness["status"], "FAIL")
            self.assertFalse(readiness["readiness_eligible"])

    def test_missing_or_tampered_manifest_is_rejected_by_facade(self):
        for mode in ("missing", "identity", "case_digest"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    internal_runner(root).run()
                    manifest_path = root / "manifest.json"
                    if mode == "missing":
                        manifest_path.unlink()
                    else:
                        manifest = json.loads(manifest_path.read_text())
                        if mode == "identity":
                            manifest["identity"]["kernel_adapter"] = "LabKernelAdapter"
                        else:
                            manifest["cases"][0]["evidence_sha256"] = "0" * 64
                        manifest_path.write_text(json.dumps(manifest))
                    readiness = CardStudioApplication(data_dir=root).l3_readiness()
                    self.assertEqual(readiness["status"], "FAIL")
                    self.assertFalse(readiness["readiness_eligible"])

    def test_host_timeout_evidence_has_one_authorization_no_completion_and_no_reversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = internal_runner(root).run()
            case = next(case for case in report.cases if case.test_case_id == "host-timeout")
            self.assertEqual(case.status, "PASS")
            self.assertEqual(case.execution_state, "UNCERTAIN")
            names = {assertion.name for assertion in case.assertions}
            self.assertTrue({
                "single_host_authorization",
                "no_online_completion",
                "no_reversal_event",
                "single_authorization_after_restart",
                "restart_conserves_uncertain",
            } <= names)
            work = root / "work" / "host-timeout"
            app = CardStudioApplication(data_dir=work)
            events = [entry["event_type"] for entry in app.audit("l3-host-timeout")]
            self.assertEqual(events.count("HOST_AUTHORIZATION_STARTED"), 1)
            self.assertNotIn("ONLINE_COMPLETED", events)
            self.assertNotIn("REVERSAL_STARTED", events)
            self.assertEqual(app.get("l3-host-timeout")["execution_state"], "UNCERTAIN")


if __name__ == "__main__":
    unittest.main()
