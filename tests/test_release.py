import json
import os
import tempfile
import unittest
from pathlib import Path

from card_studio import __build__, __version__
from card_studio.backends import CardpeekBackend
from card_studio.config import AppSettings, SettingsStore
from card_studio.startup import initialize_application
from card_studio.system import evidence_directory


class ReleaseTests(unittest.TestCase):
    def test_version_and_macos_packaging_are_updated(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(__version__, "1.2.0")
        self.assertEqual(__build__, "1")
        spec = (root / "packaging/macos/card-studio.spec").read_text()
        build = (root / "packaging/macos/build.sh").read_text()
        self.assertIn('"CFBundleShortVersionString": "1.2.0"', spec)
        self.assertIn("smartcard.System", spec)
        self.assertIn("Card-Studio-$VERSION-macOS-$TARGET_ARCH.dmg", build)
        self.assertIn("release/$VERSION/macos-$TARGET_ARCH", build)
        self.assertIn("shasum -a 256", build)
        self.assertIn("--dry-run", build)

    def test_settings_v1_migrate_without_losing_compatible_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({
                "version": 1,
                "ledgerUrl": "https://pos.sandbox.invalid",
                "terminalCode": "SANDBOX-TEST",
                "sunmiUrl": "http://127.0.0.1:5380",
                "mode": "pcsc",
                "reader": "Rocketek CSCR3",
            }))
            store = SettingsStore(Path(directory))
            settings = store.load({})
            self.assertEqual(settings.terminal_code, "SANDBOX-TEST")
            self.assertEqual(settings.reader, "Rocketek CSCR3")
            self.assertTrue(settings.hardware_validation_enabled)
            store.save(settings)
            migrated = json.loads(path.read_text())
            self.assertEqual(migrated["version"], 2)
            self.assertTrue(migrated["hardwareValidationEnabled"])

    def test_first_run_initializes_without_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            report = initialize_application(
                SettingsStore(Path(directory) / "config"),
                Path(directory) / "evidence",
                reader_provider=list,
            )
            self.assertTrue(report["software_ready"])
            self.assertTrue(report["configuration_valid"])
            self.assertTrue(report["hardware_subsystem_available"])
            self.assertTrue(report["evidence_writable"])
            self.assertEqual(report["canonical_aid"], "UNRESOLVED")
            self.assertEqual(report["physical_validation"], "PENDING")
            self.assertEqual(report["reader_names"], [])

    def test_macos_evidence_and_packaged_resources_are_portable(self):
        directory = evidence_directory("macos", {}, Path("/Users/test"))
        self.assertEqual(
            directory,
            Path("/Users/test/Library/Application Support/Swift Ledger Card Studio/Evidence/applet-validation"),
        )
        self.assertIn("cardpeek", str(CardpeekBackend().script_path))
        root = Path(__file__).resolve().parents[1]
        runtime_files = list((root / "card_studio").glob("*.py")) + [root / "packaging/macos/card-studio.spec"]
        for path in runtime_files:
            with self.subTest(path=path.name):
                self.assertNotIn(f"{os.sep}home{os.sep}", path.read_text())

    def test_ui_contains_hardware_validation_and_dry_run(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "card_studio/app.py").read_text()
        self.assertIn("Hardware Validation", source)
        self.assertIn("Run Validation", source)
        self.assertIn("Dry-run", source)
        self.assertIn("HardwareValidator", source)


if __name__ == "__main__":
    unittest.main()
