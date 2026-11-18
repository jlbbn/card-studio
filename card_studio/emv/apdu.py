from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from card_studio.interfaces.base import CardInterface, InterfaceState
from card_studio.readers.base import ReaderError, TransportResult


MAX_SHORT_APDU_DATA = 255
MAX_RESPONSE_DATA = 4096


class ApduError(ValueError):
    def __init__(self, code: str, message: str, command_sent: bool = False, outcome: str = "CERTAIN_FAILURE"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.command_sent = command_sent
        self.outcome = outcome

    @property
    def phase(self) -> str:
        return "POST_SEND" if self.command_sent else "PRE_SEND"


class ApduContext(str, Enum):
    GENERIC = "GENERIC"
    ENVIRONMENT_SELECTION = "ENVIRONMENT_SELECTION"
    AID_SELECTION = "AID_SELECTION"


class StatusAction(str, Enum):
    SUCCESS = "SUCCESS"
    GET_RESPONSE = "GET_RESPONSE"
    RETRY_WITH_LE = "RETRY_WITH_LE"
    ENVIRONMENT_NOT_FOUND = "ENVIRONMENT_NOT_FOUND"
    TRY_NEXT_AID = "TRY_NEXT_AID"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    INCORRECT_P1P2 = "INCORRECT_P1P2"
    WRONG_LENGTH = "WRONG_LENGTH"
    CONDITIONS_NOT_SATISFIED = "CONDITIONS_NOT_SATISFIED"
    INSTRUCTION_NOT_SUPPORTED = "INSTRUCTION_NOT_SUPPORTED"
    CLASS_NOT_SUPPORTED = "CLASS_NOT_SUPPORTED"
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    CARD_WARNING = "CARD_WARNING"


def _byte(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
        raise ApduError("INVALID_APDU_FIELD", f"{name} debe estar entre 0 y 255.")
    return value


@dataclass(frozen=True)
class CommandApdu:
    cla: int
    ins: int
    p1: int
    p2: int
    data: bytes = b""
    le: int | None = None

    def __post_init__(self) -> None:
        _byte(self.cla, "CLA")
        _byte(self.ins, "INS")
        _byte(self.p1, "P1")
        _byte(self.p2, "P2")
        try:
            object.__setattr__(self, "data", bytes(self.data))
        except (TypeError, ValueError) as exc:
            raise ApduError("INVALID_APDU_DATA", "Data debe ser bytes.") from exc
        if len(self.data) > MAX_SHORT_APDU_DATA:
            raise ApduError("APDU_DATA_LIMIT_EXCEEDED", "Data supera el dominio short APDU.")
        if self.le is not None:
            _byte(self.le, "Le")

    @property
    def lc(self) -> int | None:
        return len(self.data) if self.data else None

    def to_bytes(self) -> bytes:
        output = bytearray((self.cla, self.ins, self.p1, self.p2))
        if self.data:
            output.append(len(self.data))
            output.extend(self.data)
        if self.le is not None:
            output.append(self.le)
        return bytes(output)

    def with_le(self, le: int) -> "CommandApdu":
        return replace(self, le=le)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "cla": self.cla,
            "ins": self.ins,
            "p1": self.p1,
            "p2": self.p2,
            "lc": self.lc,
            "data_length": len(self.data),
            "le": self.le,
        }

    @classmethod
    def parse(cls, raw: bytes) -> "CommandApdu":
        try:
            data = bytes(raw)
        except (TypeError, ValueError) as exc:
            raise ApduError("INVALID_APDU", "El comando debe ser bytes.") from exc
        if len(data) < 4:
            raise ApduError("APDU_TRUNCATED", "Comando APDU truncado.")
        if len(data) > 261:
            raise ApduError("APDU_LIMIT_EXCEEDED", "Comando fuera del dominio short APDU.")
        cla, ins, p1, p2 = data[:4]
        if len(data) == 4:
            return cls(cla, ins, p1, p2)
        if len(data) == 5:
            return cls(cla, ins, p1, p2, le=data[4])
        lc = data[4]
        if lc == 0:
            raise ApduError("INVALID_APDU_LC", "Lc cero no es válido con datos adicionales.")
        data_end = 5 + lc
        if len(data) not in {data_end, data_end + 1}:
            raise ApduError("APDU_LENGTH_MISMATCH", "Lc no coincide con la longitud del comando.")
        le = data[-1] if len(data) == data_end + 1 else None
        return cls(cla, ins, p1, p2, data[5:data_end], le)


@dataclass(frozen=True)
class ResponseApdu:
    data: bytes
    sw1: int
    sw2: int

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "data", bytes(self.data))
        except (TypeError, ValueError) as exc:
            raise ApduError("INVALID_APDU_RESPONSE", "Response data debe ser bytes.") from exc
        if len(self.data) > MAX_RESPONSE_DATA:
            raise ApduError("APDU_RESPONSE_LIMIT_EXCEEDED", "Respuesta demasiado grande.")
        _byte(self.sw1, "SW1")
        _byte(self.sw2, "SW2")

    @property
    def sw1sw2(self) -> str:
        return bytes((self.sw1, self.sw2)).hex().upper()

    @property
    def is_success(self) -> bool:
        return self.sw1 == 0x90 and self.sw2 == 0x00

    def to_bytes(self) -> bytes:
        return self.data + bytes((self.sw1, self.sw2))

    def to_transport(self) -> TransportResult:
        return TransportResult(self.data, self.sw1sw2)

    def to_evidence(self) -> dict[str, Any]:
        return {"data_length": len(self.data), "sw1": self.sw1, "sw2": self.sw2, "sw1sw2": self.sw1sw2}

    @classmethod
    def parse(cls, raw: bytes) -> "ResponseApdu":
        try:
            data = bytes(raw)
        except (TypeError, ValueError) as exc:
            raise ApduError("INVALID_APDU_RESPONSE", "La respuesta debe ser bytes.") from exc
        if len(data) < 2:
            raise ApduError("APDU_RESPONSE_TRUNCATED", "Respuesta APDU sin SW1/SW2 completos.")
        return cls(data[:-2], data[-2], data[-1])

    @classmethod
    def from_transport(cls, response: TransportResult) -> "ResponseApdu":
        if not isinstance(response, TransportResult):
            raise ApduError("INVALID_TRANSPORT_RESPONSE", "Se requiere TransportResult.")
        if response.sw1sw2 is None:
            raise ApduError("APDU_STATUS_MISSING", "La respuesta no contiene SW1/SW2.")
        try:
            status = bytes.fromhex(response.sw1sw2)
        except ValueError as exc:
            raise ApduError("APDU_STATUS_INVALID", "SW1/SW2 no es hexadecimal.") from exc
        if len(status) != 2:
            raise ApduError("APDU_STATUS_INVALID", "SW1/SW2 debe contener dos bytes.")
        return cls(response.data, status[0], status[1])


