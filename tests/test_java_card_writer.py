import json
import tempfile
import unittest
from pathlib import Path

from card_studio.applet import ApduResponse
from card_studio.java_card_writer import JavaCardWriter
from card_studio.model import CardError

AID = "D4534C504D4401"
ATR = "3BF81300008131FE454A434F5076323431B7"
READBACK = b"SWIFT LEDGER DEMO|NONPAYMENT|5968"
WRITE_APDU = f"80DA0001{len(READBACK):02X}{READBACK.hex().upper()}"
READ_APDU = "80CA000100"


class FakeWriterBackend:
    mode = "SIMULATED"

    def __init__(self, select_sw="9000", write_sw="9000", read_sw="9000", readback=READBACK):
        self.select_sw = select_sw
        self.write_sw = write_sw
        self.read_sw = read_sw
        self.readback = readback
        self.commands = []
        self.selected = []
        self.disconnected = False

    def waitForCard(self):
        return self

    def getReader(self):
        return "Rocketek CSCR3 FAKE"

    def getATR(self):
        return bytes.fromhex(ATR)

    def selectApplet(self, aid):
        self.selected.append(aid)
        return self.response(self.select_sw)

    def transmit(self, command):
        self.commands.append(bytes(command).hex().upper())
        if self.commands[-1] == WRITE_APDU:
            return self.response(self.write_sw)
        if self.commands[-1] == READ_APDU:
            return ApduResponse(self.readback, *self.split_sw(self.read_sw))
        raise AssertionError("APDU inesperado")

    def disconnect(self):
        self.disconnected = True

    @staticmethod
    def split_sw(value):
        return int(value[:2], 16), int(value[2:], 16)

    @classmethod
    def response(cls, status):
        return ApduResponse(b"", *cls.split_sw(status))


class JavaCardWriterTests(unittest.TestCase):
    def write(self, backend, **kwargs):
        writer = JavaCardWriter(backend, AID, expected_atr=kwargs.pop("expected_atr", ATR))
        return writer.write_and_readback(
            WRITE_APDU,
            READ_APDU,
            kwargs.pop("expected_readback", READBACK),
            confirm_write=True,
            **kwargs,
        )

    def test_write_and_readback_success(self):
        backend = FakeWriterBackend()
        report = self.write(backend)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["container_state"], "PERSONALIZED")
        self.assertTrue(report["readback_match"])
        self.assertEqual(report["status_words"], {"select": "9000", "write": "9000", "read": "9000"})
        self.assertEqual(backend.commands, [WRITE_APDU, READ_APDU])
        self.assertTrue(backend.disconnected)
        self.assertEqual(report["physical_validation"], "NOT_EXECUTED")
        self.assertFalse(report["write_executed"])

    def test_select_failure_does_not_write(self):
        backend = FakeWriterBackend(select_sw="6A82")
        report = self.write(backend)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["errors"], ["APPLET_NOT_FOUND"])
        self.assertEqual(backend.commands, [])

    def test_write_failure_does_not_read(self):
        backend = FakeWriterBackend(write_sw="6985")
        report = self.write(backend)
        self.assertEqual(report["errors"], ["WRITE_FAILED"])
        self.assertEqual(backend.commands, [WRITE_APDU])
        self.assertEqual(report["container_state"], "APPLET_SELECTED")

    def test_readback_mismatch_is_detected(self):
        backend = FakeWriterBackend(readback=b"different")
        report = self.write(backend)
        self.assertEqual(report["errors"], ["READBACK_MISMATCH"])
        self.assertEqual(report["container_state"], "WRITE_ACCEPTED")
        self.assertFalse(report["readback_match"])

    def test_physical_mode_records_accepted_write_when_readback_mismatches(self):
        backend = FakeWriterBackend(readback=b"different")
        backend.mode = "PHYSICAL"
        report = self.write(backend)
        self.assertTrue(report["write_executed"])
        self.assertEqual(report["container_state"], "WRITE_ACCEPTED")
        self.assertEqual(report["physical_validation"], "FAIL")

    def test_physical_mode_success_is_explicit(self):
        backend = FakeWriterBackend()
        backend.mode = "PHYSICAL"
        report = self.write(backend)
        self.assertTrue(report["write_executed"])
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["physical_validation"], "PASS")

    def test_atr_mismatch_prevents_select(self):
        backend = FakeWriterBackend()
        report = self.write(backend, expected_atr="3B0000")
        self.assertEqual(report["errors"], ["ATR_MISMATCH"])
        self.assertEqual(backend.selected, [])
        self.assertEqual(backend.commands, [])

    def test_explicit_confirmation_is_required(self):
        backend = FakeWriterBackend()
        writer = JavaCardWriter(backend, AID)
        with self.assertRaisesRegex(CardError, "confirmación explícita"):
            writer.write_and_readback(WRITE_APDU, READ_APDU, READBACK)
        self.assertEqual(backend.commands, [])

    def test_evidence_contains_hashes_not_apdu_payloads(self):
        backend = FakeWriterBackend()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "write-evidence.json"
            report = self.write(backend, evidence_path=path)
            document = json.loads(path.read_text())
            self.assertEqual(document["result"], "PASS")
            self.assertEqual(report["evidence_path"], str(path))
            self.assertNotIn(WRITE_APDU, path.read_text())
            self.assertNotIn(READBACK.hex().upper(), path.read_text())
            self.assertEqual(len(document["write_apdu_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
