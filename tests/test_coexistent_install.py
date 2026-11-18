import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from card_studio.card_session import CardSession
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
    build_install_args,
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


class FakeInstallBackend:
    def __init__(self):
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.transmit_calls = []

        self.reader_name = "C2.4 Fake Reader"
        self.protocol = "T=0"
        self.atr = "3B6A00FF0031C173C84000009000"

    def connect(self):
        self.connect_calls += 1
        return self

    def transmit(self, command):
        self.transmit_calls.append(
            bytes(command)
        )

        return SimpleNamespace(
            data=b"",
            sw1sw2="9000",
        )

    def disconnect(self):
        self.disconnect_calls += 1


def open_fake_install_session():
    backend = FakeInstallBackend()

    session = CardSession(
        backend
    )

    session.open()

    return backend, session


class CoexistentInstallTests(unittest.TestCase):
    def registry_file(self, directory, mode="SIMULATED", entries=None, captured=True):
        path = Path(directory) / "registry.json"
        document = {"mode": mode, "entries": registry_entries() if entries is None else entries}
        if mode == "LIVE_READ_ONLY" and captured:
            document["captured_at"] = "2026-11-11T23:00:00Z"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    @unittest.skipUnless(
        Path(DEFAULT_CAP).is_file() and Path(DEFAULT_GP_JAR).is_file(),
        "CAP real / GlobalPlatformPro no disponibles en este entorno",
    )
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
            with patch(
                "card_studio.coexistent_install.inspect_cap",
                return_value={
                    "cap_path": "fixture.cap",
                    "cap_bytes": 1,
                    "cap_sha256": CAP_SHA256,
                    "load_file_data_hash_sha256": LFDB_SHA256.upper(),
                    "package_aid": PACKAGE_AID,
                    "applet_aid": APPLET_AID,
                    "capfile_parse": "PASS",
                },
            ):
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

    def test_install_args_have_no_external_reader_or_key_argument(self):
        args = build_install_args(
            DEFAULT_CAP
        )

        self.assertFalse(
            FORBIDDEN_OPTIONS.intersection(
                args
            )
        )

        self.assertNotIn(
            "java",
            args,
        )

        self.assertNotIn(
            "-jar",
            args,
        )

        self.assertNotIn(
            "--reader",
            args,
        )

        self.assertNotIn(
            "-r",
            args,
        )

        self.assertNotIn(
            "--pcsc-exclusive",
            args,
        )

        self.assertNotIn(
            "-X",
            args,
        )

        self.assertNotIn(
            "--key",
            args,
        )

        self.assertIn(
            "--install",
            args,
        )

        self.assertEqual(
            args[-1],
            str(DEFAULT_CAP),
        )

    def test_redaction_covers_key_formats_and_preamble(self):
        key = "404142434445464748494A4B4C4D4E4F"

        spaced = " ".join(
            key[index:index + 2]
            for index in range(
                0,
                len(key),
                2,
            )
        )

        colons = ":".join(
            key[index:index + 2]
            for index in range(
                0,
                len(key),
                2,
            )
        )

        output = redact_output(
            f"GP_KEY: {key}\n"
            f"spaced: {spaced}\n"
            f"colon: {colons}\n",
            key,
        )

        self.assertNotIn(
            key,
            output,
        )

        self.assertNotIn(
            spaced,
            output,
        )

        self.assertNotIn(
            colons,
            output,
        )

        self.assertIn(
            "GP_KEY: [REDACTED]",
            output,
        )

    def test_fake_physical_success_uses_in_process_bibo_and_preserves_session(self):
        key = "00112233445566778899AABBCCDDEEFF"
        calls = []

        def runner(args):
            calls.append(
                list(args)
            )
            return 0

        backend, session = (
            open_fake_install_session()
        )

        try:
            with tempfile.TemporaryDirectory() as directory:
                registry = self.registry_file(
                    directory,
                    mode="LIVE_READ_ONLY",
                )

                evidence = (
                    Path(directory)
                    / "install.json"
                )

                with patch.dict(
                    os.environ,
                    {"GP_KEY": key},
                ), patch(
                    "card_studio.coexistent_install.inspect_cap",
                    return_value={
                        "cap_path": "fixture.cap",
                        "cap_bytes": 1,
                        "cap_sha256": CAP_SHA256,
                        "load_file_data_hash_sha256": LFDB_SHA256.upper(),
                        "package_aid": PACKAGE_AID,
                        "applet_aid": APPLET_AID,
                        "capfile_parse": "PASS",
                    },
                ):
                    report = execute_install(
                        DEFAULT_CAP,
                        DEFAULT_GP_JAR,
                        registry,
                        card_session=session,
                        evidence_path=evidence,
                        runner=runner,
                    )

                self.assertEqual(
                    report["result"],
                    "INSTALL_COMMAND_PASS_POSTCHECK_REQUIRED",
                )

                self.assertEqual(
                    report["physical_validation"],
                    "INCONCLUSIVE",
                )

                self.assertEqual(
                    report["transport"]["type"],
                    "IN_PROCESS_BIBO",
                )

                self.assertTrue(
                    report["transport"]["single_card_session"]
                )

                self.assertFalse(
                    report["transport"]["external_reader_selection"]
                )

                self.assertFalse(
                    report["transport"]["subprocess_install"]
                )

                self.assertEqual(
                    len(calls),
                    1,
                )

                self.assertNotIn(
                    "java",
                    calls[0],
                )

                self.assertNotIn(
                    "--reader",
                    calls[0],
                )

                self.assertNotIn(
                    "--key",
                    calls[0],
                )

                self.assertNotIn(
                    key,
                    json.dumps(report),
                )

                self.assertNotIn(
                    key,
                    evidence.read_text(
                        encoding="utf-8"
                    ),
                )

                # execute_install must not own CardSession.
                self.assertTrue(
                    session.connected
                )

                self.assertEqual(
                    backend.connect_calls,
                    1,
                )

                self.assertEqual(
                    backend.disconnect_calls,
                    0,
                )

        finally:
            session.close()

        self.assertEqual(
            backend.disconnect_calls,
            1,
        )

    def test_fake_physical_failure_does_not_retry_or_close_session(self):
        calls = []

        def runner(args):
            calls.append(
                list(args)
            )
            return 1

        backend, session = (
            open_fake_install_session()
        )

        try:
            with tempfile.TemporaryDirectory() as directory:
                registry = self.registry_file(
                    directory,
                    mode="LIVE_READ_ONLY",
                )

                with patch.dict(
                    os.environ,
                    {
                        "GP_KEY":
                        "00112233445566778899AABBCCDDEEFF"
                    },
                ), patch(
                    "card_studio.coexistent_install.inspect_cap",
                    return_value={
                        "cap_path": "fixture.cap",
                        "cap_bytes": 1,
                        "cap_sha256": CAP_SHA256,
                        "load_file_data_hash_sha256": LFDB_SHA256.upper(),
                        "package_aid": PACKAGE_AID,
                        "applet_aid": APPLET_AID,
                        "capfile_parse": "PASS",
                    },
                ):
                    report = execute_install(
                        DEFAULT_CAP,
                        DEFAULT_GP_JAR,
                        registry,
                        card_session=session,
                        runner=runner,
                    )

                self.assertEqual(
                    report["result"],
                    "FAIL",
                )

                self.assertEqual(
                    report["errors"],
                    ["INSTALL_COMMAND_FAILED"],
                )

                self.assertEqual(
                    len(calls),
                    1,
                )

                self.assertTrue(
                    session.connected
                )

                self.assertEqual(
                    backend.disconnect_calls,
                    0,
                )

        finally:
            session.close()

        self.assertEqual(
            backend.disconnect_calls,
            1,
        )

    def test_fake_timeout_is_uncertain_and_does_not_retry(self):
        calls = []

        key = "00112233445566778899AABBCCDDEEFF"

        def runner(args):
            calls.append(
                list(args)
            )

            raise subprocess.TimeoutExpired(
                args,
                120,
                output=(
                    f"GP_KEY: {key}"
                ).encode(),
            )

        backend, session = (
            open_fake_install_session()
        )

        try:
            with tempfile.TemporaryDirectory() as directory:
                registry = self.registry_file(
                    directory,
                    mode="LIVE_READ_ONLY",
                )

                with patch.dict(
                    os.environ,
                    {"GP_KEY": key},
                ), patch(
                    "card_studio.coexistent_install.inspect_cap",
                    return_value={
                        "cap_path": "fixture.cap",
                        "cap_bytes": 1,
                        "cap_sha256": CAP_SHA256,
                        "load_file_data_hash_sha256": LFDB_SHA256.upper(),
                        "package_aid": PACKAGE_AID,
                        "applet_aid": APPLET_AID,
                        "capfile_parse": "PASS",
                    },
                ):
                    report = execute_install(
                        DEFAULT_CAP,
                        DEFAULT_GP_JAR,
                        registry,
                        card_session=session,
                        runner=runner,
                    )

                self.assertEqual(
                    report["errors"],
                    ["INSTALL_TIMEOUT_QUERY_REQUIRED"],
                )

                self.assertEqual(
                    report["physical_validation"],
                    "INCONCLUSIVE",
                )

                self.assertEqual(
                    len(calls),
                    1,
                )

                self.assertNotIn(
                    key,
                    json.dumps(report),
                )

                self.assertTrue(
                    session.connected
                )

                self.assertEqual(
                    backend.disconnect_calls,
                    0,
                )

        finally:
            session.close()

        self.assertEqual(
            backend.disconnect_calls,
            1,
        )

    def test_execute_install_requires_open_card_session(self):
        backend = FakeInstallBackend()

        session = CardSession(
            backend
        )

        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry_file(
                directory,
                mode="LIVE_READ_ONLY",
            )

            with patch.dict(
                os.environ,
                {
                    "GP_KEY":
                    "00112233445566778899AABBCCDDEEFF"
                },
            ), patch(
                "card_studio.coexistent_install.inspect_cap",
                return_value={
                    "cap_path": "fixture.cap",
                    "cap_bytes": 1,
                    "cap_sha256": CAP_SHA256,
                    "load_file_data_hash_sha256": LFDB_SHA256.upper(),
                    "package_aid": PACKAGE_AID,
                    "applet_aid": APPLET_AID,
                    "capfile_parse": "PASS",
                },
            ):
                with self.assertRaises(
                    CardError
                ) as caught:
                    execute_install(
                        DEFAULT_CAP,
                        DEFAULT_GP_JAR,
                        registry,
                        card_session=session,
                        runner=lambda args: 0,
                    )

        self.assertEqual(
            caught.exception.code,
            "GP_SESSION_NOT_OPEN",
        )

        self.assertEqual(
            backend.connect_calls,
            0,
        )

        self.assertEqual(
            backend.disconnect_calls,
            0,
        )


