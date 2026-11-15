from __future__ import annotations

import platform as host_platform
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __build__, __version__
from .applet import (
    APPLET_AID_CANDIDATES,
    ApduResponse,
    build_select_apdu,
    interpret_select_response,
)
from .model import CardError
from .store import write_json_atomic
from .system import evidence_directory, platform_name


@dataclass(frozen=True)
class SelectAttempt:
    aid: str
    source: str
    select_apdu: str
    response_data: str
    sw1: str
    sw2: str
    sw1sw2: str
    result: str
    occurrence: str = "first"


def choose_reader(readers: list[str], preferred: str = "") -> tuple[str | None, str | None]:
    if not readers:
        return None, "READER_NOT_FOUND"
    if preferred.strip():
        exact = [reader for reader in readers if reader == preferred.strip()]
        if len(exact) == 1:
            return exact[0], None
        matches = [reader for reader in readers if preferred.strip().casefold() in reader.casefold()]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "MULTIPLE_READERS"
        return None, "READER_NOT_FOUND"
    if len(readers) == 1:
        return readers[0], None
    supported = [reader for reader in readers if "rocketek" in reader.casefold() or "cscr3" in reader.casefold()]
    if len(supported) == 1:
        return supported[0], None
    return None, "MULTIPLE_READERS"


def decide_aid(attempts: list[SelectAttempt], mode: str) -> tuple[str, str | None, str | None, str | None]:
    by_aid: dict[str, dict[str, SelectAttempt]] = {}
    for attempt in attempts:
        by_aid.setdefault(attempt.aid, {})[attempt.occurrence] = attempt

    first_selected = [
        attempt for attempt in attempts
        if attempt.occurrence == "first" and attempt.result == "APPLET_SELECTED"
    ]
    if len(first_selected) > 1:
        if any(
            by_aid[attempt.aid].get("next") is not None
            and by_aid[attempt.aid]["next"].result == "APPLET_SELECTED"
            for attempt in first_selected
        ):
            return "MULTIPLE_AID_MATCH", None, None, "MULTIPLE_OCCURRENCES"
        canonical = _collapse_prefix_matches(by_aid, first_selected)
        if canonical is not None:
            if mode == "PHYSICAL":
                return "PHYSICALLY_CONFIRMED", canonical.aid, None, "PREFIX_COLLAPSED"
            return "SIMULATED_MATCH", None, canonical.aid, "PREFIX_COLLAPSED"
        return "MULTIPLE_AID_MATCH", None, None, "AMBIGUOUS"
    if len(first_selected) == 1:
        aid = first_selected[0].aid
        next_attempt = by_aid[aid].get("next")
        if next_attempt is not None and next_attempt.result == "APPLET_SELECTED":
            return "MULTIPLE_AID_MATCH", None, None, "MULTIPLE_OCCURRENCES"
        others = [attempt for attempt in attempts if attempt.occurrence == "first" and attempt.aid != aid]
        if all(attempt.result == "APPLET_NOT_FOUND" for attempt in others):
            if mode == "PHYSICAL":
                return "PHYSICALLY_CONFIRMED", aid, None, "UNIQUE"
            return "SIMULATED_MATCH", None, aid, "UNIQUE"
        return "INCONCLUSIVE", None, None, "AMBIGUOUS"
    if attempts and all(attempt.result == "APPLET_NOT_FOUND" for attempt in attempts if attempt.occurrence == "first"):
        return "APPLET_NOT_FOUND", None, None, None
    if attempts:
        return attempts[0].result, None, None, None
    return "HARDWARE_VALIDATION_PENDING", None, None, None


