import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from card_studio.applet import APPLET_AID_CANDIDATES
from card_studio.dry_run import DryRunAdapter
from card_studio.hardware_validation import HardwareValidator, choose_reader, validation_summary, write_evidence
from card_studio.readiness import run_readiness


class HardwareValidationTests(unittest.TestCase):
    def test_all_controlled_scenarios_and_evidence(self):
        expected = {
            "aid1-selected": "SIMULATED_MATCH",
            "aid2-selected": "SIMULATED_MATCH",
            "multiple": "MULTIPLE_AID_MATCH",
            "not-found": "APPLET_NOT_FOUND",
            "reader-absent": "HARDWARE_NOT_CONNECTED",
            "card-absent": "CARD_NOT_PRESENT",
            "timeout": "TRANSMIT_TIMEOUT",
            "card-removed": "CARD_REMOVED",
            "truncated": "INVALID_RESPONSE",
            "unknown-sw": "INVALID_STATUS_WORD",
        }
        with tempfile.TemporaryDirectory() as directory:
            for scenario, result in expected.items():
                with self.subTest(scenario=scenario):
                    path = Path(directory) / f"{scenario}.json"
                    report = HardwareValidator(DryRunAdapter(scenario)).run(path)
                    self.assertEqual(report["result"], result)
                    self.assertEqual(report["mode"], "SIMULATED")
                    self.assertEqual(report["physical_validation"], "NOT_EXECUTED")
                    self.assertIsNone(report["selected_aid"])
                    self.assertTrue(report["disconnect_clean"])
                    self.assertEqual(json.loads(path.read_text())["result"], result)

    def test_dry_run_selection_never_becomes_physical(self):
        with tempfile.TemporaryDirectory() as directory:
            for scenario, aid in [("aid1-selected", "D4534C504D4401"), ("aid2-selected", "D4534C504D44")]:
                with self.subTest(scenario=scenario):
                    report = HardwareValidator(DryRunAdapter(scenario)).run(Path(directory) / f"{scenario}.json")
                    self.assertEqual(report["simulated_selected_aid"], aid)
                    self.assertIsNone(report["selected_aid"])
                    self.assertNotEqual(report["result"], "PHYSICALLY_CONFIRMED")
                    self.assertIn("SIMULATED", validation_summary(report))

    def test_evidence_is_private_and_contains_required_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "validation.json"
            report = HardwareValidator(DryRunAdapter()).run(path)
            stored = json.loads(path.read_text())
            for field in ["timestamp", "application_version", "platform", "architecture", "mode", "reader_name", "card_detected", "atr", "aid_candidates", "select_attempts", "selected_aid", "result"]:
                self.assertIn(field, stored)
            self.assertEqual(len(stored["select_attempts"]), len(APPLET_AID_CANDIDATES))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(report["sw1sw2"] if "sw1sw2" in report else stored["select_attempts"][0]["sw1sw2"], "9000")

    def test_evidence_rejects_secret_fields_and_physical_dry_run_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            with self.assertRaises(ValueError):
                write_evidence(path, {"mode": "SIMULATED", "result": "SIMULATED_MATCH", "private_key": "forbidden"})
            with self.assertRaises(ValueError):
                write_evidence(path, {"mode": "SIMULATED", "result": "PHYSICALLY_CONFIRMED", "physical_validation": "PASS"})

    def test_reader_selection_is_deterministic(self):
        self.assertEqual(choose_reader(["Only Reader"]), ("Only Reader", None))
        self.assertEqual(choose_reader(["Other", "Rocketek CSCR3 00 00"]), ("Rocketek CSCR3 00 00", None))
        self.assertEqual(choose_reader(["A", "B"]), (None, "MULTIPLE_READERS"))
        self.assertEqual(choose_reader(["A", "B"], "B"), ("B", None))
        self.assertEqual(choose_reader([]), (None, "READER_NOT_FOUND"))

    def test_readiness_passes_without_hardware(self):
        root = Path(__file__).resolve().parents[1]
        report = run_readiness(root, include_tests=False)
        self.assertEqual(report["software_readiness"], "PASS")
        self.assertEqual(report["hardware_readiness"], "PASS")
        self.assertEqual(report["physical_validation"], "PENDING")


if __name__ == "__main__":
    unittest.main()