if __name__ == "__main__":
    unittest.main()



def test_build_read_only_args_is_list_only():
    from card_studio.coexistent_install import (
        READ_ONLY_FORBIDDEN_OPTIONS,
        build_read_only_args,
    )

    args = build_read_only_args()

    assert args == ["--list"]
    assert not READ_ONLY_FORBIDDEN_OPTIONS.intersection(args)


def test_read_only_contract_forbids_mutating_gp_options():
    from card_studio.coexistent_install import READ_ONLY_FORBIDDEN_OPTIONS

    expected = {
        "--install",
        "--load",
        "--delete",
        "--uninstall",
        "--put-key",
        "--replace-key",
        "--lock",
        "--force",
    }

    assert expected.issubset(READ_ONLY_FORBIDDEN_OPTIONS)



def test_secure_channel_preflight_args_are_read_only():
    from card_studio.coexistent_install import (
        READ_ONLY_FORBIDDEN_OPTIONS,
        build_secure_channel_preflight_args,
    )

    args = build_secure_channel_preflight_args()

    assert args == [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--list",
    ]

    assert not READ_ONLY_FORBIDDEN_OPTIONS.intersection(args)


def test_secure_channel_preflight_has_no_install_or_load():
    from card_studio.coexistent_install import (
        build_secure_channel_preflight_args,
    )

    args = build_secure_channel_preflight_args()

    forbidden = {
        "--install",
        "--load",
        "--delete",
        "--uninstall",
        "--put-key",
        "--replace-key",
        "--lock",
        "--force",
    }

    assert not forbidden.intersection(args)



def test_validate_gp_key_environment_accepts_16_byte_hex(monkeypatch):
    from card_studio.coexistent_install import (
        validate_gp_key_environment,
    )

    monkeypatch.setenv(
        "GP_KEY",
        "00112233445566778899AABBCCDDEEFF",
    )

    value = validate_gp_key_environment()

    assert isinstance(value, bytes)
    assert len(value) == 16


def test_validate_gp_key_environment_rejects_missing(monkeypatch):
    import pytest

    from card_studio.coexistent_install import (
        validate_gp_key_environment,
    )
    from card_studio.model import CardError

    monkeypatch.delenv(
        "GP_KEY",
        raising=False,
    )

    with pytest.raises(CardError) as exc:
        validate_gp_key_environment()

    assert exc.value.code == "GP_KEY_REQUIRED"


def test_validate_gp_key_environment_rejects_default(monkeypatch):
    import pytest

    from card_studio.coexistent_install import (
        validate_gp_key_environment,
    )
    from card_studio.model import CardError

    monkeypatch.setenv(
        "GP_KEY",
        "default-test-key",
    )

    with pytest.raises(CardError) as exc:
        validate_gp_key_environment()

    assert exc.value.code == "GP_DEFAULT_KEY_FORBIDDEN"


def test_validate_gp_key_environment_rejects_bad_hex(monkeypatch):
    import pytest

    from card_studio.coexistent_install import (
        validate_gp_key_environment,
    )
    from card_studio.model import CardError

    monkeypatch.setenv(
        "GP_KEY",
        "NOTHEX",
    )

    with pytest.raises(CardError) as exc:
        validate_gp_key_environment()

    assert exc.value.code == "GP_KEY_INVALID_HEX"


def test_validate_gp_key_environment_rejects_bad_length(monkeypatch):
    import pytest

    from card_studio.coexistent_install import (
        validate_gp_key_environment,
    )
    from card_studio.model import CardError

    monkeypatch.setenv(
        "GP_KEY",
        "00112233",
    )

    with pytest.raises(CardError) as exc:
        validate_gp_key_environment()

    assert exc.value.code == "GP_KEY_INVALID_LENGTH"



def test_secure_preflight_missing_key_stops_before_runner(
    monkeypatch,
):
    import pytest

    from card_studio.coexistent_install import (
        execute_secure_channel_preflight,
    )
    from card_studio.model import CardError

    monkeypatch.delenv(
        "GP_KEY",
        raising=False,
    )

    class FakeSession:
        connected = True

    calls = []

    def runner(args):
        calls.append(list(args))
        return 0

    with pytest.raises(CardError) as exc:
        execute_secure_channel_preflight(
            card_session=FakeSession(),
            gp_jar=Path("/tmp/gp.jar"),
            runner=runner,
        )

    assert exc.value.code == "GP_KEY_REQUIRED"
    assert calls == []


