import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from card_studio.applet import (
    APPLET_AID_CANDIDATES,
    build_select_apdu,
    interpret_status_word,
    parse_apdu_response,
)
from card_studio.model import CardError


class AppletTests(unittest.TestCase):
    def test_candidate_loading_preserves_sources_without_canonical_aid(self):
        self.assertEqual([item.aid for item in APPLET_AID_CANDIDATES], ["D4534C504D4401", "D4534C504D44"])
        self.assertEqual([item.source for item in APPLET_AID_CANDIDATES], ["PC/SC / Cardpeek", "Android Card Bridge"])
        self.assertEqual(APPLET_AID_CANDIDATES[0].data.hex().upper(), "D4534C504D4401")
        self.assertEqual(APPLET_AID_CANDIDATES[1].data.hex().upper(), "D4534C504D44")

    def test_select_builder_computes_lc_for_each_candidate(self):
        self.assertEqual(build_select_apdu(APPLET_AID_CANDIDATES[0].aid).hex().upper(), "00A4040007D4534C504D4401")
        self.assertEqual(build_select_apdu(APPLET_AID_CANDIDATES[1].aid).hex().upper(), "00A4040006D4534C504D44")

    def test_select_builder_rejects_invalid_aid(self):
        for aid in ["XYZ1", "00", "D453"]:
            with self.subTest(aid=aid), self.assertRaises(CardError) as caught:
                build_select_apdu(aid)
            self.assertEqual(caught.exception.code, "INVALID_APDU")

    def test_response_parser_separates_data_and_status(self):
        response = parse_apdu_response(bytes.fromhex("6F0281119000"))
        self.assertEqual(response.data.hex().upper(), "6F028111")
        self.assertEqual((response.sw1, response.sw2), (0x90, 0x00))
        self.assertEqual(response.sw1sw2, "9000")

    def test_truncated_response_is_rejected(self):
        for raw in [b"", b"\x90"]:
            with self.subTest(raw=raw), self.assertRaises(CardError) as caught:
                parse_apdu_response(raw)
            self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

    def test_status_words_and_unknown_values_are_preserved(self):
        expected = {
            "9000": ("APPLET_SELECTED", True),
            "6A82": ("APPLET_NOT_FOUND", False),
            "6A86": ("INVALID_STATUS_WORD", False),
            "6700": ("INVALID_STATUS_WORD", False),
            "6985": ("INVALID_STATUS_WORD", False),
            "6D00": ("INVALID_STATUS_WORD", False),
            "6E00": ("INVALID_STATUS_WORD", False),
            "9F10": ("INVALID_STATUS_WORD", False),
        }
        for code, (result, success) in expected.items():
            with self.subTest(code=code):
                status = interpret_status_word(code)
                self.assertEqual(status.code, code)
                self.assertEqual(status.result, result)
                self.assertEqual(status.success, success)

    def test_android_source_is_generated_from_android_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "AppletAid.java"
            script = Path(__file__).resolve().parents[1] / "scripts/generate_android_applet_aid.py"
            subprocess.run([sys.executable, str(script), str(target)], check=True)
            source = target.read_text()
            self.assertIn("(byte) 0x06", source)
            self.assertIn("(byte) 0xD4, (byte) 0x53, (byte) 0x4C, (byte) 0x50, (byte) 0x4D, (byte) 0x44", source)
            self.assertNotIn("(byte) 0x01", source)


if __name__ == "__main__":
    unittest.main()
