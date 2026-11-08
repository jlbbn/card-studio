from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .model import CardError


@dataclass(frozen=True)
class AidCandidate:
    aid: str
    source: str

    @property
    def data(self) -> bytes:
        return bytes.fromhex(self.aid)


APPLET_AID_CANDIDATES = (
    AidCandidate("D4534C504D4401", "PC/SC / Cardpeek"),
    AidCandidate("D4534C504D44", "Android Card Bridge"),
)
CANONICAL_AID = "UNRESOLVED"
PHYSICAL_VALIDATION = "PENDING"


@dataclass(frozen=True)
class ApduResponse:
    data: bytes
    sw1: int
    sw2: int

    @property
    def sw1sw2(self) -> str:
        return f"{self.sw1:02X}{self.sw2:02X}"

    @property
    def raw(self) -> bytes:
        return self.data + bytes([self.sw1, self.sw2])


@dataclass(frozen=True)
class StatusWord:
    code: str
    meaning: str
    result: str
    success: bool = False
    applet_not_found: bool = False


STATUS_WORDS = {
    "9000": StatusWord("9000", "SUCCESS", "APPLET_SELECTED", success=True),
    "6A82": StatusWord("6A82", "APPLICATION_OR_FILE_NOT_FOUND", "APPLET_NOT_FOUND", applet_not_found=True),
    "6A86": StatusWord("6A86", "INCORRECT_PARAMETERS", "INVALID_STATUS_WORD"),
    "6700": StatusWord("6700", "INCORRECT_LENGTH", "INVALID_STATUS_WORD"),
    "6985": StatusWord("6985", "CONDITIONS_OF_USE_NOT_SATISFIED", "INVALID_STATUS_WORD"),
    "6D00": StatusWord("6D00", "INSTRUCTION_NOT_SUPPORTED", "INVALID_STATUS_WORD"),
    "6E00": StatusWord("6E00", "CLASS_NOT_SUPPORTED", "INVALID_STATUS_WORD"),
}


def build_select_apdu(aid: str) -> bytes:
    normalized = aid.strip().upper()
    if not re.fullmatch(r"[0-9A-F]+", normalized) or len(normalized) % 2:
        raise CardError("INVALID_APDU", "El AID debe contener bytes hexadecimales completos.")
    data = bytes.fromhex(normalized)
    if not 5 <= len(data) <= 16:
        raise CardError("INVALID_APDU", "El AID debe medir entre 5 y 16 bytes.")
    return bytes([0x00, 0xA4, 0x04, 0x00, len(data)]) + data


def parse_apdu_response(response: bytes | bytearray | Iterable[int]) -> ApduResponse:
    try:
        raw = bytes(response)
    except (TypeError, ValueError) as exc:
        raise CardError("INVALID_RESPONSE", "La respuesta APDU no contiene bytes válidos.") from exc
    if len(raw) < 2:
        raise CardError("INVALID_RESPONSE", "La respuesta APDU está truncada y no contiene SW1SW2.")
    return ApduResponse(raw[:-2], raw[-2], raw[-1])


def interpret_status_word(value: str) -> StatusWord:
    normalized = value.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{4}", normalized):
        raise CardError("INVALID_STATUS_WORD", "SW1SW2 debe contener exactamente dos bytes.")
    return STATUS_WORDS.get(
        normalized,
        StatusWord(normalized, "UNKNOWN_STATUS_WORD", "INVALID_STATUS_WORD"),
    )


def interpret_select_response(response: ApduResponse) -> StatusWord:
    return interpret_status_word(response.sw1sw2)