def test_secure_preflight_invalid_key_stops_before_runner(
    monkeypatch,
):
    import pytest

    from card_studio.coexistent_install import (
        execute_secure_channel_preflight,
    )
    from card_studio.model import CardError

    monkeypatch.setenv(
        "GP_KEY",
        "NOTHEX",
    )

    class FakeSession:
        connected = True

    calls = []

    def runner(args):
        calls.append(list(args))
        return 0

    with pytest.raises(CardError) as exc:
        execute_secure_channel_preflight(
            card_session=FakeSession(),
            gp_jar=Path("/tmp/gp.jar"),
            runner=runner,
        )

    assert exc.value.code == "GP_KEY_INVALID_HEX"
    assert calls == []


def test_secure_preflight_valid_key_reaches_read_only_runner(
    monkeypatch,
):
    from card_studio.coexistent_install import (
        execute_secure_channel_preflight,
    )

    monkeypatch.setenv(
        "GP_KEY",
        "00112233445566778899AABBCCDDEEFF",
    )

    class FakeSession:
        connected = True

    calls = []

    def runner(args):
        calls.append(list(args))
        return 0

    rc = execute_secure_channel_preflight(
        card_session=FakeSession(),
        gp_jar=Path("/tmp/gp.jar"),
        runner=runner,
    )

    assert rc == 0

    assert calls == [[
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--list",
    ]]


def test_secure_preflight_closed_session_stops_before_runner(
    monkeypatch,
):
    import pytest

    from card_studio.coexistent_install import (
        execute_secure_channel_preflight,
    )
    from card_studio.model import CardError

    monkeypatch.setenv(
        "GP_KEY",
        "00112233445566778899AABBCCDDEEFF",
    )

    class FakeSession:
        connected = False

    calls = []

    def runner(args):
        calls.append(list(args))
        return 0

    with pytest.raises(CardError) as exc:
        execute_secure_channel_preflight(
            card_session=FakeSession(),
            gp_jar=Path("/tmp/gp.jar"),
            runner=runner,
        )

    assert exc.value.code == "GP_SESSION_NOT_OPEN"
    assert calls == []



def test_execute_without_confirmation_stops_before_backend(
    monkeypatch,
    tmp_path,
):
    import card_studio.coexistent_install as module

    backend_calls = []

    def forbidden_backend(*args, **kwargs):
        backend_calls.append((args, kwargs))
        raise AssertionError(
            "backend must not be created without confirmation"
        )

    monkeypatch.setattr(
        module,
        "create_pcsc_backend",
        forbidden_backend,
    )

    rc = module.main([
        "--registry",
        str(tmp_path / "unused.json"),
        "--execute",
    ])

    assert rc == 2
    assert backend_calls == []


def test_execute_with_wrong_confirmation_stops_before_backend(
    monkeypatch,
    tmp_path,
):
    import card_studio.coexistent_install as module

    backend_calls = []

    def forbidden_backend(*args, **kwargs):
        backend_calls.append((args, kwargs))
        raise AssertionError(
            "backend must not be created with wrong confirmation"
        )

    monkeypatch.setattr(
        module,
        "create_pcsc_backend",
        forbidden_backend,
    )

    rc = module.main([
        "--registry",
        str(tmp_path / "unused.json"),
        "--execute",
        "--confirm-install",
        "INCORRECTA",
    ])

    assert rc == 2
    assert backend_calls == []


def test_exact_confirmation_is_required_for_execute(
    monkeypatch,
    tmp_path,
):
    import card_studio.coexistent_install as module

    backend_calls = []

    class StopHere(Exception):
        pass

    def marker_backend(*args, **kwargs):
        backend_calls.append((args, kwargs))
        raise StopHere

    monkeypatch.setattr(
        module,
        "create_pcsc_backend",
        marker_backend,
    )

    try:
        module.main([
            "--registry",
            str(tmp_path / "unused.json"),
            "--execute",
            "--confirm-install",
            module.CONFIRMATION,
        ])
    except StopHere:
        pass
    else:
        raise AssertionError(
            "exact confirmation did not reach backend creation"
        )

    assert len(backend_calls) == 1



def test_install_argument_contract_exact():
    from card_studio.coexistent_install import (
        build_install_args,
    )

    cap = Path("/tmp/approved.cap")

    assert build_install_args(cap) == [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--install", str(cap),
    ]


def test_install_argument_contract_rejects_load():
    import pytest

    from card_studio.coexistent_install import (
        validate_install_args,
    )
    from card_studio.model import CardError

    cap = Path("/tmp/approved.cap")

    args = [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--load", str(cap),
    ]

    with pytest.raises(CardError) as exc:
        validate_install_args(
            args,
            cap,
        )

    assert exc.value.code == (
        "INSTALL_ARGUMENT_CONTRACT_VIOLATION"
    )


def test_install_argument_contract_rejects_delete():
    import pytest

    from card_studio.coexistent_install import (
        validate_install_args,
    )
    from card_studio.model import CardError

    cap = Path("/tmp/approved.cap")

    args = [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--delete", "A000000001",
        "--install", str(cap),
    ]

    with pytest.raises(CardError) as exc:
        validate_install_args(
            args,
            cap,
        )

    assert exc.value.code == (
        "INSTALL_ARGUMENT_CONTRACT_VIOLATION"
    )


def test_install_argument_contract_rejects_reader_override():
    import pytest

    from card_studio.coexistent_install import (
        validate_install_args,
    )
    from card_studio.model import CardError

    cap = Path("/tmp/approved.cap")

    args = [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--reader", "Other Reader",
        "--install", str(cap),
    ]

    with pytest.raises(CardError) as exc:
        validate_install_args(
            args,
            cap,
        )

    assert exc.value.code == (
        "INSTALL_ARGUMENT_CONTRACT_VIOLATION"
    )


def test_install_argument_contract_rejects_cli_key():
    import pytest

    from card_studio.coexistent_install import (
        validate_install_args,
    )
    from card_studio.model import CardError

    cap = Path("/tmp/approved.cap")

    args = [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--key",
        "00112233445566778899AABBCCDDEEFF",
        "--install", str(cap),
    ]

    with pytest.raises(CardError) as exc:
        validate_install_args(
            args,
            cap,
        )

    assert exc.value.code == (
        "INSTALL_ARGUMENT_CONTRACT_VIOLATION"
    )



def test_secure_preflight_production_path_uses_direct_gp_api():
    import inspect

    from card_studio.coexistent_install import (
        execute_secure_channel_preflight,
    )

    source = inspect.getsource(
        execute_secure_channel_preflight
    )

    assert (
        "key_material = validate_gp_key_material_environment()"
        in source
    )

    assert (
        "build_plaintext_keys_from_material"
        in source
    )

    assert (
        "keys.setVersion"
        in source
    )

    assert (
        "GPSession.discover"
        in source
    )

    assert (
        "gp.openSecureChannel"
        in source
    )

    assert (
        "gp.getRegistry()"
        in source
    )

    assert (
        "bridge.run_tool"
        not in source
    )



