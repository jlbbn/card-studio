import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from card_studio.coexistent_install import (
    APPLET_AID,
    CAP_SHA256,
    CONFIRMATION,
    DEFAULT_CAP,
    DEFAULT_GP_JAR,
    FORBIDDEN_OPTIONS,
    LFDB_SHA256,
    PACKAGE_AID,
    PRESERVED_APPLET_AID,
    PRESERVED_PACKAGE_AID,
    build_install_command,
    execute_install,
    inspect_cap,
    load_registry,
    parse_registry_text,
    prepare_install,
    redact_output,
    validate_registry,
)
from card_studio.model import CardError


def registry_entries():
    return [
        {"type": "PACKAGE", "aid": PRESERVED_PACKAGE_AID},
        {"type": "APPLET", "aid": PRESERVED_APPLET_AID},
    ]


class CoexistentInstallTests(unittest.TestCase):
    def registry_file(self, directory, mode="SIMULATED", entries=None, captured=True):
        path = Path(directory) / "registry.json"
        document = {"mode": mode, "entries": registry_entries() if entries is None else entries}
        if mode == "LIVE_READ_ONLY" and captured:
            document["captured_at"] = "2026-11-11T23:00:00Z"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_real_cap_hash_aids_and_lfdb(self):
        report = inspect_cap(DEFAULT_CAP, DEFAULT_GP_JAR)
        self.assertEqual(report["cap_sha256"], CAP_SHA256)
        self.assertEqual(report["load_file_data_hash_sha256"], LFDB_SHA256.upper())
        self.assertEqual(report["package_aid"], PACKAGE_AID)
        self.assertEqual(report["applet_aid"], APPLET_AID)

    def test_registry_text_parser(self):
        registry = parse_registry_text(
            f"PKG: {PRESERVED_PACKAGE_AID} (LOADED)\n"
            f"APP: {PRESERVED_APPLET_AID} (SELECTABLE)\n"
        )
        validation = validate_registry(registry)
        self.assertEqual(validation["result"], "PASS")
        self.assertTrue(validation["coexistent_applet_free"])

    def test_prepare_install_with_simulated_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry_file(directory)
            report = prepare_install(DEFAULT_CAP, DEFAULT_GP_JAR, registry)
            self.assertEqual(report["result"], "SIMULATED_READY")
            self.assertEqual(report["physical_install"], "NOT_EXECUTED")
            self.assertEqual(report["mode"], "SIMULATED")

    def test_simulated_registry_cannot_authorize_physical_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry_file(directory)
            with self.assertRaises(CardError) as caught:
                prepare_install(DEFAULT_CAP, DEFAULT_GP_JAR, registry, require_live=True)
            self.assertEqual(caught.exception.code, "LIVE_REGISTRY_REQUIRED")

    def test_live_registry_requires_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry_file(directory, mode="LIVE_READ_ONLY", captured=False)
            with self.assertRaises(CardError) as caught:
                load_registry(registry)
            self.assertEqual(caught.exception.code, "REGISTRY_TIMESTAMP_REQUIRED")

    def test_coexistent_package_or_applet_collision_blocks(self):
        for entry in ({"type": "PACKAGE", "aid": PACKAGE_AID}, {"type": "APPLET", "aid": APPLET_AID}):
            with self.subTest(entry=entry):
                registry = {"mode": "SIMULATED", "entries": [*registry_entries(), entry]}
                with self.assertRaises(CardError) as caught:
                    validate_registry(registry)
                self.assertEqual(caught.exception.code, "COEXISTENT_AID_COLLISION")

    def test_missing_preserved_aid_blocks(self):
        for entries in (registry_entries()[1:], registry_entries()[:1]):
            with self.subTest(entries=entries):
                with self.assertRaises(CardError) as caught:
                    validate_registry({"mode": "SIMULATED", "entries": entries})
                self.assertEqual(caught.exception.code, "PRESERVED_AID_MISSING")

    def test_cap_hash_mismatch_stops_before_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            cap = Path(directory) / "wrong.cap"
            cap.write_bytes(b"not the approved cap")
            with self.assertRaises(CardError) as caught:
                inspect_cap(cap, DEFAULT_GP_JAR)
            self.assertEqual(caught.exception.code, "CAP_HASH_MISMATCH")

    def test_command_has_no_destructive_options_or_key_argument(self):
        command = build_install_command(DEFAULT_GP_JAR, DEFAULT_CAP, "Reader 0")
        self.assertFalse(FORBIDDEN_OPTIONS.intersection(command))
        self.assertNotIn("--key", command)
        self.assertNotIn("--info", command)
        self.assertEqual(command[-2:], ["--reader", "Reader 0"])

    def test_redaction_covers_key_formats_and_preamble(self):
        key = "404142434445464748494A4B4C4D4E4F"
        spaced = " ".join(key[index:index + 2] for index in range(0, len(key), 2))
        colons = ":".join(key[index:index + 2] for index in range(0, len(key), 2))
        output = redact_output(f"GP_KEY: {key}\nspaced: {spaced}\ncolon: {colons}\n", key)
        self.assertNotIn(key, output)
        self.assertNotIn(spaced, output)
        self.assertNotIn(colons, output)
        self.assertIn("GP_KEY: [REDACTED]", output)

    def test_fake_physical_success_is_postcheck_required_and_redacted(self):
        key = "404142434445464748494A4B4C4D4E4F"
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=f"GP_KEY: {key}\nINSTALL OK\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry_file(directory, mode="LIVE_READ_ONLY")
            evidence = Path(directory) / "install.json"
            with patch.dict(os.environ, {"GP_KEY": key}):
                report = execute_install(DEFAULT_CAP, DEFAULT_GP_JAR, registry, evidence_path=evidence, runner=runner)
            self.assertEqual(report["result"], "INSTALL_COMMAND_PASS_POSTCHECK_REQUIRED")
            self.assertEqual(report["physical_validation"], "INCONCLUSIVE")
            self.assertEqual(len(calls), 1)
            self.assertNotIn(key, json.dumps(report))
            self.assertNotIn(key, evidence.read_text(encoding="utf-8"))

    def test_fake_physical_failure_does_not_retry(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=1, stdout="", stderr="rejected")

        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry_file(directory, mode="LIVE_READ_ONLY")
            with patch.dict(os.environ, {"GP_KEY": "404142434445464748494A4B4C4D4E4F"}):
                report = execute_install(DEFAULT_CAP, DEFAULT_GP_JAR, registry, runner=runner)
            self.assertEqual(report["result"], "FAIL")
            self.assertEqual(report["errors"], ["INSTALL_COMMAND_FAILED"])
            self.assertEqual(len(calls), 1)

    def test_fake_timeout_is_uncertain_and_does_not_retry(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            raise subprocess.TimeoutExpired(command, 120, output=b"GP_KEY: 404142434445464748494A4B4C4D4E4F")

        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry_file(directory, mode="LIVE_READ_ONLY")
            with patch.dict(os.environ, {"GP_KEY": "404142434445464748494A4B4C4D4E4F"}):
                report = execute_install(DEFAULT_CAP, DEFAULT_GP_JAR, registry, runner=runner)
            self.assertEqual(report["errors"], ["INSTALL_TIMEOUT_QUERY_REQUIRED"])
            self.assertEqual(report["physical_validation"], "INCONCLUSIVE")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("404142434445464748494A4B4C4D4E4F", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