def _collapse_prefix_matches(
    by_aid: dict[str, dict[str, SelectAttempt]],
    first_selected: list[SelectAttempt],
) -> SelectAttempt | None:
    """Colapsa AIDs solapados (prefijo exacto) que apuntan al mismo applet.

    Se colapsa cuando: cada AID tiene exactamente una ocurrencia (FIRST=9000
    y NEXT=6A82), las respuestas de FIRST son equivalentes y todos son
    prefijo del AID más largo. Devuelve el intent FIRST del AID canónico
    (el más largo) o None si no es un colapso seguro.
    """
    aids = [attempt.aid for attempt in first_selected]
    longest = max(aids, key=len)
    if not all(aid == longest or longest.startswith(aid) for aid in aids):
        return None
    reference = by_aid[longest]["first"].response_data
    for aid in aids:
        first = by_aid[aid]["first"]
        next_attempt = by_aid[aid].get("next")
        if next_attempt is None or next_attempt.result != "APPLET_NOT_FOUND":
            return None
        if first.response_data != reference:
            return None
    return by_aid[longest]["first"]


def _contains_forbidden_field(value: Any) -> bool:
    forbidden = {"password", "api_key", "private_key", "issuer_key", "pan", "pin", "cvv", "cvc", "track1", "track2", "secret"}
    if isinstance(value, dict):
        return any(str(key).casefold() in forbidden or _contains_forbidden_field(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden_field(item) for item in value)
    return False


def write_evidence(path: Path, report: dict[str, Any]) -> Path:
    if report.get("mode") not in {"SIMULATED", "PHYSICAL"}:
        raise ValueError("La evidencia debe declarar SIMULATED o PHYSICAL.")
    if report["mode"] == "SIMULATED" and (
        report.get("result") == "PHYSICALLY_CONFIRMED" or report.get("physical_validation") == "PASS"
    ):
        raise ValueError("Un dry-run no puede generar evidencia PHYSICALLY_CONFIRMED.")
    if _contains_forbidden_field(report):
        raise ValueError("La evidencia contiene campos sensibles prohibidos.")
    write_json_atomic(path, report)
    return path


def _attempt(candidate: Any, response: ApduResponse, occurrence: str = "first") -> SelectAttempt:
    status = interpret_select_response(response)
    return SelectAttempt(
        aid=candidate.aid,
        source=candidate.source,
        select_apdu=build_select_apdu(candidate.aid, occurrence=occurrence).hex().upper(),
        response_data=response.data.hex().upper(),
        sw1=f"{response.sw1:02X}",
        sw2=f"{response.sw2:02X}",
        sw1sw2=response.sw1sw2,
        result=status.result,
        occurrence=occurrence,
    )


def _failed_attempt(candidate: Any, error: CardError, occurrence: str = "first") -> SelectAttempt:
    return SelectAttempt(
        aid=candidate.aid,
        source=candidate.source,
        select_apdu=build_select_apdu(candidate.aid, occurrence=occurrence).hex().upper(),
        response_data="",
        sw1="",
        sw2="",
        sw1sw2="",
        result=error.code,
        occurrence=occurrence,
    )


class HardwareValidator:
    def __init__(self, adapter: Any, preferred_reader: str = ""):
        self.adapter = adapter
        self.preferred_reader = preferred_reader

    def run(self, evidence_path: Path | None = None) -> dict[str, Any]:
        mode = self.adapter.mode
        report: dict[str, Any] = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "application_version": __version__,
            "build": __build__,
            "platform": platform_name(),
            "architecture": host_platform.machine(),
            "mode": mode,
            "software_readiness": "PASS",
            "pcsc_available": False,
            "reader_name": None,
            "reader_detected": False,
            "reader_names": [],
            "card_detected": False,
            "atr": None,
            "aid_candidates": [asdict(candidate) for candidate in APPLET_AID_CANDIDATES],
            "select_attempts": [],
            "selected_aid": None,
            "simulated_selected_aid": None,
            "aid_resolution": None,
            "result": "HARDWARE_VALIDATION_PENDING",
            "physical_validation": "NOT_EXECUTED" if mode == "SIMULATED" else "PENDING",
            "disconnect_clean": False,
        }
        attempts: list[SelectAttempt] = []
        try:
            readers = self.adapter.listReaders()
            report["pcsc_available"] = True
            report["reader_names"] = readers
            reader, error = choose_reader(readers, self.preferred_reader)
            if error:
                report["result"] = "HARDWARE_NOT_CONNECTED" if error == "READER_NOT_FOUND" and not readers else error
            else:
                self.adapter.reader_name = reader
                report["reader_name"] = reader
                report["reader_detected"] = True
                self.adapter.waitForCard()
                report["card_detected"] = True
                report["atr"] = self.adapter.getATR().hex().upper()
                for candidate in APPLET_AID_CANDIDATES:
                    try:
                        response = self.adapter.selectApplet(candidate.aid)
                        attempt = _attempt(candidate, response, occurrence="first")
                    except CardError as error:
                        attempt = _failed_attempt(candidate, error, occurrence="first")
                    attempts.append(attempt)
                    report["select_attempts"] = [asdict(item) for item in attempts]
                    if attempt.result in {"CARD_REMOVED", "TRANSMIT_TIMEOUT", "TRANSMIT_FAILED"}:
                        break
                    if attempt.result != "APPLET_SELECTED":
                        continue
                    # Solo lectura: verificar que existe una única ocurrencia
                    # del AID (SELECT NEXT, P2=0x02). Nunca ejecuta escrituras.
                    try:
                        next_response = self.adapter.selectApplet(candidate.aid, occurrence="next")
                        next_attempt = _attempt(candidate, next_response, occurrence="next")
                    except CardError as error:
                        next_attempt = _failed_attempt(candidate, error, occurrence="next")
                    attempts.append(next_attempt)
                    report["select_attempts"] = [asdict(item) for item in attempts]
                    if next_attempt.result in {"CARD_REMOVED", "TRANSMIT_TIMEOUT", "TRANSMIT_FAILED"}:
                        break
                result, selected, simulated, aid_resolution = decide_aid(attempts, mode)
                report["result"] = result
                report["selected_aid"] = selected
                report["simulated_selected_aid"] = simulated
                report["aid_resolution"] = aid_resolution
                if result == "PHYSICALLY_CONFIRMED":
                    report["physical_validation"] = "PASS"
                elif result not in {"HARDWARE_VALIDATION_PENDING"}:
                    report["physical_validation"] = "NOT_EXECUTED" if mode == "SIMULATED" else "INCONCLUSIVE"
        except CardError as error:
            report["result"] = error.code
            if mode == "PHYSICAL" and error.code not in {"READER_NOT_FOUND", "CARD_NOT_PRESENT", "HARDWARE_NOT_CONNECTED"}:
                report["physical_validation"] = "FAIL"
        finally:
            try:
                self.adapter.disconnect()
                report["disconnect_clean"] = True
            except CardError as error:
                report["disconnect_error"] = error.code
        return self._finish(report, evidence_path)

    def _finish(self, report: dict[str, Any], evidence_path: Path | None) -> dict[str, Any]:
        path = evidence_path or evidence_directory() / ("dry-run.json" if report["mode"] == "SIMULATED" else "validation.json")
        write_evidence(path, report)
        report["evidence_path"] = str(path)
        return report


def validation_summary(report: dict[str, Any]) -> str:
    reader = "DETECTED" if report.get("reader_detected") else "NOT CONNECTED"
    card = "DETECTED" if report.get("card_detected") else "NOT PRESENT"
    lines = [
        f"Mode: {report['mode']}",
        f"Software readiness: {report['software_readiness']}",
        f"Reader: {reader}",
        f"Card: {card}",
        f"Result: {report['result']}",
    ]
    if report.get("selected_aid"):
        lines.append(f"AID: {report['selected_aid']}")
    if report.get("aid_resolution"):
        lines.append(f"Resolución de AID: {report['aid_resolution']}")
    if report.get("simulated_selected_aid"):
        lines.append(f"SIMULATED AID: {report['simulated_selected_aid']}")
    sw_values = [attempt["sw1sw2"] for attempt in report.get("select_attempts", []) if attempt["sw1sw2"]]
    if sw_values:
        lines.append(f"Status Word: {', '.join(sw_values)}")
    lines.append(f"Physical validation: {report['physical_validation']}")
    if report.get("evidence_path"):
        lines.append(f"Evidence: {report['evidence_path']}")
    return "\n".join(lines)