@dataclass(frozen=True)
class StatusClassification:
    action: StatusAction
    expected_length: int | None = None


def classify_status(response: ResponseApdu, context: ApduContext = ApduContext.GENERIC) -> StatusClassification:
    context = ApduContext(context)
    sw1, sw2 = response.sw1, response.sw2
    if (sw1, sw2) == (0x90, 0x00):
        return StatusClassification(StatusAction.SUCCESS)
    if sw1 == 0x61:
        return StatusClassification(StatusAction.GET_RESPONSE, sw2 or 256)
    if sw1 == 0x6C:
        return StatusClassification(StatusAction.RETRY_WITH_LE, sw2)
    if sw1 in {0x62, 0x63}:
        return StatusClassification(StatusAction.CARD_WARNING)
    if (sw1, sw2) == (0x6A, 0x82):
        action = {
            ApduContext.ENVIRONMENT_SELECTION: StatusAction.ENVIRONMENT_NOT_FOUND,
            ApduContext.AID_SELECTION: StatusAction.TRY_NEXT_AID,
            ApduContext.GENERIC: StatusAction.FILE_NOT_FOUND,
        }[context]
        return StatusClassification(action)
    terminal = {
        (0x6A, 0x86): StatusAction.INCORRECT_P1P2,
        (0x67, 0x00): StatusAction.WRONG_LENGTH,
        (0x69, 0x85): StatusAction.CONDITIONS_NOT_SATISFIED,
        (0x6D, 0x00): StatusAction.INSTRUCTION_NOT_SUPPORTED,
        (0x6E, 0x00): StatusAction.CLASS_NOT_SUPPORTED,
        (0x6A, 0x83): StatusAction.RECORD_NOT_FOUND,
    }.get((sw1, sw2))
    if terminal is not None:
        return StatusClassification(terminal)
    raise ApduError("UNEXPECTED_STATUS", f"Status no admitido: {response.sw1sw2}.", command_sent=True)


