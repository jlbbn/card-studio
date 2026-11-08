from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from card_studio.readers.base import TransportResult
from card_studio.tlv import TlvError, TlvNode, encode_tlv, find_tag, parse_tlv

from .decision import configured_cvm


LAB_MARKER = b"LAB_ONLY"
CARD_ACTIONS = frozenset({"APPROVED", "DECLINED", "ONLINE_REQUIRED", "ACTION_REQUIRED"})


class LaboratoryProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def select_command(aid: str) -> str:
    normalized = aid.upper()
    if not 10 <= len(normalized) <= 32 or len(normalized) % 2:
        raise LaboratoryProtocolError("INVALID_AID", "AID de laboratorio inválido.")
    try:
        raw_aid = bytes.fromhex(normalized)
    except ValueError as exc:
        raise LaboratoryProtocolError("INVALID_AID", "AID de laboratorio inválido.") from exc
    if len(raw_aid) > 127:
        raise LaboratoryProtocolError("INVALID_AID", "AID demasiado largo para SELECT.")
    return (bytes.fromhex("00A40400") + bytes((len(raw_aid),)) + raw_aid).hex().upper()


def initialize_command() -> str:
    payload = encode_tlv(TlvNode("70", children=(TlvNode("DF01", LAB_MARKER),)))
    return (bytes.fromhex("80E00000") + bytes((len(payload),)) + payload).hex().upper()


def read_data_command() -> str:
    return "80CA9F3600"


def card_action_command() -> str:
    payload = encode_tlv(TlvNode("DF13", b"LAB"))
    return (bytes.fromhex("80AE8000") + bytes((len(payload),)) + payload).hex().upper()


def laboratory_commands(aid: str) -> dict[str, str]:
    return {
        "SELECT": select_command(aid),
        "INITIALIZE": initialize_command(),
        "READ_DATA": read_data_command(),
        "CARD_ACTION": card_action_command(),
    }


def _template(template: str, *children: TlvNode) -> bytes:
    return encode_tlv(TlvNode(template, children=children))


def _response(data: bytes, sw1sw2: str = "9000") -> TransportResult:
    return TransportResult(data=data, sw1sw2=sw1sw2)


def laboratory_responses(
    profile: Any,
    action: str = "APPROVED",
    application_aid: str | None = None,
    cvm: str | None = None,
    malformed: str | None = None,
) -> dict[str, TransportResult]:
    """Build complete SYNTHETIC CONTACT/CONTACTLESS/VIRTUAL reader responses.

    The returned mapping is accepted by MockReaderAdapter and VirtualReaderAdapter.
    ``application_aid`` is mandatory for a multi-application profile. ``malformed``
    may name SELECT, INITIALIZE, READ_DATA or CARD_ACTION to inject truncated TLV;
    no financial credential, key or production cryptographic value is generated.
    """
    selected_action = action.upper()
    if selected_action not in CARD_ACTIONS:
        raise ValueError("Acción de laboratorio no reconocida.")
    applications = sorted(profile.applications, key=lambda item: (item.priority, item.aid))
    if application_aid is None:
        if len(applications) != 1:
            raise ValueError("APPLICATION_SELECTION_REQUIRED")
        selected = applications[0]
    else:
        normalized = application_aid.upper()
        selected = next((item for item in applications if item.aid == normalized), None)
        if selected is None:
            raise ValueError("APPLICATION_NOT_FOUND")
    selected_cvm = (cvm or configured_cvm(profile)).upper()
    commands = laboratory_commands(selected.aid)
    responses = {
        commands["SELECT"]: _response(_template(
            "70",
            TlvNode("4F", bytes.fromhex(selected.aid)),
            TlvNode("DF01", LAB_MARKER),
        )),
        commands["INITIALIZE"]: _response(_template(
            "77",
            TlvNode("82", b"LAB"),
            TlvNode("9F36", b"\x00\x01"),
            TlvNode("DF01", LAB_MARKER),
        )),
        commands["READ_DATA"]: _response(_template(
            "70",
            TlvNode("DF01", LAB_MARKER),
            TlvNode("DF02", selected_cvm.encode("ascii")),
        )),
        commands["CARD_ACTION"]: _response(_template(
            "70",
            TlvNode("DF01", LAB_MARKER),
            TlvNode("DF04", selected_action.encode("ascii")),
        )),
    }
    if malformed is not None:
        name = malformed.upper()
        aliases = {"READ DATA": "READ_DATA", "CARD ACTION": "CARD_ACTION"}
        name = aliases.get(name, name)
        if name not in commands:
            raise ValueError("Paso malformed no reconocido.")
        responses[commands[name]] = _response(b"\x70\x05\x4f\x01")
    return responses


def _validated_nodes(response: TransportResult, expected_template: str, limits=None) -> tuple[TlvNode, ...]:
    if response.sw1sw2 is None:
        raise LaboratoryProtocolError("MISSING_STATUS", "La respuesta de laboratorio no contiene estado.")
    if response.sw1sw2.upper() != "9000":
        raise LaboratoryProtocolError("CARD_COMMAND_FAILED", "El comando de laboratorio no terminó con 9000.")
    try:
        nodes = parse_tlv(response.data, limits=limits)
    except TlvError as exc:
        raise LaboratoryProtocolError(exc.code, "TLV de laboratorio truncado o mal formado.") from exc
    if len(nodes) != 1 or nodes[0].tag != expected_template or not nodes[0].constructed:
        raise LaboratoryProtocolError("UNEXPECTED_TEMPLATE", "Template de laboratorio inesperado.")
    marker = find_tag(nodes[0].children, "DF01")
    if marker is None or marker.constructed or marker.value != LAB_MARKER:
        raise LaboratoryProtocolError("NON_SYNTHETIC_RESPONSE", "La respuesta no está marcada LAB_ONLY.")
    return nodes[0].children


def parse_select_response(response: TransportResult, expected_aid: str, limits=None) -> None:
    children = _validated_nodes(response, "70", limits)
    aid = find_tag(children, "4F")
    if aid is None or aid.constructed or aid.value != bytes.fromhex(expected_aid):
        raise LaboratoryProtocolError("SELECTED_AID_MISMATCH", "El AID seleccionado no coincide.")


def parse_initialize_response(response: TransportResult, limits=None) -> None:
    _validated_nodes(response, "77", limits)


def parse_read_data_response(response: TransportResult, limits=None) -> None:
    _validated_nodes(response, "70", limits)


def parse_card_action_response(response: TransportResult, limits=None) -> str:
    children = _validated_nodes(response, "70", limits)
    action = find_tag(children, "DF04")
    if action is None or action.constructed:
        raise LaboratoryProtocolError("CARD_ACTION_MISSING", "Falta la acción sintética de tarjeta.")
    try:
        selected = action.value.decode("ascii").upper()
    except UnicodeDecodeError as exc:
        raise LaboratoryProtocolError("CARD_ACTION_INVALID", "Acción sintética no textual.") from exc
    if selected not in CARD_ACTIONS:
        raise LaboratoryProtocolError("CARD_ACTION_INVALID", "Acción sintética desconocida.")
    return selected
