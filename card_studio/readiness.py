from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .applet import APPLET_AID_CANDIDATES, build_select_apdu, parse_apdu_response, interpret_status_word
from .dry_run import DryRunAdapter
from .hardware_validation import HardwareValidator, write_evidence
from .pcsc import PcscBackend


def run_readiness(project_root: Path, include_tests: bool = True) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def check(name: str, action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception as error:
            checks.append({"name": name, "status": "FAIL", "detail": str(error)})
        else:
            checks.append({"name": name, "status": "PASS", "detail": ""})

    def candidates() -> None:
        assert [item.aid for item in APPLET_AID_CANDIDATES] == ["D4534C504D4401", "D4534C504D44"]
        assert all(item.source for item in APPLET_AID_CANDIDATES)

    def adapter() -> None:
        for method in ["listReaders", "connect", "waitForCard", "getATR", "transmit", "selectApplet", "disconnect"]:
            assert hasattr(PcscBackend, method), method

    def builder() -> None:
        assert build_select_apdu(APPLET_AID_CANDIDATES[0].aid).hex().upper() == "00A4040007D4534C504D4401"
        assert build_select_apdu(APPLET_AID_CANDIDATES[1].aid).hex().upper() == "00A4040006D4534C504D44"

    def parser() -> None:
        response = parse_apdu_response(bytes.fromhex("01029000"))
        assert response.data == bytes([1, 2]) and response.sw1sw2 == "9000"

    def status_words() -> None:
        assert interpret_status_word("9000").success
        assert interpret_status_word("6A82").applet_not_found
        assert interpret_status_word("9F10").code == "9F10"

    def dry_runs() -> None:
        expected = {
            "aid1-selected": "SIMULATED_MATCH",
            "aid2-selected": "SIMULATED_MATCH",
            "multiple": "SIMULATED_MATCH",
            "not-found": "APPLET_NOT_FOUND",
            "reader-absent": "HARDWARE_NOT_CONNECTED",
            "card-absent": "CARD_NOT_PRESENT",
            "timeout": "TRANSMIT_TIMEOUT",
            "card-removed": "CARD_REMOVED",
            "truncated": "INVALID_RESPONSE",
            "unknown-sw": "INVALID_STATUS_WORD",
        }
        with tempfile.TemporaryDirectory() as directory:
            for scenario, result in expected.items():
                report = HardwareValidator(DryRunAdapter(scenario)).run(Path(directory) / f"{scenario}.json")
                assert report["mode"] == "SIMULATED"
                assert report["result"] == result, (scenario, report["result"])
                assert report["physical_validation"] == "NOT_EXECUTED"
                assert report["selected_aid"] is None

    def evidence() -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = HardwareValidator(DryRunAdapter()).run(Path(directory) / "evidence.json")
            assert (Path(directory) / "evidence.json").is_file()
            assert report["mode"] == "SIMULATED"

    def tests() -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        match = re.search(r"Ran (\d+) tests", completed.stderr)
        assert match, "No se encontró el conteo de pruebas"
        checks.append({"name": "test-count", "status": "PASS", "detail": match.group(1)})

    def command() -> None:
        script = project_root / "scripts" / "validate_applet_hardware"
        assert script.is_file()
        assert script.stat().st_mode & 0o111

    check("aid-configuration", candidates)
    check("pcsc-adapter", adapter)
    check("select-builder", builder)
    check("response-parser", parser)
    check("status-word-interpreter", status_words)
    check("evidence-writer", evidence)
    check("dry-run-and-mocks", dry_runs)
    if include_tests:
        check("tests", tests)
    check("hardware-validation-command", command)
    passed = all(item["status"] == "PASS" for item in checks)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "SIMULATED",
        "checks": checks,
        "software_readiness": "PASS" if passed else "FAIL",
        "hardware_readiness": "PASS" if passed else "FAIL",
        "physical_validation": "PENDING",
        "result": "HARDWARE_READY" if passed else "NOT_READY",
    }
