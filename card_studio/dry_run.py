from __future__ import annotations

from .applet import APPLET_AID_CANDIDATES, ApduResponse, parse_apdu_response
from .model import CardError


class DryRunAdapter:
    mode = "SIMULATED"

    def __init__(self, scenario: str = "aid1-selected", reader_name: str = "Rocketek CSCR3 SIMULATED"):
        self.scenario = scenario
        self.reader_name = reader_name
        self.disconnected = False

    def listReaders(self) -> list[str]:
        return [] if self.scenario == "reader-absent" else [self.reader_name]

    def connect(self) -> "DryRunAdapter":
        if self.scenario == "card-absent":
            raise CardError("CARD_NOT_PRESENT", "SIMULATED: tarjeta ausente.")
        return self

    def waitForCard(self, timeout_seconds: float = 0.0, poll_interval: float = 0.25) -> "DryRunAdapter":
        return self.connect()

    def getReader(self) -> str:
        if self.scenario == "reader-absent":
            raise CardError("READER_NOT_FOUND", "SIMULATED: lector ausente.")
        return self.reader_name

    def getATR(self) -> bytes:
        return bytes.fromhex("3B0000")

    def transmit(self, command: bytes | str) -> ApduResponse:
        raise CardError("INVALID_APDU", "SIMULATED: este dry-run solo admite SELECT.")

    def selectApplet(self, aid: str, occurrence: str = "first") -> ApduResponse:
        candidates = tuple(candidate.aid for candidate in APPLET_AID_CANDIDATES)
        index = candidates.index(aid)
        if occurrence == "next":
            # En los escenarios simulados cada AID seleccionado tiene una
            # única ocurrencia; 'multiple-occurrences' modela una segunda
            # ocurrencia real del primer candidato.
            if self.scenario == "multiple-occurrences" and index == 0:
                return ApduResponse(bytes([0x01]), 0x90, 0x00)
            return ApduResponse(b"", 0x6A, 0x82)
        if self.scenario == "timeout" and index == 0:
            raise CardError("TRANSMIT_TIMEOUT", "SIMULATED: timeout.")
        if self.scenario == "card-removed" and index == 0:
            raise CardError("CARD_REMOVED", "SIMULATED: tarjeta retirada.")
        if self.scenario == "truncated" and index == 0:
            return parse_apdu_response(bytes([0x90]))
        responses = {
            "aid1-selected": (0x9000, 0x6A82),
            "aid2-selected": (0x6A82, 0x9000),
            "multiple": (0x9000, 0x9000),
            "multiple-occurrences": (0x9000, 0x6A82),
            "distinct-data": (0x9000, 0x9000),
            "not-found": (0x6A82, 0x6A82),
            "unknown-sw": (0x9F10, 0x6A82),
        }
        sw = responses.get(self.scenario, (0x6A82, 0x6A82))[index]
        if self.scenario == "distinct-data":
            data = bytes([index + 1]) if sw == 0x9000 else b""
            return ApduResponse(data, sw >> 8, sw & 0xFF)
        return ApduResponse(b"", sw >> 8, sw & 0xFF)

    def disconnect(self) -> None:
        self.disconnected = True
