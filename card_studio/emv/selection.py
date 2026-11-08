from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from card_studio.interfaces.base import CardInterface, InterfaceKind
from card_studio.readers.base import TransportResult
from card_studio.tlv import TlvNode, encode_tlv, find_tag, parse_tlv

from .apdu import (
    ApduContext,
    CommandApdu,
    ResponseApdu,
    StatusAction,
    exchange_with_followups,
)
from .validation import normalize_aid, normalize_tag


PSE_NAME = b"1PAY.SYS.DDF01"
PPSE_NAME = b"2PAY.SYS.DDF01"


class SelectionError(ValueError):
    def __init__(self, code: str, message: str, command_sent: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.command_sent = command_sent


def _environment(interface: InterfaceKind | str) -> InterfaceKind:
    selected = InterfaceKind(interface)
    if selected not in {InterfaceKind.CONTACT, InterfaceKind.CONTACTLESS}:
        raise SelectionError("UNSUPPORTED_INTERFACE", "PSE/PPSE solo admite CONTACT o CONTACTLESS.")
    return selected


def environment_name(interface: InterfaceKind | str) -> bytes:
    return PSE_NAME if _environment(interface) == InterfaceKind.CONTACT else PPSE_NAME


def environment_supports_read_record(interface: InterfaceKind | str) -> bool:
    return _environment(interface) == InterfaceKind.CONTACT


def select_aid_command(aid: str, next_occurrence: bool = False) -> CommandApdu:
    normalized = normalize_aid(aid)
    return CommandApdu(0x00, 0xA4, 0x04, 0x02 if next_occurrence else 0x00, bytes.fromhex(normalized), 0x00)


def select_environment_command(interface: InterfaceKind | str) -> CommandApdu:
    return CommandApdu(0x00, 0xA4, 0x04, 0x00, environment_name(interface), 0x00)


def _single_direct(
    nodes: Iterable[TlvNode],
    tag: str,
    required: bool = True,
    constructed: bool | None = False,
) -> TlvNode | None:
    matches = [node for node in nodes if node.tag == tag]
    if len(matches) > 1:
        raise SelectionError("DUPLICATE_FCI_OBJECT", f"Objeto FCI duplicado: {tag}.", command_sent=True)
    if not matches:
        if required:
            raise SelectionError("MISSING_FCI_OBJECT", f"Objeto FCI obligatorio ausente: {tag}.", command_sent=True)
        return None
    if constructed is not None and matches[0].constructed != constructed:
        expected = "constructed" if constructed else "primitivo"
        raise SelectionError("INVALID_FCI_OBJECT", f"Objeto FCI debe ser {expected}: {tag}.", command_sent=True)
    return matches[0]


def _root_fci(response: ResponseApdu) -> TlvNode:
    if not response.is_success:
        raise SelectionError("SELECTION_STATUS_ERROR", f"SELECT terminó con {response.sw1sw2}.", command_sent=True)
    try:
        nodes = parse_tlv(response.data)
    except ValueError as exc:
        raise SelectionError("MALFORMED_FCI", "FCI contiene TLV truncado o mal formado.", command_sent=True) from exc
    if len(nodes) != 1 or nodes[0].tag != "6F" or not nodes[0].constructed:
        raise SelectionError("UNEXPECTED_FCI_TEMPLATE", "Se esperaba un único template 6F.", command_sent=True)
    return nodes[0]


def validate_selected_adf(response: ResponseApdu, expected_aid: str) -> str:
    normalized = normalize_aid(expected_aid)
    root = _root_fci(response)
    df_name = _single_direct(root.children, "84")
    if df_name is None or df_name.value != bytes.fromhex(normalized):
        raise SelectionError("SELECTED_AID_MISMATCH", "El DF Name final no coincide con el AID solicitado.", command_sent=True)
    return normalized


@dataclass(frozen=True)
class DirectoryEntry:
    aid: str
    priority: int | None = None
    label: str | None = None
    kernel_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aid", normalize_aid(self.aid))
        if self.priority is not None and (
            not isinstance(self.priority, int) or isinstance(self.priority, bool) or not 0 <= self.priority <= 255
        ):
            raise SelectionError("INVALID_APPLICATION_PRIORITY", "La prioridad debe estar entre 0 y 255.")
        if self.label is not None and (
            not self.label or len(self.label) > 64 or any(ord(char) < 0x20 or ord(char) > 0x7E for char in self.label)
        ):
            raise SelectionError("INVALID_APPLICATION_LABEL", "Label debe ser ASCII visible de hasta 64 caracteres.")
        if self.kernel_id is not None:
            normalized = self.kernel_id.strip().upper()
            if not 2 <= len(normalized) <= 8 or len(normalized) % 2:
                raise SelectionError("INVALID_KERNEL_ID", "Kernel ID debe medir entre uno y cuatro bytes.")
            try:
                bytes.fromhex(normalized)
            except ValueError as exc:
                raise SelectionError("INVALID_KERNEL_ID", "Kernel ID debe ser hexadecimal.") from exc
            object.__setattr__(self, "kernel_id", normalized)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "aid": self.aid,
            "priority": self.priority,
            "label": self.label,
            "kernel_id": self.kernel_id,
        }


