import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from card_studio.evidence import (
    EvidenceRedactionError,
    EvidenceStore,
    redact_evidence,
)


class EvidenceRedactionTests(unittest.TestCase):
    def test_secret_redaction_is_recursive_and_allowlisted(self):
        report = {
            "mode": "SIMULATED",
            "result": "SIMULATED_MATCH",
            "physical_validation": "NOT_EXECUTED",
            "nested": {
                "pan": "4111111111111111",
                "track2": "4111111111111111=2512",
                "private_key": "secret-key",
                "safe": "conservado",
            },
            "raw_apdu": "00A4040000",
            "ignored": "no exportar",
        }
        redacted = redact_evidence(
            report,
            allowed_fields={"mode", "result", "physical_validation", "nested", "raw_apdu"},
        )
        serialized = json.dumps(redacted)
        self.assertNotIn("4111111111111111", serialized)
        self.assertNotIn("secret-key", serialized)
        self.assertNotIn("00A4040000", serialized)
        self.assertNotIn("ignored", redacted)
        self.assertEqual(redacted["nested"]["safe"], "conservado")
        self.assertEqual(redacted["raw_apdu"], "[REDACTED]")

    def test_track_data_inside_text_is_redacted(self):
        report = {
            "mode": "SIMULATED",
            "detail": "Entrada accidental 4111111111111111=25121234567890",
        }
        self.assertNotIn("411111", redact_evidence(report)["detail"])

    def test_simulated_evidence_cannot_become_physical(self):
        invalid_reports = [
            {"mode": "SIMULATED", "result": "PHYSICALLY_CONFIRMED"},
            {"mode": "SIMULATED", "physical_validation": "PASS"},
            {"mode": "SIMULATED", "evidence_level": "DEVICE_VALIDATED"},
        ]
        for report in invalid_reports:
            with self.subTest(report=report), self.assertRaises(EvidenceRedactionError):
                redact_evidence(report)

    def test_store_writes_private_sanitized_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory))
            path = store.write("result.json", {
                "mode": "SIMULATED",
                "result": "SIMULATED_MATCH",
                "cvv": "123",
            })
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            stored = json.loads(path.read_text())
            self.assertEqual(stored["cvv"], "[REDACTED]")
            with self.assertRaises(EvidenceRedactionError):
                store.write("../outside.json", {"mode": "SIMULATED"})
            with self.assertRaises(EvidenceRedactionError):
                store.write("physical.json", {
                    "mode": "SIMULATED",
                    "physical_validation": "PASS",
                })
            self.assertFalse((Path(directory) / "physical.json").exists())


if __name__ == "__main__":
    unittest.main()
