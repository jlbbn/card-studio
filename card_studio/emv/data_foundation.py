from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from card_studio.interfaces.base import CardInterface, InterfaceKind
from card_studio.readers.base import TransportResult
from card_studio.tlv import TlvNode, encode_tlv

from .afl import ApplicationFileLocator, RecordLocator
from .apdu import CommandApdu, ResponseApdu
from .data import DataRecord, DataSource, EmvDataStore, OwnershipDomain
from .dol import DolDocument
from .gpo import GpoResult, build_gpo_command, get_processing_options
from .record import RecordReadResult, read_afl_records, read_record_command
from .selection import (
    ApplicationSelection,
    DirectoryEntry,
    select_aid_command,
    select_application,
    select_environment_command,
    synthetic_selection_responses,
)


EMV_DATA_FOUNDATION = "EMV_DATA_FOUNDATION"


@dataclass(frozen=True)
class DataFoundationResult:
    selection: ApplicationSelection
    gpo: GpoResult
    records: RecordReadResult

    @property
    def store(self) -> EmvDataStore:
        return self.records.store

    @property
    def command_count(self) -> int:
        return self.selection.command_count + self.gpo.command_count + self.records.command_count

    @property
    def gate_status(self) -> str:
        return "PASS"

    def to_evidence(self) -> dict[str, Any]:
        return {
            "mode": "LAB_ONLY",
            "gate": EMV_DATA_FOUNDATION,
            "status": self.gate_status,
            "selection": self.selection.to_evidence(),
            "gpo": self.gpo.to_evidence(),
            "records": self.records.to_evidence(),
            "command_count": self.command_count,
        }


def run_data_foundation(
    card_interface: CardInterface,
    pdol: DolDocument,
    store: EmvDataStore,
    application_aid: str | None = None,
    supported_kernel_ids: Iterable[str] | None = None,
    allow_partial: bool = False,
    required_tags: Iterable[str] = (),
    allow_unknown_tags: bool = True,
    max_followups: int = 4,
    stage_handler: Any = None,
    relay_time_limit_ms: int = 0,
) -> DataFoundationResult:
    if stage_handler is not None:
        stage_handler("APPLICATION_SELECTION")
    selection = select_application(
        card_interface,
        application_aid=application_aid,
        supported_kernel_ids=supported_kernel_ids,
        allow_partial=allow_partial,
    )
    if stage_handler is not None:
        stage_handler("APPLICATION_INITIALIZATION")
    gpo = get_processing_options(
        card_interface,
        pdol,
        store,
        max_followups=max_followups,
        relay_time_limit_ms=relay_time_limit_ms,
    )
    if stage_handler is not None:
        stage_handler("CARD_DATA_PROCESSING")
    records = read_afl_records(
        card_interface,
        gpo.afl,
        store,
        required_tags=required_tags,
        allow_unknown_tags=allow_unknown_tags,
        max_followups=max_followups,
    )
    return DataFoundationResult(selection, gpo, records)


def _gpo_data(aip: bytes, afl: bytes, response_format: int) -> bytes:
    if response_format == 77:
        return encode_tlv(TlvNode("77", children=(TlvNode("82", aip), TlvNode("94", afl))))
    if response_format == 80:
        return encode_tlv(TlvNode("80", aip + afl))
    raise ValueError("Formato GPO debe ser 77 u 80.")


def _record_data(values: bytes | Iterable[DataRecord | tuple[str, bytes]]) -> bytes:
    if isinstance(values, bytes):
        return values
    children = []
    for item in values:
        if isinstance(item, DataRecord):
            if item.source != DataSource.CARD or item.ownership != OwnershipDomain.CARD:
                raise ValueError("Los records sintéticos deben ser CARD-owned.")
            children.append(TlvNode(item.tag, item.value))
        else:
            tag, value = item
            children.append(TlvNode(tag, value))
    return encode_tlv(TlvNode("70", children=tuple(children)))