def parse_directory_entries(response: ResponseApdu, interface: InterfaceKind | str) -> tuple[DirectoryEntry, ...]:
    selected_interface = _environment(interface)
    root = _root_fci(response)
    proprietary = _single_direct(root.children, "A5", constructed=True)
    if proprietary is None:
        raise SelectionError("INVALID_FCI_PROPRIETARY", "FCI requiere template A5 constructed.", command_sent=True)
    discretionary = _single_direct(proprietary.children, "BF0C", required=False, constructed=True)
    if discretionary is None:
        return ()
    if not discretionary.constructed:
        raise SelectionError("INVALID_DIRECTORY", "BF0C debe ser constructed.", command_sent=True)
    entries: list[DirectoryEntry] = []
    seen_aids: set[str] = set()
    for template in discretionary.children:
        if template.tag != "61":
            continue
        if not template.constructed:
            raise SelectionError("INVALID_DIRECTORY_ENTRY", "Directory Entry 61 debe ser constructed.", command_sent=True)
        aid_node = _single_direct(template.children, "4F")
        try:
            aid = normalize_aid(aid_node.value.hex() if aid_node is not None else "")
        except ValueError as exc:
            raise SelectionError("INVALID_AID", "Directory Entry contiene AID inválido.", command_sent=True) from exc
        if aid in seen_aids:
            raise SelectionError("DUPLICATE_AID", f"AID duplicado en directorio: {aid}.", command_sent=True)
        seen_aids.add(aid)
        priority_node = _single_direct(template.children, "87", required=False)
        if priority_node is not None and len(priority_node.value) != 1:
            raise SelectionError("INVALID_APPLICATION_PRIORITY", "Prioridad debe ocupar un byte.", command_sent=True)
        label_node = _single_direct(template.children, "50", required=False)
        try:
            label = label_node.value.decode("ascii") if label_node is not None else None
        except UnicodeDecodeError as exc:
            raise SelectionError("INVALID_APPLICATION_LABEL", "Label no es ASCII.", command_sent=True) from exc
        kernel_node = _single_direct(template.children, "9F2A", required=False)
        kernel_id = kernel_node.value.hex().upper() if kernel_node is not None else None
        if selected_interface == InterfaceKind.CONTACTLESS and kernel_id is None:
            raise SelectionError("MISSING_KERNEL_ID", "Directory Entry PPSE sin Kernel ID.", command_sent=True)
        try:
            entries.append(DirectoryEntry(
                aid=aid,
                priority=priority_node.value[0] if priority_node is not None else None,
                label=label,
                kernel_id=kernel_id,
            ))
        except SelectionError as exc:
            exc.command_sent = True
            raise
    return tuple(entries)


@dataclass(frozen=True)
class SelectedApplication:
    candidate: DirectoryEntry
    selection_kind: str

    def to_evidence(self) -> dict[str, Any]:
        return {"candidate": self.candidate.to_evidence(), "selection_kind": self.selection_kind}


def choose_candidate(
    entries: Iterable[DirectoryEntry],
    application_aid: str | None = None,
    supported_kernel_ids: Iterable[str] | None = None,
    allow_partial: bool = False,
) -> SelectedApplication:
    candidates = tuple(entries)
    supported = {item.upper() for item in supported_kernel_ids} if supported_kernel_ids is not None else None
    compatible = tuple(
        entry for entry in candidates
        if supported is None or (entry.kernel_id is not None and entry.kernel_id in supported)
    )
    if not compatible:
        raise SelectionError("NO_COMMON_AID", "No hay aplicaciones compatibles con interfaz/kernel.")
    if application_aid is None:
        if len(compatible) != 1:
            raise SelectionError(
                "APPLICATION_SELECTION_REQUIRED",
                "Hay varias aplicaciones compatibles; application_aid es obligatorio.",
            )
        return SelectedApplication(compatible[0], "EXACT")
    requested = normalize_aid(application_aid)
    exact = tuple(entry for entry in compatible if entry.aid == requested)
    if len(exact) > 1:
        raise SelectionError("DUPLICATE_AID", "El AID explícito aparece más de una vez.")
    if exact:
        return SelectedApplication(exact[0], "EXACT")
    if allow_partial:
        partial = tuple(
            entry for entry in compatible
            if len(entry.aid) > len(requested) and entry.aid.startswith(requested)
        )
        if len(partial) > 1:
            raise SelectionError("APPLICATION_SELECTION_REQUIRED", "Partial AID produce varios candidatos.")
        if partial:
            return SelectedApplication(partial[0], "PARTIAL")
    raise SelectionError("APPLICATION_NOT_FOUND", "No hay coincidencia para el AID solicitado.")