def test_execute_install_production_path_uses_direct_gp_api():
    import inspect

    from card_studio.coexistent_install import (
        execute_install,
    )

    source = inspect.getsource(
        execute_install
    )

    assert (
        "key_material = validate_gp_key_material_environment()"
        in source
    )

    assert (
        "build_plaintext_keys_from_material"
        in source
    )

    assert (
        "keys.setVersion"
        in source
    )

    assert (
        "GPSession.discover"
        in source
    )

    assert (
        "gp.openSecureChannel"
        in source
    )

    assert (
        "java_registry = gp.getRegistry()"
        in source
    )

    assert (
        "validate_registry("
        in source
    )

    assert (
        "CAPFile.fromFile"
        in source
    )

    assert (
        "gp.loadCapFile"
        in source
    )

    assert (
        "gp.installAndMakeSelectable"
        in source
    )

    assert (
        "bridge.run_tool"
        not in source
    )

    assert (
        "gp.deleteAID"
        not in source
    )

    # Live coexistence validation must happen before LOAD.
    assert (
        source.index(
            "validate_registry("
        )
        < source.index(
            "gp.loadCapFile"
        )
    )

    # Mutation marker must be set immediately before the
    # direct mutating operations.
    assert (
        source.index(
            "mutation_started = True"
        )
        < source.index(
            "gp.loadCapFile"
        )
    )



def test_execute_install_live_registry_blocks_before_load(
    monkeypatch,
    tmp_path,
):
    """
    Production-path simulation with NO CARD.

    The fake Secure Channel succeeds, but the authenticated live
    registry is missing the preserved applet. execute_install()
    must fail closed before CAPFile.fromFile(), LOAD or INSTALL.
    """

    import sys
    import types

    import card_studio.coexistent_install as module


    calls = {
        "bridge_start": 0,
        "discover": 0,
        "open_secure_channel": 0,
        "get_registry": 0,
        "cap_from_file": 0,
        "load": 0,
        "install": 0,
    }


    # ---------------------------------------------------------
    # Existing offline registry/preflight is deliberately mocked
    # PASS so this test isolates the NEW authenticated live gate.
    # ---------------------------------------------------------

    def fake_prepare_install(
        cap_path,
        gp_jar,
        registry_path,
        *,
        require_live=False,
    ):
        assert require_live is True

        return {
            "physical_install": "NOT_EXECUTED",
            "physical_validation": "PENDING",
            "result": "READY_FOR_AUTHORIZED_PHYSICAL_INSTALL",
            "errors": [],
        }


    monkeypatch.setattr(
        module,
        "prepare_install",
        fake_prepare_install,
    )


    # Synthetic test key only.
    monkeypatch.setenv(
        "GP_KEY",
        "00112233445566778899AABBCCDDEEFF",
    )


    # ---------------------------------------------------------
    # Existing Card Studio BIBO ownership, but NO real transport.
    # ---------------------------------------------------------

    class FakeBridge:

        def __init__(
            self,
            card_session,
            gp_jar,
        ):
            self.card_session = card_session
            self.gp_jar = gp_jar

        def start(self):
            calls["bridge_start"] += 1
            return object()


    monkeypatch.setattr(
        module,
        "GPProBIBOBridge",
        FakeBridge,
    )


    # ---------------------------------------------------------
    # Fake JPype/Java contracts.
    # ---------------------------------------------------------

    class FakeKeyInfo:

        def __init__(self):
            self.version = None

        def getVersion(self):
            return self.version


    class FakeKeys:

        def __init__(self):
            self.info = FakeKeyInfo()

        def setVersion(
            self,
            version,
        ):
            self.info.version = int(
                version
            )

        def getKeyInfo(self):
            return self.info


    class FakePlaintextKeys:

        @staticmethod
        def fromMasterKey(
            raw,
        ):
            assert len(raw) == 16
            return FakeKeys()


    class FakeMode:
        class_ = object()

        MAC = "MAC"
        ENC = "ENC"


    class FakePrivilege:
        class_ = object()


    class FakeEnumSet(set):

        @staticmethod
        def noneOf(
            _kind,
        ):
            return FakeEnumSet()


    class FakeRegistry:
        """
        Deliberately INVALID live state:

        preserved package  -> present
        preserved applet   -> MISSING

        target package     -> absent
        target applet      -> absent
        """

        def allPackageAIDs(self):
            return [
                module.PRESERVED_PACKAGE_AID,
            ]

        def allAppletAIDs(self):
            return []


    class FakeGP:

        def openSecureChannel(
            self,
            keys,
            scp,
            challenge,
            modes,
        ):
            calls[
                "open_secure_channel"
            ] += 1

            assert (
                keys.getKeyInfo().getVersion()
                == 0
            )

            assert "MAC" in modes
            assert "ENC" in modes

            assert scp is None
            assert challenge is None

        def getRegistry(self):
            calls[
                "get_registry"
            ] += 1

            return FakeRegistry()

        def getAID(self):
            raise AssertionError(
                "getAID must not be reached "
                "after invalid live registry"
            )

        def loadCapFile(
            self,
            *args,
        ):
            calls["load"] += 1

            raise AssertionError(
                "LOAD MUST NOT BE SENT"
            )

        def installAndMakeSelectable(
            self,
            *args,
        ):
            calls["install"] += 1

            raise AssertionError(
                "INSTALL MUST NOT BE SENT"
            )


    fake_gp = FakeGP()


    class FakeGPSession:

        @staticmethod
        def discover(
            proxy,
        ):
            calls["discover"] += 1
            return fake_gp


    class FakeCAPFile:

        @staticmethod
        def fromFile(
            path,
        ):
            calls[
                "cap_from_file"
            ] += 1

            raise AssertionError(
                "CAPFile.fromFile must not be reached "
                "after invalid live registry"
            )


    class FakePaths:

        @staticmethod
        def get(
            value,
        ):
            return value


    def fake_JClass(
        name,
    ):
        mapping = {
            "pro.javacard.gp.GPSession":
                FakeGPSession,

            "pro.javacard.gp.GPSession$APDUMode":
                FakeMode,

            "pro.javacard.gp.keys.PlaintextKeys":
                FakePlaintextKeys,

            "pro.javacard.capfile.CAPFile":
                FakeCAPFile,

            "pro.javacard.gp.GPRegistryEntry$Privilege":
                FakePrivilege,

            "java.util.EnumSet":
                FakeEnumSet,

            "java.nio.file.Paths":
                FakePaths,
        }

        assert name in mapping, name

        return mapping[name]


    def fake_JArray(
        _kind,
    ):
        return lambda values: list(
            values
        )


    fake_jpype = types.ModuleType(
        "jpype"
    )

    fake_jpype.JArray = fake_JArray
    fake_jpype.JByte = int
    fake_jpype.JClass = fake_JClass

    monkeypatch.setitem(
        sys.modules,
        "jpype",
        fake_jpype,
    )


    # ---------------------------------------------------------
    # Minimal connected session object.
    # There is NO actual reader/card behind it.
    # ---------------------------------------------------------

    class FakeCardSession:
        connected = True


    cap = (
        tmp_path
        / "approved.cap"
    )

    gp_jar = (
        tmp_path
        / "gp.jar"
    )

    registry = (
        tmp_path
        / "registry.json"
    )


    report = module.execute_install(
        cap,
        gp_jar,
        registry,
        card_session=FakeCardSession(),
    )


    # ---------------------------------------------------------
    # Secure Channel preparation was reached.
    # ---------------------------------------------------------

    assert (
        calls["bridge_start"]
        == 1
    )

    assert (
        calls["discover"]
        == 1
    )

    assert (
        calls["open_secure_channel"]
        == 1
    )

    assert (
        calls["get_registry"]
        == 1
    )


    # ---------------------------------------------------------
    # CRITICAL fail-closed assertions.
    # ---------------------------------------------------------

    assert (
        calls["cap_from_file"]
        == 0
    )

    assert (
        calls["load"]
        == 0
    )

    assert (
        calls["install"]
        == 0
    )


    assert (
        report["physical_install"]
        == "NOT_EXECUTED"
    )

    assert (
        report["physical_validation"]
        == "FAIL"
    )

    assert (
        report["result"]
        == "FAIL"
    )

    assert (
        "INSTALL_BLOCKED_BEFORE_MUTATION"
        in report["errors"]
    )

    assert (
        "exception_redacted"
        in report["command_result"]
    )

    print(
        "SECURE_CHANNEL_OPENED = True"
    )

    print(
        "AUTHENTICATED_REGISTRY_READ = True"
    )

    print(
        "INVALID_LIVE_REGISTRY = True"
    )

    print(
        "CAPFILE_FROM_FILE_CALLS =",
        calls["cap_from_file"],
    )

    print(
        "LOAD_CALLS =",
        calls["load"],
    )

    print(
        "INSTALL_CALLS =",
        calls["install"],
    )

    print(
        "PASS: invalid live registry blocked before mutation"
    )



