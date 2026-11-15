import json
import tempfile
import unittest
from pathlib import Path

from card_studio.applet import build_select_apdu
from card_studio.model import CardError
from card_studio.write_exercise import (
    APPLET_AID,
    EXERCISE_PAYLOAD,
    INITIAL_PAYLOAD,
    PRESERVED_APPLET_AID,
    PRESERVED_STATE,
    SimulatedDemoAdapter,
    demo_write_apdu,
    run_exercise,
)


class WriteExerciseTests(unittest.TestCase):
    def test_write_readback_restore_and_preserved_verification(self):
        adapter = SimulatedDemoAdapter()
        report = run_exercise(adapter, confirm_write=True)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["container_state"], "RESTORED_5968_PRESERVED_UNCHANGED")
        self.assertEqual(report["preflight"]["contract"], "COEXISTENT_33_BYTE")
        self.assertEqual(report["applet_aid"], APPLET_AID)
        self.assertEqual(report["preserved_applet_aid"], PRESERVED_APPLET_AID)
        self.assertNotEqual(APPLET_AID, PRESERVED_APPLET_AID)
        self.assertEqual(report["exercise_write"]["readback_match"], True)
        self.assertEqual(report["restore_write"]["readback_match"], True)
        self.assertTrue(report["postflight"]["preserved_state_match"])
        self.assertEqual(
            report["preflight"]["preserved_state_sha256"],
            report["postflight"]["preserved_state_sha256"],
        )
        self.assertEqual(report["write_commands_accepted"], 2)
        self.assertFalse(report["write_executed"])
        self.assertEqual(report["physical_validation"], "NOT_EXECUTED")
        self.assertEqual(adapter.payload, INITIAL_PAYLOAD)
        self.assertEqual(adapter.write_count, 2)
        self.assertEqual(adapter.commands[0], build_select_apdu(PRESERVED_APPLET_AID).hex().upper())

    def test_missing_coexistent_applet_is_rejected_before_write(self):
        adapter = SimulatedDemoAdapter(contract="previous")
        report = run_exercise(adapter, confirm_write=True)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["errors"], ["COEXISTENT_APPLET_NOT_FOUND"])
        self.assertEqual(report["container_state"], "NO_WRITE")
        self.assertEqual(adapter.write_count, 0)
        self.assertEqual(adapter.payload, INITIAL_PAYLOAD)

    def test_unexpected_initial_content_is_not_overwritten(self):
        adapter = SimulatedDemoAdapter(initial_payload=EXERCISE_PAYLOAD)
        report = run_exercise(adapter, confirm_write=True)
        self.assertEqual(report["errors"], ["INITIAL_CONTENT_MISMATCH"])
        self.assertEqual(adapter.write_count, 0)
        self.assertEqual(adapter.payload, EXERCISE_PAYLOAD)

    def test_restore_failure_is_explicit(self):
        adapter = SimulatedDemoAdapter(fail_restore=True)
        report = run_exercise(adapter, confirm_write=True)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["errors"], ["RESTORE_FAILED"])
        self.assertEqual(report["container_state"], "RESTORE_FAILED_READBACK_REQUIRED")
        self.assertEqual(report["write_commands_accepted"], 1)
        self.assertEqual(adapter.payload, EXERCISE_PAYLOAD)

    def test_preserved_state_change_fails_after_restore(self):
        adapter = SimulatedDemoAdapter(fail_preserved_check=True)
        report = run_exercise(adapter, confirm_write=True)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["errors"], ["PRESERVED_STATE_CHANGED"])
        self.assertEqual(report["container_state"], "RESTORED_5968_PRESERVED_CHECK_FAILED")
        self.assertEqual(adapter.payload, INITIAL_PAYLOAD)
        self.assertEqual(adapter.write_count, 2)

    def test_confirmation_is_required_before_any_apdu(self):
        adapter = SimulatedDemoAdapter()
        with self.assertRaises(CardError) as caught:
            run_exercise(adapter)
        self.assertEqual(caught.exception.code, "WRITE_CONFIRMATION_REQUIRED")
        self.assertEqual(adapter.commands, [])
        self.assertEqual(adapter.write_count, 0)

    def test_evidence_contains_hashes_and_not_payloads(self):
        adapter = SimulatedDemoAdapter()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exercise.json"
            report = run_exercise(adapter, confirm_write=True, evidence_path=path)
            raw = path.read_text(encoding="utf-8")
            document = json.loads(raw)
            self.assertEqual(document["result"], "PASS")
            self.assertEqual(report["evidence_path"], str(path))
            self.assertNotIn(INITIAL_PAYLOAD.hex().upper(), raw)
            self.assertNotIn(EXERCISE_PAYLOAD.decode("ascii"), raw)
            self.assertNotIn(PRESERVED_STATE.decode("ascii"), raw)
            self.assertEqual(len(document["initial_payload_sha256"]), 64)

    def test_apdu_builder_enforces_the_33_byte_contract(self):
        command = demo_write_apdu(EXERCISE_PAYLOAD)
        self.assertEqual(command[:5].hex().upper(), "80DA000121")
        self.assertEqual(command[5:], EXERCISE_PAYLOAD)
        with self.assertRaises(CardError):
            demo_write_apdu(b"short")


if __name__ == "__main__":
    unittest.main()
