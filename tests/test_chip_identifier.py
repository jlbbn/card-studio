import json
import subprocess
import tempfile
import unittest
from pathlib import Path

CHIP_ID_ROOT_AVAILABLE = (Path.home() / "chip-id").is_dir()
from unittest.mock import patch

from card_studio.chip_identifier import ChipIdentifierClient, ChipIdentifierError


class ChipIdentifierCommandTests(unittest.TestCase):
    def test_command_uses_run_sh_without_shell(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"offline_status": "WAITING_FOR_READER"}),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ChipIdentifierClient(root=directory)
            with patch("card_studio.chip_identifier.subprocess.run", return_value=completed) as run:
                result = client.status()
        self.assertEqual(result["offline_status"], "WAITING_FOR_READER")
        kwargs = run.call_args.kwargs
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_invalid_json_is_controlled(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            client = ChipIdentifierClient(root=directory)
            with patch("card_studio.chip_identifier.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(ChipIdentifierError, "INVALID_JSON"):
                    client.status()

    def test_failed_validation_is_controlled(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ChipIdentifierClient(root=directory)
            with patch.object(ChipIdentifierClient, "_run", return_value={"valid": False, "errors": ["BAD_HASH"]}):
                with self.assertRaisesRegex(ChipIdentifierError, "BAD_HASH"):
                    client.validate("artifact.json")

    def test_auto_probe_without_reader_is_waiting(self):
        probe = {
            "mode": "offline",
            "status": "WAITING_FOR_READER",
            "reader": None,
            "card_connected": False,
            "atr": None,
            "protocol": None,
            "errors": [],
            "warnings": ["NO_READER_PRESENT"],
        }
        with patch.object(ChipIdentifierClient, "_run", return_value=probe) as run:
            result = ChipIdentifierClient().hardware_summary("auto")
        run.assert_called_once_with("probe", "--mode", "auto", allow_failure=True)
        self.assertEqual(result["source"], "OFFLINE")
        self.assertEqual(result["status"], "WAITING_FOR_READER")
        self.assertIsNone(result["reader_model"])

    def test_rocketek_live_summary(self):
        probe = {
            "mode": "live",
            "status": "PASS",
            "reader": "Rocketek CSCR3 00 00",
            "card_connected": True,
            "atr": "3BF81300008131FE454A434F5076323431B7",
            "protocol": "T=1",
            "errors": [],
            "warnings": [],
        }
        with patch.object(ChipIdentifierClient, "_run", return_value=probe):
            result = ChipIdentifierClient().hardware_summary("live")
        self.assertEqual(result["source"], "LIVE")
        self.assertEqual(result["reader_model"], "ROCKETEK_CSCR3")
        self.assertTrue(result["card_connected"])
        self.assertEqual(result["protocol"], "T=1")
        self.assertEqual(result["physical_validation"], "NOT_EXECUTED")

    def test_live_probe_failure_is_controlled(self):
        with patch.object(
            ChipIdentifierClient,
            "_run",
            return_value={"status": "FAIL", "errors": ["NO_READER_PRESENT"]},
        ):
            with self.assertRaisesRegex(ChipIdentifierError, "NO_READER_PRESENT"):
                ChipIdentifierClient().probe("live")

    def test_invalid_probe_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "INVALID_PROBE_MODE"):
            ChipIdentifierClient().probe("invalid")


class ChipIdentifierIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = ChipIdentifierClient(
            artifact_root=Path(self.directory.name) / "artifacts"
        )

    def tearDown(self):
        self.directory.cleanup()

    @unittest.skipUnless(
        CHIP_ID_ROOT_AVAILABLE,
        "external chip-id repository not available",
    )
    def test_real_profile_is_validated_offline(self):
        profile = self.client.profile()
        self.assertEqual(profile["application"]["aid"], "D4534C504D4401")
        self.assertEqual(profile["application"]["application_label"], "Santander")
        self.assertEqual(profile["source"], "OFFLINE")
        self.assertEqual(profile["physical_validation"], "NOT_EXECUTED")

    @unittest.skipUnless(
        CHIP_ID_ROOT_AVAILABLE,
        "external chip-id repository not available",
    )
    def test_real_container_is_validated_without_hardware(self):
        container = self.client.container()
        self.assertEqual(container["state"]["current"], "PERSONALIZED")
        self.assertEqual(container["physical_evidence"]["validation_status"], "NOT_EXECUTED")

    @unittest.skipUnless(
        CHIP_ID_ROOT_AVAILABLE,
        "external chip-id repository not available",
    )
    def test_summary_exposes_only_integration_fields(self):
        summary = self.client.summary()
        self.assertEqual(summary["protocols"], ["T=0", "T=1"])
        self.assertEqual(summary["historical_bytes"], "JCOPv241")
        self.assertEqual(summary["container_state"], "PERSONALIZED")
        self.assertNotIn("tlv", summary)

    @unittest.skipUnless(
        CHIP_ID_ROOT_AVAILABLE,
        "external chip-id repository not available",
    )
    def test_status_reports_all_artifacts(self):
        status = self.client.status()
        self.assertEqual(status["offline_status"], "WAITING_FOR_READER")
        self.assertFalse(any(status["artifacts"].values()))
        self.client.offline_report()
        status = self.client.status()
        self.assertTrue(all(status["artifacts"].values()))


if __name__ == "__main__":
    unittest.main()
