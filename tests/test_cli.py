import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from card_studio.cli import main


class CliTests(unittest.TestCase):
    def test_virtual_cli_is_fully_operational_without_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--data-dir", str(root / "data"), "read"])
            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["mode"], "VIRTUAL")
            self.assertEqual(payload["card"]["balanceMinor"], 0)

    def test_configuration_output_never_contains_terminal_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main([
                    "--config-dir", directory,
                    "--ledger-url", "https://pos.sandbox.invalid",
                    "--terminal-code", "SANDBOX-TEST",
                    "configure",
                ])
            self.assertEqual(result, 0)
            self.assertFalse(json.loads(output.getvalue())["secretKeySaved"])
            self.assertNotIn("SWIFT_LEDGER_TERMINAL_KEY", Path(directory, "settings.json").read_text())


if __name__ == "__main__":
    unittest.main()
