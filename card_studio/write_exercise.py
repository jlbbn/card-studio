from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .applet import ApduResponse, build_select_apdu
from .hardware_validation import choose_reader, write_evidence
from .java_card_writer import JavaCardWriter, apdu_bytes
from .model import CardError
from .pcsc import create_pcsc_backend

APPLET_AID = "D4534C504D4601"
PRESERVED_APPLET_AID = "D4534C504D4401"
EXPECTED_ATR = "3BF81300008131FE454A434F5076323431B7"
INITIAL_PAYLOAD = b"SWIFT LEDGER DEMO|NONPAYMENT|5968"
EXERCISE_PAYLOAD = b"SWIFT LEDGER DEMO|NONPAYMENT|5969"
PRESERVED_STATE = b"SL1|SL-DEMO-5968|00000000|MXN"
READ_APDU = bytes.fromhex("80CA000100")
CONTRACT_PROBE_APDU = bytes.fromhex("80CA000200")
WRITE_PREFIX = bytes.fromhex("80DA0001")
CONFIRMATION = "ESCRIBIR_Y_RESTAURAR_5969"


def demo_write_apdu(payload: bytes) -> bytes:
    if len(payload) != 33:
        raise CardError("INVALID_PAYLOAD", "El ejercicio requiere exactamente 33 bytes.")
    return WRITE_PREFIX + bytes([len(payload)]) + payload


class SimulatedDemoAdapter:
    mode = "SIMULATED"

    def __init__(
        self,
        contract: str = "current",
        initial_payload: bytes = INITIAL_PAYLOAD,
        fail_restore: bool = False,
        fail_preserved_check: bool = False,
    ):
        self.contract = contract
        self.payload = bytes(initial_payload)
        self.fail_restore = fail_restore
        self.fail_preserved_check = fail_preserved_check
        self.reader_name = "JavaCard coexistente SIMULATED"
        self.commands: list[str] = []
        self.write_count = 0
        self.disconnected = False
        self.selected_aid: str | None = None

    def waitForCard(self) -> "SimulatedDemoAdapter":
        return self

    def getReader(self) -> str:
        return self.reader_name

    def getATR(self) -> bytes:
        return bytes.fromhex(EXPECTED_ATR)

    def selectApplet(self, aid: str) -> ApduResponse:
        self.commands.append(build_select_apdu(aid).hex().upper())
        available = aid == PRESERVED_APPLET_AID or (aid == APPLET_AID and self.contract != "previous")
        self.selected_aid = aid if available else None
        return ApduResponse(b"", 0x90, 0x00) if available else ApduResponse(b"", 0x6A, 0x82)

    def transmit(self, command: bytes | str) -> ApduResponse:
        raw = apdu_bytes(command)
        self.commands.append(raw.hex().upper())
        if raw == READ_APDU:
            return ApduResponse(self.payload, 0x90, 0x00)
        if raw == CONTRACT_PROBE_APDU:
            if self.selected_aid == PRESERVED_APPLET_AID:
                data = PRESERVED_STATE
                if self.fail_preserved_check and self.write_count >= 2:
                    data = data.replace(b"00000000", b"00000001")
                return ApduResponse(data, 0x90, 0x00)
            return ApduResponse(b"", 0x6A, 0x86)
        if raw.startswith(WRITE_PREFIX):
            self.write_count += 1
            if self.selected_aid != APPLET_AID:
                return ApduResponse(b"", 0x6A, 0x86)
            if self.fail_restore and self.write_count == 2:
                return ApduResponse(b"", 0x69, 0x85)
            if len(raw) != 38 or raw[4] != 33:
                return ApduResponse(b"", 0x67, 0x00)
            self.payload = raw[5:]
            return ApduResponse(b"", 0x90, 0x00)
        return ApduResponse(b"", 0x6D, 0x00)

    def disconnect(self) -> None:
        self.disconnected = True


def _read_preserved_state(adapter: Any) -> bytes:
    selected = adapter.selectApplet(PRESERVED_APPLET_AID)
    if selected.sw1sw2 != "9000":
        raise CardError(
            "PRESERVED_APPLET_NOT_FOUND",
            f"El applet preservado {PRESERVED_APPLET_AID} devolvió {selected.sw1sw2}.",
        )

    # El hardware físico actual no implementa CONTRACT_PROBE (80CA000200);
    # devuelve 6B00. En físico verificamos el contenido preservado mediante
    # el READ real soportado por el applet.
    if adapter.mode == "PHYSICAL":
        response = adapter.transmit(READ_APDU)
        if response.sw1sw2 != "9000":
            raise CardError(
                "PRESERVED_READ_FAILED",
                f"El applet preservado devolvió {response.sw1sw2}.",
            )
        if response.data != INITIAL_PAYLOAD:
            raise CardError(
                "PRESERVED_CONTRACT_MISMATCH",
                "El contenido del applet preservado no coincide con el estado demo 5968 esperado.",
            )
        return response.data

    response = adapter.transmit(CONTRACT_PROBE_APDU)
    if response.sw1sw2 != "9000" or not response.data.startswith(b"SL1|"):
        raise CardError(
            "PRESERVED_CONTRACT_MISMATCH",
            "El applet preservado no presenta el contrato SL1 esperado; no se escribirá nada.",
        )
    return response.data