def synthetic_data_foundation_responses(
    interface: InterfaceKind | str,
    applications: Iterable[DirectoryEntry],
    pdol: DolDocument,
    store: EmvDataStore,
    aip: bytes = b"\x00\x00",
    afl: bytes = b"\x08\x01\x01\x00",
    records: Mapping[tuple[int, int], bytes | Iterable[DataRecord | tuple[str, bytes]]] | None = None,
    gpo_format: int = 77,
    malformed: str | None = None,
    malformed_record: tuple[int, int] | None = None,
) -> dict[str, TransportResult]:
    """Genera respuestas locales SYNTHETIC para todo EMV_DATA_FOUNDATION."""
    selected_interface = InterfaceKind(interface)
    responses = dict(synthetic_selection_responses(selected_interface, applications))
    gpo_data = _gpo_data(aip, afl, gpo_format)
    if malformed == "GPO_TRUNCATED":
        gpo_data = b"\x77\x05\x82\x02"
    elif malformed == "GPO_WRONG_TEMPLATE":
        gpo_data = encode_tlv(TlvNode("70", children=()))
    elif malformed == "GPO_MISSING_AIP":
        gpo_data = encode_tlv(TlvNode("77", children=(TlvNode("94", afl),)))
    elif malformed == "GPO_MISSING_AFL":
        gpo_data = encode_tlv(TlvNode("77", children=(TlvNode("82", aip),)))
    elif malformed == "GPO_DUPLICATE_AIP":
        gpo_data = encode_tlv(TlvNode("77", children=(TlvNode("82", aip), TlvNode("82", aip), TlvNode("94", afl))))
    elif malformed == "GPO_AMBIGUOUS":
        gpo_data = _gpo_data(aip, afl, 77) + _gpo_data(aip, afl, 80)
    elif malformed is not None and not malformed.startswith("RECORD_") and malformed != "AIP_RFU":
        raise ValueError("Caso malformed foundation desconocido.")
    if malformed == "AIP_RFU":
        gpo_data = _gpo_data(b"\x00\x01", afl, gpo_format)
    responses[build_gpo_command(pdol, store).to_bytes().hex().upper()] = TransportResult(gpo_data, "9000")
    record_values = dict(records or {})
    plan = ApplicationFileLocator.parse(afl).record_plan()
    first = plan[0]
    target = malformed_record or (first.sfi, first.record)
    if malformed is not None and malformed.startswith("RECORD_"):
        current = record_values.get(target, ((("DF01", b"\x01"),)))
        if isinstance(current, bytes):
            children = (TlvNode("DF01", b"\x01"),)
            if malformed == "RECORD_DUPLICATE_TAG":
                children = (TlvNode("DF01", b"\x01"), TlvNode("DF01", b"\x01"))
        else:
            children = tuple(
                TlvNode(item.tag, item.value) if isinstance(item, DataRecord) else TlvNode(item[0], item[1])
                for item in current
            )
        if malformed == "RECORD_TRUNCATED":
            record_values[target] = b"\x70\x05\x4f\x01"
        elif malformed == "RECORD_WRONG_TEMPLATE":
            record_values[target] = encode_tlv(TlvNode("77", children=()))
        elif malformed == "RECORD_EMPTY":
            record_values[target] = encode_tlv(TlvNode("70", children=()))
        elif malformed == "RECORD_DUPLICATE_TAG":
            tag = children[0].tag if children else "DF01"
            value = children[0].value if children else b"\x01"
            record_values[target] = encode_tlv(TlvNode("70", children=(
                TlvNode(tag, value),
                TlvNode(tag, value),
            )))
        elif malformed == "RECORD_TERMINAL_TAG":
            record_values[target] = encode_tlv(TlvNode("70", children=children + (TlvNode("9F02", b"\x00"),)))
        elif malformed == "RECORD_UNKNOWN_TAG":
            record_values[target] = encode_tlv(TlvNode("70", children=children + (TlvNode("DF77", b"\x01"),)))
        else:
            raise ValueError("Caso malformed de record desconocido.")
    for locator in plan:
        key = (locator.sfi, locator.record)
        values = record_values.get(key, ())
        responses[read_record_command(locator, ApplicationFileLocator.parse(afl)).to_bytes().hex().upper()] = TransportResult(_record_data(values), "9000")
    return responses


def response_apdu_for_gpo(data: bytes) -> ResponseApdu:
    return ResponseApdu(data, 0x90, 0x00)