def test_execute_install_valid_live_registry_reaches_load_then_install(
    monkeypatch,
    tmp_path,
):
    """
    Production-path simulation with NO CARD.

    A valid authenticated live registry must allow exactly:

        Secure Channel
        -> registry validation
        -> CAP validation
        -> LOAD
        -> INSTALL AND MAKE SELECTABLE

    No DELETE or GPTool execution is involved.
    """

    import sys
    import types

    import card_studio.coexistent_install as module


    events = []

    calls = {
        "load": 0,
        "install": 0,
    }


    # ---------------------------------------------------------
    # Existing disk/evidence preflight passes.
    # This test isolates the direct authenticated path.
    # ---------------------------------------------------------

    def fake_prepare_install(
        cap_path,
        gp_jar,
        registry_path,
        *,
        require_live=False,
    ):
        assert require_live is True

        return {
            "physical_install":
                "NOT_EXECUTED",

            "physical_validation":
                "PENDING",

            "result":
                "READY_FOR_AUTHORIZED_PHYSICAL_INSTALL",

            "errors": [],
        }


    monkeypatch.setattr(
        module,
        "prepare_install",
        fake_prepare_install,
    )


    # Synthetic test key only.
    monkeypatch.setenv(
        "GP_KEY",
        "00112233445566778899AABBCCDDEEFF",
    )


    # ---------------------------------------------------------
    # Existing Card Studio BIBO owner; no actual PC/SC.
    # ---------------------------------------------------------

    class FakeBridge:

        def __init__(
            self,
            card_session,
            gp_jar,
        ):
            self.card_session = card_session
            self.gp_jar = gp_jar

        def start(self):
            events.append(
                "bridge_start"
            )

            return object()


    monkeypatch.setattr(
        module,
        "GPProBIBOBridge",
        FakeBridge,
    )


    # ---------------------------------------------------------
    # Fake Java key contract.
    # ---------------------------------------------------------

    class FakeKeyInfo:

        def __init__(self):
            self.version = None

        def getVersion(self):
            return self.version


    class FakeKeys:

        def __init__(self):
            self.info = FakeKeyInfo()

        def setVersion(
            self,
            value,
        ):
            self.info.version = int(
                value
            )

        def getKeyInfo(self):
            return self.info


    class FakePlaintextKeys:

        @staticmethod
        def fromMasterKey(
            raw,
        ):
            assert len(raw) == 16

            return FakeKeys()


    # ---------------------------------------------------------
    # Java enums/collections.
    # ---------------------------------------------------------

    class FakeMode:
        class_ = object()

        MAC = "MAC"
        ENC = "ENC"


    class FakePrivilege:
        class_ = object()


    class FakeEnumSet(set):

        @staticmethod
        def noneOf(
            _kind,
        ):
            return FakeEnumSet()


    # ---------------------------------------------------------
    # VALID authenticated registry.
    #
    # Preserved AIDs present.
    # Target AIDs absent.
    # ---------------------------------------------------------

    class FakeRegistry:

        def allPackageAIDs(self):
            return [
                module.PRESERVED_PACKAGE_AID,
            ]

        def allAppletAIDs(self):
            return [
                module.PRESERVED_APPLET_AID,
            ]


    # ---------------------------------------------------------
    # Exact approved CAP identity.
    # ---------------------------------------------------------

    class FakeCAP:

        def getPackageAID(self):
            return module.PACKAGE_AID

        def getAppletAIDs(self):
            return [
                module.APPLET_AID,
            ]


    fake_cap = FakeCAP()


    class FakeCAPFile:

        @staticmethod
        def fromFile(
            path,
        ):
            events.append(
                "cap_from_file"
            )

            return fake_cap


    class FakePaths:

        @staticmethod
        def get(
            value,
        ):
            return value


    # ---------------------------------------------------------
    # Direct GPSession.
    # ---------------------------------------------------------

    class FakeGP:

        def openSecureChannel(
            self,
            keys,
            scp,
            challenge,
            modes,
        ):
            events.append(
                "open_secure_channel"
            )

            assert (
                keys.getKeyInfo().getVersion()
                == 0
            )

            assert scp is None
            assert challenge is None

            assert "MAC" in modes
            assert "ENC" in modes


        def getRegistry(self):
            events.append(
                "get_registry"
            )

            return FakeRegistry()


        def getAID(self):
            events.append(
                "get_aid"
            )

            return (
                "A000000151000000"
            )


        def loadCapFile(
            self,
            cap,
            target_sd,
            dap_domain,
            dap_signature,
            lfdbh,
        ):
            events.append(
                "load"
            )

            calls["load"] += 1

            assert cap is fake_cap

            assert (
                str(target_sd).upper()
                == "A000000151000000"
            )

            assert dap_domain is None
            assert dap_signature is None
            assert lfdbh is None


        def installAndMakeSelectable(
            self,
            package,
            applet,
            instance,
            privileges,
            params,
        ):
            events.append(
                "install"
            )

            calls["install"] += 1

            assert (
                str(package).upper()
                == module.PACKAGE_AID
            )

            assert (
                str(applet).upper()
                == module.APPLET_AID
            )

            assert (
                str(instance).upper()
                == module.APPLET_AID
            )

            assert len(
                privileges
            ) == 0

            assert len(
                params
            ) == 0


    fake_gp = FakeGP()


    class FakeGPSession:

        @staticmethod
        def discover(
            proxy,
        ):
            events.append(
                "discover"
            )

            return fake_gp


    def fake_JClass(
        name,
    ):
        mapping = {
            "pro.javacard.gp.GPSession":
                FakeGPSession,

            "pro.javacard.gp.GPSession$APDUMode":
                FakeMode,

            "pro.javacard.gp.keys.PlaintextKeys":
                FakePlaintextKeys,

            "pro.javacard.capfile.CAPFile":
                FakeCAPFile,

            "pro.javacard.gp.GPRegistryEntry$Privilege":
                FakePrivilege,

            "java.util.EnumSet":
                FakeEnumSet,

            "java.nio.file.Paths":
                FakePaths,
        }

        assert name in mapping, name

        return mapping[name]


    def fake_JArray(
        _kind,
    ):
        return lambda values: list(
            values
        )


    fake_jpype = types.ModuleType(
        "jpype"
    )

    fake_jpype.JArray = fake_JArray
    fake_jpype.JByte = int
    fake_jpype.JClass = fake_JClass

    monkeypatch.setitem(
        sys.modules,
        "jpype",
        fake_jpype,
    )


    class FakeCardSession:
        connected = True


    cap = (
        tmp_path
        / "approved.cap"
    )

    gp_jar = (
        tmp_path
        / "gp.jar"
    )

    registry_path = (
        tmp_path
        / "registry.json"
    )


    report = module.execute_install(
        cap,
        gp_jar,
        registry_path,
        card_session=FakeCardSession(),
    )


    # ---------------------------------------------------------
    # Exact execution order.
    # ---------------------------------------------------------

    expected_events = [
        "bridge_start",
        "discover",
        "open_secure_channel",
        "get_registry",
        "cap_from_file",
        "get_aid",
        "load",
        "install",
    ]

    assert (
        events
        == expected_events
    )


    # ---------------------------------------------------------
    # Exactly one mutating LOAD and one INSTALL.
    # ---------------------------------------------------------

    assert calls["load"] == 1
    assert calls["install"] == 1


    # ---------------------------------------------------------
    # Live coexistence gate passed.
    # ---------------------------------------------------------

    live = report[
        "authenticated_live_registry"
    ]

    assert (
        live["result"]
        == "PASS"
    )

    assert (
        live["preserved_package_present"]
        is True
    )

    assert (
        live["preserved_applet_present"]
        is True
    )

    assert (
        live["coexistent_package_free"]
        is True
    )

    assert (
        live["coexistent_applet_free"]
        is True
    )


    # ---------------------------------------------------------
    # Report semantics after successful command execution.
    # Post-check is still required.
    # ---------------------------------------------------------

    assert (
        report["physical_install"]
        == "EXECUTED_PASS_POSTCHECK_REQUIRED"
    )

    assert (
        report["physical_validation"]
        == "INCONCLUSIVE"
    )

    assert (
        report["result"]
        == "INSTALL_COMMAND_PASS_POSTCHECK_REQUIRED"
    )

    assert (
        report["command_result"]["returncode"]
        == 0
    )

    assert (
        report["command_result"]["gptool_used"]
        is False
    )

    assert (
        report["command_result"]["gp_execution"]
        == "DIRECT_GPSession"
    )


    print(
        "SECURE_CHANNEL_OPENED = True"
    )

    print(
        "VALID_LIVE_REGISTRY = True"
    )

    print(
        "CAP_CONTRACT_VALIDATED = True"
    )

    print(
        "LOAD_CALLS =",
        calls["load"],
    )

    print(
        "INSTALL_CALLS =",
        calls["install"],
    )

    print(
        "ORDER =",
        " -> ".join(
            events
        ),
    )

    print(
        "GPTool_USED = False"
    )

    print(
        "CARD_CONNECTED = False"
    )

    print(
        "PASS: valid live registry reaches LOAD then INSTALL"
    )



