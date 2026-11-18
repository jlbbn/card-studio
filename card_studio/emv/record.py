from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from card_studio.interfaces.base import CardInterface
from card_studio.tlv import TlvNode, parse_tlv

from .afl import ApplicationFileLocator, RecordLocator
from .apdu import ApduContext, CommandApdu, ResponseApdu, StatusAction, exchange_with_followups
from .data import DataRecord, DataSensitivity, DataSource, EmvDataStore, OwnershipDomain


TERMINAL_OR_KERNEL_RECORD_TAGS = frozenset({
    "5F2A", "5F36", "82", "94", "95", "9A", "9B", "9C", "9F02", "9F03", "9F15",
    "9F1A", "9F1E", "9F33", "9F34", "9F35", "9F37", "9F38", "9F40", "9F4E", "9F66",
})
HOST_RESPONSE_RECORD_TAGS = frozenset({"86", "89", "8A", "91", "9F18"})
GENERATE_AC_OR_DYNAMIC_AUTH_RECORD_TAGS = frozenset({"9F26", "9F27", "9F36", "9F4B", "9F4C"})
FORBIDDEN_CARD_RECORD_TAGS = (
    TERMINAL_OR_KERNEL_RECORD_TAGS | HOST_RESPONSE_RECORD_TAGS | GENERATE_AC_OR_DYNAMIC_AUTH_RECORD_TAGS
)
KNOWN_CARD_RECORD_TAGS = frozenset({
    "50", "56", "57", "5A", "5F20", "5F24", "5F25", "5F28", "5F2D", "5F30", "5F34",
    "87", "8C", "8D", "8E", "90", "92", "93", "9F07", "9F08", "9F0D", "9F0E", "9F0F",
    "9F10", "9F11", "9F12", "9F17", "9F46", "9F47", "9F48", "9F49", "9F4A",
})
SENSITIVE_CARD_RECORD_TAGS = frozenset({"56", "57", "5A", "9F6B"})


class ReadRecordError(ValueError):
    def __init__(self, code: str, message: str, command_sent: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.command_sent = command_sent


@dataclass(frozen=True)
class RecordReadResult:
    store: EmvDataStore
    records_read: int
    command_count: int

    def to_evidence(self) -> dict[str, Any]:
        return {
            "records_read": self.records_read,
            "command_count": self.command_count,
            "store": self.store.to_evidence(),
            "mode": "LAB_ONLY",
        }


def read_record_command(locator: RecordLocator, afl: ApplicationFileLocator) -> CommandApdu:
    if not isinstance(afl, ApplicationFileLocator):
        raise ReadRecordError("INVALID_AFL", "READ RECORD requiere una AFL previamente validada.")
    in_plan = isinstance(locator, RecordLocator) and any(
        item.sfi == locator.sfi and item.record == locator.record for item in afl.record_plan()
    )
    if not in_plan:
        raise ReadRecordError("RECORD_OUTSIDE_AFL", "El locator no pertenece al plan de la AFL validada.")
    return CommandApdu(0x00, 0xB2, locator.record, (locator.sfi << 3) | 0x04, le=0x00)


def record_sensitivity(tag: str) -> DataSensitivity:
    return DataSensitivity.SENSITIVE if tag in SENSITIVE_CARD_RECORD_TAGS else DataSensitivity.PUBLIC


def parse_record_response(response: ResponseApdu, allow_unknown_tags: bool = True) -> tuple[DataRecord, ...]:
    if not response.is_success:
        raise ReadRecordError("READ_RECORD_STATUS_ERROR", f"READ RECORD terminó con {response.sw1sw2}.", command_sent=True)
    try:
        roots = parse_tlv(response.data, context="READ_RECORD")
    except ValueError as exc:
        raise ReadRecordError("MALFORMED_RECORD", "Record contiene TLV truncado o mal formado.", command_sent=True) from exc
    if len(roots) != 1 or roots[0].tag != "70" or not roots[0].constructed:
        raise ReadRecordError("UNEXPECTED_RECORD_TEMPLATE", "Se esperaba un único template 70.", command_sent=True)
    children = roots[0].children
    if not children:
        raise ReadRecordError("EMPTY_RECORD", "El record obligatorio está vacío.", command_sent=True)
    records: list[DataRecord] = []
    seen: set[str] = set()
    for child in children:
        if child.constructed:
            raise ReadRecordError("NESTED_RECORD_OBJECT", "El record no admite objetos anidados.", command_sent=True)
        if child.tag in seen:
            raise ReadRecordError("DUPLICATE_RECORD_TAG", f"Tag duplicado en record: {child.tag}.", command_sent=True)
        seen.add(child.tag)
        if child.tag in FORBIDDEN_CARD_RECORD_TAGS:
            raise ReadRecordError("OWNERSHIP_RECORD_INJECTION", f"Tag de otro ownership domain en record: {child.tag}.", command_sent=True)
        if not allow_unknown_tags and child.tag not in KNOWN_CARD_RECORD_TAGS:
            raise ReadRecordError("UNKNOWN_RECORD_TAG", f"Tag no permitido por el contexto: {child.tag}.", command_sent=True)
        records.append(DataRecord(
            tag=child.tag,
            value=child.value,
            source=DataSource.CARD,
            ownership=OwnershipDomain.CARD,
            sensitivity=record_sensitivity(child.tag),
        ))
    return tuple(records)


def read_afl_records(
    card_interface: CardInterface,
    afl: ApplicationFileLocator,
    store: EmvDataStore,
    required_tags: Iterable[str] = (),
    allow_unknown_tags: bool = True,
    max_followups: int = 4,
) -> RecordReadResult:
    """Lee toda la AFL y confirma el DataStore solo después de validar el plan completo."""
    if not isinstance(afl, ApplicationFileLocator):
        raise ReadRecordError("INVALID_AFL", "READ RECORD requiere una AFL previamente validada.")
    staged = store.clone()
    plan = afl.record_plan()
    command_count = 0
    for locator in plan:
        exchange = exchange_with_followups(
            card_interface,
            read_record_command(locator, afl),
            context=ApduContext.GENERIC,
            max_followups=max_followups,
        )
        command_count += exchange.command_count
        if exchange.action != StatusAction.SUCCESS:
            raise ReadRecordError(
                "READ_RECORD_STATUS_ERROR",
                f"READ RECORD SFI {locator.sfi} record {locator.record} terminó en {exchange.action.value}.",
                command_sent=True,
            )
        records = parse_record_response(exchange.response, allow_unknown_tags=allow_unknown_tags)
        staged.merge(records)
    for tag in required_tags:
        staged.require(tag)
    store.merge(staged.snapshot().values())
    return RecordReadResult(store, len(plan), command_count)
