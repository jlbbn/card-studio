import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace


from card_studio.model import CardError
from card_studio.registry_capture import (
    CARDLAB_APPLET_AID,
    CARDLAB_PACKAGE_AID,
    FORBIDDEN_OPTIONS,
    build_registry_command,
    capture_registry,
    detect_authentication_signal,
    load_registry_status,
    parse_registry_text,
    summarize_cardlab,
)


def test_command_is_strictly_read_only():
    command = build_registry_command(
        "/usr/local/bin/gp",
        "Reader 0",
    )

    assert command == [
        "/usr/local/bin/gp",
        "--info",
        "--list",
        "--reader",
        "Reader 0",
    ]

    assert not FORBIDDEN_OPTIONS.intersection(command)

    assert "--key" not in command
    assert "--install" not in command
    assert "--delete" not in command


def test_parser_recognizes_cardlab():
    parsed = parse_registry_text(
        f"PKG: {CARDLAB_PACKAGE_AID} (LOADED)\n"
        f"APP: {CARDLAB_APPLET_AID} (SELECTABLE)\n"
    )

    result = summarize_cardlab(
        parsed["entries"]
    )

    assert result["package_present"] is True
    assert result["applet_present"] is True
    assert result["cardlab_registry_confirmed"] is True


def test_detects_gppro_default_key_signal():
    output = (
        "# Warning: no keys given, defaulting to "
        "DEADBEEFDEADBEEFDEADBEEFDEADBEEF"
    )

    assert (
        detect_authentication_signal(output)
        == "GPPRO_DEFAULT_KEY_FALLBACK_OBSERVED"
    )


def test_capture_generates_read_only_evidence(
    tmp_path: Path,
):
    evidence = tmp_path / "registry.json"

    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "# Warning: no keys given, defaulting to "
                "DEADBEEFDEADBEEFDEADBEEFDEADBEEF\n"
                f"PKG: {CARDLAB_PACKAGE_AID} (LOADED)\n"
                f"APP: {CARDLAB_APPLET_AID} (SELECTABLE)\n"
            ),
            stderr="",
        )

    report = capture_registry(
        evidence,
        gp_executable="/fake/gp",
        runner=runner,
    )

    assert report["mode"] == "LIVE_READ_ONLY"

    assert (
        report["authentication_signal"]
        == "GPPRO_DEFAULT_KEY_FALLBACK_OBSERVED"
    )

    assert (
        report["authentication_secret_recorded"]
        is False
    )

    assert (
        report["physical_modification"]
        == "NOT_EXECUTED"
    )

    assert (
        report["result"]
        == "CARDLAB_REGISTRY_CONFIRMED"
    )

    serialized = evidence.read_text(
        encoding="utf-8"
    )

    assert "DEADBEEF" not in serialized


def test_failure_does_not_retry(
    tmp_path: Path,
):
    evidence = tmp_path / "registry.json"

    calls = []

    def runner(command, **kwargs):
        calls.append(command)

        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="failure",
        )

    with unittest.TestCase().assertRaises(CardError) as caught:
        capture_registry(
            evidence,
            gp_executable="/fake/gp",
            runner=runner,
        )

    assert caught.exception.code == "REGISTRY_READ_FAILED"
    assert len(calls) == 1
    assert evidence.exists() is False


def test_timeout_does_not_retry(
    tmp_path: Path,
):
    evidence = tmp_path / "registry.json"

    calls = []

    def runner(command, **kwargs):
        calls.append(command)

        raise subprocess.TimeoutExpired(
            command,
            120,
        )

    with unittest.TestCase().assertRaises(CardError) as caught:
        capture_registry(
            evidence,
            gp_executable="/fake/gp",
            runner=runner,
        )

    assert caught.exception.code == "REGISTRY_TIMEOUT"
    assert len(calls) == 1
    assert evidence.exists() is False


def test_card_studio_cli_exposes_registry_status():
    from card_studio.cli import parser

    args = parser().parse_args([
        "registry-status",
    ])

    assert args.command == "registry-status"
    assert args.execute is False
    assert args.confirm_registry == ""
    assert args.evidence is not None



def test_load_registry_status_from_physical_evidence(
    tmp_path: Path,
):
    evidence = tmp_path / "registry.json"

    evidence.write_text(
        """{
  "schema": "globalplatform-registry-evidence/v2",
  "mode": "LIVE_READ_ONLY",
  "captured_at": "2026-11-15T04:00:00Z",
  "physical_modification": "NOT_EXECUTED",
  "authentication_signal": "GPPRO_DEFAULT_KEY_FALLBACK_OBSERVED",
  "authentication_secret_recorded": false,
  "result": "CARDLAB_REGISTRY_CONFIRMED",
  "cardlab": {
    "package_aid": "D4534C504D47",
    "applet_aid": "D4534C504D4701",
    "package_present": true,
    "applet_present": true,
    "cardlab_registry_confirmed": true
  }
}""",
        encoding="utf-8",
    )

    report = load_registry_status(evidence)

    assert report["status"] == "PASS"
    assert report["cardlab_registry_confirmed"] is True
    assert report["package_aid"] == "D4534C504D47"
    assert report["applet_aid"] == "D4534C504D4701"
    assert report["physical_modification"] == "NOT_EXECUTED"
    assert report["authentication_secret_recorded"] is False


def test_capture_registry_in_process_without_gppro(
    tmp_path: Path,
):
    from card_studio.registry_capture import (
        capture_registry_in_process,
    )

    class FakeRegistryReader:
        def read_registry(self):
            return [
                {
                    "type": "PACKAGE",
                    "aid": CARDLAB_PACKAGE_AID,
                    "life_cycle": "01",
                },
                {
                    "type": "APPLET",
                    "aid": CARDLAB_APPLET_AID,
                    "life_cycle": "07",
                },
            ]

    evidence = tmp_path / "registry-in-process.json"

    report = capture_registry_in_process(
        evidence,
        FakeRegistryReader(),
        reader_name="Fake Reader",
    )

    assert report["transport"] == "IN_PROCESS_CARD_SESSION"
    assert report["operation"] == "GLOBALPLATFORM_GET_STATUS"
    assert report["physical_modification"] == "NOT_EXECUTED"
    assert report["authentication_secret_recorded"] is False
    assert report["result"] == "CARDLAB_REGISTRY_CONFIRMED"

    serialized = evidence.read_text(encoding="utf-8")

    assert "gp --info" not in serialized
    assert "GP_KEY" not in serialized