def test_execute_install_after_load_install_failure_requires_query(
    monkeypatch,
    tmp_path,
):
    """
    NO CARD simulation.

    LOAD succeeds, then INSTALL AND MAKE SELECTABLE raises.

    Because mutation has already started, execute_install() must
    NEVER claim rollback, clean failure, or NOT_EXECUTED.
    """

    import sys
    import types

    import card_studio.coexistent_install as module


    events = []

    calls = {
        "load": 0,
        "install": 0,
    }


    # ---------------------------------------------------------
    # Pre-existing disk/live evidence passes.
    # ---------------------------------------------------------

    def fake_prepare_install(
        cap_path,
        gp_jar,
        registry_path,
        *,
        require_live=False,
    ):
        assert require_live is True

        return {
            "physical_install":
                "NOT_EXECUTED",

            "physical_validation":
                "PENDING",

            "result":
                "READY_FOR_AUTHORIZED_PHYSICAL_INSTALL",

            "errors": [],
        }


    monkeypatch.setattr(
        module,
        "prepare_install",
        fake_prepare_install,
    )


    # Synthetic test key only.
    monkeypatch.setenv(
        "GP_KEY",
        "00112233445566778899AABBCCDDEEFF",
    )


    # ---------------------------------------------------------
    # No physical transport.
    # ---------------------------------------------------------

    class FakeBridge:

        def __init__(
            self,
            card_session,
            gp_jar,
        ):
            pass

        def start(self):
            events.append(
                "bridge_start"
            )

            return object()


    monkeypatch.setattr(
        module,
        "GPProBIBOBridge",
        FakeBridge,
    )


    # ---------------------------------------------------------
    # Fake key / Java support.
    # ---------------------------------------------------------

    class FakeKeyInfo:

        def __init__(self):
            self.version = None

        def getVersion(self):
            return self.version


    class FakeKeys:

        def __init__(self):
            self.info = FakeKeyInfo()

        def setVersion(
            self,
            value,
        ):
            self.info.version = int(
                value
            )

        def getKeyInfo(self):
            return self.info


    class FakePlaintextKeys:

        @staticmethod
        def fromMasterKey(
            raw,
        ):
            assert len(raw) == 16

            return FakeKeys()


    class FakeMode:
        class_ = object()

        MAC = "MAC"
        ENC = "ENC"


    class FakePrivilege:
        class_ = object()


    class FakeEnumSet(set):

        @staticmethod
        def noneOf(
            _kind,
        ):
            return FakeEnumSet()


    # ---------------------------------------------------------
    # Valid authenticated live registry.
    # ---------------------------------------------------------

    class FakeRegistry:

        def allPackageAIDs(self):
            return [
                module.PRESERVED_PACKAGE_AID,
            ]

        def allAppletAIDs(self):
            return [
                module.PRESERVED_APPLET_AID,
            ]


    # ---------------------------------------------------------
    # Exact approved CAP.
    # ---------------------------------------------------------

    class FakeCAP:

        def getPackageAID(self):
            return module.PACKAGE_AID

        def getAppletAIDs(self):
            return [
                module.APPLET_AID,
            ]


    fake_cap = FakeCAP()


    class FakeCAPFile:

        @staticmethod
        def fromFile(
            path,
        ):
            events.append(
                "cap_from_file"
            )

            return fake_cap


    class FakePaths:

        @staticmethod
        def get(
            value,
        ):
            return value


    # ---------------------------------------------------------
    # GPSession:
    #
    # LOAD succeeds.
    # INSTALL fails afterwards.
    # ---------------------------------------------------------

    class FakeGP:

        def openSecureChannel(
            self,
            keys,
            scp,
            challenge,
            modes,
        ):
            events.append(
                "open_secure_channel"
            )

            assert (
                keys.getKeyInfo().getVersion()
                == 0
            )

            assert "MAC" in modes
            assert "ENC" in modes

            assert scp is None
            assert challenge is None


        def getRegistry(self):
            events.append(
                "get_registry"
            )

            return FakeRegistry()


        def getAID(self):
            events.append(
                "get_aid"
            )

            return (
                "A000000151000000"
            )


        def loadCapFile(
            self,
            cap,
            target_sd,
            dap_domain,
            dap_signature,
            lfdbh,
        ):
            events.append(
                "load"
            )

            calls["load"] += 1

            assert cap is fake_cap
            assert dap_domain is None
            assert dap_signature is None
            assert lfdbh is None

            # Successful return means LOAD completed from the
            # caller's perspective.


        def installAndMakeSelectable(
            self,
            package,
            applet,
            instance,
            privileges,
            params,
        ):
            events.append(
                "install"
            )

            calls["install"] += 1

            assert (
                str(package).upper()
                == module.PACKAGE_AID
            )

            assert (
                str(applet).upper()
                == module.APPLET_AID
            )

            assert (
                str(instance).upper()
                == module.APPLET_AID
            )

            assert len(privileges) == 0
            assert len(params) == 0

            raise RuntimeError(
                "synthetic INSTALL failure after successful LOAD"
            )


    fake_gp = FakeGP()


    class FakeGPSession:

        @staticmethod
        def discover(
            proxy,
        ):
            events.append(
                "discover"
            )

            return fake_gp


    def fake_JClass(
        name,
    ):
        mapping = {
            "pro.javacard.gp.GPSession":
                FakeGPSession,

            "pro.javacard.gp.GPSession$APDUMode":
                FakeMode,

            "pro.javacard.gp.keys.PlaintextKeys":
                FakePlaintextKeys,

            "pro.javacard.capfile.CAPFile":
                FakeCAPFile,

            "pro.javacard.gp.GPRegistryEntry$Privilege":
                FakePrivilege,

            "java.util.EnumSet":
                FakeEnumSet,

            "java.nio.file.Paths":
                FakePaths,
        }

        assert name in mapping, name

        return mapping[name]


    def fake_JArray(
        _kind,
    ):
        return lambda values: list(
            values
        )


    fake_jpype = types.ModuleType(
        "jpype"
    )

    fake_jpype.JArray = fake_JArray
    fake_jpype.JByte = int
    fake_jpype.JClass = fake_JClass

    monkeypatch.setitem(
        sys.modules,
        "jpype",
        fake_jpype,
    )


    class FakeCardSession:
        connected = True


    report = module.execute_install(
        tmp_path / "approved.cap",
        tmp_path / "gp.jar",
        tmp_path / "registry.json",
        card_session=FakeCardSession(),
    )


    expected_events = [
        "bridge_start",
        "discover",
        "open_secure_channel",
        "get_registry",
        "cap_from_file",
        "get_aid",
        "load",
        "install",
    ]

    assert events == expected_events


    # ---------------------------------------------------------
    # Mutation definitely began.
    # ---------------------------------------------------------

    assert calls["load"] == 1
    assert calls["install"] == 1


    # ---------------------------------------------------------
    # CRITICAL outcome semantics.
    # ---------------------------------------------------------

    assert (
        report["physical_install"]
        == "EXECUTED_EXCEPTION_OUTCOME_UNKNOWN"
    )

    assert (
        report["physical_validation"]
        == "INCONCLUSIVE"
    )

    assert (
        report["result"]
        == "FAIL"
    )

    assert (
        "INSTALL_EXCEPTION_QUERY_REQUIRED"
        in report["errors"]
    )

    assert (
        "INSTALL_BLOCKED_BEFORE_MUTATION"
        not in report["errors"]
    )

    assert (
        report["physical_install"]
        != "NOT_EXECUTED"
    )

    assert (
        "exception_redacted"
        in report["command_result"]
    )

    assert (
        "synthetic INSTALL failure"
        in report["command_result"][
            "exception_redacted"
        ]
    )


    print(
        "LOAD_COMPLETED = True"
    )

    print(
        "INSTALL_ATTEMPTED = True"
    )

    print(
        "INSTALL_FAILED = True"
    )

    print(
        "PHYSICAL_INSTALL =",
        report["physical_install"],
    )

    print(
        "PHYSICAL_VALIDATION =",
        report["physical_validation"],
    )

    print(
        "QUERY_REQUIRED =",
        "INSTALL_EXCEPTION_QUERY_REQUIRED"
        in report["errors"],
    )

    print(
        "NOT_EXECUTED_CLAIMED =",
        report["physical_install"]
        == "NOT_EXECUTED",
    )

    print(
        "CARD_CONNECTED = False"
    )

    print(
        "PASS: partial mutation never reported as clean rollback"
    )



