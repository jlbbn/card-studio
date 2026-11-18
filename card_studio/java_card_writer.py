from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .applet import build_select_apdu
from .hardware_validation import write_evidence
from .model import CardError


def apdu_bytes(value: bytes | str) -> bytes:
    try:
        command = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
    except (TypeError, ValueError) as error:
        raise CardError("INVALID_APDU", "El comando APDU no contiene bytes hexadecimales válidos.") from error
    if len(command) < 4:
        raise CardError("INVALID_APDU", "El comando APDU debe incluir CLA, INS, P1 y P2.")
    return command


class JavaCardWriter:
    def __init__(self, adapter: Any, aid: str, expected_atr: str | None = None):
        build_select_apdu(aid)
        self.adapter = adapter
        self.aid = aid.strip().upper()
        self.expected_atr = expected_atr.replace(" ", "").upper() if expected_atr else None

    def write_and_readback(
        self,
        write_apdu: bytes | str,
        read_apdu: bytes | str,
        expected_readback: bytes,
        confirm_write: bool = False,
        evidence_path: Path | None = None,
    ) -> dict[str, Any]:
        if not confirm_write:
            raise CardError("WRITE_CONFIRMATION_REQUIRED", "La escritura requiere confirmación explícita.")
        write_command = apdu_bytes(write_apdu)
        read_command = apdu_bytes(read_apdu)
        expected = bytes(expected_readback)
        report: dict[str, Any] = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": self.adapter.mode,
            "reader_name": None,
            "atr": None,
            "selected_aid": None,
            "write_apdu_sha256": hashlib.sha256(write_command).hexdigest(),
            "read_apdu_sha256": hashlib.sha256(read_command).hexdigest(),
            "expected_readback_sha256": hashlib.sha256(expected).hexdigest(),
            "readback_sha256": None,
            "readback_match": False,
            "write_executed": False,
            "physical_validation": "PENDING" if self.adapter.mode == "PHYSICAL" else "NOT_EXECUTED",
            "container_state": "CREATED",
            "result": "NOT_EXECUTED",
            "status_words": {"select": None, "write": None, "read": None},
            "disconnect_clean": False,
            "errors": [],
        }
        try:
            self.adapter.waitForCard()
            report["reader_name"] = self.adapter.getReader()
            atr = self.adapter.getATR().hex().upper()
            report["atr"] = atr
            if self.expected_atr is not None and atr != self.expected_atr:
                raise CardError("ATR_MISMATCH", "El ATR no coincide con la tarjeta esperada.")
            selected = self.adapter.selectApplet(self.aid)
            report["status_words"]["select"] = selected.sw1sw2
            if selected.sw1sw2 != "9000":
                raise CardError("APPLET_NOT_FOUND", f"SELECT devolvió {selected.sw1sw2}.")
            report["selected_aid"] = self.aid
            report["container_state"] = "APPLET_SELECTED"

            written = self.adapter.transmit(write_command)
            report["status_words"]["write"] = written.sw1sw2
            if written.sw1sw2 != "9000":
                raise CardError("WRITE_FAILED", f"La escritura devolvió {written.sw1sw2}.")
            report["write_executed"] = self.adapter.mode == "PHYSICAL"
            report["container_state"] = "WRITE_ACCEPTED"

            read = self.adapter.transmit(read_command)
            report["status_words"]["read"] = read.sw1sw2
            if read.sw1sw2 != "9000":
                raise CardError("READBACK_FAILED", f"La lectura devolvió {read.sw1sw2}.")
            report["readback_sha256"] = hashlib.sha256(read.data).hexdigest()
            report["readback_match"] = read.data == expected
            if not report["readback_match"]:
                raise CardError("READBACK_MISMATCH", "El read-back no coincide con el contenido esperado.")

            report["container_state"] = "PERSONALIZED"
            report["physical_validation"] = "PASS" if self.adapter.mode == "PHYSICAL" else "NOT_EXECUTED"
            report["result"] = "PASS"
        except CardError as error:
            report["result"] = "FAIL"
            report["errors"].append(error.code)
            if self.adapter.mode == "PHYSICAL":
                report["physical_validation"] = "FAIL"
        finally:
            try:
                self.adapter.disconnect()
                report["disconnect_clean"] = True
            except CardError as error:
                report["errors"].append(error.code)
                report["result"] = "FAIL"
                if self.adapter.mode == "PHYSICAL":
                    report["physical_validation"] = "FAIL"
        if evidence_path is not None:
            write_evidence(Path(evidence_path), report)
            report["evidence_path"] = str(evidence_path)
        return report