@dataclass(frozen=True)
class EnvironmentSelection:
    entries: tuple[DirectoryEntry, ...]
    command_count: int


@dataclass(frozen=True)
class ApplicationSelection:
    environment: str
    selected: SelectedApplication
    command_count: int

    def to_evidence(self) -> dict[str, Any]:
        return {
            "mode": "LAB_ONLY",
            "environment": self.environment,
            "selected": self.selected.to_evidence(),
            "command_count": self.command_count,
        }


def select_payment_environment(
    card_interface: CardInterface,
    interface: InterfaceKind | str | None = None,
) -> EnvironmentSelection:
    selected_interface = _environment(interface or card_interface.kind)
    if card_interface.kind != selected_interface:
        raise SelectionError("INTERFACE_MISMATCH", "La interfaz física no coincide con PSE/PPSE.")
    exchange = exchange_with_followups(
        card_interface,
        select_environment_command(selected_interface),
        context=ApduContext.ENVIRONMENT_SELECTION,
    )
    if exchange.action == StatusAction.ENVIRONMENT_NOT_FOUND:
        raise SelectionError("ENVIRONMENT_NOT_FOUND", "PSE/PPSE no está disponible.", command_sent=True)
    if exchange.action != StatusAction.SUCCESS:
        raise SelectionError("SELECTION_STATUS_ERROR", f"SELECT PSE/PPSE terminó en {exchange.action.value}.", command_sent=True)
    entries = parse_directory_entries(exchange.response, selected_interface)
    return EnvironmentSelection(entries, exchange.command_count)


def select_direct_aid(
    card_interface: CardInterface,
    aid: str,
    next_occurrence: bool = False,
) -> int:
    exchange = exchange_with_followups(
        card_interface,
        select_aid_command(aid, next_occurrence),
        context=ApduContext.AID_SELECTION,
    )
    if exchange.action == StatusAction.TRY_NEXT_AID:
        raise SelectionError("APPLICATION_NOT_FOUND", "SELECT AID no tuvo éxito.", command_sent=True)
    if exchange.action != StatusAction.SUCCESS:
        raise SelectionError("SELECTION_STATUS_ERROR", f"SELECT AID terminó en {exchange.action.value}.", command_sent=True)
    validate_selected_adf(exchange.response, aid)
    return exchange.command_count


def select_application(
    card_interface: CardInterface,
    application_aid: str | None = None,
    supported_kernel_ids: Iterable[str] | None = None,
    allow_partial: bool = False,
) -> ApplicationSelection:
    selected_interface = _environment(card_interface.kind)
    environment = select_payment_environment(card_interface, selected_interface)
    selected = choose_candidate(
        environment.entries,
        application_aid=application_aid,
        supported_kernel_ids=supported_kernel_ids,
        allow_partial=allow_partial,
    )
    direct_count = select_direct_aid(card_interface, selected.candidate.aid)
    return ApplicationSelection(
        environment=selected_interface.value,
        selected=selected,
        command_count=environment.command_count + direct_count,
    )


def _directory_node(entry: DirectoryEntry) -> TlvNode:
    children = [TlvNode("4F", bytes.fromhex(entry.aid))]
    if entry.label is not None:
        children.append(TlvNode("50", entry.label.encode("ascii")))
    if entry.priority is not None:
        children.append(TlvNode("87", bytes((entry.priority,))))
    if entry.kernel_id is not None:
        children.append(TlvNode("9F2A", bytes.fromhex(entry.kernel_id)))
    return TlvNode("61", children=tuple(children))


