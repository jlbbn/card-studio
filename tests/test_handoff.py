import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from card_studio.cli import main
from card_studio.pcsc import MacOSPcscBackend


class HandoffTests(unittest.TestCase):
    def test_hardware_status_is_available_without_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch("card_studio.startup.create_pcsc_backend", return_value=MacOSPcscBackend(reader_provider=list)):
                with contextlib.redirect_stdout(output):
                    result = main(["--config-dir", directory, "hardware-status"])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(report["software_ready"])
            self.assertTrue(report["hardware_validation_available"])
            self.assertEqual([item["aid"] for item in report["aid_candidates"]], ["D4534C504D4401", "D4534C504D44"])
            self.assertEqual(report["canonical_aid"], "UNRESOLVED")

    def test_packaged_cli_dry_run_never_confirms_physical_aid(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "dry-run.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["hardware-validation", "--dry-run", "--evidence", str(evidence)])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(report["mode"], "SIMULATED")
            self.assertEqual(report["result"], "SIMULATED_MATCH")
            self.assertEqual(report["physical_validation"], "NOT_EXECUTED")
            self.assertIsNone(report["selected_aid"])
            self.assertTrue(evidence.is_file())

    def test_unsigned_build_mode_is_explicit(self):
        build = Path(__file__).resolve().parents[1] / "packaging/macos/build.sh"
        source = build.read_text()
        self.assertIn('CARD_STUDIO_UNSIGNED:-0', source)
        self.assertIn('CODE_SIGNING = NOT_CONFIGURED', source)
        self.assertIn('NOTARIZATION = NOT_CONFIGURED', source)


if __name__ == "__main__":
    unittest.main()