def _preflight(adapter: Any) -> tuple[dict[str, Any], bytes]:
    adapter.waitForCard()
    reader_name = adapter.getReader()
    atr = adapter.getATR().hex().upper()
    if atr != EXPECTED_ATR:
        raise CardError("ATR_MISMATCH", "El ATR no coincide con la JavaCard de desarrollo esperada.")
    preserved_state = _read_preserved_state(adapter)
    selected = adapter.selectApplet(APPLET_AID)
    if selected.sw1sw2 != "9000":
        raise CardError(
            "COEXISTENT_APPLET_NOT_FOUND",
            f"El applet coexistible {APPLET_AID} devolvió {selected.sw1sw2}; no se escribirá en el AID anterior.",
        )
    before = adapter.transmit(READ_APDU)
    if before.sw1sw2 != "9000":
        raise CardError("PREFLIGHT_READ_FAILED", f"La lectura inicial devolvió {before.sw1sw2}.")
    if before.data != INITIAL_PAYLOAD:
        raise CardError(
            "INITIAL_CONTENT_MISMATCH",
            "El contenido inicial no es el valor de demostración 5968; no se escribirá ni restaurará nada.",
        )
    probe = adapter.transmit(CONTRACT_PROBE_APDU)
    expected_probe_sw = "6B00" if adapter.mode == "PHYSICAL" else "6A86"
    if probe.sw1sw2 != expected_probe_sw:
        raise CardError(
            "UNEXPECTED_APPLET_CONTRACT",
            f"La comprobación del contrato devolvió {probe.sw1sw2}; "
            f"se esperaba {expected_probe_sw}.",
        )
    return {
        "reader_name": reader_name,
        "atr": atr,
        "preserved_aid": PRESERVED_APPLET_AID,
        "preserved_state_sha256": hashlib.sha256(preserved_state).hexdigest(),
        "selected_aid": APPLET_AID,
        "select_sw": selected.sw1sw2,
        "initial_read_sha256": hashlib.sha256(before.data).hexdigest(),
        "contract_probe_sw": probe.sw1sw2,
        "contract": "COEXISTENT_33_BYTE",
        "result": "PASS",
    }, preserved_state


def _verify_preserved_state(adapter: Any, expected: bytes) -> dict[str, Any]:
    if adapter.mode == "PHYSICAL":
        adapter.connect()

    returned = _read_preserved_state(adapter)
    if returned != expected:
        raise CardError(
            "PRESERVED_STATE_CHANGED",
            "El estado del applet preservado cambió; la comprobación de coexistencia falló.",
        )
    return {
        "preserved_aid": PRESERVED_APPLET_AID,
        "preserved_state_sha256": hashlib.sha256(returned).hexdigest(),
        "preserved_state_match": True,
        "result": "PASS",
    }