def _environment_fci(interface: InterfaceKind | str, entries: Iterable[DirectoryEntry]) -> bytes:
    selected_interface = _environment(interface)
    directory = TlvNode("BF0C", children=tuple(_directory_node(entry) for entry in entries))
    proprietary = TlvNode("A5", children=(directory,))
    return encode_tlv(TlvNode("6F", children=(TlvNode("84", environment_name(selected_interface)), proprietary)))


def _selected_fci(aid: str) -> bytes:
    return encode_tlv(TlvNode("6F", children=(TlvNode("84", bytes.fromhex(normalize_aid(aid))), TlvNode("A5", children=()))))


def synthetic_selection_responses(
    interface: InterfaceKind | str,
    entries: Iterable[DirectoryEntry] = (),
    malformed: str | None = None,
) -> dict[str, TransportResult]:
    """Construye respuestas locales SYNTHETIC para SELECT PSE/PPSE y cada AID."""
    selected_interface = _environment(interface)
    candidates = tuple(entries)
    environment_data = _environment_fci(selected_interface, candidates)
    direct_data = {entry.aid: _selected_fci(entry.aid) for entry in candidates}
    if malformed is not None:
        name = malformed.upper()
        if name == "FCI_TRUNCATED":
            environment_data = b"\x6f\x05\x84\x01"
        elif name == "FCI_WRONG_ROOT":
            environment_data = encode_tlv(TlvNode("70", children=()))
        elif name == "DUPLICATE_AID":
            if not candidates:
                raise ValueError("DUPLICATE_AID requiere un candidato.")
            environment_data = _environment_fci(selected_interface, (candidates[0], candidates[0]))
        elif name == "DUPLICATE_TAG":
            if not candidates:
                raise ValueError("DUPLICATE_TAG requiere un candidato.")
            entry = candidates[0]
            directory_entry = TlvNode("61", children=(
                TlvNode("4F", bytes.fromhex(entry.aid)),
                TlvNode("4F", bytes.fromhex(entry.aid)),
            ))
            environment_data = encode_tlv(TlvNode("6F", children=(
                TlvNode("84", environment_name(selected_interface)),
                TlvNode("A5", children=(TlvNode("BF0C", children=(directory_entry,)),)),
            )))
        elif name == "MISSING_AID":
            directory_entry = TlvNode("61", children=(TlvNode("87", b"\x01"),))
            environment_data = encode_tlv(TlvNode("6F", children=(
                TlvNode("84", environment_name(selected_interface)),
                TlvNode("A5", children=(TlvNode("BF0C", children=(directory_entry,)),)),
            )))
        elif name == "BAD_PRIORITY":
            if not candidates:
                raise ValueError("BAD_PRIORITY requiere un candidato.")
            entry = candidates[0]
            directory_entry = TlvNode("61", children=(
                TlvNode("4F", bytes.fromhex(entry.aid)),
                TlvNode("87", b"\x00\x01"),
            ))
            environment_data = encode_tlv(TlvNode("6F", children=(
                TlvNode("84", environment_name(selected_interface)),
                TlvNode("A5", children=(TlvNode("BF0C", children=(directory_entry,)),)),
            )))
        elif name == "MISSING_KERNEL_ID":
            if not candidates:
                raise ValueError("MISSING_KERNEL_ID requiere un candidato.")
            entry = candidates[0]
            directory_entry = TlvNode("61", children=(TlvNode("4F", bytes.fromhex(entry.aid)),))
            environment_data = encode_tlv(TlvNode("6F", children=(
                TlvNode("84", environment_name(selected_interface)),
                TlvNode("A5", children=(TlvNode("BF0C", children=(directory_entry,)),)),
            )))
        elif name == "DUPLICATE_DF_NAME":
            if not candidates:
                raise ValueError("DUPLICATE_DF_NAME requiere un candidato.")
            aid = candidates[0].aid
            direct_data[aid] = encode_tlv(TlvNode("6F", children=(
                TlvNode("84", bytes.fromhex(aid)),
                TlvNode("84", bytes.fromhex(aid)),
            )))
        elif name == "SELECTED_AID_MISMATCH":
            if not candidates:
                raise ValueError("SELECTED_AID_MISMATCH requiere un candidato.")
            other = "A0000000099999" if candidates[0].aid != "A0000000099999" else "A0000000099998"
            direct_data[candidates[0].aid] = _selected_fci(other)
        else:
            raise ValueError("Caso malformed de selección desconocido.")
    responses = {
        select_environment_command(selected_interface).to_bytes().hex().upper(): TransportResult(environment_data, "9000"),
    }
    for aid, data in direct_data.items():
        responses[select_aid_command(aid).to_bytes().hex().upper()] = TransportResult(data, "9000")
    return responses