def test_execute_install_load_failure_requires_query_and_skips_install(
    monkeypatch,
    tmp_path,
):
    """
    NO CARD simulation.

    Authenticated live registry and CAP validation pass.
    LOAD is attempted and raises.

    INSTALL must never be attempted. Because the mutating boundary
    has already been crossed, outcome must remain unknown/query-required.
    """

    import sys
    import types

    import card_studio.coexistent_install as module

    events = []

    calls = {
        "load": 0,
        "install": 0,
    }

    def fake_prepare_install(
        cap_path,
        gp_jar,
        registry_path,
        *,
        require_live=False,
    ):
        assert require_live is True

        return {
            "physical_install": "NOT_EXECUTED",
            "physical_validation": "PENDING",
            "result": "READY_FOR_AUTHORIZED_PHYSICAL_INSTALL",
            "errors": [],
        }

    monkeypatch.setattr(
        module,
        "prepare_install",
        fake_prepare_install,
    )

    monkeypatch.setenv(
        "GP_KEY",
        "00112233445566778899AABBCCDDEEFF",
    )

    class FakeBridge:
        def __init__(self, card_session, gp_jar):
            pass

        def start(self):
            events.append("bridge_start")
            return object()

    monkeypatch.setattr(
        module,
        "GPProBIBOBridge",
        FakeBridge,
    )

    class FakeKeyInfo:
        def __init__(self):
            self.version = None

        def getVersion(self):
            return self.version

    class FakeKeys:
        def __init__(self):
            self.info = FakeKeyInfo()

        def setVersion(self, value):
            self.info.version = int(value)

        def getKeyInfo(self):
            return self.info

    class FakePlaintextKeys:
        @staticmethod
        def fromMasterKey(raw):
            assert len(raw) == 16
            return FakeKeys()

    class FakeMode:
        class_ = object()
        MAC = "MAC"
        ENC = "ENC"

    class FakePrivilege:
        class_ = object()

    class FakeEnumSet(set):
        @staticmethod
        def noneOf(_kind):
            return FakeEnumSet()

    class FakeRegistry:
        def allPackageAIDs(self):
            return [
                module.PRESERVED_PACKAGE_AID,
            ]

        def allAppletAIDs(self):
            return [
                module.PRESERVED_APPLET_AID,
            ]

    class FakeCAP:
        def getPackageAID(self):
            return module.PACKAGE_AID

        def getAppletAIDs(self):
            return [
                module.APPLET_AID,
            ]

    fake_cap = FakeCAP()

    class FakeCAPFile:
        @staticmethod
        def fromFile(path):
            events.append("cap_from_file")
            return fake_cap

    class FakePaths:
        @staticmethod
        def get(value):
            return value

    class FakeGP:
        def openSecureChannel(
            self,
            keys,
            scp,
            challenge,
            modes,
        ):
            events.append("open_secure_channel")

            assert keys.getKeyInfo().getVersion() == 0
            assert "MAC" in modes
            assert "ENC" in modes
            assert scp is None
            assert challenge is None

        def getRegistry(self):
            events.append("get_registry")
            return FakeRegistry()

        def getAID(self):
            events.append("get_aid")
            return "A000000151000000"

        def loadCapFile(
            self,
            cap,
            target_sd,
            dap_domain,
            dap_signature,
            lfdbh,
        ):
            events.append("load")
            calls["load"] += 1

            assert cap is fake_cap
            assert dap_domain is None
            assert dap_signature is None
            assert lfdbh is None

            raise RuntimeError(
                "synthetic LOAD failure"
            )

        def installAndMakeSelectable(
            self,
            *args,
        ):
            events.append("install")
            calls["install"] += 1

            raise AssertionError(
                "INSTALL MUST NOT RUN AFTER LOAD FAILURE"
            )

    fake_gp = FakeGP()

    class FakeGPSession:
        @staticmethod
        def discover(proxy):
            events.append("discover")
            return fake_gp

    def fake_JClass(name):
        mapping = {
            "pro.javacard.gp.GPSession":
                FakeGPSession,

            "pro.javacard.gp.GPSession$APDUMode":
                FakeMode,

            "pro.javacard.gp.keys.PlaintextKeys":
                FakePlaintextKeys,

            "pro.javacard.capfile.CAPFile":
                FakeCAPFile,

            "pro.javacard.gp.GPRegistryEntry$Privilege":
                FakePrivilege,

            "java.util.EnumSet":
                FakeEnumSet,

            "java.nio.file.Paths":
                FakePaths,
        }

        assert name in mapping, name
        return mapping[name]

    def fake_JArray(_kind):
        return lambda values: list(values)

    fake_jpype = types.ModuleType("jpype")
    fake_jpype.JArray = fake_JArray
    fake_jpype.JByte = int
    fake_jpype.JClass = fake_JClass

    monkeypatch.setitem(
        sys.modules,
        "jpype",
        fake_jpype,
    )

    class FakeCardSession:
        connected = True

    report = module.execute_install(
        tmp_path / "approved.cap",
        tmp_path / "gp.jar",
        tmp_path / "registry.json",
        card_session=FakeCardSession(),
    )

    assert events == [
        "bridge_start",
        "discover",
        "open_secure_channel",
        "get_registry",
        "cap_from_file",
        "get_aid",
        "load",
    ]

    assert calls["load"] == 1
    assert calls["install"] == 0

    assert (
        report["physical_install"]
        == "EXECUTED_EXCEPTION_OUTCOME_UNKNOWN"
    )

    assert (
        report["physical_validation"]
        == "INCONCLUSIVE"
    )

    assert report["result"] == "FAIL"

    assert (
        "INSTALL_EXCEPTION_QUERY_REQUIRED"
        in report["errors"]
    )

    assert (
        "INSTALL_BLOCKED_BEFORE_MUTATION"
        not in report["errors"]
    )

    assert (
        report["physical_install"]
        != "NOT_EXECUTED"
    )

    print("LOAD_ATTEMPTED = True")
    print("LOAD_FAILED = True")
    print("INSTALL_CALLS =", calls["install"])

    print(
        "PHYSICAL_INSTALL =",
        report["physical_install"],
    )

    print(
        "QUERY_REQUIRED =",
        "INSTALL_EXCEPTION_QUERY_REQUIRED"
        in report["errors"],
    )

    print(
        "NOT_EXECUTED_CLAIMED =",
        report["physical_install"]
        == "NOT_EXECUTED",
    )

    print("CARD_CONNECTED = False")

    print(
        "PASS: LOAD failure preserves outcome-unknown semantics"
    )



