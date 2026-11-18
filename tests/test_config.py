import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from card_studio.config import AppSettings, SettingsStore
from card_studio.ledger import LedgerError, normalize_base_url
from card_studio.system import config_directory, data_directory, log_directory, platform_name


class PlatformPathTests(unittest.TestCase):
    def test_linux_paths_keep_existing_locations(self):
        home = Path("home-test")
        self.assertEqual(data_directory("linux", {}, home), home / ".local/share/swift-ledger-card-studio")
        self.assertEqual(config_directory("linux", {}, home), home / ".config/swift-ledger-card-studio")
        self.assertEqual(log_directory("linux", {}, home), home / ".local/state/swift-ledger-card-studio/logs")

    def test_macos_paths_use_application_support_and_logs(self):
        home = Path("/Users/test")
        self.assertEqual(data_directory("darwin", {}, home), home / "Library/Application Support/Swift Ledger Card Studio")
        self.assertEqual(config_directory("macos", {}, home), home / "Library/Application Support/Swift Ledger Card Studio/Config")
        self.assertEqual(log_directory("darwin", {}, home), home / "Library/Logs/Swift Ledger Card Studio")

    def test_explicit_directories_win(self):
        values = {"CARD_STUDIO_DATA_DIR": "custom/data", "CARD_STUDIO_CONFIG_DIR": "custom/config"}
        self.assertEqual(data_directory("macos", values), Path("custom/data"))
        self.assertEqual(config_directory("linux", values), Path("custom/config"))
        self.assertEqual(platform_name("darwin"), "macos")


class SettingsTests(unittest.TestCase):
    def test_settings_are_private_and_contain_no_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            settings = AppSettings(ledger_url="https://pos.sandbox.invalid", terminal_code="SANDBOX-TEST")
            store.save(settings)
            raw = store.path.read_text()
            self.assertNotIn("secret", raw.lower())
            self.assertNotIn("terminalKey", raw)
            self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
            self.assertEqual(store.load({}).terminal_code, "SANDBOX-TEST")

    def test_environment_overrides_nonsecret_api_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            with patch.dict(os.environ, {"SWIFT_LEDGER_URL": "https://api.invalid", "SWIFT_LEDGER_TERMINAL": "TEST"}, clear=True):
                settings = store.load()
            self.assertEqual(settings.ledger_url, "https://api.invalid")
            self.assertEqual(settings.terminal_code, "TEST")

    def test_rejects_credentials_and_invalid_urls(self):
        for value in ["file://invalid", "https://user:password@example.com", "https://example.com?key=secret", "https://example.com:bad"]:
            with self.subTest(value=value), self.assertRaises(LedgerError):
                normalize_base_url(value)
        self.assertEqual(normalize_base_url(" http://127.0.0.1:5300/ "), "http://127.0.0.1:5300")


if __name__ == "__main__":
    unittest.main()
