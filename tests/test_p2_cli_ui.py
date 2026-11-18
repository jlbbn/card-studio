from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from card_studio.integration.sunmi import CardFlowError, CardFlowState, SunmiCardFlow
from card_studio.transactions.view import card_flow_view


ROOT = Path(__file__).resolve().parents[1]


def cli(*arguments):
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-B", "-m", "card_studio", "--cli", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class P2CliUiTests(unittest.TestCase):
    def test_unpatched_subprocess_kernel_readiness_is_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = cli("--data-dir", temporary, "kernel", "readiness")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            value = json.loads(completed.stdout)
            self.assertTrue(value["simulation"])
            self.assertEqual(value["physical_validation"], "NOT_EXECUTED")
            self.assertEqual(value["kernels"][0]["kernel_ref"], "internal-emv-lab-v1")

    def test_unpatched_subprocess_dry_run_never_claims_physical_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "dry-run.json"
            completed = cli(
                "--data-dir", temporary,
                "hardware-validation",
                "--dry-run",
                "--scenario", "aid1-selected",
                "--evidence", str(evidence),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            value = json.loads(completed.stdout)
            self.assertEqual(value["result"], "SIMULATED_MATCH")
            self.assertEqual(value["physical_validation"], "NOT_EXECUTED")
            self.assertNotEqual(value["physical_validation"], "PASS")
            self.assertTrue(evidence.is_file())

    def test_physical_prepare_returns_json_and_never_implicit_physical_pass(self):
        completed = cli("physical-prepare")
        self.assertIn(completed.returncode, {0, 1, 2}, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertNotEqual(value.get("physical_validation"), "PASS")
        self.assertNotIn("Traceback", completed.stdout)

    def test_subprocess_error_is_json_without_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = cli(
                "--data-dir", temporary,
                "transaction", "create",
                "--amount-minor", "0",
            )
            self.assertEqual(completed.returncode, 1)
            value = json.loads(completed.stdout)
            self.assertEqual(value["error"], "CONFIG_ERROR")
            self.assertNotIn("SWIFT_LEDGER_TERMINAL_KEY", completed.stdout)
            self.assertNotIn("secret_key", completed.stdout)

    def test_card_view_and_invalid_sunmi_transition_do_not_use_cash(self):
        view = card_flow_view({
            "operationId": "card-uncertain",
            "executionState": "UNCERTAIN",
            "functionalResult": None,
        })
        self.assertEqual(view["channel"], "CARD")
        self.assertEqual(view["actions"], ["query", "review"])
        self.assertIsNone(view["cash_state"])
        flow = SunmiCardFlow()
        with self.assertRaises(CardFlowError):
            flow.advance(CardFlowState.APPROVED)
        self.assertEqual(flow.state, CardFlowState.CARD_DETECTED)


if __name__ == "__main__":
    unittest.main()