def test_new_card_registry_profile_allows_absent_legacy_aids():
    from card_studio.coexistent_install import (
        validate_registry,
    )

    registry = {
        "mode": "LIVE_READ_ONLY",
        "entries": [
            {
                "type": "PACKAGE",
                "aid": "A0000001515350",
            },
            {
                "type": "APPLET",
                "aid": "D276000085304A434F900001",
            },
        ],
    }

    result = validate_registry(
        registry,
        require_live=True,
    )

    assert result["result"] == "PASS"
    assert result["card_profile"] == "NEW_CARD"

    assert (
        result["preserved_package_present"]
        is False
    )

    assert (
        result["preserved_applet_present"]
        is False
    )

    assert (
        result["coexistent_package_free"]
        is True
    )

    assert (
        result["coexistent_applet_free"]
        is True
    )


def test_split_gp_key_environment_contract(monkeypatch):
    import card_studio.coexistent_install as module

    monkeypatch.delenv(
        "GP_KEY",
        raising=False,
    )

    monkeypatch.setenv(
        "GP_KEY_ENC",
        "000102030405060708090A0B0C0D0E0F",
    )

    monkeypatch.setenv(
        "GP_KEY_MAC",
        "101112131415161718191A1B1C1D1E1F",
    )

    monkeypatch.setenv(
        "GP_KEY_DEK",
        "202122232425262728292A2B2C2D2E2F",
    )

    material = (
        module.validate_gp_key_material_environment()
    )

    assert material["mode"] == "SPLIT"

    assert material["enc"] == bytes.fromhex(
        "000102030405060708090A0B0C0D0E0F"
    )

    assert material["mac"] == bytes.fromhex(
        "101112131415161718191A1B1C1D1E1F"
    )

    assert material["dek"] == bytes.fromhex(
        "202122232425262728292A2B2C2D2E2F"
    )


def test_split_gp_key_environment_rejects_partial(monkeypatch):
    import pytest

    import card_studio.coexistent_install as module
    from card_studio.model import CardError

    monkeypatch.delenv(
        "GP_KEY",
        raising=False,
    )

    monkeypatch.setenv(
        "GP_KEY_ENC",
        "000102030405060708090A0B0C0D0E0F",
    )

    monkeypatch.delenv(
        "GP_KEY_MAC",
        raising=False,
    )

    monkeypatch.delenv(
        "GP_KEY_DEK",
        raising=False,
    )

    with pytest.raises(CardError) as exc:
        module.validate_gp_key_material_environment()

    assert (
        exc.value.code
        == "GP_SPLIT_KEYS_INCOMPLETE"
    )


def test_split_gp_key_environment_rejects_mixed_master(monkeypatch):
    import pytest

    import card_studio.coexistent_install as module
    from card_studio.model import CardError

    monkeypatch.setenv(
        "GP_KEY",
        "303132333435363738393A3B3C3D3E3F",
    )

    monkeypatch.setenv(
        "GP_KEY_ENC",
        "000102030405060708090A0B0C0D0E0F",
    )

    monkeypatch.setenv(
        "GP_KEY_MAC",
        "101112131415161718191A1B1C1D1E1F",
    )

    monkeypatch.setenv(
        "GP_KEY_DEK",
        "202122232425262728292A2B2C2D2E2F",
    )

    with pytest.raises(CardError) as exc:
        module.validate_gp_key_material_environment()

    assert (
        exc.value.code
        == "GP_KEY_CONFIGURATION_AMBIGUOUS"
    )


def test_plaintext_keys_split_order_is_enc_mac_dek():
    from card_studio.coexistent_install import (
        build_plaintext_keys_from_material,
    )

    captured = {}

    class FakePlaintextKeys:

        @staticmethod
        def fromMasterKey(raw):
            raise AssertionError(
                "master-key constructor must not be used"
            )

        @staticmethod
        def fromKeys(enc, mac, dek):
            captured["enc"] = enc
            captured["mac"] = mac
            captured["dek"] = dek

            return "SPLIT_KEYS"


    def FakeJArray(_kind):
        return lambda values: list(values)


    material = {
        "mode": "SPLIT",

        "enc": bytes.fromhex(
            "000102030405060708090A0B0C0D0E0F"
        ),

        "mac": bytes.fromhex(
            "101112131415161718191A1B1C1D1E1F"
        ),

        "dek": bytes.fromhex(
            "202122232425262728292A2B2C2D2E2F"
        ),
    }


    result = build_plaintext_keys_from_material(
        material,
        FakePlaintextKeys,
        FakeJArray,
        int,
    )


    def unsigned(values):
        return bytes(
            value & 0xFF
            for value in values
        )


    assert result == "SPLIT_KEYS"

    assert unsigned(
        captured["enc"]
    ) == material["enc"]

    assert unsigned(
        captured["mac"]
    ) == material["mac"]

    assert unsigned(
        captured["dek"]
    ) == material["dek"]