@dataclass(frozen=True)
class ApduExchangeResult:
    response: ResponseApdu
    action: StatusAction
    command_count: int

    def to_evidence(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "command_count": self.command_count,
            "response": self.response.to_evidence(),
        }


def exchange_with_followups(
    card_interface: CardInterface,
    command: CommandApdu,
    context: ApduContext = ApduContext.GENERIC,
    max_followups: int = 4,
) -> ApduExchangeResult:
    """Intercambia APDU LAB_ONLY con cadenas 61xx/6Cxx estrictamente acotadas."""
    if not isinstance(command, CommandApdu):
        raise ApduError("INVALID_APDU", "command debe ser CommandApdu.")
    if not isinstance(max_followups, int) or isinstance(max_followups, bool) or not 0 <= max_followups <= 16:
        raise ApduError("INVALID_FOLLOWUP_LIMIT", "max_followups debe estar entre 0 y 16.")
    if card_interface.state != InterfaceState.OPEN or card_interface.connection is None:
        raise ApduError("CARD_INTERFACE_NOT_OPEN", "La interfaz no está abierta.")
    current = command
    command_count = 0
    response_data = bytearray()
    while True:
        command_count += 1
        try:
            transport = card_interface.exchange(current.to_bytes(), sensitivity="REDACTED")
            response = ResponseApdu.from_transport(transport)
            classification = classify_status(response, context)
        except ReaderError as exc:
            outcome = "UNCERTAIN" if exc.outcome == "UNCERTAIN" else "CERTAIN_FAILURE"
            raise ApduError(exc.code, exc.message, command_sent=True, outcome=outcome) from exc
        except ApduError as exc:
            if not exc.command_sent:
                exc.command_sent = True
            raise
        except RuntimeError as exc:
            raise ApduError("CARD_EXCHANGE_FAILED", str(exc), command_sent=True, outcome="UNCERTAIN") from exc
        response_data.extend(response.data)
        if classification.action == StatusAction.GET_RESPONSE:
            if command_count > max_followups:
                raise ApduError("APDU_CHAIN_LIMIT_EXCEEDED", "Cadena 61xx demasiado larga.", command_sent=True)
            current = CommandApdu(0x00, 0xC0, 0x00, 0x00, le=(classification.expected_length or 0) & 0xFF)
            continue
        if classification.action == StatusAction.RETRY_WITH_LE:
            if command_count > max_followups:
                raise ApduError("APDU_CHAIN_LIMIT_EXCEEDED", "Cadena 6Cxx demasiado larga.", command_sent=True)
            response_data.clear()
            current = command.with_le(classification.expected_length or 0)
            continue
        completed = ResponseApdu(bytes(response_data), response.sw1, response.sw2)
        return ApduExchangeResult(completed, classification.action, command_count)