def run_exercise(
    adapter: Any,
    *,
    confirm_write: bool = False,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    if not confirm_write:
        raise CardError("WRITE_CONFIRMATION_REQUIRED", "El ejercicio requiere confirmación explícita de ambas escrituras.")
    physical = adapter.mode == "PHYSICAL"
    report: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": adapter.mode,
        "exercise": "WRITE_5969_READBACK_RESTORE_5968_READBACK_VERIFY_PRESERVED",
        "applet_aid": APPLET_AID,
        "preserved_applet_aid": PRESERVED_APPLET_AID,
        "initial_payload_sha256": hashlib.sha256(INITIAL_PAYLOAD).hexdigest(),
        "exercise_payload_sha256": hashlib.sha256(EXERCISE_PAYLOAD).hexdigest(),
        "preflight": None,
        "exercise_write": None,
        "restore_write": None,
        "postflight": None,
        "physical_write_attempted": False,
        "write_commands_accepted": 0,
        "write_executed": False,
        "container_state": "CREATED",
        "physical_validation": "PENDING" if physical else "NOT_EXECUTED",
        "result": "NOT_EXECUTED",
        "errors": [],
    }
    try:
        preflight, preserved_before = _preflight(adapter)
        report["preflight"] = preflight
        report["container_state"] = "PREFLIGHT_PASS"
        adapter.disconnect()

        writer = JavaCardWriter(adapter, APPLET_AID, expected_atr=EXPECTED_ATR)
        report["physical_write_attempted"] = physical
        exercise = writer.write_and_readback(
            demo_write_apdu(EXERCISE_PAYLOAD),
            READ_APDU,
            EXERCISE_PAYLOAD,
            confirm_write=True,
        )
        report["exercise_write"] = exercise
        if exercise["status_words"]["write"] == "9000":
            report["write_commands_accepted"] += 1
        if exercise["result"] != "PASS":
            report["errors"].append("EXERCISE_WRITE_FAILED")
            report["container_state"] = "EXERCISE_FAILED"
        else:
            report["container_state"] = "EXERCISE_VERIFIED"
            restore = writer.write_and_readback(
                demo_write_apdu(INITIAL_PAYLOAD),
                READ_APDU,
                INITIAL_PAYLOAD,
                confirm_write=True,
            )
            report["restore_write"] = restore
            if restore["status_words"]["write"] == "9000":
                report["write_commands_accepted"] += 1
            if restore["result"] == "PASS":
                report["container_state"] = "RESTORED_5968"
                report["postflight"] = _verify_preserved_state(adapter, preserved_before)
                report["container_state"] = "RESTORED_5968_PRESERVED_UNCHANGED"
                report["result"] = "PASS"
                report["physical_validation"] = "PASS" if physical else "NOT_EXECUTED"
            else:
                report["errors"].append("RESTORE_FAILED")
                report["container_state"] = "RESTORE_FAILED_READBACK_REQUIRED"
        if report["result"] != "PASS":
            report["result"] = "FAIL"
            if not report["errors"]:
                report["errors"].append("EXERCISE_FAILED")
        report["write_executed"] = physical and report["write_commands_accepted"] > 0
        if physical and report["result"] != "PASS":
            report["physical_validation"] = "FAIL"
    except CardError as error:
        report["result"] = "FAIL"
        report["errors"].append(error.code)
        if report["write_commands_accepted"] == 0:
            report["container_state"] = "NO_WRITE"
        elif report["restore_write"] and report["restore_write"]["result"] == "PASS":
            report["container_state"] = "RESTORED_5968_PRESERVED_CHECK_FAILED"
        else:
            report["container_state"] = "EXERCISE_FAILED_READBACK_REQUIRED"
        report["write_executed"] = physical and report["write_commands_accepted"] > 0
        if physical:
            report["physical_validation"] = "FAIL" if report["physical_write_attempted"] else "INCONCLUSIVE"
    finally:
        try:
            adapter.disconnect()
        except CardError as error:
            report["errors"].append(error.code)
            report["result"] = "FAIL"
    if evidence_path is not None:
        write_evidence(Path(evidence_path), report)
        report["evidence_path"] = str(evidence_path)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Ejercicio JavaCard coexistible: escribir 5969, restaurar 5968 y verificar el applet anterior")
    result.add_argument("--execute", action="store_true", help="Usa PC/SC; sin esta opción todo es simulado.")
    result.add_argument("--reader", default="")
    result.add_argument("--confirm-exercise", default="")
    result.add_argument("--scenario", choices=["current", "previous", "restore-failure", "preserved-change"], default="current")
    result.add_argument("--evidence", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute and args.confirm_exercise != CONFIRMATION:
        print(json.dumps({
            "error": "WRITE_CONFIRMATION_REQUIRED",
            "required_confirmation": CONFIRMATION,
            "physical_validation": "NOT_EXECUTED",
        }, ensure_ascii=False, indent=2))
        return 2
    if args.execute and args.scenario != "current":
        print(json.dumps({"error": "SIMULATED_SCENARIO_WITH_PHYSICAL_MODE"}, indent=2))
        return 2
    try:
        if args.execute:
            discovery = create_pcsc_backend(reader_name=args.reader)
            reader, error = choose_reader(discovery.listReaders(), args.reader)
            if error:
                raise CardError(error, "No se pudo seleccionar un único lector PC/SC.")
            adapter: Any = create_pcsc_backend(reader_name=reader or "")
        else:
            adapter = SimulatedDemoAdapter(
                contract="previous" if args.scenario == "previous" else "current",
                fail_restore=args.scenario == "restore-failure",
                fail_preserved_check=args.scenario == "preserved-change",
            )
        report = run_exercise(adapter, confirm_write=True, evidence_path=args.evidence)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["result"] == "PASS" else 2
    except CardError as error:
        print(json.dumps({"error": error.code, "message": error.message}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
